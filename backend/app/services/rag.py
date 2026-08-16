from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RepositoryFile
from app.services.gemini_pool import get_pool
from app.services.limiter import generate_content_with_fallback

settings = get_settings()

# Key selection is delegated to GeminiPool. The previous module-level
# genai.configure() mutated process-global state that the background map phase
# also wrote to, so a chat during an ingest could repoint the worker's key.

async def ask_question(repo_url: str, question: str, db: Session, user_id: str = "mock_local_developer_uid") -> str:
    """
    Phase 4: Q/A Interaction (RAG)
    Converts user question to a vector, searches the pgvector database for similar code chunks,
    and uses the LLM to answer the question based on the retrieved context.
    """
    
    # 1. Convert user's question into a vector
    question_vector = await get_pool().embed_one(question)
    
    # 2. Perform Cosine Similarity Search in PostgreSQL
    # pgvector provides the `cosine_distance` method. 
    # We want the lowest distance (highest similarity).
    top_k = 10
    similar_files = db.query(RepositoryFile).filter(
        RepositoryFile.repo_url == repo_url,
        RepositoryFile.user_id == user_id,
        RepositoryFile.embedding.isnot(None),
        RepositoryFile.file_path != "__GLOBAL_REPORT__", # Exclude the global report from code search
        ~RepositoryFile.file_path.ilike("%.md"),        # Exclude README and other markdown documentation
        ~RepositoryFile.file_path.ilike("%.txt"),       # Exclude raw text files
        ~RepositoryFile.file_path.ilike("%package-lock.json"),
        ~RepositoryFile.file_path.ilike("%pnpm-lock.yaml"),
        ~RepositoryFile.file_path.ilike("%yarn.lock")
    ).order_by(
        RepositoryFile.embedding.cosine_distance(question_vector)
    ).limit(top_k).all()

    # 3. Fetch the global architecture report if it exists to help answer high-level questions
    global_report = db.query(RepositoryFile).filter(
        RepositoryFile.repo_url == repo_url,
        RepositoryFile.user_id == user_id,
        RepositoryFile.file_path == "__GLOBAL_REPORT__"
    ).first()
    
    global_overview = ""
    if global_report and isinstance(global_report.explanation_summary, dict):
        global_overview = global_report.explanation_summary.get("global_overview", "")

    if not similar_files and not global_overview:
        return "I couldn't find any relevant code or overview in this repository to answer your question. Is the repository fully processed?"

    # 4. Build the Context String
    context_blocks = []
    for f in similar_files:
        # We cap the content length just in case a massive file was retrieved,
        # ensuring we stay well within the prompt limits for fast inference.
        content_preview = f.content[:40000] + ("..." if len(f.content) > 40000 else "")
        context_blocks.append(f"--- File: {f.file_path} ---\n{content_preview}\n")
        
    context_text = "\n".join(context_blocks)

    # 5. Generate Answer using the Gemini Chat Model
    prompt = f"""
    You are an expert AI Code Assistant. Answer the user's question about their codebase using the provided project overview and code context.
    Always cite the file paths when you refer to specific logic.

    USER QUESTION: {question}
    """

    if global_overview:
        prompt += f"""
    PROJECT OVERVIEW:
        {global_overview}
    """

    if context_text:
        prompt += f"""
    CODE CONTEXT:
        {context_text}
    """

    answer = await generate_content_with_fallback(settings.GEMINI_MODEL_RAG, prompt)
    return answer
