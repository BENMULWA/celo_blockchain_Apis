import os
import asyncio
import bcrypt
from datetime import datetime, timedelta
from fastapi import FastAPI, APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient
import jwt
from web3 import Web3
from dotenv import load_dotenv

# Web3 v7 compatibility for PoA middleware
try:
    from web3.middleware import geth_poa_middleware
except ImportError:
    from web3.middleware import ExtraDataToPOAMiddleware as geth_poa_middleware

load_dotenv(override=True)

# ==========================================
# 1. DATABASE CONFIGURATION
# ==========================================
MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "Celo_APIS")

if not MONGO_URL:
    raise ValueError("CRITICAL ERROR: MONGO_URL environment variable is missing from the .env file!")

client = AsyncIOMotorClient(MONGO_URL)
db = client[MONGO_DB_NAME]

# ==========================================
# 2. CELO WEB3 CONFIGURATION
# ==========================================
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = 42220 if "forno" in CELO_RPC or "mainnet" in CELO_RPC else 44787

# 🟢 FIXED: The REAL Celo Mainnet USDC Contract Address
USDC_ADDRESS = "0x07865c6E87B9F70255377e024ef6629E264Ec76"
NETWORK_NAME = "Celo Mainnet"

w3 = Web3(Web3.HTTPProvider(CELO_RPC))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"}
]

# ==========================================
# 3. SECURITY & TOKEN SESSION (JWT)
# ==========================================
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-mamlaka-partner-key-2026")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

security_scheme = HTTPBearer()

class PartnerAuthRequest(BaseModel):
    api_key: str
    secret_key: str

async def verify_token_session(credentials: HTTPAuthorizationCredentials = Depends(security_scheme)):
    """Validates the JWT token passed by the partner in the Authorization header."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        partner_id: str = payload.get("sub")
        if partner_id is None:
            raise HTTPException(status_code=401, detail="Invalid token session.")
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token session expired. Please generate a new one.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token session.")

# ==========================================
# 4. APP INITIALIZATION & ROUTES
# ==========================================
app = FastAPI(title="Mamlaka Partner BaaS", version="1.0", description="API Gateway for Celo Liquidity")

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_credentials=True, 
    allow_methods=["*"], 
    allow_headers=["*"]
)

router = APIRouter(prefix="/v1", tags=["Partner Operations"])

# --- SCHEMAS ---
class WithdrawRequest(BaseModel):
    to_address: str
    amount: float
    idempotency_key: str

# --- ENDPOINT 1: GENERATE TOKEN SESSION ---
@router.post("/auth/token")
async def generate_partner_token(req: PartnerAuthRequest):
    """Partners must exchange their API Key & Secret Key for a JWT Session Token."""
    partner = await db["registered_partners"].find_one({"api_key": req.api_key})
    
    if not partner:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API Key.")

    try:
        is_valid_password = bcrypt.checkpw(
            req.secret_key.encode('utf-8'), 
            partner["secret_key"].encode('utf-8')
        )
        if not is_valid_password:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid Secret Key.")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized: Credential verification failed.")

    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = {
        "sub": str(partner["_id"]),
        "company": partner.get("company_name", "Unknown"),
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    return {
        "access_token": encoded_jwt, 
        "token_type": "bearer", 
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }

# --- ENDPOINT 2: CHECK BALANCE ---
@router.get("/balance")
async def check_partner_balance(session: dict = Depends(verify_token_session)):
    partner_id = session["sub"]
    wallet = await db["partner_wallets"].find_one({"partner_id": partner_id})
    usdc_balance = float(wallet.get("USDC", 0.0)) if wallet else 0.0
    
    return {"status": "success", "company": session["company"], "asset": "USDC", "balance": usdc_balance}

# --- ENDPOINT 3: DEPOSIT DETAILS ---
@router.get("/deposit")
async def get_deposit_instructions(session: dict = Depends(verify_token_session)):
    private_key = os.getenv("CELO_TREASURY_PK")
    if not private_key:
        treasury_address = "0x0000000000000000000000000000000000000000"
    else:
        if not private_key.startswith("0x"): private_key = f"0x{private_key}"
        account = w3.eth.account.from_key(private_key)
        treasury_address = account.address

    partner_id = session["sub"]
    partner_memo = f"MESH-{partner_id.upper()}"

    return {
        "status": "success",
        "instructions": "Send USDC on the Celo Network to the treasury_address. MUST include the required_memo in transaction data.",
        "treasury_address": treasury_address,
        "required_memo": partner_memo,
        "network": NETWORK_NAME
    }

# --- ENDPOINT 4: WITHDRAW (PAYOUT) ---
@router.post("/withdraw")
async def execute_partner_withdrawal(req: WithdrawRequest, session: dict = Depends(verify_token_session)):
    partner_id = session["sub"]
    
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero.")
    if not w3.is_address(req.to_address):
        raise HTTPException(status_code=400, detail="Invalid Celo destination address.")

    # Idempotency Check (Prevents duplicate withdrawals if partner clicks twice)
    existing_tx = await db["partner_transactions"].find_one({"idempotency_key": req.idempotency_key})
    if existing_tx:
        return {"status": "success", "message": "Already processed.", "tx_hash": existing_tx["tx_hash"]}

    # 1. Check & Deduct Balance Atomically
    wallet = await db["partner_wallets"].find_one({"partner_id": partner_id})
    current_balance = float(wallet.get("USDC", 0.0)) if wallet else 0.0
    
    if current_balance < req.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient balance. Available: {current_balance} USDC.")

    await db["partner_wallets"].update_one(
        {"partner_id": partner_id},
        {"$inc": {"USDC": -req.amount}},
        upsert=True
    )

    # 2. Execute Web3 Transaction
    private_key = os.getenv("CELO_TREASURY_PK")
    if not private_key:
        await db["partner_wallets"].update_one({"partner_id": partner_id}, {"$inc": {"USDC": req.amount}})
        raise HTTPException(status_code=500, detail="Server Configuration Error: Missing Treasury Key.")

    account = w3.eth.account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
    
    try:
        def sync_transfer():
            contract = w3.eth.contract(address=w3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
            amount_base_units = int(req.amount * 1_000_000) # USDC has 6 decimals
            nonce = w3.eth.get_transaction_count(account.address)

            tx = contract.functions.transfer(
                w3.to_checksum_address(req.to_address), amount_base_units
            ).build_transaction({
                'chainId': CHAIN_ID,
                'gas': 150000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
            })

            signed_tx = w3.eth.account.sign_transaction(tx, private_key=account.key)
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            return w3.to_hex(w3.eth.send_raw_transaction(raw_tx))

        tx_hash = await asyncio.to_thread(sync_transfer)

        await db["partner_transactions"].insert_one({
            "partner_id": partner_id,
            "type": "WITHDRAWAL",
            "amount": req.amount,
            "to_address": req.to_address,
            "tx_hash": tx_hash,
            "idempotency_key": req.idempotency_key,
            "timestamp": datetime.utcnow()
        })

        return {"status": "success", "message": "Withdrawal broadcasted.", "tx_hash": tx_hash}

    except Exception as e:
        await db["partner_wallets"].update_one({"partner_id": partner_id}, {"$inc": {"USDC": req.amount}})
        raise HTTPException(status_code=500, detail=f"Blockchain Error: {str(e)}")

app.include_router(router)