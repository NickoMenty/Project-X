"""
Pair Intersection Engine — Milestone 1, Step 3
===============================================
Formalizes the pair intersection logic from main.py into a standalone,
testable module. Produces a rich PairRecord for each symbol that
consolidates funding data + price data + exchange availability metadata.

This is the canonical data structure that Milestone 2's opportunity scorer
will consume. Everything downstream works with PairRecord objects.

Key design decisions:
  - A pair is "eligible" if it trades on ≥ min_exchanges AND has valid
    prices on all present exchanges (price_view is not None)
  - Exchange presence is a frozenset — order-independent, hashable,
    usable as a dict key for grouping
  - All exchange combinations (pairs of exchanges) for a symbol are
    pre-computed here so the scorer doesn't repeat the work
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple
from itertools import combinations

from exchanges.base import FundingData
from price_normalizer import PriceView


@dataclass
class ExchangePair:
    """
    A specific two-exchange combination for a symbol.
    Represents one candidate arbitrage leg pair.
    """
    exchange_a: str          # Name of first exchange
    exchange_b: str          # Name of second exchange

    # Funding rates
    rate_a: float            # Funding rate on exchange A (decimal)
    rate_b: float            # Funding rate on exchange B (decimal)
    rate_a_pct: float
    rate_b_pct: float

    # Interval hours per exchange
    interval_a: float
    interval_b: float

    # Next funding timestamps (ms)
    next_ts_a: Optional[int]
    next_ts_b: Optional[int]

    # Prices
    price_a: float
    price_b: float
    price_spread_pct: float  # (price_a - price_b) / ref_price * 100

    # Net funding spread (rate_a - rate_b), per epoch
    # Positive = A pays more (go short A, long B)
    # Negative = B pays more (go short B, long A)
    raw_spread: float
    raw_spread_pct: float

    def long_exchange(self) -> str:
        """The exchange where you go LONG (lower funding rate = you receive)."""
        return self.exchange_b if self.raw_spread >= 0 else self.exchange_a

    def short_exchange(self) -> str:
        """The exchange where you go SHORT (higher funding rate = you receive)."""
        return self.exchange_a if self.raw_spread >= 0 else self.exchange_b

    def long_rate(self) -> float:
        return self.rate_b if self.raw_spread >= 0 else self.rate_a

    def short_rate(self) -> float:
        return self.rate_a if self.raw_spread >= 0 else self.rate_b

    def abs_spread(self) -> float:
        """Absolute value of the funding rate spread."""
        return abs(self.raw_spread)

    def abs_spread_pct(self) -> float:
        return abs(self.raw_spread_pct)

    def label(self) -> str:
        return f"{self.exchange_a} / {self.exchange_b}"


@dataclass
class PairRecord:
    """
    Full normalized record for a single symbol across all exchanges it trades on.
    This is the canonical object for downstream processing (scoring, trading, logging).
    """
    symbol: str
    exchanges_present: FrozenSet[str]      # Which exchanges have this pair
    exchange_count: int                     # len(exchanges_present)

    # Raw funding data per exchange
    funding: Dict[str, FundingData]         # exchange_name -> FundingData

    # Normalized price data
    price_view: Optional[PriceView]         # None if price normalization failed

    # All 2-exchange combinations, pre-computed
    exchange_pairs: List[ExchangePair]

    # Convenience: best spread pair (highest abs funding spread)
    best_pair: Optional[ExchangePair]

    @property
    def reference_price(self) -> Optional[float]:
        return self.price_view.reference_price if self.price_view else None

    def funding_for(self, exchange: str) -> Optional[FundingData]:
        return self.funding.get(exchange)

    def __repr__(self) -> str:
        best = f" | best_spread={self.best_pair.abs_spread_pct():.4f}%" if self.best_pair else ""
        return (
            f"PairRecord({self.symbol} | "
            f"{self.exchange_count} exchanges: {sorted(self.exchanges_present)}"
            f"{best})"
        )


# ─────────────────────────────────────────────
#  Core builder
# ─────────────────────────────────────────────

def build_pair_records(
    all_data: Dict[str, List[FundingData]],
    price_views: Dict[str, PriceView],
    min_exchanges: int = 2,
) -> Dict[str, PairRecord]:
    """
    Build the full intersection table as PairRecord objects.

    Args:
        all_data:       Raw output from fetch_all() — {exchange: [FundingData]}
        price_views:    Output from normalize_prices() — {symbol: PriceView}
        min_exchanges:  Minimum number of exchanges a symbol must trade on

    Returns:
        Dict of {symbol -> PairRecord}, only for qualifying symbols
    """
    # Step 1: Group FundingData by symbol
    symbol_map: Dict[str, Dict[str, FundingData]] = {}
    for exchange, records in all_data.items():
        for fd in records:
            if fd.symbol not in symbol_map:
                symbol_map[fd.symbol] = {}
            symbol_map[fd.symbol][exchange] = fd

    # Step 2: Filter and build PairRecords
    records: Dict[str, PairRecord] = {}

    for symbol, funding_by_exchange in symbol_map.items():
        if len(funding_by_exchange) < min_exchanges:
            continue

        pv = price_views.get(symbol)
        ex_pairs = _compute_exchange_pairs(symbol, funding_by_exchange, pv)

        best = max(ex_pairs, key=lambda p: p.abs_spread(), default=None)

        records[symbol] = PairRecord(
            symbol=symbol,
            exchanges_present=frozenset(funding_by_exchange.keys()),
            exchange_count=len(funding_by_exchange),
            funding=funding_by_exchange,
            price_view=pv,
            exchange_pairs=ex_pairs,
            best_pair=best,
        )

    return records


def _compute_exchange_pairs(
    symbol: str,
    funding_by_exchange: Dict[str, FundingData],
    pv: Optional[PriceView],
) -> List[ExchangePair]:
    """
    For a symbol, compute all 2-exchange combinations as ExchangePair objects.
    Sorted by abs spread descending (best opportunity first).
    """
    exchange_names = list(funding_by_exchange.keys())
    ref_price = pv.reference_price if pv else 0.0
    result = []

    for ex_a, ex_b in combinations(exchange_names, 2):
        fd_a = funding_by_exchange[ex_a]
        fd_b = funding_by_exchange[ex_b]

        price_a = pv.price_for(ex_a) if pv else fd_a.mark_price
        price_b = pv.price_for(ex_b) if pv else fd_b.mark_price
        price_a = price_a or fd_a.mark_price or 0.0
        price_b = price_b or fd_b.mark_price or 0.0

        price_spread_pct = 0.0
        if ref_price > 0:
            price_spread_pct = ((price_a - price_b) / ref_price) * 100

        raw_spread = fd_a.funding_rate - fd_b.funding_rate
        raw_spread_pct = raw_spread * 100

        result.append(ExchangePair(
            exchange_a=ex_a,
            exchange_b=ex_b,
            rate_a=fd_a.funding_rate,
            rate_b=fd_b.funding_rate,
            rate_a_pct=fd_a.funding_rate_pct,
            rate_b_pct=fd_b.funding_rate_pct,
            interval_a=fd_a.interval_hours,
            interval_b=fd_b.interval_hours,
            next_ts_a=fd_a.next_funding_ts,
            next_ts_b=fd_b.next_funding_ts,
            price_a=price_a,
            price_b=price_b,
            price_spread_pct=price_spread_pct,
            raw_spread=raw_spread,
            raw_spread_pct=raw_spread_pct,
        ))

    result.sort(key=lambda p: p.abs_spread(), reverse=True)
    return result


# ─────────────────────────────────────────────
#  Summary helpers
# ─────────────────────────────────────────────

def summarize_intersection(records: Dict[str, PairRecord]) -> dict:
    """Return a summary dict for logging / display."""
    by_count: Dict[int, int] = {}
    for r in records.values():
        by_count[r.exchange_count] = by_count.get(r.exchange_count, 0) + 1

    return {
        "total_pairs": len(records),
        "by_exchange_count": dict(sorted(by_count.items(), reverse=True)),
        "symbols_7_exchanges": [s for s, r in records.items() if r.exchange_count == 7],
        "symbols_6_exchanges": [s for s, r in records.items() if r.exchange_count == 6],
    }