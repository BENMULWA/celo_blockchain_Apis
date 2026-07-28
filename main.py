import os
import uuid
import asyncio
import time
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, HTTPException, Depends, status, FastAPI
from pydantic import BaseModel
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from database import get_db
from dotenv import load_dotenv

# Token validation imports and logic
from auth import get_current_user, create_access_token, verify_secret

load_dotenv(override=True)

app = FastAPI()

withdraw_lock = asyncio.Lock()

router = APIRouter(prefix="/api/valora", tags=["Celo Wallets (MiniPay & Valora)"])

# 🟢 Global Concurrency Lock (Prevents nonce collisions during simultaneous withdrawals)

# 🟢 Fast Ankr RPC with a 10-second timeout to prevent hanging 
CELO_RPC = os.getenv("CELO_RPC_URL", "https://rpc.ankr.com/celo")
CHAIN_ID = 42220

w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={'timeout': 10}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

# Official 40-character Celo Mainnet checksummed addresses
ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address("0x765DE816845861e75A25fCA122bb6898B8B1282a".lower()), 
    "USDC": w3.to_checksum_address("0xcebA9300f2b948710d2653dD7B07f33A8B32118C".lower()), 
    "USDT": w3.to_checksum_address("0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e".lower())  
}

# Minimal ABI to decode transfer logs
ERC20_ABI = [
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "internalType": "address", "name": "from", "type": "address"}, {"indexed": True, "internalType": "address", "name": "to", "type": "address"}, {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"}
]

class VerifyRequest(BaseModel):
    amount: float
    tx_hash: str
    asset: str
    counterparty: str = ""

class WithdrawReq(BaseModel):
    identifier: str
    amount: float
    asset: str

class DepositMemoResponse(BaseModel):
    treasury_address: str
    memo: str
    network: str
    asset: str

class TokenRequest(BaseModel):
    api_key: str
    secret_key: str


# ======================================
# Auth & Token Routes
# ======================================

@router.post("/v1/auth/token")
async def login_for_access_token_current_user(body: TokenRequest, db = Depends(get_db)):
    """
    Exchanges API Key & Secret Key for a 1-hour JWT Bearer Token.
    """
    # 1. Look up partner by api_key (plural collection name)
    partner = await db["registered_partners"].find_one({"api_key": body.api_key})

    if not partner:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid API Key or Secret Key"
        )

    # 2. Verify hashed secret key
    if not verify_secret(body.secret_key, partner["secret_key"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid API Key or Secret Key"
        )

    # 3. Create JWT payload and generate token
    token_payload = {
        "sub": str(partner["_id"]),
        "partner_name": partner.get("partner_name"),
        "api_key": partner.get("api_key")
    }

    access_token = create_access_token(token_payload)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 7200 # in the 1 min
    }


# ======================================
# Celo Operational Routes
# ======================================

@router.get("/deposit-details", response_model=DepositMemoResponse)
async def get_deposit_details():
    private_key = os.getenv("CELO_TREASURY_PK")
    if not private_key:
        treasury_address = "0x0000000000000000000000000000000000000000"
    else:
        if not private_key.startswith("0x"): 
            private_key = f"0x{private_key}"
        account = w3.eth.account.from_key(private_key)
        treasury_address = account.address

    memo = f"MESH-ANON-{uuid.uuid4().hex[:8].upper()}"
    return {
        "treasury_address": treasury_address,
        "memo": memo,
        "network": "Celo Mainnet",
        "asset": "USDC/USDT/cUSD"
    }

@router.post("/on-ramp/verify", status_code=201)
async def verify_valora_deposit(req: VerifyRequest, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")


    clean_tx_hash = req.tx_hash.strip().lower()
    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    existing_tx = await db["ramp_entries"].find_one({"txHash": req.tx_hash, "direction": "on"})
    if existing_tx:
        raise HTTPException(status_code=409, detail="This transaction hash has already been processed.")

    def fetch_and_verify_receipt():
        for attempt in range(3):
            try:
                receipt = w3.eth.get_transaction_receipt(req.tx_hash)
                if receipt.status != 1:
                    return False, "Transaction failed or reverted on the blockchain."
                    
                contract = w3.eth.contract(address=ASSET_CONTRACTS[req.asset], abi=ERC20_ABI)
                logs = contract.events.Transfer().process_receipt(receipt)
                
                pk = os.getenv("CELO_TREASURY_PK")
                if not pk:
                    return False, "Server misconfiguration: Treasury wallet not set."
                account = w3.eth.account.from_key(pk if pk.startswith("0x") else f"0x{pk}")
                treasury_addr = account.address.lower()
                
                decimals = 18 if req.asset == "cUSD" else 6
                expected_base_units = int(Decimal(str(req.amount)) * Decimal(10 ** decimals))
                
                for log in logs:
                    if log['args']['to'].lower() == treasury_addr:
                        if log['args']['value'] >= expected_base_units:
                            return True, "Valid"
                            
                return False, f"Funds were not sent to the Treasury or amount was less than {req.amount} {req.asset}."
                
            except Exception as e:
                err_str = str(e)
                if "Connection" in err_str and attempt < 2:
                    time.sleep(1.5)
                    continue
                return False, f"Blockchain query error: {err_str}"
                
        return False, "Failed to connect to Celo RPC after 3 attempts."

    is_valid, err_msg = await asyncio.to_thread(fetch_and_verify_receipt)
    
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {req.asset: req.amount}},
        upsert=True
    )

    now = datetime.utcnow()
    
    await db["ramp_entries"].insert_one({
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "on",
        "channel": "Opera MiniPay",
        "fromAsset": req.asset,
        "toAsset": req.asset,
        "fromAmount": req.amount,
        "toAmount": req.amount,
        "status": "COMPLETED",
        "userId": user_id,
       "txHash": clean_tx_hash,
        "counterparty": req.counterparty or "MiniPay On-Chain",
        "date": now.strftime("%b %d, %Y"),
        "timeAgo": "Just now",
        "createdAt": now
    })
    
    return {"status": "success", "message": f"{req.amount} {req.asset} verified on Celo and credited!"}

@router.post("/withdraw")
async def withdraw_from_valora(req: WithdrawReq, db=Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.get("_id")

    if req.asset not in ASSET_CONTRACTS:
        raise HTTPException(status_code=400, detail="Unsupported Celo asset.")

    user_wallet = await db["retail_wallets"].find_one({"userId": user_id})
    current_bal = float(user_wallet.get(req.asset, 0.0)) if user_wallet else 0.0
    
    if current_bal < req.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient {req.asset} balance. You have {current_bal}.")

    await db["retail_wallets"].update_one(
        {"userId": user_id},
        {"$inc": {req.asset: -req.amount}}
    )

    target_address = req.identifier.strip().lower()
    if not w3.is_address(target_address):
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {req.asset: req.amount}})
        raise HTTPException(status_code=400, detail="Invalid Celo destination address.")
        
    target_address = w3.to_checksum_address(target_address)
    
    try:
        private_key = os.getenv("CELO_TREASURY_PK")
        if not private_key:
            raise ValueError("CELO_TREASURY_PK is missing in environment. Cannot sign transaction.")
            
        account = w3.eth.account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
        
        decimals = 18 if req.asset == "cUSD" else 6
        amount_base = int(Decimal(str(req.amount)) * Decimal(10 ** decimals))

        contract = w3.eth.contract(address=ASSET_CONTRACTS[req.asset], abi=ERC20_ABI)
        
        def execute_tx():
            nonce = w3.eth.get_transaction_count(account.address, 'pending')
            tx = contract.functions.transfer(target_address, amount_base).build_transaction({
                'chainId': CHAIN_ID,
                'gas': 150000,
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
            })
            signed_tx = w3.eth.account.sign_transaction(tx, account.key)
            raw_tx = getattr(signed_tx, 'raw_transaction', getattr(signed_tx, 'rawTransaction', None))
            return w3.to_hex(w3.eth.send_raw_transaction(raw_tx))

        async with withdraw_lock:
            tx_hex = await asyncio.to_thread(execute_tx)

    except Exception as e:
        await db["retail_wallets"].update_one({"userId": user_id}, {"$inc": {req.asset: req.amount}})
        raise HTTPException(status_code=502, detail=f"Blockchain transfer failed: {str(e)}")

    now = datetime.utcnow()
    
    await db["ramp_entries"].insert_one({
        "_id": f"TRADE_{uuid.uuid4().hex[:8].upper()}",
        "direction": "off", 
        "channel": "Opera MiniPay", 
        "fromAsset": req.asset, 
        "toAsset": req.asset,
        "fromAmount": req.amount, 
        "toAmount": req.amount, 
        "rate": 1.0, 
        "fee": 0.0,
        "counterparty": req.identifier, 
        "status": "COMPLETED", 
        "txHash": tx_hex, 
        "walletAddress": target_address,
        "userId": user_id, 
        "createdAt": now, 
        "date": now.strftime("%b %d, %Y"), 
        "timeAgo": "Just now"
    })

    return {"status": "success", "message": f"{req.amount} {req.asset} sent to your wallet!", "tx_hash": tx_hex}


# ======================================
# 🔑 IMPORTANT: REGISTER THE ROUTER WITH FASTAPI
# ======================================
app.include_router(router)