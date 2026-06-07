import json
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
import google.generativeai as genai
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings
from app.database import SessionLocal
from app.models import RepositoryFile
from app.pipeline_logs import add_pipeline_log

settings = get_settings()

# ---------------------------------------------------------
# SETUP LLM CLIENTS WITH SPECIFIC API KEYS
# ---------------------------------------------------------

# Map Phase Key for the Worker LLM (Task B)
genai.configure(api_key=settings.GEMINI_API_KEY_MAP)

# RAG Key for Embeddings (Task A)
# We initialize the LangChain embedding model here. 
embeddings_model = GoogleGenerativeAIEmbeddings(
    model="models/text-embedding-004", 
    google_api_key=settings.GEMINI_API_KEY_RAG
)

# Tier 3 Defense: We use a text splitter for massive files to prevent embedding models from failing.
# text-embedding-004 handles up to roughly 8192 tokens. We use a safe chunk size of 4000 characters.
text_splitter = RecursiveCharacterTextSplitter(chunk_size=4000, chunk_overlap=200)

# ---------------------------------------------------------
# THE WORKER FUNCTIONS
# ---------------------------------------------------------

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
async def task_a_generate_embedding(content: str) -> list[float]:
    """
    Task A: Generates the vector embedding for the file using Key 3.
    Uses 'tenacity' to automatically retry if we hit Google's rate limits (HTTP 429).
    If the file is long, we chunk it, embed chunks, and average them into a single vector.
    """
    chunks = text_splitter.split_text(content)
    
    if not chunks:
        # Fallback if the file was somehow completely empty of text
        return await embeddings_model.aembed_query("Empty file")
        
    # Get embeddings for all chunks concurrently (handled by langchain)
    chunk_embeddings = await embeddings_model.aembed_documents(chunks)
    
    # If it's a single chunk, just return its vector
    if len(chunk_embeddings) == 1:
        return chunk_embeddings[0]
        
    # If multiple chunks, calculate the mean vector (average embedding).
    # This represents the "average meaning" of the entire large file, 
    # fitting perfectly into our single Vector(768) database column.
    num_dimensions = len(chunk_embeddings[0])
    mean_vector = [
        sum(embedding[i] for embedding in chunk_embeddings) / len(chunk_embeddings)
        for i in range(num_dimensions)
    ]
    return mean_vector

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
async def task_b_generate_summary(file_path: str, content: str) -> tuple[dict, list]:
    """
    Task B: Uses the Worker LLM (Key 1) to generate a strict JSON summary and vulnerability list.
    """
    # We use Gemini 1.5 Pro. It has a massive context window so we pass the whole file.
    model = genai.GenerativeModel("gemini-1.5-flash")
    
    prompt = f"""
    You are an expert Code Reviewer and Security Auditor.
    Analyze the following code from the file: `{file_path}`
    
    If this file is extremely long, do not try to explain every single line. Focus on summarizing the core classes, the primary exported functions, and any obvious security vulnerabilities.
    
    Return your analysis STRICTLY as a JSON object with this exact format:
    {{
        "summary": "A detailed 2-3 paragraph explanation of what this file does, its logic, and its purpose in the overall project.",
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
    
    # We force the model to reply in strict JSON format using response_mime_type.
    # This prevents the LLM from adding markdown like ```json ... ``` wrapper.
    response = await model.generate_content_async(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json"
        )
    )
    
    try:
        # Parse the clean JSON response directly
        result = json.loads(response.text)
        summary = {"summary": result.get("summary", "No summary generated.")}
        vulnerabilities = result.get("issues", [])
        return summary, vulnerabilities
    except json.JSONDecodeError:
        # Fallback if the LLM hallucinated outside the JSON schema
        return {"summary": "Failed to parse LLM response.", "raw": response.text}, []

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
                db_file.embedding = [0.0] * 3072
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

        # Step 2: Run LLM embedding and summarization in parallel, protected by a timeout!
        try:
            # We wrap the slow network calls in an asyncio.wait_for block to prevent any infinite hangs.
            embedding_task = task_a_generate_embedding(content)
            summary_task = task_b_generate_summary(file_path, content)
            
            # 90-second timeout is extremely safe but guarantees we won't hang the worker queue forever.
            embedding_result, summary_result = await asyncio.wait_for(
                asyncio.gather(embedding_task, summary_task),
                timeout=90.0
            )
            
            summary_dict, vulnerabilities_list = summary_result
            
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
            add_pipeline_log(repo_url, f"✗ Error analyzing file {file_id[:8]}...: {str(e)[:60]}")
            
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

async def trigger_map_phase(file_ids: list[str], repo_url: str = ""):
    """
    The entry point called by the FastAPI route as a Background Task. 
    It sets up the Semaphore and fires off all processing concurrently.
    """
    # Semaphore(3) ensures we don't overwhelm the Gemini API rate limits or our server's RAM.
    # Lowering this to 3 fits perfectly within standard Gemini 15 RPM rate limits.
    semaphore = asyncio.Semaphore(3)
    
    # Create a list of tasks
    tasks = [process_single_file(file_id, semaphore, repo_url) for file_id in file_ids]
    
    # Run all tasks asynchronously in the background
    await asyncio.gather(*tasks)
    
    print("[DONE] Map Phase complete for all submitted files! Ready for Reduce Phase.")
