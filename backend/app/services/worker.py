import json
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from google.api_core.exceptions import GoogleAPICallError, InvalidArgument, PermissionDenied, NotFound
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings
from app.database import SessionLocal
from app.models import RepositoryFile
from app.pipeline_logs import add_pipeline_log
from app.services.gemini_pool import AllKeysExhausted, MAX_EMBED_BATCH, get_pool
from app.services.limiter import generate_content_with_fallback

settings = get_settings()

# Tier 3 Defense: We use a text splitter for massive files to prevent embedding models from failing.
# gemini-embedding-2 handles up to roughly 8192 tokens. We use a safe chunk size of 4000 characters.
text_splitter = RecursiveCharacterTextSplitter(chunk_size=4000, chunk_overlap=200)

# ---------------------------------------------------------
# THE WORKER FUNCTIONS & RETRY HELPERS
# ---------------------------------------------------------

def is_transient_error(exception):
    """
    Decide whether tenacity should retry.

    Quota errors are deliberately excluded: GeminiPool already rotates keys and
    steps down models for those, so retrying here would re-run a call the pool
    has already established has nowhere left to go. The old version retried
    every 429 five times per file, which is what turned a single exhausted key
    into hundreds of wasted requests.
    """
    if isinstance(exception, AllKeysExhausted):
        return False
    # Do not retry if it's InvalidArgument (400), PermissionDenied (403), or NotFound (404)
    if isinstance(exception, (InvalidArgument, PermissionDenied, NotFound)):
        return False
    # Only retry genuine server/network faults
    if isinstance(exception, GoogleAPICallError):
        return exception.code in (500, 503, 504)
    if isinstance(exception, (asyncio.TimeoutError, ConnectionError, IOError)):
        return True

    err_str = str(exception).upper()
    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "QUOTA" in err_str:
        return False
    if "500" in err_str or "503" in err_str:
        return True

    return False

@retry(
    retry=retry_if_exception(is_transient_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
async def task_a_generate_embedding(content: str) -> list[float]:
    """
    Task A: Generates the vector embedding for the file.

    The chunks of a file are embedded in a SINGLE batched request. The previous
    version fired one request per chunk via asyncio.gather, so a 40 KB file cost
    ten requests against the daily quota instead of one - and the concurrent
    gather also sidestepped the rate limiter it had just awaited.
    """
    chunks = text_splitter.split_text(content) or ["Empty file"]

    # One batch is one request, so cap at the batch ceiling to keep the cost of
    # any single file fixed at exactly one embedding call.
    if len(chunks) > MAX_EMBED_BATCH:
        chunks = chunks[:MAX_EMBED_BATCH]

    chunk_embeddings = await get_pool().embed(chunks)

    # If it's a single chunk, just return its vector
    if len(chunk_embeddings) == 1:
        return chunk_embeddings[0]

    # If multiple chunks, calculate the mean vector (average embedding).
    # This represents the "average meaning" of the entire large file,
    # fitting perfectly into our single Vector(3072) database column.
    num_dimensions = len(chunk_embeddings[0])
    mean_vector = [
        sum(embedding[i] for embedding in chunk_embeddings) / len(chunk_embeddings)
        for i in range(num_dimensions)
    ]
    return mean_vector

@retry(
    retry=retry_if_exception(is_transient_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
async def task_b_generate_summary(file_path: str, content: str) -> tuple[dict, list]:
    """
    Task B: Uses the Worker LLM to generate a strict JSON summary and vulnerability list.
    Key selection and quota rotation are handled by GeminiPool.
    """
    model_name = settings.GEMINI_MODEL_MAP

    prompt = f"""
    You are an expert Code Reviewer and Security Auditor.
    Analyze the following code from the file: `{file_path}`
    
    If this file is extremely long, do not try to explain every single line. Focus on summarizing the core classes, the primary exported functions, key external integrations/APIs, hardcoded configuration values (like model names, API models, ports), and any obvious security vulnerabilities.
    
    Return your analysis STRICTLY as a JSON object with this exact format:
    {{
        "summary": "A detailed 2-3 paragraph explanation of what this file does, its logic, hardcoded configurations (such as model names/strings or ports), external APIs used, and its purpose in the overall project.",
        "issues": [
            {{"type": "vulnerability", "description": "SQL injection at line X...", "severity": "High"}},
            {{"type": "dead_code", "description": "Function unused_xyz() is never called.", "severity": "Low"}}
        ]
    }}
    
    CODE:
    ```
    {content}
    ```
    """
    
    # The pool paces each key independently and rotates on quota errors.
    response_text = await generate_content_with_fallback(
        model_name=model_name,
        prompt=prompt,
        response_mime_type="application/json"
    )

    try:
        # Parse the clean JSON response directly
        result = json.loads(response_text)
        summary = {"summary": result.get("summary", "No summary generated.")}
        vulnerabilities = result.get("issues", [])
        return summary, vulnerabilities
    except json.JSONDecodeError:
        # Fallback if the LLM hallucinated outside the JSON schema
        return {"summary": "Failed to parse LLM response.", "raw": response_text}, []

async def process_single_file(file_id: str, semaphore: asyncio.Semaphore, repo_url: str = ""):
    """
    This function processes a single file, bounded by our concurrency semaphore.
    """
    async with semaphore:
        # Step 1: Read the file content and check if it needs processing
        db = SessionLocal()
        content = None
        file_path = None
        try:
            db_file = db.query(RepositoryFile).filter(RepositoryFile.id == file_id).first()
            if not db_file or db_file.status != "pending":
                return
            
            # If the file has no content (e.g. empty files like .gitkeep or empty configs),
            # mark it as completed immediately with a mock summary to avoid leaving it in "pending" status.
            if not db_file.content:
                db_file.status = "completed"
                db_file.explanation_summary = {"summary": "Empty file."}
                db_file.vulnerabilities_found = []
                # Leave the embedding NULL. A zero vector has no direction, so
                # pgvector's cosine distance against it is NaN, which makes the
                # RAG ORDER BY nondeterministic. The retrieval query already
                # filters on embedding IS NOT NULL.
                db_file.embedding = None
                db.commit()
                return
            
            content = db_file.content
            file_path = db_file.file_path
            
            # Mark as processing immediately and release the connection
            db_file.status = "processing"
            db.commit()
        except Exception as e:
            print(f"[ERROR] DB session read error for {file_id}: {str(e)}")
            db.rollback()
            return
        finally:
            db.close()

        # Step 2: Run LLM embedding and summarization sequentially, spaced out by the rate limiter
        try:
            # We wrap the slow network calls in an asyncio.wait_for block to prevent any infinite hangs.
            embedding_result = await asyncio.wait_for(
                task_a_generate_embedding(content),
                timeout=180.0
            )
            
            summary_dict, vulnerabilities_list = await asyncio.wait_for(
                task_b_generate_summary(file_path, content),
                timeout=180.0
            )
            
            # Step 3: Write results back to the database in a fresh session
            db = SessionLocal()
            try:
                db_file = db.query(RepositoryFile).filter(RepositoryFile.id == file_id).first()
                if db_file:
                    db_file.embedding = embedding_result
                    db_file.explanation_summary = summary_dict
                    db_file.vulnerabilities_found = vulnerabilities_list
                    db_file.status = "completed"
                    db.commit()
                    print(f"[OK] Map Phase Completed for: {db_file.file_path}")
                    add_pipeline_log(repo_url, f"✓ Analyzed: {db_file.file_path}")
            except Exception as e:
                print(f"[ERROR] DB session write results error for {file_id}: {str(e)}")
                db.rollback()
            finally:
                db.close()

        except Exception as e:
            # Step 4: Handle any exceptions (LLM rate limits, network timeouts, etc.)
            print(f"[ERROR] Map Phase Error for {file_id}: {str(e)}")

            # Log the actual file path instead of the UUID to make errors clear to the user
            friendly_name = file_path if file_path else file_id[:8]
            add_pipeline_log(repo_url, f"✗ Error analyzing file {friendly_name}: {str(e)[:120]}")

            # Open a fresh session to mark the file as error, ensuring we don't hold connections
            db = SessionLocal()
            try:
                db_file = db.query(RepositoryFile).filter(RepositoryFile.id == file_id).first()
                if db_file:
                    db_file.status = "error"
                    db_file.explanation_summary = {"error": str(e)}
                    db.commit()
            except Exception as dbe:
                print(f"[ERROR] DB session write error status failed for {file_id}: {str(dbe)}")
                db.rollback()
            finally:
                db.close()

            # Once every key is spent there is nothing left to try, so stop the
            # run rather than marching the remaining files through the same
            # doomed calls. The caller keeps whatever completed so far.
            if isinstance(e, AllKeysExhausted):
                raise

async def trigger_map_phase(file_ids: list[str], repo_url: str = ""):
    """
    The entry point called by the FastAPI route as a Background Task.
    It processes all pending files sequentially using a Semaphore to prevent concurrent execution
    and stays within Gemini API rate limits.
    """
    semaphore = asyncio.Semaphore(1)

    # Process each file one by one sequentially
    for index, file_id in enumerate(file_ids):
        try:
            await process_single_file(file_id, semaphore, repo_url)
        except AllKeysExhausted as e:
            remaining = len(file_ids) - index - 1
            print(f"[ABORT] Map Phase stopped: {e}")
            add_pipeline_log(
                repo_url,
                f"✗ Daily Gemini quota exhausted on every key. Stopped with "
                f"{remaining} file(s) unanalysed - the report will cover what "
                f"finished. Quota resets at midnight Pacific."
            )
            break

    print("[DONE] Map Phase complete for all submitted files! Ready for Reduce Phase.")
