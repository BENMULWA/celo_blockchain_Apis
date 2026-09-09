"""Encrypted, at-rest backup for chain mnemonics/private keys — Celo, Cardano,
Stellar, whatever else. This is a disaster-recovery backup mechanism, NOT how
the running app loads its secrets — the app still reads plaintext CELO_MNEMONIC
etc. from .env at runtime like it always has. This tool exists so you have a
recoverable copy that isn't just "the one plaintext .env file on one disk."

Usage:
    python backup_secrets.py encrypt      # prompts for label, secret, master password
    python backup_secrets.py decrypt      # prompts for label, master password; reads .env

Encryption: PBKDF2-HMAC-SHA256 (480,000 iterations) -> Fernet (AES-128-CBC + HMAC).
Same construction already used for the archived Cardano master key
(see _archived_cardano/security/encryption.py) — kept consistent so anyone
familiar with that pattern recognizes this one.

CRITICAL: the master password must NEVER be stored in this .env file, this
repo, or any chat/ticket. Write it down physically, separately from where the
encrypted blob lives. If both end up in the same place, the encryption is
theater — anyone with that one place has everything. (The old Cardano backup
in this repo's history made exactly this mistake — MAMLAKA_MASTER_PASSWORD
was stored right next to ENCRYPTED_MASTER_KEY in the same .env. Don't repeat
that here.)
"""
import base64
import getpass
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ENV_PATH = Path(__file__).resolve().parent / ".env"
KDF_ITERATIONS = 480_000


def _derive_key(master_password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(master_password.encode()))


def encrypt_secret(plaintext: str, master_password: str) -> tuple[str, str]:
    """Returns (encrypted_b64, salt_b64)."""
    salt = os.urandom(16)
    key = _derive_key(master_password, salt)
    encrypted = Fernet(key).encrypt(plaintext.encode())
    return encrypted.decode("utf-8"), base64.b64encode(salt).decode("utf-8")


def decrypt_secret(encrypted_b64: str, salt_b64: str, master_password: str) -> str:
    salt = base64.b64decode(salt_b64)
    key = _derive_key(master_password, salt)
    return Fernet(key).decrypt(encrypted_b64.encode()).decode("utf-8")


def _read_env_var(key: str) -> str | None:
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def _prompt_env_key(label: str) -> str:
    key = label.strip().upper().replace(" ", "_")
    return f"{key}_MNEMONIC" if not key.endswith("_MNEMONIC") and not key.endswith("_KEY") else key


def cmd_encrypt():
    print("=" * 60)
    print("ENCRYPT A CHAIN SECRET FOR BACKUP")
    print("=" * 60)
    label = input("Label for this secret (e.g. CELO, CARDANO, STELLAR): ").strip()
    env_key = _prompt_env_key(label)

    secret = getpass.getpass("Paste the mnemonic/private key (input hidden, won't echo): ").strip()
    if not secret:
        print("Nothing entered — aborting.")
        return

    password1 = getpass.getpass("Create a master password to lock this backup: ")
    password2 = getpass.getpass("Confirm master password: ")
    if password1 != password2:
        print("Passwords didn't match — aborting.")
        return
    if len(password1) < 12:
        print("Master password should be at least 12 characters for a real backup — aborting.")
        return

    encrypted_b64, salt_b64 = encrypt_secret(secret, password1)

    print("\n" + "=" * 60)
    print("ENCRYPTION SUCCESSFUL")
    print("=" * 60)
    print(f"Add these two lines to {ENV_PATH.name} (safe — they're useless without the master password):\n")
    print(f"{env_key}_ENCRYPTED={encrypted_b64}")
    print(f"{env_key}_SALT={salt_b64}")
    print("\n" + "-" * 60)
    print("DO NOT store the master password in this .env file, this repo, or any")
    print("chat/ticket. Write it down physically, in a different secure location")
    print("from wherever the .env / this encrypted blob lives.")
    print("-" * 60)


def cmd_decrypt():
    print("=" * 60)
    print("DECRYPT A BACKED-UP CHAIN SECRET")
    print("=" * 60)
    label = input("Label used when encrypting (e.g. CELO, CARDANO, STELLAR): ").strip()
    env_key = _prompt_env_key(label)

    encrypted_b64 = _read_env_var(f"{env_key}_ENCRYPTED")
    salt_b64 = _read_env_var(f"{env_key}_SALT")
    if not encrypted_b64 or not salt_b64:
        print(f"Could not find {env_key}_ENCRYPTED / {env_key}_SALT in {ENV_PATH}.")
        print("You can also paste them manually if they live in a different file:")
        encrypted_b64 = encrypted_b64 or input(f"{env_key}_ENCRYPTED: ").strip()
        salt_b64 = salt_b64 or input(f"{env_key}_SALT: ").strip()

    password = getpass.getpass("Master password: ")
    try:
        plaintext = decrypt_secret(encrypted_b64, salt_b64, password)
    except InvalidToken:
        print("Decryption failed — wrong master password, or the data was tampered with.")
        return

    print("\n" + "=" * 60)
    print("DECRYPTED SECRET (shown once, not logged anywhere):")
    print("=" * 60)
    print(plaintext)
    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("encrypt", "decrypt"):
        print("Usage: python backup_secrets.py encrypt|decrypt")
        sys.exit(1)
    (cmd_encrypt if sys.argv[1] == "encrypt" else cmd_decrypt)()
