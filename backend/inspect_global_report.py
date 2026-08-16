import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from app.database import SessionLocal
from app.models import RepositoryFile

db = SessionLocal()
repo_url = "https://github.com/aryankale14/aryankale14-ai-stock-insight"

report = db.query(RepositoryFile).filter(
    RepositoryFile.repo_url == repo_url,
    RepositoryFile.file_path == "__GLOBAL_REPORT__"
).first()

if report:
    print("Report Found!")
    print(f"ID: {report.id}")
    print(f"Status: {report.status}")
    print(f"User ID: {report.user_id}")
    print(f"Explanation Summary Type: {type(report.explanation_summary)}")
    print(f"Explanation Summary Keys: {report.explanation_summary.keys() if report.explanation_summary else None}")
    print(f"Vulnerabilities Found Type: {type(report.vulnerabilities_found)}")
    print(f"Vulnerabilities Found Keys: {report.vulnerabilities_found.keys() if report.vulnerabilities_found else None}")
else:
    print("Report NOT found in database!")

db.close()
