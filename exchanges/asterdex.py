"""
AsterDex Perpetuals Funding Rate Fetcher
AsterDex uses a Binance-compatible API structure.

Endpoints to try (in order of preference):
  1. GET https://api.asterdex.com/fapi/v1/premiumIndex   (Binance-compat)
  2. GET https://api.asterdex.com/fapi/v1/ticker/price   (fallback for prices)

Funding interval: 8 hours (matches Binance-style default)

NOTE: AsterDex is a newer exchange — if the primary endpoint fails,
      a fallback URL is attempted. Update BASE_URLS if the domain changes.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


# Try primary, then fallback
BASE_URLS = [
    "https://api.asterdex.com",
    "https://www.asterdex.com",
]
EXCHANGE_NAME = "AsterDex"
INTERVAL_HOURS = 8.0


def _normalize_symbol(raw: str) -> Optional[str]:
    for suffix in ["USDT", "USDC", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def _try_fetch(url: str) -> Optional[dict]:
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all perpetual funding rates from AsterDex.
    Tries multiple base URLs for resilience.
    """
    data = None
    for base in BASE_URLS:
        url = f"{base}/fapi/v1/premiumIndex"
        data = _try_fetch(url)
        if data is not None:
            break

    if data is None:
        print(f"[{EXCHANGE_NAME}] All endpoints failed — exchange may be down or API changed")
        return []

    # Response may be a list (all symbols) or a single dict
    if isinstance(data, dict):
        data = [data]

    results = []
    now_ms = int(time.time() * 1000)

    for t in data:
        raw_symbol = t.get("symbol", "")
        if not raw_symbol.endswith("USDT"):
            continue

        base_asset = _normalize_symbol(raw_symbol)
        if not base_asset:
            continue

        try:
            funding_rate = float(t.get("lastFundingRate") or t.get("fundingRate") or 0)
            mark_price = float(t.get("markPrice") or t.get("indexPrice") or 0)

            # nextFundingTime may or may not be present
            next_funding_ts = int(t.get("nextFundingTime") or 0)
            if next_funding_ts == 0:
                # Estimate: next 8h boundary from now
                next_boundary = (int(now_ms / 1000) // (8 * 3600) + 1) * (8 * 3600)
                next_funding_ts = next_boundary * 1000

            annualized = funding_rate * (8760 / INTERVAL_HOURS) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_asset,
                    raw_symbol=raw_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_funding_ts,
                    interval_hours=INTERVAL_HOURS,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results