import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from app.database import SessionLocal
from app.models import RepositoryFile
from app.pipeline_logs import get_pipeline_logs

db = SessionLocal()
repo_url = "https://github.com/aryankale14/aryankale14-ai-stock-insight"
user_id = "oHgk4liHoZPPPCm6QYeGSURyCzw2"

has_report = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.user_id == user_id,
    RepositoryFile.file_path == "__GLOBAL_REPORT__"
).first() is not None

pending_count = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.user_id == user_id,
    RepositoryFile.status == "pending",
    RepositoryFile.file_path != "__GLOBAL_REPORT__"
).count()

processing_count = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.user_id == user_id,
    RepositoryFile.status == "processing",
    RepositoryFile.file_path != "__GLOBAL_REPORT__"
).count()

completed_count = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.user_id == user_id,
    RepositoryFile.status == "completed",
    RepositoryFile.file_path != "__GLOBAL_REPORT__"
).count()

error_count = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.user_id == user_id,
    RepositoryFile.status == "error",
    RepositoryFile.file_path != "__GLOBAL_REPORT__"
).count()

# Check if the Reduce phase is currently active in the background
logs = get_pipeline_logs(repo_url)
reduce_started = any("Phase 3: Reduce — synthesizing" in log["message"] for log in logs)
reduce_finished = any("Phase 3: Reduce complete" in log["message"] or "Phase 3: Reduce failed" in log["message"] for log in logs)
reduce_active = reduce_started and not reduce_finished

print(f"has_report: {has_report}")
print(f"pending_count: {pending_count}")
print(f"processing_count: {processing_count}")
print(f"completed_count: {completed_count}")
print(f"error_count: {error_count}")
print(f"reduce_started: {reduce_started}")
print(f"reduce_finished: {reduce_finished}")
print(f"reduce_active: {reduce_active}")

if has_report:
    status = "completed"
elif reduce_active:
    status = "processing"
elif processing_count > 0:
    status = "processing"
elif pending_count > 0:
    status = "pending"
else:
    status = "error"

print(f"Computed Status: {status}")

db.close()
