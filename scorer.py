"""
Opportunity Scoring Engine — Milestone 2
=========================================
Scores every ExchangePair produced by the pair engine and ranks them by
risk-adjusted expected return per epoch.

Three sequential checks per pair:

  1. Alignment check   — finds the next moment both exchanges fund within a
                         time tolerance. Only enter when both clocks tick together.
  2. Spread check      — correctly normalises funding rates across different
                         intervals using LCM-based math, then filters by APR floor.
  3. Entry cost check  — verifies that projected funding income (over
                         ASSUMED_CLOSE_PERIODS epochs) covers the price-leg
                         disadvantage plus round-trip taker fees.

Why LCM normalisation matters
------------------------------
raw_spread = rate_a - rate_b conflates rates expressed over *different* periods.
Example: Exchange A pays 0.01% every 1 h, Exchange B pays 0.05% every 8 h.

  - Raw spread = 0.01% - 0.05% = -0.04%  ← looks like B wins
  - But per 8-h LCM: A credits 8× → 8 × 0.01% = 0.08%; B credits 1× → 0.05%
  - Net over 8 h = 0.08% − 0.05% = +0.03%  ← A wins!

Short A, Long B → collect 0.03% per 8 h → APR ≈ 32.8%

The existing _fmt_spread in main.py uses a shortcut (min_interval, constant
raw_spread) that gives correct answers only when both intervals are identical.
This scorer always computes the LCM path.

Outputs
-------
score_all(records) → (passing: List[OpportunityScore], failing: List[OpportunityScore])
"""

import time
from dataclasses import dataclass, field
from math import gcd
from typing import Dict, List, Optional, Tuple

from exchanges.base import FundingData
from pair_engine import ExchangePair, PairRecord


# ── Tunable constants ──────────────────────────────────────────────────────────

# Alignment: how close (in minutes) two funding events must be to count as aligned
ALIGNMENT_TOLERANCE_MINUTES: float = 10.0

# Alignment: how far ahead (in hours) to scan for the next joint funding event
ALIGNMENT_LOOKAHEAD_HOURS: float = 48.0

# Spread: minimum annualised net yield to bother scoring further
MIN_NET_APR_PCT: float = 1.0

# Entry cost: assume we hold for this many LCM epochs when projecting income
ASSUMED_CLOSE_PERIODS: int = 3

# Fees: one-way taker fee per leg (%) — round trip = 4 × this (enter + exit × 2 legs)
TAKER_FEE_PCT: float = 0.05


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _lcm_hours(interval_a: float, interval_b: float) -> float:
    """LCM of two interval values (hours). Inputs are in {1, 2, 4, 8}."""
    a = max(1, int(round(interval_a)))
    b = max(1, int(round(interval_b)))
    return float(a * b // gcd(a, b))


def _net_per_lcm_short_a(
    rate_a: float, interval_a: float,
    rate_b: float, interval_b: float,
    lcm_h: float,
) -> float:
    """
    Net funding collected per LCM period when SHORT on A and LONG on B.

    For each exchange X with rate r:
      Short position receives  +r  per epoch  (positive r = longs pay shorts)
      Long  position pays      +r  per epoch  (positive r = longs pay shorts → we pay)

    Therefore:
      net = rate_a * (lcm / interval_a)  [collect from short A]
          - rate_b * (lcm / interval_b)  [pay on long B]
    """
    return rate_a * (lcm_h / interval_a) - rate_b * (lcm_h / interval_b)


def _find_next_alignment(
    next_ts_a: Optional[int],
    interval_a: float,
    next_ts_b: Optional[int],
    interval_b: float,
    tolerance_minutes: float = ALIGNMENT_TOLERANCE_MINUTES,
    lookahead_hours: float = ALIGNMENT_LOOKAHEAD_HOURS,
) -> Optional[int]:
    """
    Return the earliest future Unix-ms timestamp at which exchange A and
    exchange B both have a funding event within ±tolerance_minutes of each other.

    Returns None if:
      - Either next_ts is missing (connector didn't provide it)
      - No alignment found within lookahead_hours

    Algorithm: two-pointer walk on both sorted event sequences. O(lookahead/min_interval).
    """
    if next_ts_a is None or next_ts_b is None:
        return None

    now_ms = int(time.time() * 1000)
    tol_ms = int(tolerance_minutes * 60_000)
    deadline_ms = now_ms + int(lookahead_hours * 3_600_000)
    step_a_ms = int(interval_a * 3_600_000)
    step_b_ms = int(interval_b * 3_600_000)

    # Advance both pointers to their first *future* events
    ts_a = int(next_ts_a)
    while ts_a < now_ms:
        ts_a += step_a_ms

    ts_b = int(next_ts_b)
    while ts_b < now_ms:
        ts_b += step_b_ms

    # Two-pointer scan
    while ts_a <= deadline_ms and ts_b <= deadline_ms:
        diff = ts_a - ts_b
        if abs(diff) <= tol_ms:
            return ts_a          # Return A's timestamp as the canonical event time
        elif diff > 0:           # A is ahead — advance B
            ts_b += step_b_ms
        else:                    # B is ahead — advance A
            ts_a += step_a_ms

    return None


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class AlignmentResult:
    """Outcome of the funding-timestamp alignment check."""
    passes: bool
    alignment_interval_hours: float      # LCM of the two funding intervals
    next_alignment_ts: Optional[int]     # Unix ms of next joint funding event
    minutes_to_alignment: Optional[float]
    fail_reason: Optional[str] = None


@dataclass
class SpreadResult:
    """
    LCM-normalised funding spread for the better trade direction.

    long_exchange  — exchange where we go LONG (lower effective rate)
    short_exchange — exchange where we go SHORT (higher effective rate)
    """
    passes: bool

    long_exchange: str
    short_exchange: str
    long_interval_hours: float
    short_interval_hours: float
    long_rate_per_epoch: float        # Rate we *pay* per native epoch on long leg
    short_rate_per_epoch: float       # Rate we *collect* per native epoch on short leg

    net_per_lcm: float                # Net decimal spread per LCM period
    net_per_lcm_pct: float            # net_per_lcm * 100
    net_apr_pct: float                # Annualised net yield in %

    lcm_hours: float
    short_events_per_lcm: float       # Short exchange credits this many times per LCM
    long_events_per_lcm: float        # Long exchange debits this many times per LCM

    fail_reason: Optional[str] = None


@dataclass
class EntryCostResult:
    """
    Entry-price disadvantage + fees vs. projected funding income check.

    entry_disadvantage_pct — price premium we pay to open the long leg
                             relative to where we short; 0 if long is cheaper
    round_trip_fee_pct     — 4 × TAKER_FEE_PCT (enter + exit, two legs)
    total_entry_cost_pct   — sum of the above two
    projected_funding_pct  — net_per_lcm_pct × ASSUMED_CLOSE_PERIODS
    periods_to_breakeven   — epochs needed to cover total_entry_cost
    """
    passes: bool

    long_price: float
    short_price: float
    entry_disadvantage_pct: float
    round_trip_fee_pct: float
    total_entry_cost_pct: float
    projected_funding_pct: float
    periods_to_breakeven: float

    fail_reason: Optional[str] = None


@dataclass
class OpportunityScore:
    """
    Full scored assessment of one ExchangePair. This is the canonical output
    of the scoring engine and the input to Milestone 3 paper trading.
    """
    symbol: str
    exchange_pair: ExchangePair

    alignment: AlignmentResult
    spread: SpreadResult
    entry_cost: EntryCostResult

    passes_all_checks: bool
    fail_reasons: List[str] = field(default_factory=list)

    # Composite score: higher = better. 0.0 if any check fails.
    score: float = 0.0

    # Convenience accessors
    def long_exchange(self) -> str:
        return self.spread.long_exchange

    def short_exchange(self) -> str:
        return self.spread.short_exchange

    def net_apr_pct(self) -> float:
        return self.spread.net_apr_pct

    def minutes_to_alignment(self) -> Optional[float]:
        return self.alignment.minutes_to_alignment

    def __repr__(self) -> str:
        status = "✓" if self.passes_all_checks else "✗"
        mins = self.alignment.minutes_to_alignment
        align_str = f"{mins:.0f}m" if mins is not None else "unknown"
        return (
            f"[{status}] {self.symbol:10s} | "
            f"L:{self.spread.long_exchange[:8]:<8} S:{self.spread.short_exchange[:8]:<8} | "
            f"APR={self.spread.net_apr_pct:+.2f}% | align_in={align_str} | "
            f"score={self.score:.4f}"
        )


# ── Per-check logic ────────────────────────────────────────────────────────────

def _check_alignment(ep: ExchangePair) -> AlignmentResult:
    lcm_h = _lcm_hours(ep.interval_a, ep.interval_b)

    alignment_ts = _find_next_alignment(
        ep.next_ts_a, ep.interval_a,
        ep.next_ts_b, ep.interval_b,
    )

    if alignment_ts is None:
        if ep.next_ts_a is None or ep.next_ts_b is None:
            reason = "next_funding_ts missing on one or both exchanges"
        else:
            reason = f"no joint funding event within {ALIGNMENT_LOOKAHEAD_HOURS:.0f}h lookahead"
        return AlignmentResult(
            passes=False,
            alignment_interval_hours=lcm_h,
            next_alignment_ts=None,
            minutes_to_alignment=None,
            fail_reason=reason,
        )

    now_ms = time.time() * 1000
    mins_to = max(0.0, (alignment_ts - now_ms) / 60_000)

    return AlignmentResult(
        passes=True,
        alignment_interval_hours=lcm_h,
        next_alignment_ts=alignment_ts,
        minutes_to_alignment=mins_to,
    )


def _check_spread(ep: ExchangePair) -> SpreadResult:
    """
    Compute net spread for both trade directions, pick the one with positive net.
    Fails if net <= 0 or net APR < MIN_NET_APR_PCT.
    """
    lcm_h = _lcm_hours(ep.interval_a, ep.interval_b)
    a_events = lcm_h / ep.interval_a
    b_events = lcm_h / ep.interval_b

    # net_ab > 0 → short A, long B
    net_ab = _net_per_lcm_short_a(ep.rate_a, ep.interval_a, ep.rate_b, ep.interval_b, lcm_h)
    # net_ba = -net_ab → short B, long A

    if net_ab >= 0:
        net = net_ab
        long_ex, short_ex         = ep.exchange_b, ep.exchange_a
        long_rate, short_rate     = ep.rate_b, ep.rate_a
        long_int, short_int       = ep.interval_b, ep.interval_a
        long_events, short_events = b_events, a_events
    else:
        net = -net_ab  # net_ba
        long_ex, short_ex         = ep.exchange_a, ep.exchange_b
        long_rate, short_rate     = ep.rate_a, ep.rate_b
        long_int, short_int       = ep.interval_a, ep.interval_b
        long_events, short_events = a_events, b_events

    net_apr_pct = net * (8760.0 / lcm_h) * 100.0

    def _fail(reason: str) -> SpreadResult:
        return SpreadResult(
            passes=False,
            long_exchange=long_ex, short_exchange=short_ex,
            long_interval_hours=long_int, short_interval_hours=short_int,
            long_rate_per_epoch=long_rate, short_rate_per_epoch=short_rate,
            net_per_lcm=net, net_per_lcm_pct=net * 100.0,
            net_apr_pct=net_apr_pct,
            lcm_hours=lcm_h,
            short_events_per_lcm=short_events, long_events_per_lcm=long_events,
            fail_reason=reason,
        )

    if net <= 0:
        return _fail(f"net spread non-positive ({net * 100:.5f}% per {lcm_h:.0f}h epoch)")

    if net_apr_pct < MIN_NET_APR_PCT:
        return _fail(f"net APR {net_apr_pct:.2f}% < {MIN_NET_APR_PCT:.1f}% floor")

    return SpreadResult(
        passes=True,
        long_exchange=long_ex, short_exchange=short_ex,
        long_interval_hours=long_int, short_interval_hours=short_int,
        long_rate_per_epoch=long_rate, short_rate_per_epoch=short_rate,
        net_per_lcm=net, net_per_lcm_pct=net * 100.0,
        net_apr_pct=net_apr_pct,
        lcm_hours=lcm_h,
        short_events_per_lcm=short_events, long_events_per_lcm=long_events,
    )


def _check_entry_cost(ep: ExchangePair, spread: SpreadResult) -> EntryCostResult:
    """
    Check whether projected funding income covers entry price disadvantage + fees.

    Price disadvantage: if long_price > short_price we "buy high / sell low"
    on entry — that premium must be recouped by funding before we close.
    """
    if spread.long_exchange == ep.exchange_a:
        long_price  = ep.price_a
        short_price = ep.price_b
    else:
        long_price  = ep.price_b
        short_price = ep.price_a

    # Price disadvantage — clamp to zero if long leg is cheaper (a bonus, not a cost)
    if short_price > 0:
        entry_disadvantage_pct = max(0.0, (long_price - short_price) / short_price * 100.0)
    else:
        entry_disadvantage_pct = 0.0

    round_trip_fee_pct  = 4.0 * TAKER_FEE_PCT
    total_entry_cost    = entry_disadvantage_pct + round_trip_fee_pct
    projected_funding   = spread.net_per_lcm_pct * ASSUMED_CLOSE_PERIODS

    if spread.net_per_lcm_pct > 0:
        breakeven = total_entry_cost / spread.net_per_lcm_pct
    else:
        breakeven = float("inf")

    passes = projected_funding >= total_entry_cost

    fail_reason = None
    if not passes:
        fail_reason = (
            f"projected funding {projected_funding:.4f}% "
            f"< entry cost {total_entry_cost:.4f}% "
            f"(breakeven: {breakeven:.1f} epochs)"
        )

    return EntryCostResult(
        passes=passes,
        long_price=long_price,
        short_price=short_price,
        entry_disadvantage_pct=entry_disadvantage_pct,
        round_trip_fee_pct=round_trip_fee_pct,
        total_entry_cost_pct=total_entry_cost,
        projected_funding_pct=projected_funding,
        periods_to_breakeven=breakeven,
        fail_reason=fail_reason,
    )


def _compute_score(
    alignment: AlignmentResult,
    spread: SpreadResult,
    entry_cost: EntryCostResult,
) -> float:
    """
    Combine all three checks into a single comparable float. Zero if any fails.

    score = net_apr_pct / (1 + breakeven_penalty + wait_penalty)

    breakeven_penalty = periods_to_breakeven
      (more epochs to recoup entry cost → worse)
    wait_penalty = hours_to_alignment / lcm_hours
      (fraction of one epoch spent idle waiting for the window → slightly worse)
    """
    if not (alignment.passes and spread.passes and entry_cost.passes):
        return 0.0

    wait_hours    = (alignment.minutes_to_alignment or 0.0) / 60.0
    wait_penalty  = wait_hours / max(alignment.alignment_interval_hours, 1.0)
    breakeven_pen = entry_cost.periods_to_breakeven

    return round(spread.net_apr_pct / (1.0 + breakeven_pen + wait_penalty), 6)


# ── Public API ─────────────────────────────────────────────────────────────────

def score_exchange_pair(symbol: str, ep: ExchangePair) -> OpportunityScore:
    """Score a single ExchangePair. All three checks are run in sequence."""
    alignment  = _check_alignment(ep)
    spread     = _check_spread(ep)
    entry_cost = _check_entry_cost(ep, spread)

    fail_reasons: List[str] = []
    if not alignment.passes  and alignment.fail_reason:
        fail_reasons.append(f"alignment: {alignment.fail_reason}")
    if not spread.passes     and spread.fail_reason:
        fail_reasons.append(f"spread: {spread.fail_reason}")
    if not entry_cost.passes and entry_cost.fail_reason:
        fail_reasons.append(f"entry_cost: {entry_cost.fail_reason}")

    passes_all = alignment.passes and spread.passes and entry_cost.passes

    return OpportunityScore(
        symbol=symbol,
        exchange_pair=ep,
        alignment=alignment,
        spread=spread,
        entry_cost=entry_cost,
        passes_all_checks=passes_all,
        fail_reasons=fail_reasons,
        score=_compute_score(alignment, spread, entry_cost),
    )


def score_all(
    records: Dict[str, "PairRecord"],
) -> Tuple[List[OpportunityScore], List[OpportunityScore]]:
    """
    Score every ExchangePair inside every PairRecord.

    Returns:
        passing — sorted by score descending (best first)
        failing — sorted by net_apr_pct descending (closest to passing first,
                  useful for spotting near-misses)
    """
    passing: List[OpportunityScore] = []
    failing: List[OpportunityScore] = []

    for symbol, pr in records.items():
        for ep in pr.exchange_pairs:
            opp = score_exchange_pair(symbol, ep)
            (passing if opp.passes_all_checks else failing).append(opp)

    passing.sort(key=lambda o: -o.score)
    failing.sort(key=lambda o: -o.spread.net_apr_pct)

    return passing, failing


# ── Fail-reason summary helper ─────────────────────────────────────────────────

def summarize_failures(failing: List[OpportunityScore]) -> Dict[str, int]:
    """
    Return counts per fail category for the status line.
    Categories: 'alignment', 'spread', 'entry_cost'
    """
    counts: Dict[str, int] = {"alignment": 0, "spread": 0, "entry_cost": 0}
    for opp in failing:
        seen = set()
        for reason in opp.fail_reasons:
            cat = reason.split(":")[0].strip()
            if cat in counts and cat not in seen:
                counts[cat] += 1
                seen.add(cat)
    return counts
