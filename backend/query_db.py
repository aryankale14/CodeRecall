import os
import sys
from dotenv import load_dotenv

# Add backend directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from app.database import SessionLocal
from app.models import RepositoryFile

db = SessionLocal()
files = db.query(RepositoryFile).all()
print(f"Total files in database: {len(files)}")
for f in files[:20]:
    print(f"ID: {f.id}, Path: {f.file_path}, Status: {f.status}")
db.close()
