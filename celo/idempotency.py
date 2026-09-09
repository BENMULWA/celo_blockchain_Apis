"""Idempotency-key support for money-moving endpoints.

A caller (e.g. a partner backend retrying after a network timeout) can pass
an `idempotency_key` so a retried request with the exact same parameters
returns the original result instead of executing a second real transfer.
Without this, a timed-out /withdraw call has no safe way to retry — calling
again risks a genuine double-send, and not retrying risks leaving a valid
request unfulfilled.

Semantics, keyed by (partner_id, idempotency_key):
  - First request: claims the key atomically, caller proceeds normally.
  - Retry with the SAME parameters while the first is still running: rejected
    with 409 (ask the caller to poll/retry later) rather than racing two
    broadcasts.
  - Retry with the SAME parameters after the first completed: the original
    response is replayed verbatim — no new transaction is broadcast.
  - Retry with the SAME key but DIFFERENT parameters: rejected with 409 —
    a key means "this exact request", not "this caller's turn".
  - Retry after the first attempt failed (e.g. blockchain broadcast error):
    allowed to proceed fresh, since nothing was actually sent.
"""
import hashlib
import json
from datetime import datetime

from fastapi import HTTPException


def compute_request_hash(**fields) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key_id(partner_id: str, idempotency_key: str) -> str:
    return f"{partner_id}:{idempotency_key}"


async def claim_idempotency_key(db, partner_id: str, idempotency_key: str, request_hash: str):
    """Returns the stored response dict to replay if this exact request
    already completed successfully, or None if the caller should proceed
    and execute the real operation. Raises HTTPException(409) on a
    same-key-different-params conflict or a concurrent in-flight duplicate.
    """
    key_id = _key_id(partner_id, idempotency_key)
    now = datetime.utcnow()

    try:
        await db["celo_idempotency_keys"].insert_one({
            "_id": key_id,
            "partnerId": partner_id,
            "idempotencyKey": idempotency_key,
            "requestHash": request_hash,
            "status": "in_progress",
            "response": None,
            "createdAt": now,
            "updatedAt": now,
        })
        return None  # freshly claimed — caller proceeds
    except Exception:
        pass  # fall through to inspect the existing record (DuplicateKeyError)

    existing = await db["celo_idempotency_keys"].find_one({"_id": key_id})
    if not existing:
        # Lost a race reading right after another request's insert/delete —
        # extremely narrow window; safe to treat as "try again".
        raise HTTPException(status_code=409, detail="Idempotency key is being processed. Retry shortly.")

    if existing["requestHash"] != request_hash:
        raise HTTPException(
            status_code=409,
            detail="This idempotency key was already used with different request parameters.",
        )

    if existing["status"] == "completed":
        return existing["response"]

    if existing["status"] == "in_progress":
        raise HTTPException(status_code=409, detail="A request with this idempotency key is already in progress. Retry shortly.")

    # status == "failed": the prior attempt never actually broadcast anything
    # successfully, so it's safe to let this retry proceed as a fresh attempt.
    # Compare-and-swap back to in_progress; if we lose the race to another
    # concurrent retry, fall back to the same "in progress" response they'll get.
    result = await db["celo_idempotency_keys"].update_one(
        {"_id": key_id, "status": "failed"},
        {"$set": {"status": "in_progress", "updatedAt": now}},
    )
    if result.modified_count == 1:
        return None  # re-claimed — caller proceeds
    raise HTTPException(status_code=409, detail="A request with this idempotency key is already in progress. Retry shortly.")


async def complete_idempotency_key(db, partner_id: str, idempotency_key: str, response: dict) -> None:
    await db["celo_idempotency_keys"].update_one(
        {"_id": _key_id(partner_id, idempotency_key)},
        {"$set": {"status": "completed", "response": response, "updatedAt": datetime.utcnow()}},
    )


async def fail_idempotency_key(db, partner_id: str, idempotency_key: str, error: str) -> None:
    await db["celo_idempotency_keys"].update_one(
        {"_id": _key_id(partner_id, idempotency_key)},
        {"$set": {"status": "failed", "error": error, "updatedAt": datetime.utcnow()}},
    )
