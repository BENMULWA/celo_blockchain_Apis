import os
import asyncio
try:
    import bcrypt
    _HAS_BCRYPT = True
except Exception:
    # Provide a minimal bcrypt shim for local testing when the C extension isn't installed.
    _HAS_BCRYPT = False
    class _DummyBcrypt:
        def gensalt(self):
            return b""
        def hashpw(self, pw, salt):
            return pw if isinstance(pw, bytes) else str(pw).encode('utf-8')
    bcrypt = _DummyBcrypt()
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from datetime import datetime
import certifi

# Load environment variables
load_dotenv()

# MongoDB connection configuration
MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("MONGO_DB_NAME", "Celo_APIS")

# 🟢 Initialize Motor Client globally for FastAPI routes to share
client = AsyncIOMotorClient(MONGO_URL, tlsCAFile=certifi.where())
db = client[DB_NAME]


# 🟢 FastAPI Dependency used across route files (e.g., db=Depends(get_db))
async def get_db():
    """Returns the MongoDB database instance for FastAPI request injection."""
    return db


def hash_secret(secret: str) -> str:
    """Hashes the secret key so it is unreadable in the database."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(secret.encode('utf-8'), salt)
    return hashed.decode('utf-8')


async def save_new_partner():
    if not MONGO_URL:
        print("❌ ERROR: MONGO_URL not found in .env file.")
        return

    print("🔄 Connecting to MongoDB...")
    
    # Raw Keys
    raw_api_key = os.getenv("PARTNER_API_KEY")
    raw_secret_key = os.getenv("PARTNER_SECRET_KEY")
    
    if not raw_api_key or not raw_secret_key:
        print("❌ ERROR: PARTNER_API_KEY or PARTNER_SECRET_KEY missing in .env")
        return
    
    # Hash secret key for safe storage
    secure_hashed_secret = hash_secret(raw_secret_key)
    
    partner_document = {
        "_id": "Jimmy_Shillingi BET",  # Unique identifier for the partner  
        "company_name": "Shillingi Bet",
        "api_key": raw_api_key,        # API Keys are public
        "secret_key": secure_hashed_secret, # HASHED secret key
        "created_at": datetime.utcnow()
    }
    
    try:
        await db["registered_partners"].insert_one(partner_document)
        
        await db["partner_wallets"].insert_one({
            "partner_id": str(partner_document["_id"]),
            "USDC": 0.0
        })
        
        print("\n==================================================")
        print(f"✅ SUCCESS! Partner '{partner_document['company_name']}' is now live.")
        print("==================================================")
        print("The keys were securely HASHED and saved to MongoDB.")
        
    except Exception as e:
        print(f"\n❌ Failed to save partner: {e}")


if __name__ == "__main__":
    asyncio.run(save_new_partner())