"""
Price Feed Normalizer — Milestone 1, Step 2
============================================
Takes raw FundingData (which already carries mark_price) and produces a
normalized PriceView per symbol that:

  1. Collects all mark prices for a symbol across exchanges
  2. Computes a reference price (median, most robust to outliers)
  3. Computes per-exchange deviation from reference (absolute + percentage)
  4. Flags anomalous prices (deviation > threshold) — useful for detecting
     stale feeds or oracle issues before entering a trade
  5. Computes cross-exchange price spread (max - min) — this is the
     "entry cost" used in Milestone 2's opportunity scorer

Why median and not mean?
  A single stale or manipulated price feed won't drag the reference price.
  Median is the standard used by on-chain oracle aggregators for the same reason.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from statistics import median

from exchanges.base import FundingData


# Flag any exchange whose price deviates more than this from the median
ANOMALY_THRESHOLD_PCT = 0.5  # 0.5% deviation triggers a warning


@dataclass
class ExchangePrice:
    """Price from a single exchange for a given symbol."""
    exchange: str
    mark_price: float
    deviation_from_ref_pct: float   # % difference from reference price
    is_anomalous: bool              # True if deviation exceeds threshold


@dataclass
class PriceView:
    """
    Aggregated, normalized price data for a single symbol across all exchanges.
    This is the canonical price representation used by the rest of the system.
    """
    symbol: str
    reference_price: float              # Median mark price across all exchanges
    prices: List[ExchangePrice]         # Per-exchange breakdown

    # Cross-exchange spread
    min_price: float
    max_price: float
    spread_usd: float                   # max - min in USD
    spread_pct: float                   # spread as % of reference price

    has_anomaly: bool                   # Any exchange flagged as anomalous
    anomalous_exchanges: List[str]      # Names of flagged exchanges

    def price_for(self, exchange: str) -> Optional[float]:
        """Return mark price for a specific exchange, or None if not available."""
        for ep in self.prices:
            if ep.exchange == exchange:
                return ep.mark_price
        return None

    def entry_cost_pct(self, long_exchange: str, short_exchange: str) -> Optional[float]:
        """
        Calculate the entry cost as a percentage of reference price when:
          - Going LONG on long_exchange (buying at that exchange's higher price)
          - Going SHORT on short_exchange (selling at that exchange's lower price)

        Returns the cost as a positive percentage if long_exchange price >
        short_exchange price (you're entering at a disadvantage), or a
        negative number (a benefit) if long_exchange price is actually lower.

        Returns None if either exchange price is unavailable.
        """
        long_price = self.price_for(long_exchange)
        short_price = self.price_for(short_exchange)

        if long_price is None or short_price is None:
            return None
        if self.reference_price == 0:
            return None

        # Positive = disadvantage (long leg is more expensive)
        # Negative = advantage (long leg is cheaper, rare but possible)
        return ((long_price - short_price) / self.reference_price) * 100

    def __repr__(self) -> str:
        anomaly_str = f" ⚠ ANOMALY: {self.anomalous_exchanges}" if self.has_anomaly else ""
        return (
            f"{self.symbol:10s} | ref=${self.reference_price:,.4f} | "
            f"spread=${self.spread_usd:.4f} ({self.spread_pct:.4f}%){anomaly_str}"
        )


# ─────────────────────────────────────────────
#  Core normalization function
# ─────────────────────────────────────────────

def normalize_prices(
    pair_table: Dict[str, Dict[str, FundingData]],
    anomaly_threshold_pct: float = ANOMALY_THRESHOLD_PCT,
) -> Dict[str, PriceView]:
    """
    Given the pair intersection table {symbol -> {exchange -> FundingData}},
    produce a {symbol -> PriceView} mapping with normalized, validated prices.

    Args:
        pair_table: Output of build_pair_table() from main.py
        anomaly_threshold_pct: Flag exchanges whose price deviates by more
                                than this % from the median reference price

    Returns:
        Dict mapping symbol -> PriceView
    """
    price_views: Dict[str, PriceView] = {}

    for symbol, ex_map in pair_table.items():
        # Collect valid prices (skip zero/None)
        raw_prices: List[Tuple[str, float]] = []
        for ex_name, fd in ex_map.items():
            if fd.mark_price and fd.mark_price > 0:
                raw_prices.append((ex_name, fd.mark_price))

        if not raw_prices:
            continue

        # Reference price = median across available exchanges
        price_values = [p for _, p in raw_prices]
        ref_price = median(price_values)

        # Per-exchange deviation
        exchange_prices: List[ExchangePrice] = []
        anomalous: List[str] = []

        for ex_name, price in raw_prices:
            if ref_price > 0:
                dev_pct = ((price - ref_price) / ref_price) * 100
            else:
                dev_pct = 0.0

            is_anomalous = abs(dev_pct) > anomaly_threshold_pct
            if is_anomalous:
                anomalous.append(ex_name)

            exchange_prices.append(ExchangePrice(
                exchange=ex_name,
                mark_price=price,
                deviation_from_ref_pct=dev_pct,
                is_anomalous=is_anomalous,
            ))

        # Sort by exchange name for deterministic output
        exchange_prices.sort(key=lambda x: x.exchange)

        min_price = min(price_values)
        max_price = max(price_values)
        spread_usd = max_price - min_price
        spread_pct = (spread_usd / ref_price * 100) if ref_price > 0 else 0.0

        price_views[symbol] = PriceView(
            symbol=symbol,
            reference_price=ref_price,
            prices=exchange_prices,
            min_price=min_price,
            max_price=max_price,
            spread_usd=spread_usd,
            spread_pct=spread_pct,
            has_anomaly=len(anomalous) > 0,
            anomalous_exchanges=anomalous,
        )

    return price_views


# ─────────────────────────────────────────────
#  Display helpers
# ─────────────────────────────────────────────

def format_price_summary(price_views: Dict[str, PriceView]) -> List[dict]:
    """
    Convert price views to a list of dicts suitable for tabulate or JSON export.
    One row per symbol.
    """
    rows = []
    for symbol, pv in sorted(price_views.items()):
        row = {
            "symbol": symbol,
            "ref_price_usd": round(pv.reference_price, 6),
            "spread_usd": round(pv.spread_usd, 6),
            "spread_pct": round(pv.spread_pct, 6),
            "has_anomaly": pv.has_anomaly,
            "anomalous_exchanges": ",".join(pv.anomalous_exchanges) if pv.anomalous_exchanges else "",
        }
        # Add per-exchange prices
        for ep in pv.prices:
            row[f"price_{ep.exchange}"] = round(ep.mark_price, 6)
            row[f"dev_{ep.exchange}_pct"] = round(ep.deviation_from_ref_pct, 6)
        rows.append(row)
    return rows