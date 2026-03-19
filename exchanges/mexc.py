"""
MEXC Futures Funding Rate Fetcher
Endpoints:
  Funding (batch): GET https://contract.mexc.com/api/v1/contract/funding_rate
                   Returns all pairs with collectCycle and nextSettleTime per pair.
  Prices:          GET https://contract.mexc.com/api/v1/contract/ticker
                   Returns mark price (fairPrice) per pair.

Each MEXC pair has its own collectCycle (1h / 4h / 8h) and nextSettleTime —
we must not assume a single shared schedule.
"""
import requests
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://contract.mexc.com"
EXCHANGE_NAME = "MEXC"


def _normalize_symbol(raw: str) -> Optional[str]:
    """Convert BTC_USDT or BTCUSDT -> BTC."""
    raw = raw.replace("_", "")
    for suffix in ["USDT", "USDC", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-margined futures funding rates from MEXC.

    Uses two endpoints:
      1. /api/v1/contract/funding_rate — funding rate, collectCycle, nextSettleTime per pair
      2. /api/v1/contract/ticker       — mark price (fairPrice) per pair
    """
    # ── 1. Funding rates (with per-pair schedule) ──────────────────────────────
    try:
        resp = requests.get(f"{BASE_URL}/api/v1/contract/funding_rate", timeout=10)
        resp.raise_for_status()
        funding_data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Funding request failed: {e}")
        return []

    funding_rows = funding_data.get("data", [])
    if not funding_rows:
        print(f"[{EXCHANGE_NAME}] Empty funding data")
        return []

    # ── 2. Ticker for mark prices and volume ──────────────────────────────────
    prices: dict = {}
    volumes: dict = {}
    try:
        resp2 = requests.get(f"{BASE_URL}/api/v1/contract/ticker", timeout=10)
        resp2.raise_for_status()
        for t in resp2.json().get("data", []):
            sym = t.get("symbol", "")
            price = float(t.get("fairPrice") or t.get("lastPrice") or 0)
            volume = float(t.get("volume24") or 0)
            if sym and price > 0:
                prices[sym] = price
            if sym:
                volumes[sym] = volume
    except requests.RequestException:
        pass  # continue without prices — mark_price will be 0 and filtered by FundingData validator

    # ── 3. Merge ───────────────────────────────────────────────────────────────
    results = []
    for row in funding_rows:
        raw_symbol = row.get("symbol", "")
        if "USDT" not in raw_symbol:
            continue

        base_asset = _normalize_symbol(raw_symbol)
        if not base_asset:
            continue

        # Skip pairs with no trading activity
        if volumes and volumes.get(raw_symbol, 0) == 0:
            continue

        try:
            funding_rate  = float(row.get("fundingRate") or 0)
            interval_hours = float(row.get("collectCycle") or 8)
            next_settle_ts = int(row.get("nextSettleTime") or 0)  # already ms
            mark_price     = prices.get(raw_symbol, 0.0)

            annualized = funding_rate * (8760 / interval_hours) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_asset,
                    raw_symbol=raw_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_settle_ts if next_settle_ts > 0 else None,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results