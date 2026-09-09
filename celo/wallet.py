"""Per-(partner, external user) Celo deposit sub-accounts.

Derives a unique Celo address per (partner_id, external_user_id) from
CELO_MNEMONIC via standard BIP-44 HD derivation (m/44'/60'/0'/0/{index}).
This lets the deposit watcher attribute an incoming ERC-20 transfer to the
exact partner + end-user it belongs to, instead of guessing from amounts
against one shared treasury address.

The private key for a given index is never stored — it's re-derived on
demand from CELO_MNEMONIC + index whenever it's needed to sign a sweep or
withdrawal transaction. Scoping the lookup key by partner as well as
external_user_id is what lets two different partner platforms (e.g.
"Shillingi Bet" and another platform) both use external_user_id "42"
without colliding — each gets its own address and its own balance.
"""
from datetime import datetime

from eth_account import Account
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config import settings

Account.enable_unaudited_hdwallet_features()

# Account index is this gateway's own reserved branch of the shared mnemonic
# — see config.py's celo_hd_account_index docstring for why this isn't 0.
CELO_DERIVATION_PATH = f"m/44'/60'/{settings.celo_hd_account_index}'/0/{{index}}"


def derive_celo_account(index: int):
    """Return the eth_account LocalAccount for the given HD index."""
    if not settings.celo_mnemonic:
        raise ValueError("CELO_MNEMONIC is not configured.")
    return Account.from_mnemonic(settings.celo_mnemonic, account_path=CELO_DERIVATION_PATH.format(index=index))


def wallet_lookup_id(partner_id: str, external_user_id: str) -> str:
    return f"{partner_id}:{external_user_id}"


async def get_or_create_deposit_index(db, partner_id: str, external_user_id: str) -> int:
    """Return this (partner, user)'s persistent Celo deposit HD index,
    allocating one from a shared global counter if it doesn't exist yet."""
    lookup_id = wallet_lookup_id(partner_id, external_user_id)

    existing = await db["celo_wallet_indexes"].find_one({"_id": lookup_id})
    if existing:
        return existing["index"]

    counter = await db["celo_wallet_counters"].find_one_and_update(
        {"_id": "global"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    new_index = counter["value"]

    try:
        await db["celo_wallet_indexes"].insert_one({
            "_id": lookup_id,
            "partnerId": partner_id,
            "externalUserId": external_user_id,
            "index": new_index,
            "createdAt": datetime.utcnow(),
        })
        return new_index
    except DuplicateKeyError:
        # Another concurrent request allocated this (partner, user)'s index first.
        existing = await db["celo_wallet_indexes"].find_one({"_id": lookup_id})
        return existing["index"]


async def get_deposit_address(db, partner_id: str, external_user_id: str) -> str:
    index = await get_or_create_deposit_index(db, partner_id, external_user_id)
    return derive_celo_account(index).address
