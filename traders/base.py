"""
Abstract base class for all exchange trading clients.
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class TradeResult:
    exchange: str
    symbol: str        # normalized, e.g. "BTC"
    raw_symbol: str    # exchange-native, e.g. "BTCUSDT"
    side: str          # "buy" or "sell"
    qty: float         # base-asset quantity filled
    fill_price: float  # average fill price
    notional_usd: float
    leverage: int
    order_id: str = ""
    error: str = ""

    @property
    def success(self) -> bool:
        return not self.error

    @property
    def fee_usd(self) -> float:
        """Estimated fee — real fee should come from exchange response if available."""
        return 0.0


class BaseTrader(ABC):
    """Abstract interface every exchange trading client must implement."""

    exchange_name: str = ""

    # ── Symbol formatting ──────────────────────────────────────────────────────

    def fmt_symbol(self, symbol: str) -> str:
        """Normalize symbol → exchange raw symbol. Override if exchange uses different format."""
        return symbol + "USDT"

    # ── Quantity helpers ───────────────────────────────────────────────────────

    @staticmethod
    def calc_qty(notional_usd: float, mark_price: float) -> float:
        """Base-asset quantity for a given notional and mark price."""
        if mark_price <= 0:
            raise ValueError("mark_price must be > 0")
        return notional_usd / mark_price

    @staticmethod
    def round_step(qty: float, step: float) -> float:
        """Floor qty to nearest step size, respecting decimal precision."""
        if step <= 0:
            return qty
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        floored = math.floor(qty / step) * step
        return round(floored, decimals)

    # ── Required methods ───────────────────────────────────────────────────────

    @abstractmethod
    def get_balance(self) -> float:
        """Return available USDT balance."""
        ...

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> int:
        """
        Set leverage for symbol. Try requested leverage first; if rejected, try 5x.
        Returns the leverage actually set.
        symbol is normalized (e.g. 'BTC'), implementations convert via fmt_symbol.
        """
        ...

    @abstractmethod
    def open_long(self, symbol: str, notional_usd: float, mark_price: float, leverage: int) -> TradeResult:
        """Open a long (buy) position for the given notional."""
        ...

    @abstractmethod
    def open_short(self, symbol: str, notional_usd: float, mark_price: float, leverage: int) -> TradeResult:
        """Open a short (sell) position for the given notional."""
        ...

    @abstractmethod
    def close_long(self, symbol: str, qty: float) -> TradeResult:
        """Close an open long position (sell reduceOnly)."""
        ...

    @abstractmethod
    def close_short(self, symbol: str, qty: float) -> TradeResult:
        """Close an open short position (buy reduceOnly)."""
        ...
