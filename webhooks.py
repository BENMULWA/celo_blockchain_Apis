"""Outbound event notifications to partner platforms.

This is a B2B gateway with no browser UI of its own, so there's no websocket
to push to (unlike the meshex platform this was extracted from) — instead,
each partner registers a `webhook_url` and we POST them a signed event
whenever something happens to one of their users' balances. Best-effort:
a partner's webhook being down must never block or fail the underlying
deposit/withdrawal/swap operation.
"""
import hashlib
import hmac
import json
import logging

import httpx

from config import settings

logger = logging.getLogger("celo.webhooks")


def _sign(payload_bytes: bytes, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def notify_partner(partner: dict, event: str, **fields) -> None:
    webhook_url = partner.get("webhook_url")
    if not webhook_url:
        return

    payload = {"event": event, "partnerId": str(partner.get("_id")), **fields}
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    webhook_secret = partner.get("webhook_secret")
    if webhook_secret:
        headers["X-Celo-Gateway-Signature"] = _sign(body, webhook_secret)

    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
            await client.post(webhook_url, content=body, headers=headers)
    except Exception:
        logger.warning("Webhook delivery failed for partner=%s event=%s", partner.get("_id"), event, exc_info=True)
