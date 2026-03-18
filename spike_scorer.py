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
from funding_schedule import next_joint_event_ms, exchanges_align


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
    """
    symbol: str
    exchange_pair: ExchangePair

    # Trade direction
    long_exchange: str          # go LONG here (lower effective rate — pay less)
    short_exchange: str         # go SHORT here (higher effective rate — collect more)

    # Rates at the time of scoring (%, per native epoch)
    long_rate_pct: float        # rate paid by longs on long_exchange
    short_rate_pct: float       # rate paid by longs on short_exchange (we collect as short)

    # Core numbers
    funding_spread_pct: float   # ABS(short_rate_pct - long_rate_pct) — gross capture
    total_fee_pct: float        # fee_long×2 + fee_short×2 — round-trip cost
    score: float                # funding_spread_pct - total_fee_pct  (>0 = profitable)

    # Individual fees (for display)
    long_fee_pct: float
    short_fee_pct: float

    # Prices at time of scoring
    long_price: float
    short_price: float
    price_spread_pct: float     # ABS price deviation between the two legs (%)

    # Timing
    next_funding_ts: int        # Unix ms — the joint funding event
    seconds_to_funding: float   # seconds from now to that event

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
    else:
        long_ex,    short_ex    = ep.exchange_a, ep.exchange_b
        long_rate,  short_rate  = ep.rate_a_pct, ep.rate_b_pct
        long_price, short_price = ep.price_a,    ep.price_b
        long_next,  short_next  = ep.next_ts_a,  ep.next_ts_b

    funding_spread_pct = abs(net_ab)

    # ── Fees ──────────────────────────────────────────────────────────────────
    long_fee  = _fee(long_ex)
    short_fee = _fee(short_ex)
    total_fee = long_fee * 2.0 + short_fee * 2.0
    score     = funding_spread_pct - total_fee

    # ── Filter 1: Schedule alignment (hardcoded UTC funding windows) ─────────
    # Uses canonical exchange schedules — not API-returned timestamps,
    # which can be stale or already rolled over near an epoch boundary.
    alignment_ts: int = 0
    secs_to: float = float("inf")

    joint_ts = next_joint_event_ms(long_ex, short_ex)
    if joint_ts is None:
        fail_reasons.append(
            f"{long_ex} and {short_ex} share no common funding windows"
        )
    else:
        alignment_ts = joint_ts
        secs_to = max(0.0, (alignment_ts - now_ms) / 1000.0)

    # ── Filter 2: Price spread ────────────────────────────────────────────────
    if long_price > 0 and short_price > 0:
        price_spread_pct = abs(long_price - short_price) / min(long_price, short_price) * 100.0
    else:
        price_spread_pct = 0.0

    if price_spread_pct >= MAX_PRICE_SPREAD_PCT:
        fail_reasons.append(
            f"price spread {price_spread_pct:.3f}% >= {MAX_PRICE_SPREAD_PCT:.1f}% max"
        )

    # ── Filter 3: Positive score ──────────────────────────────────────────────
    if score <= 0:
        fail_reasons.append(
            f"funding spread {funding_spread_pct:.4f}% "
            f"<= total fees {total_fee:.4f}%"
        )

    # ── USD estimates ─────────────────────────────────────────────────────────
    est_funding_usd = POSITION_SIZE_USD * funding_spread_pct / 100.0
    est_fee_usd     = POSITION_SIZE_USD * total_fee / 100.0
    est_profit_usd  = POSITION_SIZE_USD * score / 100.0

    return SpikeOpportunity(
        symbol=symbol,
        exchange_pair=ep,
        long_exchange=long_ex,
        short_exchange=short_ex,
        long_rate_pct=long_rate,
        short_rate_pct=short_rate,
        funding_spread_pct=funding_spread_pct,
        total_fee_pct=total_fee,
        score=score,
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
