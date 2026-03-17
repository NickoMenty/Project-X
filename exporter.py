"""
Persistent Export — Milestone 1, Step 4
=========================================
Saves each fetch cycle's data to disk for:
  - Backtesting in Milestone 3
  - Auditing / debugging
  - Spotting funding rate drift over time

Output structure:
  data/
    snapshots/
      funding_YYYYMMDD_HHMMSS.json    ← full snapshot per cycle (all pairs, all exchanges)
    history/
      funding_history.csv             ← append-only CSV (one row per symbol per cycle)
      prices_history.csv              ← append-only CSV (one row per symbol per cycle)
    latest/
      funding_latest.json             ← always overwritten with most recent data
      funding_latest.csv              ← always overwritten with most recent data
      prices_latest.csv               ← always overwritten with most recent data

Rotation policy:
  - Snapshots older than SNAPSHOT_RETENTION_DAYS are deleted automatically
  - history CSVs grow indefinitely (they're append-only, designed for analysis)
  - The 'latest' files are always safe to read — never mid-write (atomic rename)
"""

import csv
import json
import os
import time
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from exchanges.base import FundingData
from price_normalizer import PriceView, format_price_summary
from pair_engine import PairRecord


# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

DATA_DIR = Path("data")
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
HISTORY_DIR = DATA_DIR / "history"
LATEST_DIR = DATA_DIR / "latest"

SNAPSHOT_RETENTION_DAYS = 7   # Delete snapshots older than this

FUNDING_HISTORY_CSV = HISTORY_DIR / "funding_history.csv"
PRICES_HISTORY_CSV  = HISTORY_DIR / "prices_history.csv"

LATEST_JSON         = LATEST_DIR / "funding_latest.json"
LATEST_FUNDING_CSV  = LATEST_DIR / "funding_latest.csv"
LATEST_PRICES_CSV   = LATEST_DIR / "prices_latest.csv"

# Ordered columns for funding CSV
FUNDING_CSV_FIELDS = [
    "timestamp_utc", "unix_ts",
    "symbol", "exchange",
    "raw_symbol", "funding_rate", "funding_rate_pct",
    "mark_price", "next_funding_ts", "interval_hours", "annualized_rate",
]

PRICES_CSV_FIELDS = [
    "timestamp_utc", "unix_ts",
    "symbol", "ref_price_usd",
    "spread_usd", "spread_pct",
    "has_anomaly", "anomalous_exchanges",
]


def _ensure_dirs():
    for d in [SNAPSHOTS_DIR, HISTORY_DIR, LATEST_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, content: str):
    """Write to a temp file then rename — prevents reading a half-written file."""
    dir_ = path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        f.write(content)
        tmp_path = f.name
    os.replace(tmp_path, path)


# ─────────────────────────────────────────────
#  Serialization helpers
# ─────────────────────────────────────────────

def _funding_to_row(fd: FundingData, ts_utc: str, unix_ts: int) -> dict:
    return {
        "timestamp_utc":   ts_utc,
        "unix_ts":         unix_ts,
        "symbol":          fd.symbol,
        "exchange":        fd.exchange,
        "raw_symbol":      fd.raw_symbol,
        "funding_rate":    fd.funding_rate,
        "funding_rate_pct": fd.funding_rate_pct,
        "mark_price":      fd.mark_price,
        "next_funding_ts": fd.next_funding_ts,
        "interval_hours":  fd.interval_hours,
        "annualized_rate": fd.annualized_rate,
    }


def _price_to_row(pv: PriceView, ts_utc: str, unix_ts: int) -> dict:
    return {
        "timestamp_utc":       ts_utc,
        "unix_ts":             unix_ts,
        "symbol":              pv.symbol,
        "ref_price_usd":       pv.reference_price,
        "spread_usd":          pv.spread_usd,
        "spread_pct":          pv.spread_pct,
        "has_anomaly":         pv.has_anomaly,
        "anomalous_exchanges": ",".join(pv.anomalous_exchanges),
    }


def _build_snapshot(
    records: Dict[str, PairRecord],
    ts_utc: str,
    unix_ts: int,
) -> dict:
    """Build the full JSON snapshot structure."""
    snapshot = {
        "timestamp_utc": ts_utc,
        "unix_ts": unix_ts,
        "total_pairs": len(records),
        "pairs": {}
    }

    for symbol, pr in records.items():
        pair_data = {
            "symbol": symbol,
            "exchange_count": pr.exchange_count,
            "exchanges": sorted(pr.exchanges_present),
            "reference_price": pr.reference_price,
        }

        # Funding rates per exchange
        pair_data["funding"] = {
            ex: {
                "funding_rate":     fd.funding_rate,
                "funding_rate_pct": fd.funding_rate_pct,
                "mark_price":       fd.mark_price,
                "next_funding_ts":  fd.next_funding_ts,
                "interval_hours":   fd.interval_hours,
                "annualized_rate":  fd.annualized_rate,
            }
            for ex, fd in pr.funding.items()
        }

        # Price view
        if pr.price_view:
            pv = pr.price_view
            pair_data["prices"] = {
                "reference_price": pv.reference_price,
                "spread_usd":      pv.spread_usd,
                "spread_pct":      pv.spread_pct,
                "has_anomaly":     pv.has_anomaly,
                "anomalous_exchanges": pv.anomalous_exchanges,
                "per_exchange": {
                    ep.exchange: {
                        "mark_price": ep.mark_price,
                        "deviation_pct": ep.deviation_from_ref_pct,
                    }
                    for ep in pv.prices
                }
            }

        # Best exchange pair spread
        if pr.best_pair:
            bp = pr.best_pair
            pair_data["best_spread"] = {
                "exchange_a":       bp.exchange_a,
                "exchange_b":       bp.exchange_b,
                "rate_a_pct":       bp.rate_a_pct,
                "rate_b_pct":       bp.rate_b_pct,
                "spread_pct":       bp.raw_spread_pct,
                "abs_spread_pct":   bp.abs_spread_pct(),
                "long_exchange":    bp.long_exchange(),
                "short_exchange":   bp.short_exchange(),
            }

        snapshot["pairs"][symbol] = pair_data

    return snapshot


# ─────────────────────────────────────────────
#  CSV writers
# ─────────────────────────────────────────────

def _write_csv_rows(path: Path, fields: List[str], rows: List[dict], append: bool):
    """Write rows to a CSV. If append=True and file exists, skip header."""
    mode = "a" if (append and path.exists()) else "w"
    write_header = not (append and path.exists())

    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _write_csv_atomic(path: Path, fields: List[str], rows: List[dict]):
    """Write CSV to a temp file then atomically replace the target."""
    dir_ = path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", dir=dir_, delete=False, suffix=".tmp",
        newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        tmp_path = f.name
    os.replace(tmp_path, path)


# ─────────────────────────────────────────────
#  Snapshot rotation
# ─────────────────────────────────────────────

def _rotate_snapshots(retention_days: int = SNAPSHOT_RETENTION_DAYS):
    """Delete snapshot JSON files older than retention_days."""
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for f in SNAPSHOTS_DIR.glob("funding_*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    if deleted:
        print(f"  [Export] Rotated {deleted} old snapshot(s) (>{retention_days}d)")


# ─────────────────────────────────────────────
#  Main export function — call this each cycle
# ─────────────────────────────────────────────

def export_snapshot(
    records: Dict[str, PairRecord],
    price_views: Dict[str, PriceView],
    all_data: Dict[str, List[FundingData]],
    retention_days: int = SNAPSHOT_RETENTION_DAYS,
) -> dict:
    """
    Persist one full cycle of data to disk. Call this after each fetch cycle.

    Args:
        records:       Output of build_pair_records()
        price_views:   Output of normalize_prices()
        all_data:      Raw output of fetch_all() — used for the full funding CSV
        retention_days: Snapshot retention window

    Returns:
        Dict with paths written and row counts, for logging.
    """
    _ensure_dirs()

    now = datetime.now(timezone.utc)
    ts_utc = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    unix_ts = int(now.timestamp())
    ts_file = now.strftime("%Y%m%d_%H%M%S")

    # ── 1. Build flat funding rows (all exchanges, all symbols from all_data) ──
    funding_rows = []
    for exchange, fd_list in all_data.items():
        for fd in fd_list:
            funding_rows.append(_funding_to_row(fd, ts_utc, unix_ts))

    # ── 2. Build price rows (only symbols in pair_records) ──
    price_rows = [
        _price_to_row(pv, ts_utc, unix_ts)
        for pv in price_views.values()
    ]

    # ── 3. Write history CSVs (append) ──
    _write_csv_rows(FUNDING_HISTORY_CSV, FUNDING_CSV_FIELDS, funding_rows, append=True)
    _write_csv_rows(PRICES_HISTORY_CSV,  PRICES_CSV_FIELDS,  price_rows,   append=True)

    # ── 4. Write latest CSVs (atomic overwrite) ──
    _write_csv_atomic(LATEST_FUNDING_CSV, FUNDING_CSV_FIELDS, funding_rows)
    _write_csv_atomic(LATEST_PRICES_CSV,  PRICES_CSV_FIELDS,  price_rows)

    # ── 5. Build and write JSON snapshot ──
    snapshot = _build_snapshot(records, ts_utc, unix_ts)
    snapshot_path = SNAPSHOTS_DIR / f"funding_{ts_file}.json"
    _atomic_write(snapshot_path, json.dumps(snapshot, indent=2))

    # ── 6. Write latest JSON (atomic overwrite) ──
    _atomic_write(LATEST_JSON, json.dumps(snapshot, indent=2))

    # ── 7. Rotate old snapshots ──
    _rotate_snapshots(retention_days)

    return {
        "timestamp_utc":    ts_utc,
        "snapshot_path":    str(snapshot_path),
        "funding_rows":     len(funding_rows),
        "price_rows":       len(price_rows),
        "pairs_exported":   len(records),
    }


def load_latest_snapshot() -> Optional[dict]:
    """Load the most recent snapshot JSON from disk. Returns None if not found."""
    if not LATEST_JSON.exists():
        return None
    with open(LATEST_JSON, encoding="utf-8") as f:
        return json.load(f)


def get_history_stats() -> dict:
    """Return quick stats about accumulated history files."""
    stats = {}
    for name, path in [
        ("funding_history", FUNDING_HISTORY_CSV),
        ("prices_history",  PRICES_HISTORY_CSV),
    ]:
        if path.exists():
            size_kb = path.stat().st_size / 1024
            # Count rows (subtract header)
            with open(path, encoding="utf-8") as f:
                row_count = sum(1 for _ in f) - 1
            stats[name] = {"path": str(path), "rows": row_count, "size_kb": round(size_kb, 1)}
        else:
            stats[name] = {"path": str(path), "rows": 0, "size_kb": 0}

    snapshot_count = len(list(SNAPSHOTS_DIR.glob("funding_*.json"))) if SNAPSHOTS_DIR.exists() else 0
    stats["snapshots"] = {"count": snapshot_count, "dir": str(SNAPSHOTS_DIR)}
    return stats