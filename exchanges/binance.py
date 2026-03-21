"""
Binance USDT-M Futures Funding Rate Fetcher
Endpoints:
  GET https://fapi.binance.com/fapi/v1/premiumIndex   — rates, mark prices, next funding ts
  GET https://fapi.binance.com/fapi/v1/fundingInfo    — per-pair fundingIntervalHours (1h/4h/8h)
  GET https://fapi.binance.com/fapi/v1/exchangeInfo   — active TRADING symbols filter
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


def _fetch_funding_intervals() -> dict:
    """Return {symbol: interval_hours} from /fapi/v1/fundingInfo."""
    try:
        resp = requests.get(f"{BASE_URL}/fapi/v1/fundingInfo", timeout=10)
        resp.raise_for_status()
        return {
            item["symbol"]: float(item.get("fundingIntervalHours") or 8)
            for item in resp.json()
            if item.get("symbol")
        }
    except Exception:
        return {}


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-M perpetual funding rates from Binance Futures.
    Only includes contracts with status=TRADING (excludes SETTLING/delisted).
    Per-pair interval read from fundingInfo — never hardcoded.
    """
    active    = _fetch_active_symbols()
    intervals = _fetch_funding_intervals()   # {symbol: hours}

    try:
        resp = requests.get(f"{BASE_URL}/fapi/v1/premiumIndex", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    results = []

    for t in data:
        raw_symbol = t.get("symbol", "")

        if not raw_symbol.endswith("USDT"):
            continue

        if active and raw_symbol not in active:
            continue

        base = _normalize_symbol(raw_symbol)
        if not base:
            continue

        try:
            funding_rate    = float(t.get("lastFundingRate") or 0)
            mark_price      = float(t.get("markPrice") or 0)
            next_funding_ts = int(t.get("nextFundingTime") or 0)

            # Per-pair interval from fundingInfo; fall back to 8h if missing
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