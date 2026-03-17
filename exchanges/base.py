"""
FundingData — Canonical normalized data object
===============================================
Every exchange connector returns a list of these.
Nothing outside the exchanges/ folder ever touches
raw API responses — it only works with FundingData objects.

Fields
------
exchange        Human-readable exchange name e.g. "Bybit"
symbol          Normalized base asset e.g. "BTC" (no quote currency, no suffix)
raw_symbol      Exchange-native symbol e.g. "BTCUSDT", "BTC-PERP", "BTC_USDT"
funding_rate    Current funding rate as a decimal  e.g. 0.0001  (= 0.01%)
funding_rate_pct  funding_rate * 100               e.g. 0.01    (display %)
mark_price      Current mark price in USD
next_funding_ts Unix timestamp in milliseconds of the next funding credit event
interval_hours  How often funding credits: 1.0, 4.0, or 8.0 hours
annualized_rate Estimated APR = funding_rate * (8760 / interval_hours) * 100
fetched_at_ts   Unix timestamp in milliseconds when this record was fetched
                (set automatically by each exchange connector)
is_valid        False if any critical field failed validation — skip these records
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# Funding intervals all known exchanges use — used for validation
VALID_INTERVAL_HOURS = {1.0, 2.0, 4.0, 8.0}

# Any annualized rate beyond this is almost certainly a data error
MAX_SANE_APR_PCT = 50_000.0

# Mark price sanity bounds (USD) — anything outside is almost certainly stale/broken
MIN_SANE_PRICE = 0.000_000_01
MAX_SANE_PRICE = 10_000_000.0


@dataclass
class FundingData:
    # ── Core fields (set by exchange connectors) ──────────────────────────────
    exchange:         str
    symbol:           str
    raw_symbol:       str
    funding_rate:     float
    funding_rate_pct: float
    mark_price:       float
    next_funding_ts:  Optional[int]
    interval_hours:   float
    annualized_rate:  float

    # ── Auto-populated fields ─────────────────────────────────────────────────
    fetched_at_ts: int   = field(default_factory=lambda: int(time.time() * 1000))
    is_valid:      bool  = field(default=True)
    invalid_reason: str  = field(default="")

    # ─────────────────────────────────────────────────────────────────────────
    #  Post-init validation
    #  Called automatically by Python after __init__ on a dataclass.
    #  Sets is_valid=False and records a reason instead of raising — so a
    #  single bad record from one exchange never crashes the whole fetch cycle.
    # ─────────────────────────────────────────────────────────────────────────

    def __post_init__(self):
        reasons = []

        if not self.exchange or not isinstance(self.exchange, str):
            reasons.append("missing exchange name")

        if not self.symbol or not isinstance(self.symbol, str):
            reasons.append("missing symbol")

        if not isinstance(self.funding_rate, (int, float)):
            reasons.append("funding_rate is not numeric")

        if not isinstance(self.mark_price, (int, float)) or self.mark_price <= 0:
            reasons.append(f"mark_price invalid ({self.mark_price})")
        elif not (MIN_SANE_PRICE <= self.mark_price <= MAX_SANE_PRICE):
            reasons.append(f"mark_price out of sane range ({self.mark_price})")

        if self.interval_hours not in VALID_INTERVAL_HOURS:
            # Warn but don't invalidate — some exotic exchanges may use other intervals
            # We clamp to nearest known value to avoid division weirdness downstream
            nearest = min(VALID_INTERVAL_HOURS, key=lambda x: abs(x - self.interval_hours))
            self.interval_hours = nearest

        if abs(self.annualized_rate) > MAX_SANE_APR_PCT:
            reasons.append(f"annualized_rate implausibly large ({self.annualized_rate:.1f}%)")

        if reasons:
            self.is_valid = False
            self.invalid_reason = "; ".join(reasons)

    # ─────────────────────────────────────────────────────────────────────────
    #  Derived properties
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def is_positive(self) -> bool:
        """True when longs pay shorts (positive funding). Short side collects."""
        return self.funding_rate > 0

    @property
    def is_negative(self) -> bool:
        """True when shorts pay longs (negative funding). Long side collects."""
        return self.funding_rate < 0

    @property
    def abs_rate(self) -> float:
        """Absolute value of funding rate (decimal)."""
        return abs(self.funding_rate)

    @property
    def abs_rate_pct(self) -> float:
        """Absolute value of funding rate as percentage."""
        return abs(self.funding_rate_pct)

    @property
    def seconds_to_next_funding(self) -> Optional[float]:
        """
        Seconds until the next funding credit.
        Returns None if next_funding_ts is not set.
        Returns 0.0 if it has already passed (funding imminent or just occurred).
        """
        if not self.next_funding_ts:
            return None
        now_ms = time.time() * 1000
        delta_ms = self.next_funding_ts - now_ms
        return max(0.0, delta_ms / 1000)

    @property
    def minutes_to_next_funding(self) -> Optional[float]:
        secs = self.seconds_to_next_funding
        return secs / 60 if secs is not None else None

    @property
    def data_age_seconds(self) -> float:
        """How many seconds ago this record was fetched."""
        return (time.time() * 1000 - self.fetched_at_ts) / 1000

    @property
    def is_stale(self) -> bool:
        """
        True if this record is older than one full funding interval.
        A stale record should not be traded on — refetch first.
        """
        return self.data_age_seconds > (self.interval_hours * 3600)

    # ─────────────────────────────────────────────────────────────────────────
    #  Comparison — allows sorting lists of FundingData by rate
    # ─────────────────────────────────────────────────────────────────────────

    def __lt__(self, other: "FundingData") -> bool:
        return self.funding_rate < other.funding_rate

    def __le__(self, other: "FundingData") -> bool:
        return self.funding_rate <= other.funding_rate

    def __gt__(self, other: "FundingData") -> bool:
        return self.funding_rate > other.funding_rate

    def __ge__(self, other: "FundingData") -> bool:
        return self.funding_rate >= other.funding_rate

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FundingData):
            return NotImplemented
        return (
            self.exchange == other.exchange
            and self.symbol == other.symbol
            and self.funding_rate == other.funding_rate
        )

    def __hash__(self):
        return hash((self.exchange, self.symbol, self.funding_rate))

    # ─────────────────────────────────────────────────────────────────────────
    #  Serialization
    # ─────────────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize to a plain dict — used by the exporter for JSON/CSV output."""
        return {
            "exchange":         self.exchange,
            "symbol":           self.symbol,
            "raw_symbol":       self.raw_symbol,
            "funding_rate":     self.funding_rate,
            "funding_rate_pct": self.funding_rate_pct,
            "mark_price":       self.mark_price,
            "next_funding_ts":  self.next_funding_ts,
            "interval_hours":   self.interval_hours,
            "annualized_rate":  self.annualized_rate,
            "fetched_at_ts":    self.fetched_at_ts,
            "is_valid":         self.is_valid,
            "invalid_reason":   self.invalid_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FundingData":
        """Deserialize from a dict — used when loading snapshots from disk."""
        return cls(
            exchange=         d.get("exchange", ""),
            symbol=           d.get("symbol", ""),
            raw_symbol=       d.get("raw_symbol", ""),
            funding_rate=     float(d.get("funding_rate", 0)),
            funding_rate_pct= float(d.get("funding_rate_pct", 0)),
            mark_price=       float(d.get("mark_price", 0)),
            next_funding_ts=  d.get("next_funding_ts"),
            interval_hours=   float(d.get("interval_hours", 8)),
            annualized_rate=  float(d.get("annualized_rate", 0)),
            fetched_at_ts=    int(d.get("fetched_at_ts", int(time.time() * 1000))),
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Display
    # ─────────────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        validity = "" if self.is_valid else f" [INVALID: {self.invalid_reason}]"
        next_str = ""
        mins = self.minutes_to_next_funding
        if mins is not None:
            next_str = f" | next={mins:.0f}m"
        return (
            f"{self.exchange:12s} | {self.symbol:10s} | "
            f"rate={self.funding_rate_pct:+.4f}% | "
            f"price={self.mark_price:,.4f} | "
            f"interval={self.interval_hours}h{next_str} | "
            f"APR={self.annualized_rate:+.2f}%"
            f"{validity}"
        )

    def __str__(self) -> str:
        return self.__repr__()