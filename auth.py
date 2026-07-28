import os
import bcrypt
import jwt
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
    """Verifies plain secret key against the bcrypt hash in MongoDB."""
    try:
        return bcrypt.checkpw(plain_secret.encode('utf-8'), hashed_secret.encode('utf-8'))
    except Exception:
        return False

def create_access_token(partner_data: dict) -> str:
    """Generates a signed JWT access token valid for 1 hour."""
    to_encode = partner_data.copy()
    expire = datetime.now(timezone.utc) + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)

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
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        partner_id: str = payload.get("sub")
        if partner_id is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    # Find the partner in MongoDB to ensure they are active
    partner = await db["registered_partners"].find_one({"api_key": payload.get("api_key")})
    if not partner:
        raise credentials_exception

    return partner