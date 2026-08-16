import os
import sys
from dotenv import load_dotenv

# Add backend directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from app.database import SessionLocal
from app.models import RepositoryFile

db = SessionLocal()
files = db.query(RepositoryFile).filter(RepositoryFile.status == "error").all()
print(f"Total failed files in database: {len(files)}")
for f in files:
    print(f"ID: {f.id}, Path: {f.file_path}, Error detail: {f.explanation_summary}")
db.close()
