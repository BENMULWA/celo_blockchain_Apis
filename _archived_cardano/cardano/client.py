"""Minimal Blockfrost HTTP client wrapper used by the local Cardano code.

This avoids requiring `blockfrost-python` for read-only calls. It returns
lightweight objects with attributes accessed by the existing `cardano_wallet` code.
"""
from types import SimpleNamespace
import os
import requests

BASE_URL = os.getenv("BLOCKFROST_URL", "https://cardano-mainnet.blockfrost.io/api/v0")
PROJECT_ID = os.getenv("BLOCKFROST_PROJECT_ID")

if not PROJECT_ID:
    raise RuntimeError("BLOCKFROST_PROJECT_ID is not set in environment")

HEADERS = {"project_id": PROJECT_ID}


def _wrap(obj):
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(x) for x in obj]
    return obj


class BlockfrostHTTPClient:
    def __init__(self, base_url: str = BASE_URL, project_id: str = PROJECT_ID):
        self.base = base_url.rstrip('/')
        self.headers = {"project_id": project_id}

    def address(self, address: str):
        url = f"{self.base}/addresses/{address}"
        r = requests.get(url, headers=self.headers, timeout=10)
        r.raise_for_status()
        return _wrap(r.json())

    def address_transactions(self, address: str, count: int = 20, order: str = "desc"):
        url = f"{self.base}/addresses/{address}/transactions"
        params = {"count": count, "order": order}
        r = requests.get(url, headers=self.headers, params=params, timeout=10)
        r.raise_for_status()
        return _wrap(r.json())

    def transaction_utxos(self, tx_hash: str):
        url = f"{self.base}/txs/{tx_hash}/utxos"
        r = requests.get(url, headers=self.headers, timeout=10)
        r.raise_for_status()
        return _wrap(r.json())


def get_blockfrost_api():
    return BlockfrostHTTPClient()


def get_chain_context():
    """Return a pycardano BlockFrostChainContext if available.

    This is required for building/signing/submitting transactions. If `pycardano`
    is not installed this will raise an informative error.
    """
    try:
        from pycardano import BlockFrostChainContext, Network
    except Exception:
        raise RuntimeError("pycardano is required for chain context. Install pycardano to enable tx building/submission.")

    project_id = os.getenv("BLOCKFROST_PROJECT_ID")
    if not project_id:
        raise RuntimeError("BLOCKFROST_PROJECT_ID must be set to create chain context")

    # pycardano BlockFrostChainContext expects project_id and network
    network = os.getenv("CARDANO_NETWORK", "mainnet")
    base_url = os.getenv("BLOCKFROST_URL", "https://cardano-mainnet.blockfrost.io/api/v0")
    return BlockFrostChainContext(project_id=project_id, base_url=base_url, network=Network.MAINNET if network == "mainnet" else Network.TESTNET)
