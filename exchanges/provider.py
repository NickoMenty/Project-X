"""
Funding rate provider interface and implementations.

FundingRateProvider   Base interface — any object with fetch_all() qualifies.
LiveFundingProvider   Fetches live rates in parallel from exchange connectors.
MockFundingProvider   Returns configurable test data without hitting any API.
                      Used automatically when DRY_RUN=true.

Usage — mock::

    mock = MockFundingProvider()
    mock.add("Binance", "BTC", rate_pct= 0.05, mark_price=65_000)
    mock.add("Bybit",   "BTC", rate_pct=-0.03, mark_price=65_000)
    # same symbol, opposing rates → opportunity will score
    data = mock.fetch_all()   # {"Binance": [FundingData(...)], "Bybit": [...]}

Usage — live::

    from main import _ALL_FETCHERS
    provider = LiveFundingProvider(_ALL_FETCHERS)
    data = provider.fetch_all()
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from colorama import Fore, Style

from .base import FundingData


class FundingRateProvider:
    """Base interface.  Subclass or duck-type — only fetch_all() is required."""

    def fetch_all(self) -> Dict[str, List[FundingData]]:
        raise NotImplementedError


class LiveFundingProvider(FundingRateProvider):
    """
    Fetches live funding rates from all supplied exchange connectors in parallel.
    Replaces the old standalone fetch_all() function in main.py.

    Args:
        fetchers: mapping of exchange name → zero-arg callable that returns
                  List[FundingData], e.g. {"Binance": binance.fetch_funding_rates}.
                  Typically built from _ALL_FETCHERS filtered to active exchanges.
    """

    def __init__(self, fetchers: Dict[str, Callable[[], List[FundingData]]]):
        self._fetchers = fetchers

    def fetch_all(self) -> Dict[str, List[FundingData]]:
        results: Dict[str, List[FundingData]] = {}

        with ThreadPoolExecutor(max_workers=max(1, len(self._fetchers))) as executor:
            futures = {executor.submit(fn): name for name, fn in self._fetchers.items()}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    data = future.result(timeout=15)
                    valid = [d for d in data if d.is_valid]
                    results[name] = valid
                    status = Fore.GREEN + f"✓ {len(data)} pairs" + Style.RESET_ALL
                except Exception as e:
                    results[name] = []
                    status = Fore.RED + f"✗ {e}" + Style.RESET_ALL
                print(f"  {name:14s} {status}")

        return results


class MockFundingProvider(FundingRateProvider):
    """
    Returns configurable test funding data without hitting any exchange API.

    Call add() to register entries before fetch_all() is called.  Returns self
    so calls can be chained.  Returns an empty dict by default — safe, no trades
    will score unless entries are explicitly added.

    Example::

        mock = MockFundingProvider()
        mock.add("Binance", "BTC",  rate_pct= 0.05, mark_price=65_000)
             .add("Bybit",   "BTC",  rate_pct=-0.03, mark_price=65_000)
             .add("Binance", "ETH",  rate_pct= 0.01, mark_price=3_000)
             .add("Bybit",   "ETH",  rate_pct= 0.01, mark_price=3_000)
    """

    def __init__(self):
        self._entries: Dict[str, List[FundingData]] = {}

    @staticmethod
    def _next_epoch_ms(interval_hours: float = 8.0) -> int:
        """Unix ms of the next top-of-interval funding epoch from now."""
        now_s = time.time()
        iv_s  = interval_hours * 3600.0
        return int((math.floor(now_s / iv_s) + 1) * iv_s * 1000)

    def add(
        self,
        exchange:       str,
        symbol:         str,
        rate_pct:       float,
        mark_price:     float = 100.0,
        next_ts_ms:     Optional[int] = None,
        interval_hours: float = 8.0,
        raw_symbol:     str   = "",
    ) -> "MockFundingProvider":
        """
        Register a funding data entry.  Returns self for method chaining.

        Args:
            exchange:       Exchange name, e.g. "Binance"
            symbol:         Normalised base asset, e.g. "BTC"
            rate_pct:       Funding rate as a percentage, e.g. 0.05 means +0.05%
            mark_price:     Current mark price in USD
            next_ts_ms:     Unix ms of next funding epoch; defaults to the next
                            top-of-interval from the current time
            interval_hours: Funding interval in hours (1, 4, or 8)
            raw_symbol:     Exchange-native symbol; defaults to "{symbol}USDT"
        """
        rate = rate_pct / 100.0
        fd = FundingData(
            exchange        = exchange,
            symbol          = symbol,
            raw_symbol      = raw_symbol or f"{symbol}USDT",
            funding_rate    = rate,
            funding_rate_pct= rate_pct,
            mark_price      = mark_price,
            next_funding_ts = next_ts_ms or self._next_epoch_ms(interval_hours),
            interval_hours  = interval_hours,
            annualized_rate = rate_pct * (8760.0 / interval_hours),
        )
        self._entries.setdefault(exchange, []).append(fd)
        return self

    def fetch_all(self) -> Dict[str, List[FundingData]]:
        if not self._entries:
            print(
                f"  {Fore.YELLOW}[MOCK] No entries configured — "
                f"no opportunities will score{Style.RESET_ALL}"
            )
        else:
            total = sum(len(v) for v in self._entries.values())
            print(
                f"  {Fore.YELLOW}[MOCK] {total} entries across "
                f"{len(self._entries)} exchanges{Style.RESET_ALL}"
            )
        return {ex: list(entries) for ex, entries in self._entries.items()}
