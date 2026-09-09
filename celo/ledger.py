"""Partner-scoped balance ledger.

One `partner_balances` row per (partner_id, external_user_id), with one field
per asset. Every op is keyed by that composite id, so balances never leak or
collide across partner platforms sharing this gateway.
"""
from datetime import datetime

from fastapi import HTTPException

from celo.wallet import wallet_lookup_id


async def get_balance(db, partner_id: str, external_user_id: str, asset: str) -> float:
    doc = await db["partner_balances"].find_one({"_id": wallet_lookup_id(partner_id, external_user_id)})
    return float((doc or {}).get(asset, 0.0) or 0.0)


async def get_all_balances(db, partner_id: str, external_user_id: str) -> dict:
    doc = await db["partner_balances"].find_one({"_id": wallet_lookup_id(partner_id, external_user_id)}) or {}
    return {k: v for k, v in doc.items() if k not in ("_id", "partnerId", "externalUserId", "updatedAt")}


async def credit_balance(db, partner_id: str, external_user_id: str, asset: str, amount: float) -> None:
    lookup_id = wallet_lookup_id(partner_id, external_user_id)
    await db["partner_balances"].update_one(
        {"_id": lookup_id},
        {
            "$inc": {asset: amount},
            "$set": {"partnerId": partner_id, "externalUserId": external_user_id, "updatedAt": datetime.utcnow()},
        },
        upsert=True,
    )


async def adjust_balance(db, partner_id: str, external_user_id: str, asset: str, delta: float) -> None:
    """Unconditional $inc, positive or negative, with no sufficiency check.

    Used for record-keeping on partner-authorized withdrawals (see
    routes/celo.py) where the gateway trusts the calling partner's own
    ledger (e.g. Shillingi Bet's KES balance) to have decided the amount is
    owed — a payout of betting winnings has no specific prior deposit
    backing it in *this* gateway's per-user ledger, so gating on it here
    would incorrectly reject legitimate withdrawals. A resulting negative
    balance is a meaningful reconciliation signal, not a bug: it shows this
    user has withdrawn more crypto than they personally deposited here.
    """
    await credit_balance(db, partner_id, external_user_id, asset, delta)


async def debit_balance(db, partner_id: str, external_user_id: str, asset: str, amount: float) -> None:
    """Atomically debit `amount` of `asset`. Raises HTTPException(400) if the
    balance is insufficient — checked and applied in the same DB operation via
    a $gte guard, so two concurrent debits can never both succeed past zero."""
    lookup_id = wallet_lookup_id(partner_id, external_user_id)
    result = await db["partner_balances"].update_one(
        {"_id": lookup_id, asset: {"$gte": amount}},
        {"$inc": {asset: -amount}, "$set": {"updatedAt": datetime.utcnow()}},
    )
    if result.modified_count != 1:
        current = await get_balance(db, partner_id, external_user_id, asset)
        raise HTTPException(status_code=400, detail=f"Insufficient {asset} balance. You have {round(current, 6)}.")
