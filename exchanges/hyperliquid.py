"""
Hyperliquid Perps Funding Rate Fetcher
Endpoint: POST https://api.hyperliquid.xyz/info
Body:      {"type": "metaAndAssetCtxs"}

Response structure:
  [0] = meta: { universe: [{name, szDecimals, maxLeverage, ...}] }
  [1] = assetCtxs: [{funding, openInterest, prevDayPx, dayNtlVlm, premium,
                     oraclePx, markPx, midPx, impactPxs}]
  Both arrays are index-aligned.

Funding interval: 1 hour (every hour on the hour UTC)
The 'funding' field is the rate per hour. Annualized = funding * 8760 * 100
"""
import requests
import time
from typing import List
from .base import FundingData


BASE_URL = "https://api.hyperliquid.xyz"
EXCHANGE_NAME = "Hyperliquid"
INTERVAL_HOURS = 1.0  # Hyperliquid always funds hourly


def _next_hour_ms() -> int:
    """Return the Unix timestamp in ms of the next top-of-hour."""
    now = time.time()
    next_hour = (int(now) // 3600 + 1) * 3600
    return int(next_hour * 1000)


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all perpetual funding rates from Hyperliquid.
    No API key required — this is a public info endpoint.
    """
    url = f"{BASE_URL}/info"
    payload = {"type": "metaAndAssetCtxs"}

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    if not isinstance(data, list) or len(data) < 2:
        print(f"[{EXCHANGE_NAME}] Unexpected response format")
        return []

    meta = data[0].get("universe", [])
    ctxs = data[1]

    if len(meta) != len(ctxs):
        print(f"[{EXCHANGE_NAME}] Meta/ctx length mismatch: {len(meta)} vs {len(ctxs)}")
        return []

    next_funding_ts = _next_hour_ms()
    results = []

    for asset_meta, ctx in zip(meta, ctxs):
        name = asset_meta.get("name", "")
        if not name:
            continue

        try:
            # funding is hourly rate as a decimal string e.g. "0.0000125"
            funding_rate = float(ctx.get("funding") or 0)
            mark_price = float(ctx.get("markPx") or ctx.get("midPx") or 0)

            annualized = funding_rate * 8760 * 100  # hourly rate * hours in year

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=name.upper(),
                    raw_symbol=name,
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