"""
MEXC Futures Funding Rate Fetcher
Endpoint: GET https://contract.mexc.com/api/v1/contract/funding_rate/{symbol}
Batch:    GET https://contract.mexc.com/api/v1/contract/ticker  (all tickers, includes fundingRate)

MEXC funds every 8 hours at 04:00, 12:00, 20:00 UTC.
The ticker endpoint is used as it returns all pairs in one call.
"""
import requests
import time
from typing import List, Optional
from .base import FundingData


BASE_URL = "https://contract.mexc.com"
EXCHANGE_NAME = "MEXC"
INTERVAL_HOURS = 8.0

# MEXC funding times (UTC hours)
MEXC_FUNDING_HOURS_UTC = [4, 12, 20]


def _next_mexc_funding_ms() -> int:
    """Return next MEXC funding timestamp in milliseconds."""
    now = time.gmtime()
    current_hour = now.tm_hour
    today_base = int(time.mktime(time.strptime(
        f"{now.tm_year}-{now.tm_mon:02d}-{now.tm_mday:02d} 00:00:00",
        "%Y-%m-%d %H:%M:%S"
    )))
    # Actually use UTC properly
    import calendar
    today_utc = calendar.timegm(time.gmtime()) - (time.gmtime().tm_hour * 3600
                                                   + time.gmtime().tm_min * 60
                                                   + time.gmtime().tm_sec)
    for h in MEXC_FUNDING_HOURS_UTC:
        ts = today_utc + h * 3600
        if ts > time.time():
            return ts * 1000
    # All passed today — first slot tomorrow
    return (today_utc + 86400 + MEXC_FUNDING_HOURS_UTC[0] * 3600) * 1000


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
    Uses the ticker endpoint which returns fundingRate for all symbols.
    """
    url = f"{BASE_URL}/api/v1/contract/ticker"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    # Response: {"success": true, "code": 0, "data": [...]}
    if not data.get("success") and data.get("code") != 0:
        print(f"[{EXCHANGE_NAME}] API error: {data}")
        return []

    tickers = data.get("data", [])
    if not tickers:
        print(f"[{EXCHANGE_NAME}] Empty data in response")
        return []

    next_funding_ts = _next_mexc_funding_ms()
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
            mark_price = float(t.get("fairPrice") or t.get("lastPrice") or 0)

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