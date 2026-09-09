"""USD-stablecoin <-> KES conversion.

Every asset this gateway supports (cUSD, USDC, USDT) is treated as ~1 USD —
so pricing any of them against KES only requires a USD/KES rate, pulled from
a live FX API and cached in-process. A stale-but-present cache is preferred
over a hard failure: if the FX API is briefly down we'd rather quote the
last known rate (and mark the response as stale) than block every deposit
credit/withdrawal quote in the system.
"""
import logging
import time

import httpx

from config import settings

logger = logging.getLogger("celo.rates")

_cache: dict = {"rate": None, "fetched_at": 0.0}


async def _fetch_usd_kes_rate() -> float:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(settings.fx_api_url)
        resp.raise_for_status()
        data = resp.json()
        rate = data["rates"]["KES"]
        return float(rate)


async def get_usd_kes_rate() -> tuple[float, bool]:
    """Returns (rate, is_stale). Refreshes from the FX API when the cache is
    older than fx_cache_ttl_seconds; falls back to the last cached value (or
    the configured static fallback) if the API call fails."""
    now = time.monotonic()
    cache_age = now - _cache["fetched_at"]

    if _cache["rate"] is not None and cache_age < settings.fx_cache_ttl_seconds:
        return _cache["rate"], False

    try:
        rate = await _fetch_usd_kes_rate()
        _cache["rate"] = rate
        _cache["fetched_at"] = now
        return rate, False
    except Exception:
        logger.warning("USD/KES FX fetch failed; falling back to cached/static rate", exc_info=True)
        if _cache["rate"] is not None:
            return _cache["rate"], True
        return settings.fx_fallback_usd_kes_rate, True


def _apply_margin(rate: float) -> float:
    """Shaves the configured margin off the raw market rate before quoting a
    user — protects the business against FX movement between quote and
    settlement. 0% by default (pass-through market rate)."""
    return rate * (1 - settings.kes_conversion_margin_pct / 100)


async def usd_to_kes(amount: float) -> tuple[float, float, bool]:
    """Returns (kes_amount, effective_rate, is_stale) for `amount` USD-equivalent."""
    raw_rate, is_stale = await get_usd_kes_rate()
    effective_rate = _apply_margin(raw_rate)
    return amount * effective_rate, effective_rate, is_stale


async def kes_to_usd(kes_amount: float) -> tuple[float, float, bool]:
    """Returns (usd_amount, effective_rate, is_stale) for `kes_amount` KES.

    Uses the same margin-adjusted rate as usd_to_kes so a partner sizing a
    USDT withdrawal against a KES payout amount gets the rate consistent
    with what a deposit would have been credited at.
    """
    raw_rate, is_stale = await get_usd_kes_rate()
    effective_rate = _apply_margin(raw_rate)
    return kes_amount / effective_rate, effective_rate, is_stale
