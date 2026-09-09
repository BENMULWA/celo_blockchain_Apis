import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Always load this service's own .env regardless of process working directory.
load_dotenv(dotenv_path=ENV_PATH, override=True)


class Settings(BaseSettings):
    # ── Database ─────────────────────────────────────────────────────────
    mongo_url: str = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    mongo_db_name: str = os.getenv("MONGO_DB_NAME", "celo_baas_gateway")

    # ── Partner JWT (BaaS session tokens, not end-user tokens) ─────────────
    jwt_secret: str = os.getenv("JWT_SECRET", "changeme-super-secret-jwt-key-32chars")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiry_seconds: int = int(os.getenv("JWT_EXPIRY_SECONDS", "3600"))

    # ── Celo chain ───────────────────────────────────────────────────────
    celo_rpc_url: str = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
    celo_chain_id: int = int(os.getenv("CELO_CHAIN_ID", "42220"))

    # HD wallet mnemonic used to derive one deposit address per (partner, user).
    # The private key for a given index is never stored — see celo/wallet.py.
    #
    # This mnemonic is SHARED with the meshex and Jasiri platforms (same
    # treasury, deliberately, for now). meshex derives with BIP-44 account
    # index 0 (m/44'/60'/0'/0/{index}) — confirmed 2026-09-09 when a test
    # deposit to this gateway's account-0 index 2 was independently swept by
    # another platform on the same mnemonic before this gateway's own
    # watcher could react. Using a distinct account index here reserves a
    # completely separate branch of the HD tree, so this gateway's deposit
    # addresses can never collide with meshex's or Jasiri's regardless of
    # allocation order — without needing a separate mnemonic yet. Change
    # this to a fresh dedicated mnemonic entirely once partner-specific
    # treasuries are split out.
    celo_mnemonic: Optional[str] = os.getenv("CELO_MNEMONIC", None)
    celo_hd_account_index: int = int(os.getenv("CELO_HD_ACCOUNT_INDEX", "5"))

    # Treasury: signs withdrawals and sweeps, and receives swept deposits.
    celo_treasury_pk: Optional[str] = os.getenv("CELO_TREASURY_PK", None)
    celo_treasury_address: Optional[str] = os.getenv("CELO_TREASURY_ADDRESS", None)

    # Per-asset ERC-20 contract addresses on Celo mainnet. Overridable via env
    # so a partner deployment can point at testnet contracts.
    celo_cusd_contract: str = os.getenv("CELO_CUSD_CONTRACT", "0x765DE816845861e75A25fCA122bb6898B8B1282a")
    celo_usdc_contract: str = os.getenv("CELO_USDC_CONTRACT", "0xcebA9300f2b948710d2653dD7B07f33A8B32118C")
    celo_usdt_contract: str = os.getenv("CELO_USDT_CONTRACT", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")

    # ── Withdrawal guardrails ───────────────────────────────────────────────
    # A stolen/leaked partner API key should never drain an unbounded amount
    # instantly. Per-asset, since these are all ~1:1 USD-pegged stablecoins.
    celo_max_withdrawal_per_tx: float = float(os.getenv("CELO_MAX_WITHDRAWAL_PER_TX", "5000"))
    celo_max_withdrawal_per_day: float = float(os.getenv("CELO_MAX_WITHDRAWAL_PER_DAY", "20000"))

    # ── Deposit watcher tuning ──────────────────────────────────────────────
    celo_watcher_interval_seconds: int = int(os.getenv("CELO_WATCHER_INTERVAL_SECONDS", "6"))
    celo_watcher_max_lookback_blocks: int = int(os.getenv("CELO_WATCHER_MAX_LOOKBACK_BLOCKS", "2000"))
    celo_watcher_scan_chunk_blocks: int = int(os.getenv("CELO_WATCHER_SCAN_CHUNK_BLOCKS", "150"))
    celo_sweep_gas_limit: int = int(os.getenv("CELO_SWEEP_GAS_LIMIT", "200000"))
    celo_gas_topup_buffer_pct: float = float(os.getenv("CELO_GAS_TOPUP_BUFFER_PCT", "0.25"))

    # ── Outbound partner webhooks ────────────────────────────────────────────
    webhook_timeout_seconds: int = int(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "10"))

    # ── USD-stablecoin <-> KES conversion (see celo/rates.py) ───────────────
    # open.er-api.com is free, keyless, and updates roughly hourly — fine for
    # a USDT/KES quote where volatility is fiat-slow, not crypto-fast.
    fx_api_url: str = os.getenv("FX_API_URL", "https://open.er-api.com/v6/latest/USD")
    fx_cache_ttl_seconds: int = int(os.getenv("FX_CACHE_TTL_SECONDS", "300"))
    # Only used if the FX API is unreachable AND nothing has ever been cached.
    fx_fallback_usd_kes_rate: float = float(os.getenv("FX_FALLBACK_USD_KES_RATE", "129.0"))
    # Percentage shaved off the raw market rate before quoting KES to a
    # partner — protects against FX movement between quote and settlement.
    kes_conversion_margin_pct: float = float(os.getenv("KES_CONVERSION_MARGIN_PCT", "0"))

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

# Assets this gateway supports moving on Celo. Order matters only for display.
ASSET_DECIMALS = {"cUSD": 18, "USDC": 6, "USDT": 6}
SUPPORTED_ASSETS = tuple(ASSET_DECIMALS.keys())
