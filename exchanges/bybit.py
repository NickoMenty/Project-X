"""
Bybit Futures Funding Rate Fetcher
Endpoint: GET https://api.bybit.com/v5/market/tickers?category=linear
Funding interval: 8 hours (00:00, 08:00, 16:00 UTC) for most pairs
                  Some newer pairs fund every 4h or 1h — nextFundingTime field used.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://api.bybit.com"
EXCHANGE_NAME = "Bybit"


def _normalize_symbol(raw: str) -> Optional[str]:
    """Convert BTCUSDT -> BTC, ETHPERP -> ETH, etc."""
    for suffix in ["USDT", "USDC", "PERP", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all linear (USDT-margined) perpetual funding rates from Bybit.
    Returns a list of normalized FundingData objects.
    """
    url = f"{BASE_URL}/v5/market/tickers"
    params = {"category": "linear"}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    if data.get("retCode") != 0:
        print(f"[{EXCHANGE_NAME}] API error: {data.get('retMsg')}")
        return []

    results = []
    tickers = data.get("result", {}).get("list", [])

    for t in tickers:
        raw_symbol = t.get("symbol", "")

        # Only process USDT perpetuals
        if not raw_symbol.endswith("USDT"):
            continue

        base = _normalize_symbol(raw_symbol)
        if not base:
            continue

        try:
            funding_rate = float(t.get("fundingRate") or 0)
            mark_price = float(t.get("markPrice") or 0)
            next_funding_ts = int(t.get("nextFundingTime") or 0)  # ms

            # Derive interval from next funding time vs now
            # Bybit default is 8h; some pairs are 4h or 1h
            now_ms = int(time.time() * 1000)
            ms_to_next = next_funding_ts - now_ms if next_funding_ts > now_ms else 0

            # Bybit funding intervals are always a divisor of 8h
            # We infer by checking which standard interval the next timestamp aligns with
            if ms_to_next <= 1 * 3600 * 1000:
                interval_hours = 1.0
            elif ms_to_next <= 4 * 3600 * 1000:
                interval_hours = 4.0
            else:
                interval_hours = 8.0

            annualized = funding_rate * (8760 / interval_hours) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base,
                    raw_symbol=raw_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_funding_ts,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results