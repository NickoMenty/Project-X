"""
Lighter (zklighter) Perpetuals Funding Rate Fetcher
Lighter is a ZK-rollup-based perpetuals DEX.

Endpoint: GET https://mainnet.zklighter.elliot.ai/api/v1/orderbook/all_market_stats

Response fields per market (representative — verify against live response):
  - market_id
  - base_symbol (e.g. "BTC")
  - mark_price
  - index_price
  - funding_rate_8h  OR  funding_rate  (per-epoch rate)
  - next_funding_timestamp
  - funding_interval_seconds

NOTE: Lighter's API is less documented than CEX APIs.
      Field names are reverse-engineered from public responses and may change.
      The connector logs raw field names on first run to help debug.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


BASE_URLS = [
    "https://mainnet.zklighter.elliot.ai/api/v1/orderbook/all_market_stats",
    "https://app.lighter.xyz/api/v1/orderbook/all_market_stats",
]
EXCHANGE_NAME = "Lighter"


def _parse_interval(seconds: Optional[int]) -> float:
    """Convert funding interval in seconds to hours."""
    if not seconds:
        return 8.0  # assume default
    return seconds / 3600


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all perpetual funding rates from Lighter DEX.
    """
    data = None
    for url in BASE_URLS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException:
            continue

    if data is None:
        print(f"[{EXCHANGE_NAME}] All endpoints failed — check if API URL has changed")
        return []

    # Log available keys on first successful fetch to help with debugging
    if isinstance(data, list) and len(data) > 0:
        sample = data[0]
        print(f"[{EXCHANGE_NAME}] Sample fields: {list(sample.keys())}")
    elif isinstance(data, dict):
        # May be wrapped in a key like "markets" or "data"
        for wrapper_key in ["markets", "data", "result", "items"]:
            if wrapper_key in data:
                data = data[wrapper_key]
                break
        if isinstance(data, list) and len(data) > 0:
            print(f"[{EXCHANGE_NAME}] Sample fields: {list(data[0].keys())}")

    if not isinstance(data, list):
        print(f"[{EXCHANGE_NAME}] Unexpected response type: {type(data)}")
        return []

    results = []

    for market in data:
        try:
            # Try multiple possible field name conventions
            base_symbol = (
                market.get("base_symbol")
                or market.get("baseSymbol")
                or market.get("base_asset")
                or market.get("symbol", "").split("-")[0].split("USDT")[0]
            )
            if not base_symbol:
                continue

            base_symbol = base_symbol.upper()

            mark_price = float(
                market.get("mark_price")
                or market.get("markPrice")
                or market.get("index_price")
                or 0
            )

            # Funding rate — try various field names
            raw_rate = (
                market.get("funding_rate_8h")
                or market.get("funding_rate")
                or market.get("fundingRate")
                or market.get("current_funding_rate")
                or 0
            )
            funding_rate = float(raw_rate)

            # Interval
            interval_secs = (
                market.get("funding_interval_seconds")
                or market.get("funding_interval")
                or None
            )
            interval_hours = _parse_interval(interval_secs)

            next_ts_raw = (
                market.get("next_funding_timestamp")
                or market.get("nextFundingTime")
                or market.get("next_funding_time")
            )
            if next_ts_raw:
                # Could be seconds or milliseconds
                next_ts = int(next_ts_raw)
                if next_ts < 1e12:  # seconds
                    next_ts *= 1000
            else:
                next_boundary = (int(time.time()) // (int(interval_hours) * 3600) + 1) * (int(interval_hours) * 3600)
                next_ts = next_boundary * 1000

            annualized = funding_rate * (8760 / interval_hours) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_symbol,
                    raw_symbol=market.get("symbol") or base_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_ts,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError, KeyError):
            continue

    return results