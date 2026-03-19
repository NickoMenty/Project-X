"""
Paper Trader — Milestone 3
==========================
Simulates entry and exit of single-epoch spike trades. No real orders are
placed. All fills use mark prices fetched from live exchange data.

Execution sequence
------------------
  T-10s  simulate_entry()    — record mark prices as fill prices
  T+0    funding epoch fires  — exchanges credit/debit funding
  T+EXIT_WAIT_SECONDS        — simulate_exit() fetches fresh prices, closes both legs

P&L breakdown
-------------
  Long leg price P&L  = (exit_long  - entry_long)  / entry_long  × position_size
  Short leg price P&L = (entry_short - exit_short) / entry_short × position_size
  Funding collected   = position_size × short_rate_pct / 100
  Funding paid        = position_size × long_rate_pct  / 100  (negative if paid)
  Net funding P&L     = funding_collected - funding_paid
  Total fees          = position_size × (long_fee_pct×2 + short_fee_pct×2) / 100
  Net P&L             = long_price_pnl + short_price_pnl + net_funding - total_fees
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from colorama import Fore, Style

from spike_scorer import SpikeOpportunity, POSITION_SIZE_USD


# ── Configurable constants ────────────────────────────────────────────────────

# Seconds to wait after the funding epoch fires before simulating exit.
# Gives the market a moment to settle post-funding.
EXIT_WAIT_SECONDS: int = 0


# ── Trade record ─────────────────────────────────────────────────────────────

@dataclass
class SimulatedTrade:
    # Identity
    symbol: str
    long_exchange: str
    short_exchange: str
    position_size_usd: float

    # Rates recorded at pre-scan time (used for funding P&L)
    long_rate_pct: float
    short_rate_pct: float

    # Fees (one-way %)
    long_fee_pct: float
    short_fee_pct: float

    # Funding epoch timestamp
    funding_ts_ms: int

    # Which leg is primary (fires for sure) and whether hedge also fires
    primary_is_short: bool = True   # True → short_exchange is primary
    hedge_credits: bool = False     # True → hedge leg's funding also counted

    # Entry (T-10s)
    entry_ts: float = 0.0
    entry_long_price: float = 0.0
    entry_short_price: float = 0.0

    # Exit (T+EXIT_WAIT_SECONDS)
    exit_ts: float = 0.0
    exit_long_price: float = 0.0
    exit_short_price: float = 0.0

    # P&L components (populated after exit)
    long_price_pnl_usd: float = 0.0
    short_price_pnl_usd: float = 0.0
    funding_collected_usd: float = 0.0
    funding_paid_usd: float = 0.0
    net_funding_usd: float = 0.0
    total_fee_usd: float = 0.0
    net_pnl_usd: float = 0.0

    completed: bool = False


# ── Core operations ───────────────────────────────────────────────────────────

def simulate_entry(opportunity: SpikeOpportunity, event_ts_ms: int) -> SimulatedTrade:
    """
    Record entry at current mark prices (simulated fill at T-10s).
    Funding rates are locked in from the pre-scan snapshot.
    event_ts_ms is the confirmed joint funding epoch timestamp from the scheduler
    (used instead of opportunity.next_funding_ts which may have already rolled over).
    """
    return SimulatedTrade(
        symbol=opportunity.symbol,
        long_exchange=opportunity.long_exchange,
        short_exchange=opportunity.short_exchange,
        position_size_usd=POSITION_SIZE_USD,
        long_rate_pct=opportunity.long_rate_pct,
        short_rate_pct=opportunity.short_rate_pct,
        long_fee_pct=opportunity.long_fee_pct,
        short_fee_pct=opportunity.short_fee_pct,
        funding_ts_ms=event_ts_ms,
        primary_is_short=opportunity.primary_is_short,
        hedge_credits=opportunity.hedge_credits,
        entry_ts=time.time(),
        entry_long_price=opportunity.long_price,
        entry_short_price=opportunity.short_price,
    )


def simulate_exit(
    trade: SimulatedTrade,
    exit_long_price: float,
    exit_short_price: float,
) -> SimulatedTrade:
    """
    Record exit prices and compute full P&L breakdown.
    Call this after funding has been credited and exit prices are fetched.
    """
    trade.exit_ts = time.time()
    trade.exit_long_price = exit_long_price
    trade.exit_short_price = exit_short_price

    size = trade.position_size_usd

    # ── Price P&L ─────────────────────────────────────────────────────────────
    long_pnl_pct  = (exit_long_price  - trade.entry_long_price)  / trade.entry_long_price  * 100
    short_pnl_pct = (trade.entry_short_price - exit_short_price) / trade.entry_short_price * 100
    trade.long_price_pnl_usd  = size * long_pnl_pct  / 100
    trade.short_price_pnl_usd = size * short_pnl_pct / 100

    # ── Funding P&L ───────────────────────────────────────────────────────────
    # Primary leg always fires. Hedge leg only fires if hedge_credits=True.
    # Short leg: rate>0 → shorts collect (+); rate<0 → shorts pay (-).
    # Long  leg: rate>0 → longs pay (-);     rate<0 → longs collect (+).
    short_funding_usd =  size * trade.short_rate_pct / 100
    long_funding_usd  = -size * trade.long_rate_pct  / 100

    if trade.primary_is_short:
        trade.funding_collected_usd = short_funding_usd
        trade.funding_paid_usd      = long_funding_usd if trade.hedge_credits else 0.0
    else:
        trade.funding_collected_usd = short_funding_usd if trade.hedge_credits else 0.0
        trade.funding_paid_usd      = long_funding_usd

    trade.net_funding_usd = trade.funding_collected_usd + trade.funding_paid_usd

    # ── Fees ──────────────────────────────────────────────────────────────────
    total_fee_pct = trade.long_fee_pct * 2.0 + trade.short_fee_pct * 2.0
    trade.total_fee_usd = size * total_fee_pct / 100

    # ── Net P&L ───────────────────────────────────────────────────────────────
    trade.net_pnl_usd = (
        trade.long_price_pnl_usd
        + trade.short_price_pnl_usd
        + trade.net_funding_usd
        - trade.total_fee_usd
    )
    trade.completed = True
    return trade


# ── Report display ────────────────────────────────────────────────────────────

def _ts(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%H:%M:%S.%f UTC")


def _pnl_color(val: float) -> str:
    color = Fore.GREEN if val >= 0 else Fore.RED
    return color + Style.BRIGHT + f"${val:+.4f}" + Style.RESET_ALL


def print_trade_report(trade: SimulatedTrade) -> None:
    """Print a full P&L breakdown for a completed simulated trade."""
    W = 72
    sep  = Style.BRIGHT + "─" * W + Style.RESET_ALL
    sep2 = Style.BRIGHT + "═" * W + Style.RESET_ALL

    print()
    print(sep2)
    print(Style.BRIGHT + f"  [SIM] PAPER TRADE REPORT — {trade.symbol}" + Style.RESET_ALL)
    print(sep2)

    # ── Entry ─────────────────────────────────────────────────────────────────
    print(f"\n  ENTRY  [{_ts(trade.entry_ts)}]  (simulated at T-10s)")
    print(f"    Long   {trade.long_exchange:<14}  entry price:  ${trade.entry_long_price:,.4f}")
    print(f"    Short  {trade.short_exchange:<14}  entry price:  ${trade.entry_short_price:,.4f}")
    print(f"    Position size: ${trade.position_size_usd:.2f} per leg")

    # ── Funding event ─────────────────────────────────────────────────────────
    epoch_str = _ts(trade.funding_ts_ms / 1000)
    print(f"\n  FUNDING EPOCH  [{epoch_str}]")
    short_fires = trade.primary_is_short or trade.hedge_credits
    long_fires  = (not trade.primary_is_short) or trade.hedge_credits

    size        = trade.position_size_usd
    short_usd   =  size * trade.short_rate_pct / 100
    long_usd    = -size * trade.long_rate_pct  / 100

    def _leg_str(usd: float, fires: bool, label_pos: str, label_neg: str) -> str:
        if not fires:
            return f"{Fore.YELLOW}(not fired — hedge only){Style.RESET_ALL}"
        color = Fore.GREEN if usd >= 0 else Fore.RED
        label = label_pos if usd >= 0 else label_neg
        return f"{color}{Style.BRIGHT}${usd:+.4f}{Style.RESET_ALL}  ({label})"

    print(
        f"    Short  {trade.short_exchange:<14}  rate: {trade.short_rate_pct:+.4f}%  "
        f"→  {_leg_str(short_usd, short_fires, 'collected', 'paid')}"
    )
    print(
        f"    Long   {trade.long_exchange:<14}  rate: {trade.long_rate_pct:+.4f}%  "
        f"→  {_leg_str(long_usd, long_fires, 'collected', 'paid')}"
    )
    print(f"    {'Net funding':38}  {_pnl_color(trade.net_funding_usd)}")

    # ── Exit ──────────────────────────────────────────────────────────────────
    print(f"\n  EXIT   [{_ts(trade.exit_ts)}]  ({EXIT_WAIT_SECONDS}s post-epoch)")
    long_pct  = (trade.exit_long_price  - trade.entry_long_price)  / trade.entry_long_price  * 100
    short_pct = (trade.entry_short_price - trade.exit_short_price) / trade.entry_short_price * 100
    print(
        f"    Long   {trade.long_exchange:<14}  "
        f"${trade.entry_long_price:,.4f} → ${trade.exit_long_price:,.4f}  "
        f"({long_pct:+.4f}%)  {_pnl_color(trade.long_price_pnl_usd)}"
    )
    print(
        f"    Short  {trade.short_exchange:<14}  "
        f"${trade.entry_short_price:,.4f} → ${trade.exit_short_price:,.4f}  "
        f"({short_pct:+.4f}%)  {_pnl_color(trade.short_price_pnl_usd)}"
    )
    print(
        f"    {'Price P&L combined':38}  "
        f"{_pnl_color(trade.long_price_pnl_usd + trade.short_price_pnl_usd)}"
    )

    # ── Fees ──────────────────────────────────────────────────────────────────
    print(f"\n  FEES")
    print(
        f"    Long   {trade.long_exchange:<14}  "
        f"{trade.long_fee_pct:.3f}% × 2 (o+c) = "
        f"{Fore.RED}${trade.position_size_usd * trade.long_fee_pct * 2 / 100:.4f}{Style.RESET_ALL}"
    )
    print(
        f"    Short  {trade.short_exchange:<14}  "
        f"{trade.short_fee_pct:.3f}% × 2 (o+c) = "
        f"{Fore.RED}${trade.position_size_usd * trade.short_fee_pct * 2 / 100:.4f}{Style.RESET_ALL}"
    )
    print(f"    {'Total fees':38}  {Fore.RED}${trade.total_fee_usd:.4f}{Style.RESET_ALL}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(sep)
    price_combined = trade.long_price_pnl_usd + trade.short_price_pnl_usd
    print(f"  {'Price P&L (long + short):':<38}  {_pnl_color(price_combined)}")
    print(f"  {'Net funding received:':<38}  {_pnl_color(trade.net_funding_usd)}")
    print(f"  {'Total fees paid:':<38}  {Fore.RED}${trade.total_fee_usd:.4f}{Style.RESET_ALL}")
    print(sep)
    net_color = Fore.GREEN if trade.net_pnl_usd >= 0 else Fore.RED
    result    = "PROFIT" if trade.net_pnl_usd >= 0 else "LOSS"
    print(
        f"  {'NET P&L:':<38}  "
        f"{net_color}{Style.BRIGHT}${trade.net_pnl_usd:+.4f}  [{result}]{Style.RESET_ALL}"
    )
    print(sep2)
    print()
