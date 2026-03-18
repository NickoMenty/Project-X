"""
Funding Schedule — Hardcoded exchange funding windows
=====================================================
Each exchange credits funding at fixed UTC hours. Using hardcoded
schedules instead of API-returned next_funding_ts timestamps eliminates
stale/rolled-over timestamp bugs and removes the need for tolerance windows.

Usage
-----
  from funding_schedule import next_joint_event_ms, joint_hours, exchanges_align
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional


# ── Canonical funding hours (UTC) per exchange ────────────────────────────────

FUNDING_HOURS: Dict[str, List[int]] = {
    "Bybit":       [0, 8, 16],
    "Binance":     [0, 4, 8, 12, 16, 20],
    "AsterDex":    [0, 8, 16],
    "Bitget":      [0, 8, 16],
    "Hyperliquid": list(range(24)),   # every hour
    "MEXC":        [0, 8, 16],
    "Lighter":     list(range(24)),   # every hour
}


def joint_hours(ex_a: str, ex_b: str) -> List[int]:
    """Sorted list of UTC hours when both exchanges credit funding."""
    hours_a = set(FUNDING_HOURS.get(ex_a, []))
    hours_b = set(FUNDING_HOURS.get(ex_b, []))
    return sorted(hours_a & hours_b)


def exchanges_align(ex_a: str, ex_b: str) -> bool:
    """True if the two exchanges share at least one common funding hour."""
    return len(joint_hours(ex_a, ex_b)) > 0


def next_joint_event_utc(ex_a: str, ex_b: str) -> Optional[datetime]:
    """
    Return the next UTC datetime when both exchanges credit funding.
    Returns None if the pair shares no common funding hours.
    Searches up to 2 days ahead (safe upper bound — max LCM is 24h).
    """
    shared = joint_hours(ex_a, ex_b)
    if not shared:
        return None

    now_utc = datetime.now(timezone.utc)
    today_base = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    for day_offset in range(2):
        base = today_base + timedelta(days=day_offset)
        for h in shared:
            candidate = base.replace(hour=h)
            if candidate > now_utc:
                return candidate

    return None


def next_joint_event_ms(ex_a: str, ex_b: str) -> Optional[int]:
    """Unix milliseconds of the next joint funding event, or None."""
    dt = next_joint_event_utc(ex_a, ex_b)
    return int(dt.timestamp() * 1000) if dt is not None else None


def seconds_to_next_joint(ex_a: str, ex_b: str) -> Optional[float]:
    """Seconds until the next joint funding event, or None."""
    import time
    ts = next_joint_event_ms(ex_a, ex_b)
    if ts is None:
        return None
    return max(0.0, (ts - time.time() * 1000) / 1000.0)
