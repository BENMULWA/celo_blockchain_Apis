"""The Celo BaaS gateway's core API surface: deposit address issuance,
manual deposit verification, intra-ledger swaps between supported
stablecoins, on-chain withdrawals, and balance lookups.

Every endpoint is partner-scoped: the caller authenticates once via
/v1/auth/token (see routes/partner_auth.py) to get a Bearer JWT identifying
their platform, then every request carries an `external_user_id` that
platform assigns to its own end-user (e.g. a Shillingi Bet bettor id).
This gateway never sees that user's credentials — only its own derived
Celo address and ledger balance for them.
"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_partner
from celo.audit import log_celo_audit_event
from celo.contracts import (
    get_contract,
    get_treasury_account,
    get_treasury_address,
    to_base_units,
    w3,
)
from celo.idempotency import claim_idempotency_key, complete_idempotency_key, compute_request_hash, fail_idempotency_key
from celo.ledger import adjust_balance, credit_balance, debit_balance, get_all_balances
from celo.rates import kes_to_usd, usd_to_kes
from celo.wallet import get_or_create_deposit_index, derive_celo_account
from config import SUPPORTED_ASSETS, settings
from database import get_db
from webhooks import notify_partner

router = APIRouter(prefix="/api/celo", tags=["Celo Gateway"])

withdraw_lock = asyncio.Lock()


def _require_supported_asset(asset: str) -> None:
    if asset not in SUPPORTED_ASSETS:
        raise HTTPException(status_code=400, detail=f"Unsupported Celo asset. Supported: {', '.join(SUPPORTED_ASSETS)}")


# ======================================================================
# Models
# ======================================================================
class InitiateDepositReq(BaseModel):
    external_user_id: str
    asset: str


class DepositStatusRes(BaseModel):
    status: str
    tx_hash: Optional[str] = None
    message: str = ""


class VerifyDepositReq(BaseModel):
    external_user_id: str
    amount: float
    tx_hash: str
    asset: str


class SwapReq(BaseModel):
    external_user_id: str
    from_asset: str
    to_asset: str
    amount: float


class WithdrawReq(BaseModel):
    external_user_id: str
    asset: str
    amount: float
    destination_address: str
    idempotency_key: Optional[str] = None


# ======================================================================
# Deposits
# ======================================================================
@router.post("/deposit/initiate")
async def initiate_deposit(req: InitiateDepositReq, db=Depends(get_db), partner=Depends(get_current_partner)):
    """Returns this end-user's permanent Celo deposit address for `asset`.

    The address is derived once and reused forever after — there's no
    "session" to expire, matching a Binance/Coinbase-style deposit address.
    The background watcher (workers/celo_deposit_watcher.py) credits
    whatever amount actually arrives and fires the partner's webhook.
    """
    _require_supported_asset(req.asset)
    partner_id = str(partner["_id"])

    wallet_index = await get_or_create_deposit_index(db, partner_id, req.external_user_id)
    deposit_address = derive_celo_account(wallet_index).address

    dep_id = f"DEP_{uuid.uuid4().hex[:8].upper()}"
    await db["pending_deposits"].insert_one({
        "_id": dep_id,
        "partnerId": partner_id,
        "externalUserId": req.external_user_id,
        "asset": req.asset,
        "network": "celo",
        "depositAddress": deposit_address,
        "walletIndex": wallet_index,
        "status": "listening",
        "createdAt": datetime.utcnow(),
    })

    return {"deposit_id": dep_id, "address": deposit_address, "network": "Celo Mainnet", "asset": req.asset}


@router.get("/deposit/{dep_id}/status", response_model=DepositStatusRes)
async def get_deposit_status(dep_id: str, db=Depends(get_db), partner=Depends(get_current_partner)):
    dep = await db["pending_deposits"].find_one({"_id": dep_id, "partnerId": str(partner["_id"])})
    if not dep:
        raise HTTPException(status_code=404, detail="Deposit session not found.")

    if dep["status"] == "credited":
        return DepositStatusRes(status="credited", tx_hash=dep.get("txHash"), message="Funds credited!")
    return DepositStatusRes(status=dep["status"], message="Waiting for on-chain confirmation...")


@router.post("/on-ramp/verify", status_code=201)
async def verify_deposit(req: VerifyDepositReq, db=Depends(get_db), partner=Depends(get_current_partner)):
    """Manual fallback: verify+credit a deposit by tx hash directly, for
    clients that can't wait on the background watcher/webhook."""
    _require_supported_asset(req.asset)
    partner_id = str(partner["_id"])

    clean_tx_hash = req.tx_hash.strip()
    if not clean_tx_hash.startswith("0x"):
        clean_tx_hash = f"0x{clean_tx_hash}"

    existing = await db["celo_ledger_entries"].find_one({"txHash": clean_tx_hash, "direction": "deposit"})
    if existing:
        raise HTTPException(status_code=409, detail="This transaction hash has already been processed.")

    def fetch_and_verify_receipt():
        for attempt in range(3):
            try:
                receipt = w3.eth.get_transaction_receipt(clean_tx_hash)
                if receipt.status != 1:
                    return False, "Transaction failed or reverted on the blockchain."

                contract = get_contract(req.asset)
                logs = contract.events.Transfer().process_receipt(receipt)
                treasury_addr = get_treasury_address().lower()
                expected_base_units = to_base_units(req.amount, req.asset)

                for log in logs:
                    if log["args"]["to"].lower() == treasury_addr and log["args"]["value"] >= expected_base_units:
                        return True, "Valid"
                return False, f"Funds were not sent to the treasury, or amount was less than {req.amount} {req.asset}."
            except Exception as exc:
                if "Connection" in str(exc) and attempt < 2:
                    time.sleep(1.5)
                    continue
                return False, f"Blockchain query error: {exc}"
        return False, "Failed to connect to Celo RPC after 3 attempts."

    is_valid, err_msg = await asyncio.to_thread(fetch_and_verify_receipt)
    if not is_valid:
        raise HTTPException(status_code=400, detail=err_msg)

    await credit_balance(db, partner_id, req.external_user_id, req.asset, req.amount)

    kes_amount, kes_rate, kes_rate_stale = None, None, None
    try:
        kes_amount, kes_rate, kes_rate_stale = await usd_to_kes(req.amount)
    except Exception:
        pass

    now = datetime.utcnow()
    await db["celo_ledger_entries"].insert_one({
        "_id": f"TXN_{uuid.uuid4().hex[:10].upper()}",
        "partnerId": partner_id,
        "externalUserId": req.external_user_id,
        "direction": "deposit",
        "asset": req.asset,
        "amount": req.amount,
        "kesAmount": kes_amount,
        "kesRate": kes_rate,
        "status": "completed",
        "txHash": clean_tx_hash,
        "createdAt": now,
    })

    await notify_partner(
        partner, "deposit.credited",
        externalUserId=req.external_user_id, asset=req.asset, amount=req.amount, txHash=clean_tx_hash,
        kesAmount=kes_amount, kesRate=kes_rate, kesRateStale=kes_rate_stale,
    )

    return {"status": "success", "message": f"{req.amount} {req.asset} verified on Celo and credited!"}


# ======================================================================
# Balance
# ======================================================================
@router.get("/balance/{external_user_id}")
async def get_balances(external_user_id: str, db=Depends(get_db), partner=Depends(get_current_partner)):
    balances = await get_all_balances(db, str(partner["_id"]), external_user_id)
    return {"external_user_id": external_user_id, "balances": {a: balances.get(a, 0.0) for a in SUPPORTED_ASSETS}}


# ======================================================================
# KES <-> USD-stablecoin quoting
# ======================================================================
@router.get("/quote")
async def get_quote(
    amount: float,
    from_currency: str,
    to_currency: str,
    partner=Depends(get_current_partner),
):
    """Quotes a conversion between KES and any supported stablecoin (all
    treated as ~1 USD). Used two ways:
      - Shillingi Bet sizing a USDT withdrawal against a KES payout amount
        (from_currency=KES, to_currency=USDT).
      - Verifying/displaying the same rate the deposit watcher will use to
        auto-credit KES on a stablecoin deposit (from_currency=USDT,
        to_currency=KES).
    The rate is margin-adjusted the same way in both directions, so a quote
    here always matches what actually gets credited/settled.
    """
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")

    if from_currency == "KES" and to_currency in SUPPORTED_ASSETS:
        converted, rate, is_stale = await kes_to_usd(amount)
    elif from_currency in SUPPORTED_ASSETS and to_currency == "KES":
        converted, rate, is_stale = await usd_to_kes(amount)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported quote pair: {from_currency}->{to_currency}.")

    return {
        "from_currency": from_currency,
        "to_currency": to_currency,
        "from_amount": amount,
        "to_amount": converted,
        "rate": rate,
        "rate_is_stale": is_stale,
    }


# ======================================================================
# Swap (ledger-only conversion between supported stablecoins — no on-chain
# transfer, since these are all ~1:1 USD-pegged assets already sitting in
# the treasury after a deposit sweep). Use /withdraw for on-chain payout.
# ======================================================================
@router.post("/swap")
async def swap_asset(req: SwapReq, db=Depends(get_db), partner=Depends(get_current_partner)):
    _require_supported_asset(req.from_asset)
    _require_supported_asset(req.to_asset)
    if req.from_asset == req.to_asset:
        raise HTTPException(status_code=400, detail="from_asset and to_asset must differ.")
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")

    partner_id = str(partner["_id"])
    # All supported assets are ~1:1 USD-pegged stablecoins on Celo, so the
    # swap rate is 1.0. If a partner ever needs a non-stable asset, price it
    # here before crediting instead of assuming parity.
    to_amount = req.amount

    await debit_balance(db, partner_id, req.external_user_id, req.from_asset, req.amount)
    await credit_balance(db, partner_id, req.external_user_id, req.to_asset, to_amount)

    swap_id = f"SWP_{uuid.uuid4().hex[:10].upper()}"
    await db["celo_ledger_entries"].insert_one({
        "_id": swap_id,
        "partnerId": partner_id,
        "externalUserId": req.external_user_id,
        "direction": "swap",
        "fromAsset": req.from_asset,
        "toAsset": req.to_asset,
        "fromAmount": req.amount,
        "toAmount": to_amount,
        "status": "completed",
        "createdAt": datetime.utcnow(),
    })

    await notify_partner(
        partner, "swap.completed",
        externalUserId=req.external_user_id, fromAsset=req.from_asset, toAsset=req.to_asset,
        fromAmount=req.amount, toAmount=to_amount, swapId=swap_id,
    )

    return {"status": "success", "swap_id": swap_id, "from_amount": req.amount, "to_amount": to_amount}


# ======================================================================
# Withdrawals (real on-chain broadcast from the treasury)
# ======================================================================
@router.post("/withdraw")
async def withdraw(req: WithdrawReq, db=Depends(get_db), partner=Depends(get_current_partner)):
    _require_supported_asset(req.asset)
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Withdrawal amount must be positive.")

    partner_id = str(partner["_id"])

    try:
        target_address = w3.to_checksum_address(req.destination_address.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Celo destination address. Must be a valid 0x format.")

    idempotency_key = req.idempotency_key
    if idempotency_key:
        request_hash = compute_request_hash(
            external_user_id=req.external_user_id, asset=req.asset,
            amount=req.amount, destination_address=target_address,
        )
        replay = await claim_idempotency_key(db, partner_id, idempotency_key, request_hash)
        if replay is not None:
            return replay

    try:
        response = await _execute_withdraw(req, db, partner, partner_id, target_address)
    except HTTPException as exc:
        if idempotency_key:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            await fail_idempotency_key(db, partner_id, idempotency_key, detail)
        raise

    if idempotency_key:
        await complete_idempotency_key(db, partner_id, idempotency_key, response)
    return response


async def _execute_withdraw(req: WithdrawReq, db, partner, partner_id: str, target_address: str) -> dict:
    """The real withdrawal logic, separated out so the idempotency wrapper
    above can catch any HTTPException raised anywhere in here (limit
    rejections included) and release the idempotency key for a clean retry."""
    await log_celo_audit_event(
        db, "withdrawal_requested",
        partnerId=partner_id, externalUserId=req.external_user_id, asset=req.asset,
        amount=req.amount, destination=target_address,
    )

    if req.amount > settings.celo_max_withdrawal_per_tx:
        await log_celo_audit_event(
            db, "withdrawal_blocked_limit", partnerId=partner_id, externalUserId=req.external_user_id,
            asset=req.asset, amount=req.amount, reason="per_tx_limit", limit=settings.celo_max_withdrawal_per_tx,
        )
        raise HTTPException(status_code=400, detail=f"Withdrawals are capped at {settings.celo_max_withdrawal_per_tx} {req.asset} per transaction.")

    day_ago = datetime.utcnow() - timedelta(hours=24)
    daily_totals = await db["celo_audit_log"].aggregate([
        {"$match": {"event": "withdrawal_broadcast", "partnerId": partner_id, "externalUserId": req.external_user_id, "asset": req.asset, "createdAt": {"$gte": day_ago}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]).to_list(length=1)
    already_withdrawn_today = daily_totals[0]["total"] if daily_totals else 0.0

    if already_withdrawn_today + req.amount > settings.celo_max_withdrawal_per_day:
        await log_celo_audit_event(
            db, "withdrawal_blocked_limit", partnerId=partner_id, externalUserId=req.external_user_id,
            asset=req.asset, amount=req.amount, reason="daily_limit", limit=settings.celo_max_withdrawal_per_day,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Daily withdrawal limit reached: up to {settings.celo_max_withdrawal_per_day} {req.asset} per 24 hours. Already withdrawn {already_withdrawn_today:.2f} today.",
        )

    # No ledger balance check here: this is a partner-authorized payout, not
    # a self-service withdrawal against a balance this gateway tracks.
    # Shillingi Bet (already JWT-authenticated) has decided this amount is
    # owed based on its own KES ledger — a betting win has no specific prior
    # deposit backing it in *this* gateway's per-user balance. The per-tx and
    # daily caps above, plus withdraw_lock below, are what bound the blast
    # radius of a compromised partner credential — not a balance gate.
    try:
        account = get_treasury_account()
        if not account:
            raise ValueError("CELO_TREASURY_PK is missing in environment. Cannot sign transaction.")

        contract = get_contract(req.asset)
        amount_base = to_base_units(req.amount, req.asset)

        def check_gas_balance():
            gas_price = w3.eth.gas_price
            required_wei = gas_price * 150000
            available_wei = w3.eth.get_balance(account.address)
            if available_wei < required_wei:
                raise ValueError(
                    f"Treasury insufficient CELO for gas. Have: {available_wei / 1e18:.6f}, Need: {required_wei / 1e18:.6f}"
                )

        await asyncio.to_thread(check_gas_balance)

        def execute_tx():
            nonce = w3.eth.get_transaction_count(account.address, "pending")
            tx = contract.functions.transfer(target_address, amount_base).build_transaction({
                "chainId": settings.celo_chain_id,
                "gas": 150000,
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
            })
            signed_tx = w3.eth.account.sign_transaction(tx, account.key)
            raw_tx = getattr(signed_tx, "raw_transaction", getattr(signed_tx, "rawTransaction", None))
            return w3.to_hex(w3.eth.send_raw_transaction(raw_tx))

        async with withdraw_lock:
            tx_hex = await asyncio.to_thread(execute_tx)

    except Exception as exc:
        await log_celo_audit_event(
            db, "withdrawal_failed", partnerId=partner_id, externalUserId=req.external_user_id,
            asset=req.asset, amount=req.amount, error=str(exc),
        )
        await notify_partner(
            partner, "withdrawal.failed",
            externalUserId=req.external_user_id, asset=req.asset, amount=req.amount, error=str(exc),
        )
        raise HTTPException(status_code=502, detail=f"Blockchain transfer failed: {exc}")

    # Record-keeping only, after the fact — see adjust_balance's docstring.
    # A negative resulting balance here is expected for winnings payouts and
    # is a useful reconciliation signal, not an error condition.
    await adjust_balance(db, partner_id, req.external_user_id, req.asset, -req.amount)

    now = datetime.utcnow()
    await db["celo_ledger_entries"].insert_one({
        "_id": f"TXN_{uuid.uuid4().hex[:10].upper()}",
        "partnerId": partner_id,
        "externalUserId": req.external_user_id,
        "direction": "withdrawal",
        "asset": req.asset,
        "amount": req.amount,
        "destinationAddress": target_address,
        "status": "completed",
        "txHash": tx_hex,
        "createdAt": now,
    })

    await log_celo_audit_event(
        db, "withdrawal_broadcast", partnerId=partner_id, externalUserId=req.external_user_id,
        asset=req.asset, amount=req.amount, destination=target_address, txHash=tx_hex,
    )

    await notify_partner(
        partner, "withdrawal.completed",
        externalUserId=req.external_user_id, asset=req.asset, amount=req.amount,
        destination=target_address, txHash=tx_hex,
    )

    return {"status": "success", "message": f"{req.amount} {req.asset} sent!", "tx_hash": tx_hex}
