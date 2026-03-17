"""
Bitget USDT-M Futures Funding Rate Fetcher
Endpoint: GET https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES

Bitget funds every 8 hours at 00:00, 08:00, 16:00 UTC.
The tickers endpoint includes fundingRate for all pairs — no auth needed.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://api.bitget.com"
EXCHANGE_NAME = "Bitget"
INTERVAL_HOURS = 8.0

# Bitget funding times (UTC hours) — same as Bybit/Binance
BITGET_FUNDING_HOURS_UTC = [0, 8, 16]


def _next_bitget_funding_ms() -> int:
    """Return next Bitget funding timestamp in milliseconds."""
    import calendar
    now_ts = time.time()
    # Find the next 8h boundary (0, 8, 16 UTC)
    gmt = time.gmtime(now_ts)
    today_midnight_utc = calendar.timegm(
        time.strptime(
            f"{gmt.tm_year}-{gmt.tm_mon:02d}-{gmt.tm_mday:02d} 00:00:00",
            "%Y-%m-%d %H:%M:%S"
        )
    )
    for h in BITGET_FUNDING_HOURS_UTC:
        ts = today_midnight_utc + h * 3600
        if ts > now_ts:
            return int(ts * 1000)
    return int((today_midnight_utc + 86400) * 1000)


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
    No API key required for market data.
    """
    url = f"{BASE_URL}/api/v2/mix/market/tickers"
    params = {"productType": "USDT-FUTURES"}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    if data.get("code") != "00000":
        print(f"[{EXCHANGE_NAME}] API error: {data.get('msg')}")
        return []

    tickers = data.get("data", [])
    next_funding_ts = _next_bitget_funding_ms()
    results = []

    for t in tickers:
        raw_symbol = t.get("symbol", "")
        if "USDT" not in raw_symbol:
            continue

        base_asset = _normalize_symbol(raw_symbol)
        if not base_asset:
            continue

        try:
            funding_rate = float(t.get("fundingRate") or 0)
            mark_price = float(t.get("markPrice") or t.get("lastPr") or 0)

            # Bitget also provides nextFundingTime in some responses
            raw_next = t.get("nextFundingTime")
            if raw_next:
                nft = int(raw_next)
                if nft < 1e12:
                    nft *= 1000
                next_funding_ts_used = nft
            else:
                next_funding_ts_used = next_funding_ts

            annualized = funding_rate * (8760 / INTERVAL_HOURS) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_asset,
                    raw_symbol=raw_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_funding_ts_used,
                    interval_hours=INTERVAL_HOURS,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results