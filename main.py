"""
Funding Rate Aggregator — Milestone 1 + Opportunity Scorer — Milestone 2
=========================================================================
Step 1: Fetch funding rates from all 7 exchanges concurrently
Step 2: Normalize prices across exchanges, detect anomalies
Step 3: Build pair intersection table as rich PairRecord objects
Step 4: Persist every cycle to CSV + JSON (history + latest + snapshots)
Step 5: Score and rank all exchange-pair combinations (Milestone 2)

Usage:
    python main.py                   # Refresh every 60s
    python main.py --once            # Run once and exit
    python main.py --interval 30     # Refresh every 30s
    python main.py --min-exchanges 3 # Only pairs on 3+ exchanges
    python main.py --no-export       # Skip disk writes (display only)
    python main.py --no-score        # Skip opportunity scoring section
    python main.py --stats           # Show history file stats and exit
"""

import argparse
import time
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

from tabulate import tabulate
from colorama import init, Fore, Style

from exchanges.base import FundingData
from exchanges import bybit, binance, hyperliquid, asterdex, lighter, mexc, bitget
from price_normalizer import normalize_prices
from pair_engine import build_pair_records, summarize_intersection, PairRecord
from exporter import export_snapshot, get_history_stats
from spike_scorer import (
    score_all_spikes, SpikeOpportunity, POSITION_SIZE_USD,
)
from paper_trader import (
    simulate_entry, simulate_exit, print_trade_report,
    EXIT_WAIT_SECONDS,
)
from telegram_reporter import send_trade_report, send_spike_opportunities, send_alert

init(autoreset=True)

EXCHANGE_FETCHERS = {
    "Bybit":       bybit.fetch_funding_rates,
    "Binance":     binance.fetch_funding_rates,
    "Hyperliquid": hyperliquid.fetch_funding_rates,
    "AsterDex":    asterdex.fetch_funding_rates,
    "Lighter":     lighter.fetch_funding_rates,
    "MEXC":        mexc.fetch_funding_rates,
    "Bitget":      bitget.fetch_funding_rates,
}

ALL_EXCHANGES = list(EXCHANGE_FETCHERS.keys())


# ─────────────────────────────────────────────
#  Step 1 — Concurrent fetch
# ─────────────────────────────────────────────

def fetch_all() -> Dict[str, List[FundingData]]:
    """Fetch funding rates from all 7 exchanges in parallel."""
    results = {}

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(fn): name for name, fn in EXCHANGE_FETCHERS.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                data = future.result(timeout=15)
                results[name] = data
                status = Fore.GREEN + f"✓ {len(data)} pairs" + Style.RESET_ALL
            except Exception as e:
                results[name] = []
                status = Fore.RED + f"✗ {e}" + Style.RESET_ALL
            print(f"  {name:14s} {status}")

    return results


# ─────────────────────────────────────────────
#  Display helpers
# ─────────────────────────────────────────────

def _color_rate(rate_pct: float) -> str:
    s = f"{rate_pct:+.4f}%"
    if rate_pct > 0:
        return Fore.GREEN + s + Style.RESET_ALL
    elif rate_pct < 0:
        return Fore.RED + s + Style.RESET_ALL
    return s


def _fmt_price(price: float) -> str:
    if price == 0:
        return "—"
    if price >= 1000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    return f"${price:.8f}"


def _fmt_spread(spread_pct: float, interval_a: float, interval_b: float) -> str:
    """
    Annualize spread using the MINIMUM interval of the two exchanges.
    The bottleneck is the less-frequent payer — you can only collect
    as often as the slower exchange credits.
    """
    min_interval = min(interval_a, interval_b)
    apr = spread_pct * (8760 / min_interval)
    s = f"{apr:+.1f}%"
    if apr > 20:
        return Fore.YELLOW + Style.BRIGHT + s + Style.RESET_ALL
    if apr > 5:
        return Fore.YELLOW + s + Style.RESET_ALL
    return s


def display_summary(
    all_data: Dict[str, List[FundingData]],
    records: Dict[str, PairRecord],
    elapsed: float,
    export_info: dict = None,
):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = summarize_intersection(records)
    total_pairs_per_ex = {ex: len(recs) for ex, recs in all_data.items()}

    print()
    print(Style.BRIGHT + "═" * 110 + Style.RESET_ALL)
    print(Style.BRIGHT + f"  FUNDING RATE MONITOR  |  {ts}  |  Fetch: {elapsed:.2f}s" + Style.RESET_ALL)
    print("═" * 110)
    print()

    ex_summary = "  ".join(
        f"{ex}: {Fore.CYAN}{total_pairs_per_ex.get(ex, 0)}{Style.RESET_ALL}"
        for ex in ALL_EXCHANGES
    )
    print(f"  Pairs per exchange : {ex_summary}")

    dist = summary["by_exchange_count"]
    dist_str = "  ".join(f"{k} exchanges: {Fore.YELLOW}{v}{Style.RESET_ALL}" for k, v in dist.items())
    print(f"  Pair distribution  : {dist_str}")

    if summary["symbols_7_exchanges"]:
        print(f"  On all 7 exchanges : {Fore.GREEN}{', '.join(sorted(summary['symbols_7_exchanges']))}{Style.RESET_ALL}")

    if export_info:
        print(
            f"  Last export        : {export_info['timestamp_utc']}  "
            f"({export_info['funding_rows']} funding rows, "
            f"{export_info['pairs_exported']} pairs)  "
            f"→ {export_info['snapshot_path']}"
        )
    print()


def display_funding_table(records: Dict[str, PairRecord]):
    """
    Main display table: one row per symbol, funding rate per exchange,
    price spread, and best APR spread.
    """
    sorted_records = sorted(
        records.values(),
        key=lambda r: (
            -r.exchange_count,
            -(r.best_pair.abs_spread() if r.best_pair else 0),
            r.symbol,
        )
    )[:5]

    headers = (
        ["Symbol", "N"] +
        ALL_EXCHANGES +
        ["Ref Price", "Price Spread", "Best Spread (APR)", "Long→Short"]
    )
    rows = []

    for pr in sorted_records:
        row = [Style.BRIGHT + pr.symbol + Style.RESET_ALL, str(pr.exchange_count)]

        for ex in ALL_EXCHANGES:
            fd = pr.funding.get(ex)
            if fd:
                row.append(_color_rate(fd.funding_rate_pct))
            else:
                row.append(Fore.BLACK + Style.DIM + "—" + Style.RESET_ALL)

        # Reference price
        ref = pr.reference_price
        row.append(_fmt_price(ref) if ref else "—")

        # Price spread
        if pr.price_view:
            pv = pr.price_view
            anomaly_marker = Fore.RED + " ⚠" + Style.RESET_ALL if pv.has_anomaly else ""
            row.append(f"{pv.spread_pct:.4f}%{anomaly_marker}")
        else:
            row.append("—")

        # Best spread (APR) and direction
        if pr.best_pair:
            bp = pr.best_pair
            spread_str = _fmt_spread(bp.abs_spread_pct(), bp.interval_a, bp.interval_b)
            direction = (
                Fore.CYAN +
                f"L:{bp.long_exchange()[:4]}  S:{bp.short_exchange()[:4]}" +
                Style.RESET_ALL
            )
            row.append(spread_str)
            row.append(direction)
        else:
            row.append("—")
            row.append("—")

        rows.append(row)

    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print()
    print(
        f"  {Fore.GREEN}Green{Style.RESET_ALL} rate = longs pay (collect if short)  "
        f"  {Fore.RED}Red{Style.RESET_ALL} rate = shorts pay (collect if long)  "
        f"  {Fore.RED}⚠{Style.RESET_ALL} = price anomaly detected"
    )



def display_spike_opportunities(
    passing: "List[SpikeOpportunity]",
    failing: "List[SpikeOpportunity]",
):
    """
    Spike scorer section: single-epoch funding arbitrage opportunities.

    Passing pairs are sorted by score descending (highest net after fees first).
    Near-misses show the top failing pairs sorted by funding spread so it's
    easy to see which are closest to clearing fees.
    """
    total = len(passing) + len(failing)
    fail_align   = sum(1 for o in failing if any("misaligned" in r or "missing" in r for r in o.fail_reasons))
    fail_price   = sum(1 for o in failing if any("price spread" in r for r in o.fail_reasons))
    fail_spread  = sum(1 for o in failing if any("funding spread" in r for r in o.fail_reasons))

    print()
    print(Style.BRIGHT + "─" * 110 + Style.RESET_ALL)
    print(
        Style.BRIGHT +
        f"  SPIKE OPPORTUNITIES  |  "
        f"{Fore.GREEN}{len(passing)} passing{Style.RESET_ALL}{Style.BRIGHT}  |  "
        f"{total} scored  |  "
        f"filtered: {fail_spread} spread<fees  "
        f"{fail_align} misaligned  "
        f"{fail_price} price spread" +
        Style.RESET_ALL
    )
    print("─" * 110)

    if not passing:
        print(f"\n  {Fore.YELLOW}No spike opportunities pass all checks this cycle.{Style.RESET_ALL}\n")
    else:
        headers = [
            "#", "Symbol", "Long", "Short",
            "L.Rate", "S.Rate", "Spread",
            "Fees Long (o+c)", "Fees Short (o+c)", "Score",
            "Price Spread", "Funding At (UTC)", "In", f"Est.Profit (${POSITION_SIZE_USD:.0f})"
        ]
        rows = []
        for i, opp in enumerate(passing, 1):
            secs = opp.seconds_to_funding
            if secs == float("inf") or not opp.next_funding_ts:
                countdown_str = Fore.YELLOW + "?" + Style.RESET_ALL
                exec_str = "?"
            else:
                exec_str = datetime.fromtimestamp(
                    opp.next_funding_ts / 1000, tz=timezone.utc
                ).strftime("%H:%M:%S")
                if secs < 60:
                    countdown_str = Fore.RED + Style.BRIGHT + f"{secs:.0f}s" + Style.RESET_ALL
                elif secs < 3600:
                    countdown_str = Fore.YELLOW + f"{int(secs//60)}m {int(secs%60)}s" + Style.RESET_ALL
                else:
                    countdown_str = f"{secs/3600:.1f}h"

            score_color = Fore.GREEN if opp.score > 0.1 else Fore.YELLOW
            rows.append([
                str(i),
                Style.BRIGHT + opp.symbol + Style.RESET_ALL,
                opp.long_exchange,
                opp.short_exchange,
                f"{opp.long_rate_pct:+.4f}%",
                Fore.GREEN + f"{opp.short_rate_pct:+.4f}%" + Style.RESET_ALL,
                Fore.CYAN + f"{opp.funding_spread_pct:.4f}%" + Style.RESET_ALL,
                f"{opp.long_fee_pct * 2:.4f}%",
                f"{opp.short_fee_pct * 2:.4f}%",
                score_color + Style.BRIGHT + f"{opp.score:+.4f}%" + Style.RESET_ALL,
                f"{opp.price_spread_pct:.3f}%",
                exec_str,
                countdown_str,
                Fore.GREEN + f"${opp.estimated_profit_usd:.4f}" + Style.RESET_ALL,
            ])

        print()
        print(tabulate(rows, headers=headers, tablefmt="simple"))
        print()
        print(
            f"  Score = funding spread - round-trip fees  |  "
            f"Fees = (fee_long + fee_short) × 2  |  "
            f"Position size: ${POSITION_SIZE_USD:.0f} per leg"
        )

    # Near-misses: top 5 failing by funding spread
    near_misses = [o for o in failing if o.funding_spread_pct > 0][:5]
    if near_misses:
        print()
        print(f"  {Fore.YELLOW}Near-misses (closest to clearing fees):{Style.RESET_ALL}")
        for opp in near_misses:
            reasons = "  |  ".join(opp.fail_reasons)
            print(
                f"    {opp.symbol:8s}  "
                f"L:{opp.long_exchange[:8]:<8} S:{opp.short_exchange[:8]:<8}  "
                f"spread={opp.funding_spread_pct:.4f}%  fees={opp.total_fee_pct:.4f}%  "
                f"→ {Fore.RED}{reasons}{Style.RESET_ALL}"
            )


def display_price_anomalies(records: Dict[str, PairRecord]):
    """Separate section listing any active price anomalies."""
    anomalies = [
        pr for pr in records.values()
        if pr.price_view and pr.price_view.has_anomaly
    ]
    if not anomalies:
        return

    print()
    print(Fore.RED + Style.BRIGHT + f"  ⚠  PRICE ANOMALIES ({len(anomalies)} pairs)" + Style.RESET_ALL)
    print()
    for pr in anomalies:
        pv = pr.price_view
        flagged = ", ".join(
            f"{ep.exchange}=${ep.mark_price:,.4f} ({ep.deviation_from_ref_pct:+.3f}%)"
            for ep in pv.prices
            if ep.is_anomalous
        )
        print(f"  {pr.symbol:10s}  ref=${pv.reference_price:,.4f}  flagged: {flagged}")


# ─────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────

def run(interval: int, once: bool, min_exchanges: int, no_export: bool, no_score: bool):
    export_info = None
    last_scanned_event: Optional[int] = None  # avoid double-scanning same epoch
    send_alert("Started")

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(Style.BRIGHT + "\n  Fetching funding rates...\n" + Style.RESET_ALL)

        all_data, records, _, elapsed, export_info = _fetch_and_build(
            min_exchanges, no_export, export_info
        )

        display_summary(all_data, records, elapsed, export_info)
        display_funding_table(records)
        display_price_anomalies(records)

        if not no_score:
            try:
                passing, failing = score_all_spikes(records)
                display_spike_opportunities(passing, failing)
            except Exception as e:
                print(f"  {Fore.RED}[Scorer] Failed: {e}{Style.RESET_ALL}")

        if once:
            break

        # ── Smart sleep: wake at T-15s before the nearest joint funding event ──
        now_ms = time.time() * 1000
        cycle_end_ms = now_ms + interval * 1000
        next_event = _next_primary_event_ms(records) if not no_score else None
        prescan_ms = (next_event - 15_000) if next_event else None

        # Fire pre-scan if: event is within this cycle window, T-15s hasn't
        # passed yet, and we haven't already scanned this exact event.
        if (
            prescan_ms
            and prescan_ms > now_ms
            and prescan_ms < cycle_end_ms
            and next_event != last_scanned_event
        ):
            sleep_s = (prescan_ms - time.time() * 1000) / 1000
            print()
            print(
                f"  {Fore.CYAN}Next joint epoch in "
                f"{(next_event - time.time()*1000)/1000:.0f}s — "
                f"pre-scan in {sleep_s:.0f}s  |  Ctrl+C to exit{Style.RESET_ALL}"
            )
            if sleep_s > 0:
                time.sleep(sleep_s)

            try:
                _run_pre_epoch_scan(next_event, min_exchanges, no_export, export_info)
            except Exception as e:
                print(f"  {Fore.RED}[Pre-scan] Failed: {e}{Style.RESET_ALL}")

            last_scanned_event = next_event

            # Sleep out the rest of the cycle
            remaining = (cycle_end_ms - time.time() * 1000) / 1000
            if remaining > 1:
                print()
                print(f"  {Fore.CYAN}Next full refresh in {remaining:.0f}s  |  Ctrl+C to exit{Style.RESET_ALL}")
                time.sleep(remaining)
        else:
            print()
            print(f"  {Fore.CYAN}Refreshing in {interval}s  |  Ctrl+C to exit{Style.RESET_ALL}")
            time.sleep(interval)


# ─────────────────────────────────────────────
#  Shared fetch + build helper
# ─────────────────────────────────────────────

def _fetch_and_build(
    min_exchanges: int,
    no_export: bool,
    export_info: Optional[dict],
) -> Tuple[dict, dict, dict, float, Optional[dict]]:
    """
    Run one full fetch → normalise → build cycle.
    Returns (all_data, records, price_views, elapsed, export_info).
    """
    t0 = time.time()
    all_data = fetch_all()
    elapsed = time.time() - t0

    pair_table_raw: dict = defaultdict(dict)
    for exchange, fds in all_data.items():
        for fd in fds:
            pair_table_raw[fd.symbol][exchange] = fd
    filtered = {s: m for s, m in pair_table_raw.items() if len(m) >= min_exchanges}
    price_views = normalize_prices(filtered)
    records = build_pair_records(all_data, price_views, min_exchanges=min_exchanges)

    if not no_export:
        try:
            export_info = export_snapshot(records, price_views, all_data)
        except Exception as e:
            print(f"  {Fore.RED}[Export] Failed: {e}{Style.RESET_ALL}")

    return all_data, records, price_views, elapsed, export_info


# ─────────────────────────────────────────────
#  Epoch watcher helpers
# ─────────────────────────────────────────────

def _next_primary_event_ms(records: dict) -> Optional[int]:
    """
    Return the soonest upcoming funding event on ANY individual exchange for
    ANY pair. The pre-scan uses this to wake up at the right moment — the
    scorer then determines which pairs have their PRIMARY (high-rate) exchange
    crediting at that time, regardless of whether the hedge also credits.
    """
    now_ms = time.time() * 1000
    earliest: Optional[int] = None

    seen: set = set()
    for pr in records.values():
        for ep in pr.exchange_pairs:
            for exchange, next_ts, interval_h in [
                (ep.exchange_a, ep.next_ts_a, ep.interval_a),
                (ep.exchange_b, ep.next_ts_b, ep.interval_b),
            ]:
                key = (exchange, pr.symbol)
                if key in seen or next_ts is None:
                    continue
                seen.add(key)
                ts = int(next_ts)
                step = int(interval_h * 3_600_000)
                while ts < now_ms:
                    ts += step
                if earliest is None or ts < earliest:
                    earliest = ts

    return earliest


def _run_pre_epoch_scan(
    event_ts_ms: int,
    min_exchanges: int,
    no_export: bool,
    export_info: Optional[dict],
) -> None:
    """
    Full Milestone 3 execution sequence triggered 15s before a joint funding event:

      T-15s  this function is called — fresh fetch + score
      T-10s  simulate entry on best passing opportunity (if any)
      T+0    funding epoch fires on exchanges
      T+EXIT_WAIT_SECONDS  fetch exit prices, simulate close, print P&L report
    """
    secs_to = max(0.0, (event_ts_ms - time.time() * 1000) / 1000)
    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print()
    print(Fore.YELLOW + Style.BRIGHT + "═" * 110 + Style.RESET_ALL)
    print(
        Fore.YELLOW + Style.BRIGHT +
        f"  PRE-EPOCH SCAN  |  {ts_str}  |  T-{secs_to:.0f}s  |  fetching fresh rates..."
        + Style.RESET_ALL
    )

    _, records, _, elapsed, _ = _fetch_and_build(min_exchanges, no_export, export_info)
    passing, failing = score_all_spikes(records)

    secs_to = max(0.0, (event_ts_ms - time.time() * 1000) / 1000)
    print(
        Fore.YELLOW + Style.BRIGHT +
        f"  PRE-EPOCH SCAN  |  fetch: {elapsed:.2f}s  |  "
        f"{Fore.GREEN}{len(passing)} passing{Fore.YELLOW}  |  T-{secs_to:.0f}s to epoch"
        + Style.RESET_ALL
    )
    print(Fore.YELLOW + Style.BRIGHT + "═" * 110 + Style.RESET_ALL)

    display_spike_opportunities(passing, failing)
    secs_to = max(0.0, (event_ts_ms - time.time() * 1000) / 1000)
    send_spike_opportunities(passing, failing, secs_to)

    if not passing:
        return

    # Only trade pairs whose own joint funding event is imminent (≤ 30s away).
    # The pre-scan may be triggered by a different exchange pair's event
    # (e.g. Binance+Hyperliquid at 12:00 UTC) while the highest-scoring pair
    # (e.g. Bybit+MEXC) doesn't credit until 16:00 UTC. Entering that trade
    # would mean no funding is collected at the 12:00 epoch.
    passing_now = [o for o in passing if o.seconds_to_funding <= 30.0]
    if not passing_now:
        print(
            f"\n  {Fore.YELLOW}No passing pairs have a joint funding event "
            f"within 30s of this trigger — skipping trade.{Style.RESET_ALL}"
        )
        return

    best = passing_now[0]
    secs_to = max(0.0, (event_ts_ms - time.time() * 1000) / 1000)
    print()
    print(
        Fore.GREEN + Style.BRIGHT +
        f"  ★  BEST: {best.symbol}  "
        f"L:{best.long_exchange}  S:{best.short_exchange}  "
        f"Score={best.score:+.4f}%  "
        f"Est.Profit=${best.estimated_profit_usd:.4f}  "
        f"T-{secs_to:.0f}s"
        + Style.RESET_ALL
    )

    # Send imminent-only opportunities to Telegram right before trade entry
    send_spike_opportunities(passing_now, [], secs_to)

    # ── T-10s: simulate entry ─────────────────────────────────────────────────
    wait_to_entry = max(0.0, (event_ts_ms - 10_000 - time.time() * 1000) / 1000)
    if wait_to_entry > 0:
        print(f"\n  {Fore.CYAN}Waiting {wait_to_entry:.1f}s for entry window (T-10s)...{Style.RESET_ALL}")
        time.sleep(wait_to_entry)

    secs_to = max(0.0, (event_ts_ms - time.time() * 1000) / 1000)
    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print()
    print(Fore.GREEN + Style.BRIGHT + "─" * 110 + Style.RESET_ALL)
    print(
        Fore.GREEN + Style.BRIGHT +
        f"  [SIM] ENTRY  |  {ts_str}  |  T-{secs_to:.0f}s  |  "
        f"{best.symbol}  L:{best.long_exchange} @ ${best.long_price:,.4f}  "
        f"S:{best.short_exchange} @ ${best.short_price:,.4f}"
        + Style.RESET_ALL
    )
    print(Fore.GREEN + Style.BRIGHT + "─" * 110 + Style.RESET_ALL)

    trade = simulate_entry(best, event_ts_ms)

    # ── Wait for funding epoch + exit window ──────────────────────────────────
    wait_to_exit = max(0.0, (event_ts_ms + EXIT_WAIT_SECONDS * 1000 - time.time() * 1000) / 1000)
    print(
        f"\n  {Fore.CYAN}Waiting {wait_to_exit:.0f}s for funding epoch "
        f"+ {EXIT_WAIT_SECONDS}s settle window...{Style.RESET_ALL}"
    )
    time.sleep(wait_to_exit)

    # ── T+EXIT_WAIT_SECONDS: fetch exit prices ────────────────────────────────
    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print()
    print(
        Fore.CYAN + Style.BRIGHT +
        f"  [SIM] EXIT FETCH  |  {ts_str}  |  fetching exit prices..."
        + Style.RESET_ALL
    )
    exit_all_data, _, _, _, _ = _fetch_and_build(min_exchanges, no_export=True, export_info=None)

    # Extract mark prices for the two relevant exchanges
    exit_long_price  = _extract_price(exit_all_data, best.symbol, best.long_exchange,  best.long_price)
    exit_short_price = _extract_price(exit_all_data, best.symbol, best.short_exchange, best.short_price)

    simulate_exit(trade, exit_long_price, exit_short_price)
    print_trade_report(trade)
    send_trade_report(trade)


def _extract_price(
    all_data: dict,
    symbol: str,
    exchange: str,
    fallback: float,
) -> float:
    """
    Pull the mark price for a symbol on a specific exchange from raw fetch data.
    Returns fallback if not found (e.g. exchange fetch failed).
    """
    for fd in all_data.get(exchange, []):
        if fd.symbol == symbol and fd.mark_price and fd.mark_price > 0:
            return fd.mark_price
    return fallback


def show_stats():
    stats = get_history_stats()
    print(Style.BRIGHT + "\n  Data History Stats\n" + Style.RESET_ALL)
    for name, info in stats.items():
        if name == "snapshots":
            print(f"  Snapshots       : {info['count']} files in {info['dir']}")
        else:
            print(f"  {name:20s}: {info['rows']:,} rows  ({info['size_kb']} KB)  {info['path']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Crypto Funding Rate Aggregator — Milestone 1")
    parser.add_argument("--once",           action="store_true", help="Run once and exit")
    parser.add_argument("--interval",       type=int, default=60, help="Refresh interval in seconds (default: 60)")
    parser.add_argument("--min-exchanges",  type=int, default=2,  help="Min exchanges a pair must be on (default: 2)")
    parser.add_argument("--no-export",      action="store_true",  help="Disable disk export (display only)")
    parser.add_argument("--no-score",       action="store_true",  help="Skip Milestone 2 opportunity scoring section")
    parser.add_argument("--stats",          action="store_true",  help="Show history stats and exit")
    args = parser.parse_args()

    if args.stats:
        show_stats()
        return

    try:
        run(
            interval=args.interval,
            once=args.once,
            min_exchanges=args.min_exchanges,
            no_export=args.no_export,
            no_score=args.no_score,
        )
    except KeyboardInterrupt:
        print("\n  Exiting. Goodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()