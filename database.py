import os
import asyncio
import bcrypt
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from datetime import datetime

# Load the database URL from your .env file
load_dotenv()

def hash_secret(secret: str) -> str:
    """Hashes the secret key so it is unreadable in the database."""
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(secret.encode('utf-8'), salt)
    return hashed.decode('utf-8')

async def save_new_partner():
    mongo_url = os.getenv("MONGO_URL")
    db_name = os.getenv("MONGO_DB_NAME", "Celo_APIS")
    
    if not mongo_url:
        print("❌ ERROR: MONGO_URL not found in .env file.")
        return

    print("🔄 Connecting to MongoDB...")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    
    # 1. The Raw Keys (What you email to the partner)
    raw_api_key = os.getenv("PARTNER_API_KEY")
    raw_secret_key = os.getenv("PARTNER_SECRET_KEY")
    
    # 2. Hash the secret key for storage!
    secure_hashed_secret = hash_secret(raw_secret_key)
    
    partner_document = {
        "_id": "Jimmy_Shillingi BET",  # Unique identifier for the partner  
        "company_name": "Shillingi Bet",
        "api_key": raw_api_key, # API Keys are public, so plain text is fine
        "secret_key": secure_hashed_secret, # SECRET Keys MUST be hashed!
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