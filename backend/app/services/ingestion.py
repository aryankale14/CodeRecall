import os
import tempfile
import git
import uuid
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from app.models import RepositoryFile
from app.services.sanitizer import FileSanitizer
from app.pipeline_logs import add_pipeline_log

# Disable Git interactive credential prompting and ssh warning hangs
os.environ["GIT_TERMINAL_PROMPT"] = "0"
os.environ["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

# Only public HTTP(S) git hosts may be cloned. Without this check a repo_url of
# "file:///srv/some/repo" makes the server clone its OWN filesystem and serve
# the contents back through the UI.
ALLOWED_GIT_HOSTS = {
    "github.com", "www.github.com",
    "gitlab.com", "www.gitlab.com",
    "bitbucket.org", "www.bitbucket.org",
}

# Hosts that resolve to the machine itself or to cloud metadata endpoints.
BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254"}


def validate_repo_url(repo_url: str) -> str:
    """
    Reject anything that is not a public HTTP(S) repository on a known host.

    Raises ValueError with a user-safe message; the caller surfaces it as a
    normal ingestion failure.
    """
    if not repo_url or not repo_url.strip():
        raise ValueError("Repository URL is required.")

    url = repo_url.strip()
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(
            "Only http:// and https:// repository URLs are supported. "
            "Local paths, file://, git://, ssh:// and ext:// are not allowed."
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in BLOCKED_HOSTNAMES:
        raise ValueError("That host is not allowed.")

    if hostname not in ALLOWED_GIT_HOSTS:
        allowed = ", ".join(sorted(h for h in ALLOWED_GIT_HOSTS if not h.startswith("www.")))
        raise ValueError(f"Only repositories hosted on {allowed} can be analysed.")

    # Credentials in the URL would be written into the clone config.
    if parsed.username or parsed.password:
        raise ValueError("Repository URLs must not contain embedded credentials.")

    return url


# A very simple map to guess the language based on extension (Static Analysis)
LANGUAGE_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".jsx": "React",
    ".tsx": "React",
    ".html": "HTML",
    ".css": "CSS",
    ".go": "Go",
    ".java": "Java",
    ".c": "C",
    ".cpp": "C++",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".sh": "Shell"
}

def guess_language(file_path: str) -> str:
    """Helper to guess the programming language based on file extension."""
    _, ext = os.path.splitext(file_path.lower())
    return LANGUAGE_MAP.get(ext, "Unknown")

def ingest_repository(repo_url: str, db: Session, user_id: str = "mock_local_developer_uid") -> list[str]:
    """
    Phase 1: Ingestion & Static Analysis.
    Clones the repository locally, filters out bad files, and saves legitimate 
    files to the database. Returns a list of pending file IDs to be processed.
    """
    pending_file_ids = []

    # Reject non-public / non-HTTP targets before anything touches the disk.
    repo_url = validate_repo_url(repo_url)

    # Clear out any previous database entries for this repo to prevent duplicate files
    db.query(RepositoryFile).filter(
        RepositoryFile.repo_url == repo_url,
        RepositoryFile.user_id == user_id
    ).delete()
    db.commit()

    # Create a temporary directory that automatically deletes itself when done
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Cloning {repo_url} into temporary directory...")
        
        try:
            # We use depth=1 for a shallow clone. This is vastly faster than a full clone
            # because we don't download the entire git history, just the latest files.
            git.Repo.clone_from(repo_url, temp_dir, depth=1)
            add_pipeline_log(repo_url, "Repository cloned successfully — scanning files...")
        except Exception as e:
            raise Exception(f"Failed to clone repository: {str(e)}")

        # Walk through all directories and files in the cloned repo
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                absolute_path = os.path.join(root, file)
                
                # Get the relative path (e.g., "src/main.py") instead of the temp folder path
                relative_path = os.path.relpath(absolute_path, temp_dir)
                
                # ---------------------------------------------------
                # TIER 1 DEFENSE: The Blacklist
                # ---------------------------------------------------
                if FileSanitizer.should_ignore_file(relative_path):
                    continue # Skip this file completely!

                file_size = os.path.getsize(absolute_path)
                language = guess_language(relative_path)
                
                # Prepare a new database record using a Python-generated UUID
                file_uuid = uuid.uuid4()
                db_record = RepositoryFile(
                    id=file_uuid,
                    repo_url=repo_url,
                    file_path=relative_path,
                    language=language,
                    user_id=user_id
                )

                # ---------------------------------------------------
                # TIER 2 DEFENSE: The Hard Size Limit
                # ---------------------------------------------------
                if FileSanitizer.is_too_large(file_size, relative_path):
                    # It's too large. We save a mock summary and skip LLM processing.
                    db_record.content = "<FILE TOO LARGE TO PROCESS>"
                    db_record.explanation_summary = FileSanitizer.get_large_file_mock_summary()
                    db_record.status = "skipped" # Mark as skipped so the worker ignores it
                else:
                    # It's a valid, reasonably sized file. Let's read the content.
                    try:
                        # errors='ignore' ensures we don't crash on weird binary files 
                        # that somehow bypassed our Tier 1 filters.
                        with open(absolute_path, "r", encoding="utf-8", errors="ignore") as f:
                            raw_content = f.read()
                            # Strip NUL bytes (0x00) as PostgreSQL text columns cannot store them
                            db_record.content = raw_content.replace("\x00", "")
                        db_record.status = "pending" # Mark as ready for the Worker LLM
                    except Exception as e:
                        print(f"Error reading file {relative_path}: {e}")
                        continue

                # Add the record to the database session
                db.add(db_record)
                
                if db_record.status == "pending":
                    pending_file_ids.append(str(file_uuid))

        # Commit all the inserted files to the database
        db.commit()
        add_pipeline_log(repo_url, f"Static analysis complete — {len(pending_file_ids)} files queued for AI processing")

    # We return the list of pending IDs. 
    # The FastAPI route will immediately pass these IDs to the background queue.
    return pending_file_ids
