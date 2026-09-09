"""Persistent background watcher for Celo deposit addresses across every
partner platform this gateway serves.

Runs continuously, independent of any client connection, scanning new
blocks for ERC-20 transfers into ANY registered (partner, user) deposit
address and crediting whatever amount actually arrives. On a match it:

  1. Credits the (partner, user)'s ledger balance and records a completed
     `celo_ledger_entries` row.
  2. Fires the partner's webhook with a `deposit.credited` event.
  3. Tops up the deposit address with a little CELO for gas, then sweeps its
     full token balance to the treasury so funds are available for
     withdrawals. The sweep is best-effort: a failure never affects the
     balance credit the end-user already received.

Each on-chain transfer is credited at most once via an atomic insert into
`celo_processed_transfers` keyed by `{tx_hash}:{log_index}` — MongoDB's
`_id` uniqueness is the dedupe guard, so a rescanned block (e.g. after a
restart) can never double-credit anyone.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from pymongo.errors import DuplicateKeyError
from web3 import Web3

from celo.audit import log_celo_audit_event
from celo.contracts import ASSET_CONTRACTS, ERC20_ABI, TRANSFER_TOPIC, from_base_units, get_treasury_account, w3
from celo.ledger import credit_balance
from celo.rates import usd_to_kes
from celo.wallet import derive_celo_account
from config import settings
from database import db as database
from webhooks import notify_partner

logger = logging.getLogger("celo.deposit_watcher")


def _get_logs_chunked(w3_client: Web3, checksum_address: str, from_block: int, to_block: int) -> list:
    """Fetch logs across a wide block range in bounded chunks.

    Forno (and most RPC providers) caps eth_getLogs results — a busy contract
    like USDT can exceed that cap over just a few hundred blocks. Catching up
    after any gap (a restart, a deploy, or the watcher falling behind) can
    span thousands of blocks, so this always chunks defensively.
    """
    all_logs: list = []
    chunk_start = from_block
    chunk_size = settings.celo_watcher_scan_chunk_blocks
    while chunk_start <= to_block:
        chunk_end = min(chunk_start + chunk_size - 1, to_block)
        try:
            all_logs.extend(w3_client.eth.get_logs({
                "fromBlock": chunk_start, "toBlock": chunk_end,
                "address": checksum_address, "topics": [TRANSFER_TOPIC],
            }))
        except Exception as exc:
            narrower_end = min(chunk_start + max(chunk_size // 5, 1) - 1, chunk_end)
            try:
                all_logs.extend(w3_client.eth.get_logs({
                    "fromBlock": chunk_start, "toBlock": narrower_end,
                    "address": checksum_address, "topics": [TRANSFER_TOPIC],
                }))
                chunk_end = narrower_end
            except Exception:
                logger.warning("get_logs failed for %s over %s-%s: %s", checksum_address, chunk_start, chunk_end, exc)
        chunk_start = chunk_end + 1
    return all_logs


def _scan_logs_sync(w3_client: Web3, from_block: int, current_block: int, watched_addresses: set[str]) -> list[dict]:
    """Blocking web3 log scan. Returns matches without touching the database."""
    matches: list[dict] = []
    for asset, contract_address in ASSET_CONTRACTS.items():
        logs = _get_logs_chunked(w3_client, contract_address, from_block, current_block)
        if not logs:
            continue

        contract = w3_client.eth.contract(address=contract_address, abi=ERC20_ABI)
        for log in logs:
            try:
                parsed = contract.events.Transfer().process_log(log)
            except Exception:
                continue

            to_addr = parsed["args"]["to"].lower()
            if to_addr not in watched_addresses:
                continue

            tx_hash = parsed["transactionHash"].hex()
            if not tx_hash.startswith("0x"):
                tx_hash = "0x" + tx_hash

            matches.append({
                "asset": asset, "to": to_addr, "value": parsed["args"]["value"],
                "tx_hash": tx_hash, "log_index": parsed["logIndex"],
            })
    return matches


def _sweep_deposit_sync(w3_client: Web3, asset: str, wallet_index: int) -> dict:
    """Top up gas from the treasury, then sweep the deposit address's full
    token balance back to the treasury. Best-effort: runs after the end-user
    has already been credited, so a failure here is a reconciliation task,
    not a customer-facing incident."""
    result: dict = {"topupTxHash": None, "topupWei": 0, "sweepTxHash": None, "sweptRawAmount": 0}

    treasury = get_treasury_account()
    if not treasury:
        logger.warning("CELO_TREASURY_PK not configured; skipping sweep for index=%s", wallet_index)
        return result

    deposit_account = derive_celo_account(wallet_index)
    contract = w3_client.eth.contract(address=ASSET_CONTRACTS[asset], abi=ERC20_ABI)

    gas_price = w3_client.eth.gas_price
    required_wei = int(gas_price * settings.celo_sweep_gas_limit * (1 + settings.celo_gas_topup_buffer_pct))
    existing_balance_wei = w3_client.eth.get_balance(deposit_account.address)
    topup_wei = max(required_wei - existing_balance_wei, 0)

    if topup_wei > 0:
        treasury_balance_wei = w3_client.eth.get_balance(treasury.address)
        if treasury_balance_wei < topup_wei:
            raise RuntimeError(
                f"Treasury has {Web3.from_wei(treasury_balance_wei, 'ether')} CELO, "
                f"needs {Web3.from_wei(topup_wei, 'ether')} CELO to fund this sweep's gas top-up."
            )
        gas_tx = {
            "from": treasury.address, "to": deposit_account.address, "value": topup_wei,
            "nonce": w3_client.eth.get_transaction_count(treasury.address),
            "gas": 21000, "gasPrice": gas_price, "chainId": w3_client.eth.chain_id,
        }
        signed_gas_tx = treasury.sign_transaction(gas_tx)
        raw_gas_tx = getattr(signed_gas_tx, "raw_transaction", getattr(signed_gas_tx, "rawTransaction", None))
        gas_tx_hash = w3_client.eth.send_raw_transaction(raw_gas_tx)
        w3_client.eth.wait_for_transaction_receipt(gas_tx_hash, timeout=60)
        result["topupTxHash"] = gas_tx_hash.hex()
        result["topupWei"] = topup_wei

    balance = contract.functions.balanceOf(deposit_account.address).call()
    if balance <= 0:
        return result

    sweep_tx = contract.functions.transfer(treasury.address, balance).build_transaction({
        "from": deposit_account.address,
        "nonce": w3_client.eth.get_transaction_count(deposit_account.address),
        "gas": settings.celo_sweep_gas_limit, "gasPrice": gas_price, "chainId": w3_client.eth.chain_id,
    })
    signed_sweep_tx = deposit_account.sign_transaction(sweep_tx)
    raw_sweep_tx = getattr(signed_sweep_tx, "raw_transaction", getattr(signed_sweep_tx, "rawTransaction", None))
    sweep_tx_hash = w3_client.eth.send_raw_transaction(raw_sweep_tx)
    logger.info("Swept %s raw units of %s from index=%s to treasury: %s", balance, asset, wallet_index, sweep_tx_hash.hex())
    result["sweepTxHash"] = sweep_tx_hash.hex()
    result["sweptRawAmount"] = balance
    return result


async def _process_scan(db, w3_client: Web3):
    index_docs = await db["celo_wallet_indexes"].find(
        {}, {"_id": 1, "partnerId": 1, "externalUserId": 1, "index": 1}
    ).to_list(length=50000)
    if not index_docs:
        return

    by_address: dict[str, dict] = {}
    for doc in index_docs:
        try:
            address = derive_celo_account(doc["index"]).address.lower()
        except Exception:
            logger.warning("Could not derive address for wallet index doc %s", doc.get("_id"))
            continue
        by_address[address] = doc

    watermark_doc = await db["chain_watermarks"].find_one({"_id": "celo"})
    current_block = w3_client.eth.block_number
    default_from = current_block - 200  # ~15 min of Celo blocks (5s block time)
    from_block = max((watermark_doc or {}).get("lastBlock", default_from), current_block - settings.celo_watcher_max_lookback_blocks)

    matches = await asyncio.to_thread(_scan_logs_sync, w3_client, from_block, current_block, set(by_address.keys()))

    partner_cache: dict[str, dict] = {}

    for match in matches:
        owner = by_address.get(match["to"])
        if not owner:
            continue

        received_amount = from_base_units(match["value"], match["asset"])
        if received_amount <= 0:
            continue

        dedupe_id = f"{match['tx_hash']}:{match['log_index']}"
        try:
            await db["celo_processed_transfers"].insert_one({"_id": dedupe_id, "processedAt": datetime.utcnow()})
        except DuplicateKeyError:
            continue

        partner_id = owner["partnerId"]
        external_user_id = owner["externalUserId"]
        wallet_index = owner["index"]

        await credit_balance(db, partner_id, external_user_id, match["asset"], received_amount)

        # Every supported asset is a USD-pegged stablecoin, so every deposit
        # can be quoted in KES immediately — this is what lets a partner
        # (e.g. Shillingi Bet) credit a user's betting balance in KES the
        # moment a deposit lands, with no separate /swap call needed. A
        # rate-fetch failure must never block crediting the underlying
        # crypto deposit, so it's best-effort and logged, not raised.
        kes_amount, kes_rate, kes_rate_stale = None, None, None
        try:
            kes_amount, kes_rate, kes_rate_stale = await usd_to_kes(received_amount)
        except Exception:
            logger.warning("KES quote failed for deposit tx %s (deposit still credited)", match["tx_hash"], exc_info=True)

        now = datetime.utcnow()
        await db["celo_ledger_entries"].insert_one({
            "_id": f"TXN_{uuid.uuid4().hex[:10].upper()}",
            "partnerId": partner_id,
            "externalUserId": external_user_id,
            "direction": "deposit",
            "asset": match["asset"],
            "amount": received_amount,
            "kesAmount": kes_amount,
            "kesRate": kes_rate,
            "status": "completed",
            "txHash": match["tx_hash"],
            "depositAddress": match["to"],
            "createdAt": now,
        })

        if partner_id not in partner_cache:
            partner_cache[partner_id] = await db["registered_partners"].find_one({"_id": partner_id})
        partner = partner_cache[partner_id]
        if partner:
            await notify_partner(
                partner, "deposit.credited",
                externalUserId=external_user_id, asset=match["asset"], amount=received_amount, txHash=match["tx_hash"],
                kesAmount=kes_amount, kesRate=kes_rate, kesRateStale=kes_rate_stale,
            )

        try:
            sweep_result = await asyncio.to_thread(_sweep_deposit_sync, w3_client, match["asset"], wallet_index)
            await log_celo_audit_event(
                db, "sweep_completed", partnerId=partner_id, externalUserId=external_user_id,
                asset=match["asset"], depositTxHash=match["tx_hash"], depositAddress=match["to"], walletIndex=wallet_index,
                topupTxHash=sweep_result.get("topupTxHash"), topupWei=sweep_result.get("topupWei"),
                sweepTxHash=sweep_result.get("sweepTxHash"), sweptRawAmount=sweep_result.get("sweptRawAmount"),
            )
        except Exception as exc:
            logger.exception("Sweep failed for tx %s — funds remain safely on the deposit address", match["tx_hash"])
            await log_celo_audit_event(
                db, "sweep_failed", partnerId=partner_id, externalUserId=external_user_id,
                asset=match["asset"], depositTxHash=match["tx_hash"], depositAddress=match["to"],
                walletIndex=wallet_index, error=str(exc),
            )

    await db["chain_watermarks"].update_one(
        {"_id": "celo"}, {"$set": {"lastBlock": current_block, "updatedAt": datetime.utcnow()}}, upsert=True,
    )


async def celo_deposit_watcher_loop(stop_event: asyncio.Event | None = None):
    stop_event = stop_event or asyncio.Event()
    logger.info("Starting Celo deposit watcher (interval=%ss)", settings.celo_watcher_interval_seconds)
    try:
        while not stop_event.is_set():
            try:
                await _process_scan(database, w3)
            except Exception:
                logger.exception("Unexpected error in Celo deposit watcher iteration")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.celo_watcher_interval_seconds)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("Celo deposit watcher stopping")
