"""
Bitget USDT-M Futures Funding Rate Fetcher
Endpoints:
  Funding (batch): GET https://api.bitget.com/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES
                   Returns fundingRateInterval and nextUpdate per pair.
  Prices:          GET https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES
                   Returns markPrice per pair.

Each Bitget pair has its own fundingRateInterval and nextUpdate —
we must not assume a single shared schedule.
"""
import requests
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://api.bitget.com"
EXCHANGE_NAME = "Bitget"


def _normalize_symbol(raw: str) -> Optional[str]:
    """Convert BTCUSDT or BTCUSDT_UMCBL -> BTC."""
    raw = raw.split("_")[0]  # strip suffix like _UMCBL
    for suffix in ["USDT", "USDC", "USD"]:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-M perpetual futures funding rates from Bitget.

    Uses two endpoints:
      1. /api/v2/mix/market/current-fund-rate — funding rate, interval, nextUpdate per pair
      2. /api/v2/mix/market/tickers           — mark price per pair
    """
    # ── 1. Funding rates (with per-pair schedule) ──────────────────────────────
    try:
        resp = requests.get(
            f"{BASE_URL}/api/v2/mix/market/current-fund-rate",
            params={"productType": "USDT-FUTURES"},
            timeout=10,
        )
        resp.raise_for_status()
        funding_data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Funding request failed: {e}")
        return []

    if funding_data.get("code") != "00000":
        print(f"[{EXCHANGE_NAME}] API error: {funding_data.get('msg')}")
        return []

    funding_rows = funding_data.get("data", [])
    if not funding_rows:
        print(f"[{EXCHANGE_NAME}] Empty funding data")
        return []

    # ── 2. Ticker for mark prices ──────────────────────────────────────────────
    prices: dict = {}
    try:
        resp2 = requests.get(
            f"{BASE_URL}/api/v2/mix/market/tickers",
            params={"productType": "USDT-FUTURES"},
            timeout=10,
        )
        resp2.raise_for_status()
        for t in resp2.json().get("data", []):
            sym = t.get("symbol", "")
            price = float(t.get("markPrice") or t.get("lastPr") or 0)
            if sym and price > 0:
                prices[sym] = price
    except requests.RequestException:
        pass

    # ── 3. Merge ───────────────────────────────────────────────────────────────
    results = []
    for row in funding_rows:
        raw_symbol = row.get("symbol", "")
        if "USDT" not in raw_symbol:
            continue

        base_asset = _normalize_symbol(raw_symbol)
        if not base_asset:
            continue

        try:
            funding_rate   = float(row.get("fundingRate") or 0)
            interval_hours = float(row.get("fundingRateInterval") or 8)
            next_update_ts = int(row.get("nextUpdate") or 0)  # already ms
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
                    next_funding_ts=next_update_ts if next_update_ts > 0 else None,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results