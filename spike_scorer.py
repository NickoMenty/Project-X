"""
Spike Scorer — Single-Epoch Funding Arbitrage
==============================================
Scores every exchange pair for single-epoch funding spike trades.

Strategy
--------
Enter a delta-neutral position (long one exchange, short the other)
just before a joint funding event. Collect one epoch of net funding.
Exit immediately after funding is credited.

Profitability condition
-----------------------
  ABS(rate_A% - rate_B%) > fee_A×2 + fee_B×2

  The funding spread over a single epoch must exceed the round-trip
  taker fees on both legs (open + close = 2× per exchange).

Score
-----
  score = funding_spread_pct - total_fee_pct

  Higher is better. Only positive scores are profitable.

Filters (all three must pass)
------------------------------
  1. Timestamp alignment — next_funding_ts on both exchanges must be
     within ALIGNMENT_TOLERANCE_SECONDS of each other.
     ("Funding execution time B must equal Funding execution time A")
  2. Price spread       — mark price difference between the two legs
     must be < MAX_PRICE_SPREAD_PCT.
  3. Positive score     — funding spread must exceed total fees.

Configurable constants
----------------------
  POSITION_SIZE_USD         — USD size of each leg
  TAKER_FEE_PCT             — per-exchange taker fee dict (%)
  MAX_PRICE_SPREAD_PCT      — maximum allowed price gap between legs
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pair_engine import ExchangePair, PairRecord


# ── Configurable constants ────────────────────────────────────────────────────

# USD size of each leg — used for estimated P&L only, does not affect scoring
POSITION_SIZE_USD: float = 1000.0

# Taker fee as % of notional, one-way, per exchange.
# Round-trip per leg = 2×.  Total both legs = fee_A×2 + fee_B×2.
TAKER_FEE_PCT: Dict[str, float] = {
    "Bybit":       0.100,   # confirmed: 0.1000% non-VIP futures taker
    "Binance":     0.050,   # standard non-VIP futures taker
    "Hyperliquid": 0.035,   # published non-VIP perps taker
    "AsterDex":    0.050,   # ⚠ verify — using conservative default
    "Lighter":     0.030,   # ⚠ verify — using conservative default
    "MEXC":        0.040,   # confirmed: 0.04% futures taker
    "Bitget":      0.060,   # standard non-VIP futures taker
}
DEFAULT_TAKER_FEE_PCT: float = 0.050

# Max allowed absolute price deviation between the two legs (%)
MAX_PRICE_SPREAD_PCT: float = 2.0

# Max allowed difference between the two per-pair next_funding_ts values (seconds).
# Handles minor API clock skew between exchanges.
ALIGNMENT_TOLERANCE_SECONDS: float = 60.0



# ── Helpers ───────────────────────────────────────────────────────────────────

def _fee(exchange: str) -> float:
    """One-way taker fee % for an exchange."""
    return TAKER_FEE_PCT.get(exchange, DEFAULT_TAKER_FEE_PCT)


def _total_fee_pct(ex_a: str, ex_b: str) -> float:
    """
    Total round-trip fee as % of position for both legs combined.
      open_A + close_A + open_B + close_B = fee_A×2 + fee_B×2
    """
    return _fee(ex_a) * 2.0 + _fee(ex_b) * 2.0


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class SpikeOpportunity:
    """
    Full scored assessment of one ExchangePair for a single-epoch spike trade.

    The primary exchange (short_exchange) is the one about to credit funding —
    its event must be imminent for the trade to execute.
    The hedge exchange (long_exchange) neutralises delta. It may or may not
    credit funding at the same time (hedge_credits flag).

    gross_pct = short_rate_pct                    (primary only)
              = short_rate_pct - long_rate_pct    (both credit, current model)
    score     = gross_pct - total_fee_pct
    """
    symbol: str
    exchange_pair: ExchangePair

    # Trade direction
    long_exchange: str          # go LONG here — delta hedge
    short_exchange: str         # go SHORT here — primary funding collector

    # Rates at the time of scoring (%, per native epoch)
    long_rate_pct: float
    short_rate_pct: float

    # Core numbers
    gross_pct: float            # actual expected funding capture this epoch
    funding_spread_pct: float   # full ABS(short_rate - long_rate) — reference only
    total_fee_pct: float        # fee_long×2 + fee_short×2 — round-trip cost
    score: float                # gross_pct - total_fee_pct  (>0 = profitable)

    # Hedge behaviour
    hedge_credits: bool         # True if hedge exchange also credits at this epoch

    # Individual fees (for display)
    long_fee_pct: float
    short_fee_pct: float

    # Prices at time of scoring
    long_price: float
    short_price: float
    price_spread_pct: float     # ABS price deviation between the two legs (%)

    # Timing — based on primary (short) exchange epoch
    next_funding_ts: int        # Unix ms — primary funding event
    seconds_to_funding: float   # seconds from now to primary event

    # USD estimates at POSITION_SIZE_USD
    estimated_funding_usd: float
    estimated_fee_usd: float
    estimated_profit_usd: float

    # Pass/fail
    passes_all: bool = True
    fail_reasons: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        status = "✓" if self.passes_all else "✗"
        return (
            f"[{status}] {self.symbol:10s} | "
            f"L:{self.long_exchange[:8]:<8} S:{self.short_exchange[:8]:<8} | "
            f"spread={self.funding_spread_pct:+.4f}% "
            f"fees={self.total_fee_pct:.4f}% "
            f"score={self.score:+.4f}% | "
            f"T-{self.seconds_to_funding:.0f}s"
        )


# ── Per-pair scoring ──────────────────────────────────────────────────────────

def score_spike(symbol: str, ep: ExchangePair) -> SpikeOpportunity:
    """
    Score a single ExchangePair for single-epoch spike trading.
    Always returns a SpikeOpportunity — check .passes_all for actionability.
    """
    fail_reasons: List[str] = []
    now_ms = time.time() * 1000

    # ── Trade direction ───────────────────────────────────────────────────────
    # Short the higher-rate side (collect), long the lower-rate side (pay less).
    # net_ab > 0 means rate_a > rate_b → short A, long B.
    net_ab = ep.rate_a_pct - ep.rate_b_pct
    if net_ab >= 0:
        long_ex,    short_ex    = ep.exchange_b, ep.exchange_a
        long_rate,  short_rate  = ep.rate_b_pct, ep.rate_a_pct
        long_price, short_price = ep.price_b,    ep.price_a
        long_next,  short_next  = ep.next_ts_b,  ep.next_ts_a
        step_short = int(ep.interval_a * 3_600_000)
        step_long  = int(ep.interval_b * 3_600_000)
    else:
        long_ex,    short_ex    = ep.exchange_a, ep.exchange_b
        long_rate,  short_rate  = ep.rate_a_pct, ep.rate_b_pct
        long_price, short_price = ep.price_a,    ep.price_b
        long_next,  short_next  = ep.next_ts_a,  ep.next_ts_b
        step_short = int(ep.interval_b * 3_600_000)
        step_long  = int(ep.interval_a * 3_600_000)

    funding_spread_pct = abs(net_ab)

    # ── Fees ──────────────────────────────────────────────────────────────────
    long_fee  = _fee(long_ex)
    short_fee = _fee(short_ex)
    total_fee = long_fee * 2.0 + short_fee * 2.0

    # ── Filter 1: Primary (short) exchange must be imminent ───────────────────
    # The hedge (long) exchange does NOT need to align — it only contributes
    # bonus funding if it happens to credit at the same time.
    alignment_ts: int = 0
    secs_to: float = float("inf")
    hedge_credits: bool = False

    if short_next is None:
        fail_reasons.append(f"{short_ex} missing next_funding_ts for this pair")
    else:
        ts_short = int(short_next)
        while ts_short < now_ms:
            ts_short += step_short
        alignment_ts = ts_short
        secs_to = max(0.0, (alignment_ts - now_ms) / 1000.0)

        # Check if hedge also credits near the same time (bonus — not required)
        if long_next is not None:
            ts_long = int(long_next)
            while ts_long < now_ms:
                ts_long += step_long
            if abs(ts_long - ts_short) <= ALIGNMENT_TOLERANCE_SECONDS * 1000:
                hedge_credits = True

    # ── Gross funding capture ─────────────────────────────────────────────────
    # Primary (short) always contributes short_rate_pct.
    # Hedge contributes only if it credits at the same epoch.
    gross_pct = short_rate
    if hedge_credits:
        gross_pct -= long_rate  # long_rate > 0 → we pay; long_rate < 0 → we receive

    score = gross_pct - total_fee

    # ── Filter 2: Price spread ────────────────────────────────────────────────
    if long_price > 0 and short_price > 0:
        price_spread_pct = abs(long_price - short_price) / min(long_price, short_price) * 100.0
    else:
        price_spread_pct = 0.0

    if price_spread_pct >= MAX_PRICE_SPREAD_PCT:
        fail_reasons.append(
            f"price spread {price_spread_pct:.3f}% >= {MAX_PRICE_SPREAD_PCT:.1f}% max"
        )

    # ── Filter 3: Gross must exceed total fees ────────────────────────────────
    if score <= 0:
        fail_reasons.append(
            f"gross {gross_pct:.4f}% "
            f"({'primary+hedge' if hedge_credits else 'primary only'}) "
            f"<= total fees {total_fee:.4f}%"
        )

    # ── USD estimates ─────────────────────────────────────────────────────────
    est_funding_usd = POSITION_SIZE_USD * gross_pct / 100.0
    est_fee_usd     = POSITION_SIZE_USD * total_fee / 100.0
    est_profit_usd  = POSITION_SIZE_USD * score / 100.0

    return SpikeOpportunity(
        symbol=symbol,
        exchange_pair=ep,
        long_exchange=long_ex,
        short_exchange=short_ex,
        long_rate_pct=long_rate,
        short_rate_pct=short_rate,
        gross_pct=gross_pct,
        funding_spread_pct=funding_spread_pct,
        total_fee_pct=total_fee,
        score=score,
        hedge_credits=hedge_credits,
        long_fee_pct=long_fee,
        short_fee_pct=short_fee,
        long_price=long_price,
        short_price=short_price,
        price_spread_pct=price_spread_pct,
        next_funding_ts=alignment_ts,
        seconds_to_funding=secs_to,
        estimated_funding_usd=est_funding_usd,
        estimated_fee_usd=est_fee_usd,
        estimated_profit_usd=est_profit_usd,
        passes_all=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )


# ── Batch scoring ─────────────────────────────────────────────────────────────

def score_all_spikes(
    records: Dict[str, "PairRecord"],
) -> Tuple[List[SpikeOpportunity], List[SpikeOpportunity]]:
    """
    Score every ExchangePair in every PairRecord.

    Returns:
        passing — sorted by score descending (best first)
        failing — sorted by funding_spread_pct descending (near-misses first)
    """
    passing: List[SpikeOpportunity] = []
    failing: List[SpikeOpportunity] = []

    for symbol, pr in records.items():
        for ep in pr.exchange_pairs:
            opp = score_spike(symbol, ep)
            (passing if opp.passes_all else failing).append(opp)

    passing.sort(key=lambda o: -o.score)
    failing.sort(key=lambda o: -o.funding_spread_pct)

    return passing, failing
