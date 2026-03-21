"""
Bybit Futures Funding Rate Fetcher
Endpoints:
  GET https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000
    Returns fundingInterval (minutes) per instrument — fetched first.
  GET https://api.bybit.com/v5/market/tickers?category=linear
    Returns fundingRate, markPrice, nextFundingTime per instrument.
"""
import requests
import time
from typing import Dict, List, Optional
from .base import FundingData


BASE_URL = "https://api.bybit.com"
EXCHANGE_NAME = "Bybit"


def _normalize_symbol(raw: str) -> Optional[str]:
    """Convert BTCUSDT -> BTC, ETHPERP -> ETH, etc."""
    for suffix in ["USDT", "USDC", "PERP", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def _fetch_funding_intervals() -> Dict[str, float]:
    """Return {symbol: interval_hours} from instruments-info (fundingInterval in minutes)."""
    try:
        resp = requests.get(
            f"{BASE_URL}/v5/market/instruments-info",
            params={"category": "linear", "limit": 1000},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("result", {}).get("list", [])
        return {
            item["symbol"]: float(item["fundingInterval"]) / 60.0
            for item in items
            if item.get("symbol") and item.get("fundingInterval")
        }
    except Exception:
        return {}


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all linear (USDT-margined) perpetual funding rates from Bybit.
    Per-pair interval read from instruments-info — never inferred from timing.
    """
    intervals = _fetch_funding_intervals()   # {symbol: hours}

    try:
        resp = requests.get(
            f"{BASE_URL}/v5/market/tickers",
            params={"category": "linear"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    if data.get("retCode") != 0:
        print(f"[{EXCHANGE_NAME}] API error: {data.get('retMsg')}")
        return []

    results = []
    for t in data.get("result", {}).get("list", []):
        raw_symbol = t.get("symbol", "")

        if not raw_symbol.endswith("USDT"):
            continue

        base = _normalize_symbol(raw_symbol)
        if not base:
            continue

        try:
            funding_rate    = float(t.get("fundingRate") or 0)
            mark_price      = float(t.get("markPrice") or 0)
            next_funding_ts = int(t.get("nextFundingTime") or 0)

            # Per-pair interval from instruments-info; fall back to 8h if missing
            interval_hours = intervals.get(raw_symbol, 8.0)

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