"""
OKX USDT-M Perpetual Swap Funding Rate Fetcher
================================================
Two-step fetch — both public, no auth required:

  1. GET /api/v5/public/mark-price?instType=SWAP
     Batch mark prices for all SWAP instruments.

  2. GET /api/v5/public/funding-rate?instId={instId}  (one per USDT pair)
     Returns fundingRate, fundingTime, nextFundingTime.
     Interval = nextFundingTime - fundingTime  (per-pair, varies: 1h/4h/8h).
     Fetched in parallel to keep total latency low.

OKX public rate limit: 20 req/2s — we cap workers at 20.
"""
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from .base import FundingData

BASE_URL = "https://www.okx.com"
EXCHANGE_NAME = "OKX"
_MAX_WORKERS = 20


def _normalize_symbol(inst_id: str) -> Optional[str]:
    """Convert BTC-USDT-SWAP -> BTC."""
    if inst_id.endswith("-USDT-SWAP"):
        return inst_id[: -len("-USDT-SWAP")]
    return None


def _fetch_mark_prices() -> Dict[str, float]:
    """Return {instId: markPx} for all SWAP instruments."""
    try:
        resp = requests.get(
            f"{BASE_URL}/api/v5/public/mark-price",
            params={"instType": "SWAP"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except requests.RequestException:
        return {}
    return {
        item["instId"]: float(item.get("markPx") or 0)
        for item in data
        if item.get("markPx")
    }


def _fetch_one_funding(inst_id: str) -> Optional[Tuple[str, float, Optional[int], float]]:
    """
    Fetch funding data for a single instId.
    Returns (instId, funding_rate, next_funding_ts_ms, interval_hours) or None on error.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            return None
        item = items[0]

        funding_rate  = float(item.get("fundingRate") or 0)
        funding_time  = int(item.get("fundingTime") or 0)
        next_time     = int(item.get("nextFundingTime") or 0)

        # Derive interval from the difference between current and next epoch
        if funding_time > 0 and next_time > funding_time:
            interval_ms = next_time - funding_time
            interval_hours = interval_ms / 3_600_000.0
        else:
            interval_hours = 8.0

        next_ts = next_time if next_time > 0 else None
        return (inst_id, funding_rate, next_ts, interval_hours)
    except Exception:
        return None


def fetch_funding_rates() -> List[FundingData]:
    """
    Fetch all USDT-M perpetual swap funding rates from OKX.
    Mark prices fetched in one batch; funding rates fetched in parallel per pair.
    """
    # ── 1. Mark prices (one request) ──────────────────────────────────────────
    mark_prices = _fetch_mark_prices()
    if not mark_prices:
        print(f"[{EXCHANGE_NAME}] Failed to fetch mark prices")
        return []

    # ── 2. Filter to USDT-SWAP pairs with a valid price ────────────────────────
    inst_ids = [
        inst_id for inst_id in mark_prices
        if inst_id.endswith("-USDT-SWAP") and mark_prices[inst_id] > 0
    ]

    # ── 3. Parallel funding rate fetch ────────────────────────────────────────
    funding: Dict[str, Tuple[float, Optional[int], float]] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one_funding, inst_id): inst_id for inst_id in inst_ids}
        for future in as_completed(futures):
            result = future.result()
            if result:
                inst_id, rate, next_ts, interval_h = result
                funding[inst_id] = (rate, next_ts, interval_h)

    # ── 4. Build FundingData objects ───────────────────────────────────────────
    results = []
    for inst_id in inst_ids:
        base_asset = _normalize_symbol(inst_id)
        if not base_asset:
            continue

        mark_price = mark_prices.get(inst_id, 0.0)
        if mark_price <= 0:
            continue

        if inst_id not in funding:
            continue

        funding_rate, next_ts, interval_hours = funding[inst_id]

        try:
            annualized = funding_rate * (8760 / interval_hours) * 100
            results.append(
                FundingData(
                    exchange=EXCHANGE_NAME,
                    symbol=base_asset,
                    raw_symbol=inst_id,
                    funding_rate=funding_rate,
                    funding_rate_pct=funding_rate * 100,
                    mark_price=mark_price,
                    next_funding_ts=next_ts,
                    interval_hours=interval_hours,
                    annualized_rate=annualized,
                )
            )
        except (ValueError, TypeError, ZeroDivisionError):
            continue

    return results
