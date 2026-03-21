"""
Funding Table
=============
Fetches all perpetual futures from every exchange and prints a full table:
  Exchange | Symbol | Mark Price | Funding Rate | Next Funding (UTC) | In

Usage
-----
    python funding_table.py               # show all pairs, sorted by |rate| desc
    python funding_table.py --exchange Bybit          # single exchange
    python funding_table.py --symbol BTC              # single symbol across all exchanges
    python funding_table.py --min-rate 0.05           # only rates >= 0.05% absolute
    python funding_table.py --sort rate               # sort by rate (default)
    python funding_table.py --sort time               # sort by time-to-funding
    python funding_table.py --sort symbol             # sort alphabetically
    python funding_table.py --watch                   # refresh every 30s (Ctrl+C to stop)
    python funding_table.py --watch --interval 60     # refresh every 60s
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from colorama import init, Fore, Style
from tabulate import tabulate

from exchanges import bybit, binance, hyperliquid, asterdex, bitget
from exchanges.base import FundingData

init(autoreset=True)

EXCHANGE_FETCHERS = {
    "Bybit":       bybit.fetch_funding_rates,
    "Binance":     binance.fetch_funding_rates,
    "Hyperliquid": hyperliquid.fetch_funding_rates,
    "AsterDex":    asterdex.fetch_funding_rates,
    "Bitget":      bitget.fetch_funding_rates,
}


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all(exchanges: Optional[List[str]] = None) -> List[FundingData]:
    """Fetch funding rates from all (or selected) exchanges in parallel."""
    fetchers = {
        name: fn for name, fn in EXCHANGE_FETCHERS.items()
        if exchanges is None or name in exchanges
    }
    results: List[FundingData] = []
    errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {executor.submit(fn): name for name, fn in fetchers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result(timeout=15)
                valid = [d for d in data if d.is_valid]
                results.extend(valid)
                print(
                    f"  {name:14s} "
                    + Fore.GREEN + f"✓ {len(valid)} pairs" + Style.RESET_ALL
                )
            except Exception as e:
                errors[name] = str(e)
                print(
                    f"  {name:14s} "
                    + Fore.RED + f"✗ {e}" + Style.RESET_ALL
                )

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _advance_ts(ts_ms: Optional[int], interval_hours: float) -> Optional[int]:
    """
    Advance a (potentially stale) next_funding_ts forward by interval until it
    is in the future. Mirrors the same logic used in spike_scorer.py.
    """
    if not ts_ms:
        return None
    now_ms = time.time() * 1000
    step_ms = int(interval_hours * 3_600_000)
    while ts_ms < now_ms:
        ts_ms += step_ms
    return ts_ms


def _seconds_to(ts_ms: Optional[int]) -> Optional[float]:
    if ts_ms is None:
        return None
    return max(0.0, (ts_ms - time.time() * 1000) / 1000)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(price: float) -> str:
    if price <= 0:
        return "—"
    if price >= 1_000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    return f"${price:.8f}"


def _fmt_rate(rate_pct: float) -> str:
    s = f"{rate_pct:+.4f}%"
    if rate_pct > 0.05:
        return Fore.GREEN + Style.BRIGHT + s + Style.RESET_ALL
    if rate_pct > 0:
        return Fore.GREEN + s + Style.RESET_ALL
    if rate_pct < -0.05:
        return Fore.RED + Style.BRIGHT + s + Style.RESET_ALL
    if rate_pct < 0:
        return Fore.RED + s + Style.RESET_ALL
    return Style.DIM + s + Style.RESET_ALL


def _fmt_next_ts(ts_ms: Optional[int]) -> str:
    if not ts_ms:
        return "—"
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M:%S UTC")


def _fmt_countdown(secs: Optional[float]) -> str:
    if secs is None:
        return "—"
    if secs < 60:
        return Fore.RED + Style.BRIGHT + f"{secs:.0f}s" + Style.RESET_ALL
    if secs < 3600:
        m, s = int(secs // 60), int(secs % 60)
        color = Fore.YELLOW if secs < 600 else ""
        return color + f"{m}m {s:02d}s" + (Style.RESET_ALL if color else "")
    return f"{secs / 3600:.1f}h"


# ── Display ───────────────────────────────────────────────────────────────────

def display(
    records: List[FundingData],
    sort_by: str,
    min_rate: float,
    symbol_filter: Optional[str],
    exchange_filter: Optional[List[str]],
    elapsed: float,
):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Apply filters ──────────────────────────────────────────────────────────
    filtered = records
    if symbol_filter:
        sym = symbol_filter.upper()
        filtered = [r for r in filtered if r.symbol == sym]
    if exchange_filter:
        filtered = [r for r in filtered if r.exchange in exchange_filter]
    if min_rate > 0:
        filtered = [r for r in filtered if abs(r.funding_rate_pct) >= min_rate]

    # ── Advance stale timestamps once so all display values are consistent ───────
    advanced: Dict[int, Optional[int]] = {
        id(r): _advance_ts(r.next_funding_ts, r.interval_hours) for r in filtered
    }

    # ── Sort ───────────────────────────────────────────────────────────────────
    if sort_by == "time":
        filtered.sort(key=lambda r: (
            _seconds_to(advanced[id(r)]) if advanced[id(r)] is not None else float("inf")
        ))
    elif sort_by == "symbol":
        filtered.sort(key=lambda r: (r.symbol, r.exchange))
    else:  # rate (default)
        filtered.sort(key=lambda r: -abs(r.funding_rate_pct))

    # ── Header ─────────────────────────────────────────────────────────────────
    os.system("cls" if os.name == "nt" else "clear")
    print()
    print(Style.BRIGHT + "═" * 90 + Style.RESET_ALL)
    print(
        Style.BRIGHT +
        f"  FUTURES FUNDING TABLE  |  {ts}  |  "
        f"{len(filtered)} pairs  |  fetch: {elapsed:.2f}s  |  sort: {sort_by}"
        + Style.RESET_ALL
    )
    print("═" * 90)
    print()

    if not filtered:
        print(f"  {Fore.YELLOW}No pairs match the current filters.{Style.RESET_ALL}\n")
        return

    # ── Build rows ─────────────────────────────────────────────────────────────
    rows = []
    for r in filtered:
        ts = advanced[id(r)]
        rows.append([
            Style.BRIGHT + r.exchange + Style.RESET_ALL,
            Style.BRIGHT + r.symbol + Style.RESET_ALL,
            _fmt_price(r.mark_price),
            _fmt_rate(r.funding_rate_pct),
            f"{r.interval_hours:.0f}h",
            _fmt_next_ts(ts),
            _fmt_countdown(_seconds_to(ts)),
        ])

    headers = [
        "Exchange", "Symbol", "Mark Price",
        "Funding Rate", "Interval",
        "Next Funding (UTC)", "In",
    ]
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print()
    print(
        f"  {Fore.GREEN}Green{Style.RESET_ALL} = longs pay (short side collects)  "
        f"  {Fore.RED}Red{Style.RESET_ALL} = shorts pay (long side collects)  "
        f"  {len(records)} total pairs fetched"
    )
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Futures funding rate table for all exchanges")
    parser.add_argument("--exchange",  nargs="+",   help="Filter to specific exchange(s)")
    parser.add_argument("--symbol",                 help="Filter to a specific symbol (e.g. BTC)")
    parser.add_argument("--min-rate",  type=float, default=0.0,
                        help="Minimum absolute funding rate %% to show (e.g. 0.05)")
    parser.add_argument("--sort",      choices=["rate", "time", "symbol"], default="rate",
                        help="Sort order: rate (default), time, symbol")
    parser.add_argument("--watch",     action="store_true",
                        help="Refresh automatically (use with --interval)")
    parser.add_argument("--interval",  type=int, default=30,
                        help="Refresh interval in seconds when --watch is set (default: 30)")
    args = parser.parse_args()

    exchange_filter = args.exchange  # None or list

    try:
        while True:
            print(Style.BRIGHT + "\n  Fetching funding rates...\n" + Style.RESET_ALL)
            t0 = time.time()
            records = fetch_all(exchange_filter)
            elapsed = time.time() - t0

            display(
                records,
                sort_by=args.sort,
                min_rate=args.min_rate,
                symbol_filter=args.symbol,
                exchange_filter=exchange_filter,
                elapsed=elapsed,
            )

            if not args.watch:
                break

            print(
                f"  {Fore.CYAN}Refreshing in {args.interval}s  |  Ctrl+C to stop{Style.RESET_ALL}"
            )
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n  Exiting. Goodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()
