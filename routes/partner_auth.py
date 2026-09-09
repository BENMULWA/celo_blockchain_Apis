from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auth import create_access_token, verify_secret
from config import settings
from database import get_db

router = APIRouter(prefix="/v1/auth", tags=["Partner Auth"])


class TokenRequest(BaseModel):
    api_key: str
    secret_key: str


@router.post("/token")
async def login_for_access_token(body: TokenRequest, db=Depends(get_db)):
    """Exchanges a partner's API key & secret key for a Bearer JWT."""
    partner = await db["registered_partners"].find_one({"api_key": body.api_key})
    if not partner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key or Secret Key")

    if not verify_secret(body.secret_key, partner["secret_key"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key or Secret Key")

    token_payload = {
        "sub": str(partner["_id"]),
        "partner_name": partner.get("company_name"),
        "api_key": partner.get("api_key"),
    }
    access_token = create_access_token(token_payload)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_expiry_seconds,
    }
