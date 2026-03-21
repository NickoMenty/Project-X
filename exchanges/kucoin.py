"""
KuCoin Futures USDT-M Funding Rate Fetcher
Endpoints:
  GET https://api-futures.kucoin.com/api/v1/contracts/active
  Returns fundingFeeRate, nextFundingRateTime, markPrice, fundingFeeInterval per contract.
  Public endpoint — no auth required.

  fundingFeeInterval is per-pair (e.g. 8h for most, 4h for some like VANRY).
  We read it directly from the API — never hardcode.
"""
import requests
from typing import List, Optional
from .base import FundingData

BASE_URL = "https://api-futures.kucoin.com"
EXCHANGE_NAME = "KuCoin"

# KuCoin uses XBT for Bitcoin
_SYMBOL_MAP = {
    "XBT": "BTC",
}


def _normalize_symbol(raw: str) -> Optional[str]:
    """Convert XBTUSDTM -> BTC, ETHUSDTM -> ETH, etc."""
    if raw.endswith("USDTM"):
        base = raw[: -len("USDTM")]
        return _SYMBOL_MAP.get(base, base)
    return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-margined perpetual contracts funding rates from KuCoin Futures.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/api/v1/contracts/active",
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        print(f"[{EXCHANGE_NAME}] Request failed: {e}")
        return []

    if body.get("code") != "200000":
        print(f"[{EXCHANGE_NAME}] API error: {body.get('msg')}")
        return []

    results = []
    for c in body.get("data", []):
        raw_symbol = c.get("symbol", "")
        base_asset = _normalize_symbol(raw_symbol)
        if not base_asset:
            continue

        try:
            funding_rate = float(c.get("fundingFeeRate") or 0)
            next_ts      = int(c.get("nextFundingRateTime") or 0)
            mark_price   = float(c.get("markPrice") or 0)

            if mark_price <= 0:
                continue

            # Read per-pair interval from API — do NOT hardcode.
            # KuCoin returns fundingRateGranularity in milliseconds.
            # e.g. 28800000 ms = 8h, 14400000 ms = 4h, 3600000 ms = 1h
            granularity_ms = float(c.get("fundingRateGranularity") or 0)
            if granularity_ms > 0:
                interval_hours = granularity_ms / 3_600_000.0
            else:
                interval_hours = 8.0

            annualized = funding_rate * (8760 / interval_hours) * 100

            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_asset,
                    raw_symbol=raw_symbol,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_ts if next_ts > 0 else None,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError):
            continue

    return results
