"""
Binance USDT-M Futures Funding Rate Fetcher
Endpoint: GET https://fapi.binance.com/fapi/v1/premiumIndex
Funding interval: 8 hours (00:00, 08:00, 16:00 UTC) standard
                  Some pairs use 4h — nextFundingTime field used.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://fapi.binance.com"
EXCHANGE_NAME = "Binance"


def _normalize_symbol(raw: str) -> Optional[str]:
    for suffix in ["USDT", "USDC", "BUSD", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def _fetch_active_symbols() -> set:
    """Return the set of USDT-M perpetual symbols currently in TRADING status."""
    try:
        resp = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        info = resp.json()
        return {
            s["symbol"]
            for s in info.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
        }
    except Exception:
        return set()  # if this fails, don't filter (fail open)


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-M perpetual funding rates from Binance Futures.
    Only includes contracts with status=TRADING (excludes SETTLING/delisted).
    No API key required for these endpoints.
    """
    active = _fetch_active_symbols()

    url = f"{BASE_URL}/fapi/v1/premiumIndex"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    results = []
    now_ms = int(time.time() * 1000)

    for t in data:
        raw_symbol = t.get("symbol", "")

        # Only USDT-margined perpetuals
        if not raw_symbol.endswith("USDT"):
            continue

        # Skip contracts not actively trading (e.g. SETTLING, delisted)
        if active and raw_symbol not in active:
            continue

        base = _normalize_symbol(raw_symbol)
        if not base:
            continue

        try:
            funding_rate = float(t.get("lastFundingRate") or 0)
            mark_price = float(t.get("markPrice") or 0)
            next_funding_ts = int(t.get("nextFundingTime") or 0)  # ms

            ms_to_next = next_funding_ts - now_ms if next_funding_ts > now_ms else 0

            # Binance standard is 8h; some pairs are 4h
            if ms_to_next <= 4 * 3600 * 1000:
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