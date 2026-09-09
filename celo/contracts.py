"""Celo Web3 client + ERC-20 contract wiring shared by every route/worker."""
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import ASSET_DECIMALS, SUPPORTED_ASSETS, settings

__all__ = ["w3", "ASSET_CONTRACTS", "ASSET_DECIMALS", "SUPPORTED_ASSETS", "ERC20_ABI", "TRANSFER_TOPIC"]

w3 = Web3(Web3.HTTPProvider(settings.celo_rpc_url, request_kwargs={"timeout": 15}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

ASSET_CONTRACTS = {
    "cUSD": w3.to_checksum_address(settings.celo_cusd_contract),
    "USDC": w3.to_checksum_address(settings.celo_usdc_contract),
    "USDT": w3.to_checksum_address(settings.celo_usdt_contract),
}

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]

_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
TRANSFER_TOPIC = _topic if _topic.startswith("0x") else f"0x{_topic}"


def get_contract(asset: str):
    if asset not in ASSET_CONTRACTS:
        raise ValueError(f"Unsupported Celo asset: {asset}")
    return w3.eth.contract(address=ASSET_CONTRACTS[asset], abi=ERC20_ABI)


def to_base_units(amount: float, asset: str) -> int:
    return int(round(amount * (10 ** ASSET_DECIMALS[asset])))


def from_base_units(raw: int, asset: str) -> float:
    return raw / (10 ** ASSET_DECIMALS[asset])


def get_treasury_account():
    """Returns the eth_account LocalAccount for the treasury, or None if
    CELO_TREASURY_PK isn't configured (deposit-only / read-only deployments)."""
    pk = settings.celo_treasury_pk
    if not pk:
        return None
    clean_pk = pk if pk.startswith("0x") else f"0x{pk}"
    return w3.eth.account.from_key(clean_pk)


def get_treasury_address() -> str:
    account = get_treasury_account()
    if account:
        return account.address
    if settings.celo_treasury_address:
        return w3.to_checksum_address(settings.celo_treasury_address)
    raise ValueError("Neither CELO_TREASURY_PK nor CELO_TREASURY_ADDRESS is configured.")
