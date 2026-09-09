"""Partner-facing auth: exchanges a partner's API key + secret key for a
short-lived JWT, then validates that JWT on every subsequent gateway call.

This is B2B auth (one identity per calling platform — e.g. "Shillingi Bet"),
not end-user auth. Each partner's own end-users are identified by whatever
`external_user_id` string that partner sends us; we never see their users'
credentials.
"""
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
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from database import get_db

security = HTTPBearer()


def verify_secret(plain_secret: str, hashed_secret: str) -> bool:
    """Verifies a plaintext secret key against its bcrypt hash.

    Falls back to plain-text equality only when bcrypt isn't installed
    (local testing) — never the case in a real deployment.
    """
    if _HAS_BCRYPT:
        try:
            return bcrypt.checkpw(plain_secret.encode("utf-8"), hashed_secret.encode("utf-8"))
        except Exception:
            return False
    return plain_secret == hashed_secret


if not _HAS_PYJWT:
    # Minimal HS256 JWT encode/decode fallback — local testing only, so the
    # service still runs before `pip install -r requirements.txt` has PyJWT.
    import base64
    import hashlib
    import hmac
    import json
    import time

    def _b64u_encode(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    def _b64u_decode(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def _jwt_encode(payload: dict, secret: str, algorithm: str = "HS256") -> str:
        header = {"alg": algorithm, "typ": "JWT"}
        h = _b64u_encode(json.dumps(header, separators=(",", ":")).encode())
        p = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{h}.{p}".encode()
        sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        return f"{h}.{p}.{_b64u_encode(sig)}"

    def _jwt_decode(token: str, secret: str, algorithms=None) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        h, p, s = parts
        expected = _b64u_encode(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, s):
            raise ValueError("Invalid signature")
        payload = json.loads(_b64u_decode(p))
        if payload.get("exp") is not None and time.time() > float(payload["exp"]):
            raise ValueError("Token expired")
        return payload


def create_access_token(partner_data: dict) -> str:
    """Generates a signed JWT access token valid for `jwt_expiry_seconds`."""
    to_encode = partner_data.copy()
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_expiry_seconds)
    to_encode["exp"] = int(expire.timestamp())
    if _HAS_PYJWT:
        return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return _jwt_encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_current_partner(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db=Depends(get_db),
):
    """FastAPI dependency: validates the Bearer token and returns the calling
    partner's document (so routes can read partner_id, webhook_url, etc.)."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials
    try:
        if _HAS_PYJWT:
            payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        else:
            payload = _jwt_decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        partner_id = payload.get("sub")
        if partner_id is None:
            raise credentials_exception
    except HTTPException:
        raise
    except Exception:
        raise credentials_exception

    partner = await db["registered_partners"].find_one({"_id": partner_id, "api_key": payload.get("api_key")})
    if not partner or not partner.get("active", True):
        raise credentials_exception

    return partner
