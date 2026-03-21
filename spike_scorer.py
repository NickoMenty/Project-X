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
POSITION_SIZE_USD: float = 500.0

# Taker fee as % of notional, one-way, per exchange.
# Round-trip per leg = 2×.  Total both legs = fee_A×2 + fee_B×2.
TAKER_FEE_PCT: Dict[str, float] = {
    "Bybit":       0.100,   # confirmed: 0.1000% non-VIP futures taker
    "Binance":     0.050,   # standard non-VIP futures taker
    "Hyperliquid": 0.035,   # published non-VIP perps taker
    "AsterDex":    0.050,   # ⚠ verify — using conservative default
    "Bitget":      0.060,   # standard non-VIP futures taker
    "OKX":         0.050,   # standard non-VIP futures taker
    "KuCoin":      0.060,   # standard non-VIP futures taker
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

    # Trade direction meta
    primary_is_short: bool      # True = short_exchange holds next_funding_ts; False = long does

    # Hedge behaviour
    hedge_credits: bool         # True if hedge exchange also credits at this epoch

    # Individual fees (for display)
    long_fee_pct: float
    short_fee_pct: float

    # Prices at time of scoring
    long_price: float
    short_price: float
    price_spread_pct: float     # ABS price deviation between the two legs (%)

    # Timing
    next_funding_ts: int        # Unix ms — primary (trigger) exchange funding event
    seconds_to_funding: float   # seconds from now to primary event
    hedge_funding_ts: int       # Unix ms — hedge exchange next funding event

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

def _score_with_primary(symbol: str, ep: ExchangePair, use_a_as_primary: bool) -> SpikeOpportunity:
    """
    Score one direction of an ExchangePair for single-epoch trading.

    use_a_as_primary=True  → exchange_a's next event is the trigger.
                             Position on a = receiving side (short if rate_a>0, long if rate_a<0).
                             Position on b = opposite (hedge, delta neutral).
    use_a_as_primary=False → same logic but exchange_b drives the trigger.

    Calling both directions per pair catches opportunities where the "worse"
    exchange (by spread) has an imminent event and the single-epoch gross
    still exceeds fees.
    """
    fail_reasons: List[str] = []
    now_ms = time.time() * 1000
    tol_ms = ALIGNMENT_TOLERANCE_SECONDS * 1000

    # ── Assign primary / hedge ────────────────────────────────────────────────
    if use_a_as_primary:
        p_ex, p_rate, p_next, p_step = ep.exchange_a, ep.rate_a_pct, ep.next_ts_a, int(ep.interval_a * 3_600_000)
        p_price = ep.price_a
        h_ex, h_rate, h_next, h_step = ep.exchange_b, ep.rate_b_pct, ep.next_ts_b, int(ep.interval_b * 3_600_000)
        h_price = ep.price_b
    else:
        p_ex, p_rate, p_next, p_step = ep.exchange_b, ep.rate_b_pct, ep.next_ts_b, int(ep.interval_b * 3_600_000)
        p_price = ep.price_b
        h_ex, h_rate, h_next, h_step = ep.exchange_a, ep.rate_a_pct, ep.next_ts_a, int(ep.interval_a * 3_600_000)
        h_price = ep.price_a

    # ── Position: be on the RECEIVING side of the primary exchange ────────────
    # rate > 0 → shorts receive → SHORT primary, LONG hedge
    # rate < 0 → longs receive  → LONG  primary, SHORT hedge
    if p_rate >= 0:
        short_ex, short_rate, short_price = p_ex, p_rate, p_price
        long_ex,  long_rate,  long_price  = h_ex, h_rate, h_price
    else:
        long_ex,  long_rate,  long_price  = p_ex, p_rate, p_price
        short_ex, short_rate, short_price = h_ex, h_rate, h_price

    # ── Fees ──────────────────────────────────────────────────────────────────
    long_fee  = _fee(long_ex)
    short_fee = _fee(short_ex)
    total_fee = long_fee * 2.0 + short_fee * 2.0

    # ── Advance timestamps past now ───────────────────────────────────────────
    ts_primary: Optional[int] = None
    ts_hedge:   Optional[int] = None
    if p_next is not None:
        ts_primary = int(p_next)
        while ts_primary < now_ms: ts_primary += p_step
    if h_next is not None:
        ts_hedge = int(h_next)
        while ts_hedge < now_ms: ts_hedge += h_step

    # ── Timing: align to primary event ───────────────────────────────────────
    if ts_primary is None:
        fail_reasons.append(f"{p_ex} missing next_funding_ts for this pair")
        alignment_ts = ts_hedge or 0
        secs_to = float("inf")
    else:
        alignment_ts = ts_primary
        secs_to = max(0.0, (alignment_ts - now_ms) / 1000.0)

    # Filter 1: primary funding must be within the next 1 hour
    if secs_to > 3600:
        fail_reasons.append(f"primary funding > 1h away (T-{secs_to/60:.0f}m)")

    # ── Gross funding capture ─────────────────────────────────────────────────
    # Primary always contributes |p_rate| (we chose the receiving side).
    gross_primary = abs(p_rate)

    # Hedge contributes only if it credits near the same epoch.
    hedge_credits = (
        ts_primary is not None
        and ts_hedge is not None
        and abs(ts_hedge - ts_primary) <= tol_ms
    )
    if hedge_credits:
        # Our hedge position: LONG hedge if p_rate>=0 (we're short primary),
        #                     SHORT hedge if p_rate<0  (we're long primary).
        # LONG  hedge receives when h_rate < 0  → hedge_contribution = -h_rate
        # SHORT hedge receives when h_rate > 0  → hedge_contribution =  h_rate
        hedge_contribution = -h_rate if p_rate >= 0 else h_rate
    else:
        hedge_contribution = 0.0

    gross_pct = gross_primary + hedge_contribution
    score     = gross_pct - total_fee

    funding_spread_pct = abs(ep.rate_a_pct - ep.rate_b_pct)

    # ── Filter 2: Price spread ────────────────────────────────────────────────
    if long_price > 0 and short_price > 0:
        price_spread_pct = abs(long_price - short_price) / min(long_price, short_price) * 100.0
    else:
        price_spread_pct = 0.0
        fail_reasons.append(f"missing price data (long={long_price}, short={short_price})")

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
        primary_is_short=(p_rate >= 0),
        hedge_credits=hedge_credits,
        long_fee_pct=long_fee,
        short_fee_pct=short_fee,
        long_price=long_price,
        short_price=short_price,
        price_spread_pct=price_spread_pct,
        next_funding_ts=alignment_ts,
        seconds_to_funding=secs_to,
        hedge_funding_ts=ts_hedge or 0,
        estimated_funding_usd=est_funding_usd,
        estimated_fee_usd=est_fee_usd,
        estimated_profit_usd=est_profit_usd,
        passes_all=len(fail_reasons) == 0,
        fail_reasons=fail_reasons,
    )


def score_spike(symbol: str, ep: ExchangePair) -> List[SpikeOpportunity]:
    """
    Score both trigger directions for an ExchangePair.
    Returns two SpikeOpportunity objects — one per exchange as primary.
    """
    return [
        _score_with_primary(symbol, ep, use_a_as_primary=True),
        _score_with_primary(symbol, ep, use_a_as_primary=False),
    ]


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

    # Deduplicate by (symbol, long_exchange, short_exchange) — both directions
    # can produce identical assignments when both exchanges fire together.
    # Keep the higher-scored entry for each unique trade direction.
    seen: Dict[tuple, SpikeOpportunity] = {}

    for symbol, pr in records.items():
        for ep in pr.exchange_pairs:
            for opp in score_spike(symbol, ep):
                key = (opp.symbol, opp.long_exchange, opp.short_exchange)
                existing = seen.get(key)
                if existing is None or opp.score > existing.score:
                    seen[key] = opp

    for opp in seen.values():
        (passing if opp.passes_all else failing).append(opp)

    passing.sort(key=lambda o: -o.score)
    failing.sort(key=lambda o: -o.funding_spread_pct)

    return passing, failing
