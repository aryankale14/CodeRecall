import os
import json
import time
import requests
import jwt
from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.backends import default_backend

import firebase_admin
from firebase_admin import credentials, auth
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings

# Initialize Firebase Admin SDK
# Looks for 'serviceAccountKey.json' in the backend root directory.
# If not present, falls back to environment variables or graceful debug mode.
firebase_initialized = False
service_key_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "serviceAccountKey.json")

try:
    if os.path.exists(service_key_path):
        cred = credentials.Certificate(service_key_path)
        firebase_admin.initialize_app(cred)
        firebase_initialized = True
        print("[INFO] Firebase Admin SDK initialized successfully via service account JSON file.")
    else:
        # Graceful initialize using settings or fallback
        settings = get_settings()
        project_id = settings.FIREBASE_PROJECT_ID or "code-reviewer-9019f"
        # Set environmental variable so Firebase Admin SDK knows the project ID
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        firebase_admin.initialize_app(options={'projectId': project_id})
        firebase_initialized = True
        print(f"[INFO] Firebase Admin SDK initialized with project ID: {project_id}")
except Exception as e:
    print(f"[WARNING] Firebase Admin SDK initialization failed: {str(e)}.")
    print("[WARNING] The backend will run in grace/development mode. Active signature verification is disabled until serviceAccountKey.json is configured.")

security_scheme = HTTPBearer(auto_error=False)

# File to store persistent mapping of uid -> email to avoid calling Admin SDK when it's unconfigured
USER_MAPPINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_mappings.json")

def save_user_mapping(uid: str, email: str):
    if not uid or not email:
        return
    try:
        mappings = {}
        if os.path.exists(USER_MAPPINGS_FILE):
            with open(USER_MAPPINGS_FILE, "r") as f:
                mappings = json.load(f)
        if mappings.get(uid) != email:
            mappings[uid] = email
            with open(USER_MAPPINGS_FILE, "w") as f:
                json.dump(mappings, f, indent=2)
    except Exception as e:
        print(f"[WARNING] Failed to save user mapping: {e}")

def get_email_from_uid(uid: str) -> str:
    if uid == "mock_local_developer_uid":
        return "developer@aetherreview.local"
    try:
        if os.path.exists(USER_MAPPINGS_FILE):
            with open(USER_MAPPINGS_FILE, "r") as f:
                mappings = json.load(f)
                return mappings.get(uid) or uid
    except Exception:
        pass
    return uid

# A simple in-memory cache for public keys
_public_keys_cache = {}
_public_keys_cache_expiry = 0

def _get_firebase_public_keys() -> dict:
    global _public_keys_cache, _public_keys_cache_expiry
    now = time.time()
    
    # If cache is valid, return it
    if _public_keys_cache and now < _public_keys_cache_expiry:
        return _public_keys_cache
        
    try:
        cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
        res = requests.get(cert_url, timeout=5)
        keys = res.json()
        
        # Parse Cache-Control header to get max-age
        cache_control = res.headers.get("Cache-Control", "")
        max_age = 3600  # Default 1 hour
        for part in cache_control.split(","):
            part = part.strip().lower()
            if part.startswith("max-age="):
                try:
                    max_age = int(part.split("=")[1])
                except ValueError:
                    pass
                    
        _public_keys_cache = keys
        _public_keys_cache_expiry = now + max_age
        return keys
    except Exception as e:
        print(f"[WARNING] Failed to fetch Firebase public keys: {e}")
        # Fall back to stale cache if we have one
        if _public_keys_cache:
            return _public_keys_cache
        raise e

def verify_token_manually(token: str, project_id: str) -> dict:
    """
    Manually verify a Firebase ID Token using PyJWT and cryptography,
    bypassing the Firebase Admin SDK requirement for service accounts.
    """
    # 1. Get the unverified header to find the 'kid'
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("Firebase ID token is missing 'kid' in header.")
        
    # 2. Get Google's public certificates
    public_keys = _get_firebase_public_keys()
    if kid not in public_keys:
        raise ValueError("Firebase ID token 'kid' not found in public keys.")
        
    # 3. Load the PEM certificate
    cert_pem = public_keys[kid].encode('utf-8')
    cert = load_pem_x509_certificate(cert_pem, default_backend())
    public_key = cert.public_key()
    
    # 4. Decode and verify signature, expiration, audience, issuer
    decoded = jwt.decode(
        token,
        key=public_key,
        algorithms=["RS256"],
        audience=project_id,
        issuer=f"https://securetoken.google.com/{project_id}"
    )
    
    return decoded

def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security_scheme)) -> dict:
    """
    FastAPI security dependency to intercept HTTP Bearer tokens,
    verify them against the Firebase Admin SDK (or manual fallback if unconfigured),
    and return user payload.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Header Bearer Token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # If Firebase is not initialized, run in Graceful Local Mock Development mode
    if not firebase_initialized:
        print("[MOCK AUTH] Verifying Bearer Token locally (Mock Mode active)...")
        return {
            "uid": "mock_local_developer_uid",
            "email": "developer@aetherreview.local",
            "name": "Local Developer"
        }

    settings = get_settings()
    project_id = settings.FIREBASE_PROJECT_ID or "code-reviewer-9019f"

    # Try standard verification first
    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token.get("uid") or decoded_token.get("sub")
        email = decoded_token.get("email")
        name = decoded_token.get("name", "AetherUser")
        
        # Save mapping of UID -> Email
        if uid and email:
            save_user_mapping(uid, email)
            
        return {
            "uid": uid,
            "email": email,
            "name": name
        }
    except Exception as e:
        # If standard verification fails (e.g. because of missing service account credentials),
        # perform manual JWT signature verification as a fully secure fallback.
        print(f"[INFO] Firebase Admin verification failed ({e}). Falling back to manual verification...")
        try:
            decoded_token = verify_token_manually(token, project_id)
            uid = decoded_token.get("uid") or decoded_token.get("sub")
            email = decoded_token.get("email")
            name = decoded_token.get("name", "AetherUser")
            
            # Save mapping of UID -> Email
            if uid and email:
                save_user_mapping(uid, email)
                
            return {
                "uid": uid,
                "email": email,
                "name": name
            }
        except Exception as manual_err:
            print(f"[ERROR] Manual Firebase token verification failed: {manual_err}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid or expired Firebase Authentication Token: {str(manual_err)}",
                headers={"WWW-Authenticate": "Bearer"},
            )

