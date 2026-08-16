import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from app.database import SessionLocal
from app.models import RepositoryFile, UserMapping

db = SessionLocal()
repo_url = "https://github.com/aryankale14/aryankale14-ai-stock-insight"

print("--- Repository Files ---")
files = db.query(RepositoryFile).filter(RepositoryFile.repo_url == repo_url).all()
print(f"Found {len(files)} files for {repo_url}:")
for f in files:
    print(f"ID: {f.id}, Path: {f.file_path}, Status: {f.status}, User ID: {f.user_id}")

print("\n--- User Mappings ---")
mappings = db.query(UserMapping).all()
for m in mappings:
    print(f"UID: {m.uid}, Email: {m.email}")

db.close()
