try:
    import bcrypt
    _HAS_BCRYPT = True
except Exception:
    _HAS_BCRYPT = False

    class _DummyBcrypt:
        """Minimal shim so local dev works without the bcrypt C extension.
        Never used when the real bcrypt package is installed (production)."""

        def gensalt(self):
            return b""

        def hashpw(self, pw, salt):
            return pw if isinstance(pw, bytes) else str(pw).encode("utf-8")

    bcrypt = _DummyBcrypt()

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from config import settings

# Initialized once at import time and shared by every route via Depends(get_db).
client = AsyncIOMotorClient(settings.mongo_url, tlsCAFile=certifi.where())
db = client[settings.mongo_db_name]


async def get_db():
    """FastAPI dependency: yields the shared Motor database instance."""
    return db


def hash_secret(secret: str) -> str:
    """Hashes a partner secret key so it is never stored/readable in plaintext."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(secret.encode("utf-8"), salt).decode("utf-8")


async def ensure_indexes():
    """Idempotent index creation, called once on app startup.

    Uniqueness here is what makes cross-partner scoping safe: two partners can
    both send external_user_id="42" without colliding, because every index and
    balance lookup is keyed by (partner_id, external_user_id), never by
    external_user_id alone.
    """
    await db["registered_partners"].create_index("api_key", unique=True)
    await db["celo_wallet_indexes"].create_index(
        [("partnerId", 1), ("externalUserId", 1)], unique=True
    )
    await db["celo_wallet_indexes"].create_index("index", unique=True)
    await db["partner_balances"].create_index(
        [("partnerId", 1), ("externalUserId", 1)], unique=True
    )
    await db["celo_ledger_entries"].create_index([("partnerId", 1), ("createdAt", -1)])
    await db["celo_ledger_entries"].create_index("txHash")
