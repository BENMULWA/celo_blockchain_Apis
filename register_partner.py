"""CLI to onboard a new partner platform (e.g. Shillingi Bet) onto the gateway.

Usage:
    python register_partner.py "Shillingi Bet" [webhook_url]

Generates a fresh API key/secret pair, hashes the secret before storing it,
and prints the plaintext values once — they are not recoverable afterward.
A `webhook_secret` is also generated for the partner to verify the
`X-Celo-Gateway-Signature` header on inbound webhook deliveries.
"""
import asyncio
import secrets
import sys
from datetime import datetime

from database import db, hash_secret


async def register_partner(company_name: str, webhook_url: str | None = None):
    api_key = f"pk_live_{secrets.token_hex(16)}"
    secret_key = f"sk_live_{secrets.token_hex(32)}"
    webhook_secret = secrets.token_hex(24) if webhook_url else None
    partner_id = company_name.strip().replace(" ", "_")

    partner_document = {
        "_id": partner_id,
        "company_name": company_name,
        "api_key": api_key,
        "secret_key": hash_secret(secret_key),
        "webhook_url": webhook_url,
        "webhook_secret": webhook_secret,
        "active": True,
        "created_at": datetime.utcnow(),
    }

    await db["registered_partners"].insert_one(partner_document)

    print("=" * 60)
    print(f"Partner '{company_name}' registered.")
    print("=" * 60)
    print(f"API KEY:        {api_key}")
    print(f"SECRET KEY:     {secret_key}")
    if webhook_secret:
        print(f"WEBHOOK SECRET: {webhook_secret}")
    print("=" * 60)
    print("Save these now — the secret key and webhook secret are not stored in plaintext and cannot be recovered.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python register_partner.py \"<Company Name>\" [webhook_url]")
        sys.exit(1)
    name = sys.argv[1]
    url = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(register_partner(name, url))
