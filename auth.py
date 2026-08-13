import os
try:
    import bcrypt
    _HAS_BCRYPT = True
except Exception:
    bcrypt = None
    _HAS_BCRYPT = False
try:
    import jwt  # PyJWT
    _HAS_PYJWT = hasattr(jwt, "encode")
except Exception:
    jwt = None
    _HAS_PYJWT = False
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import get_db

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-fallback-jwt-secret-key-32-chars")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_SECONDS = 3600  # 1 hour

# Extract Bearer token from 'Authorization: Bearer <token>' header
security = HTTPBearer()

def verify_secret(plain_secret: str, hashed_secret: str) -> bool:
    """Verifies plain secret key against the bcrypt hash in MongoDB.

    Falls back to plain-text equality if `bcrypt` is not installed (local testing only).
    """
    if _HAS_BCRYPT:
        try:
            return bcrypt.checkpw(plain_secret.encode('utf-8'), hashed_secret.encode('utf-8'))
        except Exception:
            return False
    # Fallback: compare raw strings (insecure, testing only)
    return plain_secret == hashed_secret

if not _HAS_PYJWT:
    # Minimal JWT HS256 encode/decode fallback (ONLY for local testing)
    import base64
    import json
    import hmac
    import hashlib
    import time

    def _b64u_encode(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    def _b64u_decode(s: str) -> bytes:
        padded = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(padded.encode())

    def _jwt_encode(payload: dict, secret: str, algorithm: str = "HS256") -> str:
        header = {"alg": algorithm, "typ": "JWT"}
        h = _b64u_encode(json.dumps(header, separators=(",", ":")).encode())
        p = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{h}.{p}".encode()
        if algorithm == "HS256":
            sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        else:
            raise NotImplementedError("Only HS256 is supported in fallback JWT")
        return f"{h}.{p}.{_b64u_encode(sig)}"

    def _jwt_decode(token: str, secret: str, algorithms=None) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        h, p, s = parts
        signing_input = f"{h}.{p}".encode()
        expected = _b64u_encode(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(expected, s):
            raise ValueError("Invalid signature")
        payload_bytes = _b64u_decode(p)
        payload = json.loads(payload_bytes)
        exp = payload.get("exp")
        if exp is not None and time.time() > float(exp):
            raise ValueError("Token expired")
        return payload


def create_access_token(partner_data: dict) -> str:
    """Generates a signed JWT access token valid for 1 hour."""
    to_encode = partner_data.copy()
    expire = datetime.now(timezone.utc) + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS)
    # Use integer timestamp for exp to be compatible with both PyJWT and fallback
    to_encode.update({"exp": int(expire.timestamp())})
    if _HAS_PYJWT:
        return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)
    else:
        return _jwt_encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db = Depends(get_db)
):
    """
    FastAPI Dependency:
    Extracts and validates the JWT Bearer token from incoming API requests.
    """
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        if _HAS_PYJWT:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        else:
            payload = _jwt_decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        partner_id: str = payload.get("sub")
        if partner_id is None:
            raise credentials_exception
    except Exception:
        raise credentials_exception

    # Find the partner in MongoDB to ensure they are active
    partner = await db["registered_partners"].find_one({"api_key": payload.get("api_key")})
    if not partner:
        raise credentials_exception

    return partner