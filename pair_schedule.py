"""
Pair Schedule — Live per-pair funding times from each exchange.
===============================================================
Fetches actual next_funding_ts for every trading pair and shows
when each pair funds, grouped by exchange.

Usage
-----
    python pair_schedule.py                        # all enabled exchanges
    python pair_schedule.py --exchange Bybit       # single exchange
    python pair_schedule.py --exchange OKX KuCoin  # multiple exchanges
    python pair_schedule.py --symbol BTC           # single symbol across all
    python pair_schedule.py --sort time            # sort by time (default)
    python pair_schedule.py --sort symbol          # sort alphabetically
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from colorama import init, Fore, Style
from tabulate import tabulate

from exchanges import bybit, binance, hyperliquid, asterdex, bitget, okx, kucoin
from exchanges.base import FundingData
from settings import active_exchanges

init(autoreset=True)

_ALL_FETCHERS = {
    "Bybit":       bybit.fetch_funding_rates,
    "Binance":     binance.fetch_funding_rates,
    "Hyperliquid": hyperliquid.fetch_funding_rates,
    "AsterDex":    asterdex.fetch_funding_rates,
    "Bitget":      bitget.fetch_funding_rates,
    "OKX":         okx.fetch_funding_rates,
    "KuCoin":      kucoin.fetch_funding_rates,
}


def _advance_ts(ts_ms: Optional[int], interval_hours: float) -> Optional[int]:
    if not ts_ms:
        return None
    now_ms = time.time() * 1000
    step_ms = int(interval_hours * 3_600_000)
    while ts_ms < now_ms:
        ts_ms += step_ms
    return ts_ms


def _fmt_ts(ts_ms: Optional[int]) -> str:
    if not ts_ms:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S UTC")


def _fmt_countdown(ts_ms: Optional[int]) -> str:
    if ts_ms is None:
        return "—"
    secs = max(0.0, (ts_ms - time.time() * 1000) / 1000)
    if secs < 60:
        return Fore.RED + Style.BRIGHT + f"{secs:.0f}s" + Style.RESET_ALL
    if secs < 3600:
        m, s = int(secs // 60), int(secs % 60)
        color = Fore.YELLOW if secs < 600 else ""
        return color + f"{m}m {s:02d}s" + (Style.RESET_ALL if color else "")
    return f"{secs / 3600:.1f}h"


def fetch(exchange_names: List[str]) -> Dict[str, List[FundingData]]:
    fetchers = {n: _ALL_FETCHERS[n] for n in exchange_names if n in _ALL_FETCHERS}
    results: Dict[str, List[FundingData]] = {}

    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {executor.submit(fn): name for name, fn in fetchers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result(timeout=15)
                valid = [d for d in data if d.is_valid]
                results[name] = valid
                print(f"  {name:14s} " + Fore.GREEN + f"✓ {len(valid)} pairs" + Style.RESET_ALL)
            except Exception as e:
                results[name] = []
                print(f"  {name:14s} " + Fore.RED + f"✗ {e}" + Style.RESET_ALL)

    return results


def display(results: Dict[str, List[FundingData]], sort_by: str, symbol_filter: Optional[str]):
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print()
    print(Style.BRIGHT + "═" * 72 + Style.RESET_ALL)
    print(Style.BRIGHT + f"  PAIR FUNDING SCHEDULE  |  {ts_now}" + Style.RESET_ALL)
    print("═" * 72)

    for exchange, records in sorted(results.items()):
        pairs = records
        if symbol_filter:
            sym = symbol_filter.upper()
            pairs = [r for r in pairs if r.symbol == sym]

        if not pairs:
            print(f"\n  {Style.BRIGHT}{exchange}{Style.RESET_ALL}  — no pairs")
            continue

        # Advance stale timestamps
        advanced = {id(r): _advance_ts(r.next_funding_ts, r.interval_hours) for r in pairs}

        if sort_by == "symbol":
            pairs.sort(key=lambda r: r.symbol)
        else:  # time (default)
            pairs.sort(key=lambda r: advanced[id(r)] or float("inf"))

        print(f"\n  {Style.BRIGHT}{exchange}{Style.RESET_ALL}  ({len(pairs)} pairs)\n")

        rows = []
        for r in pairs:
            ts = advanced[id(r)]
            rows.append([
                Style.BRIGHT + r.symbol + Style.RESET_ALL,
                f"{r.interval_hours:.0f}h",
                _fmt_ts(ts),
                _fmt_countdown(ts),
            ])

        print(tabulate(rows, headers=["Symbol", "Interval", "Next Funding (UTC)", "In"],
                       tablefmt="simple"))

    print()


def main():
    parser = argparse.ArgumentParser(description="Live per-pair funding schedule")
    parser.add_argument("--exchange", nargs="+", metavar="NAME",
                        help="Exchange(s) to show (default: all enabled in settings.py)")
    parser.add_argument("--symbol", help="Filter to a specific symbol, e.g. BTC")
    parser.add_argument("--sort", choices=["time", "symbol"], default="time",
                        help="Sort order: time (default) or symbol")
    parser.add_argument("--all", action="store_true",
                        help="Include all known exchanges, not just enabled ones")
    args = parser.parse_args()

    if args.exchange:
        # Case-insensitive match against known exchanges
        known = list(_ALL_FETCHERS.keys())
        chosen = []
        for name in args.exchange:
            match = next((k for k in known if k.lower() == name.lower()), None)
            if match:
                chosen.append(match)
            else:
                print(f"  Unknown exchange: {name}  (known: {', '.join(known)})")
                sys.exit(1)
    elif args.all:
        chosen = list(_ALL_FETCHERS.keys())
    else:
        chosen = active_exchanges()
        if not chosen:
            print("  No exchanges enabled in settings.py. Use --exchange NAME or --all.")
            sys.exit(1)

    print(Style.BRIGHT + f"\n  Fetching from: {', '.join(chosen)}\n" + Style.RESET_ALL)
    results = fetch(chosen)
    display(results, sort_by=args.sort, symbol_filter=args.symbol)


if __name__ == "__main__":
    main()
