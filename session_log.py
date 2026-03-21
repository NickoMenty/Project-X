"""
Session Log — per-session CSV trade journal.
A new CSV file is created for each run of main.py.
Columns: deal, timestamp, symbol, long_exchange, short_exchange,
         position_size_usd, leverage,
         binance_bal, bybit_bal, bitget_bal, hyperliquid_bal, asterdex_bal,
         price_pnl_usd, funding_pnl_usd, fees_usd, net_pnl_usd, net_balance_change
"""
import csv
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from settings import active_exchanges
EXCHANGES = active_exchanges()


def _session_filename() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    os.makedirs("data/sessions", exist_ok=True)
    return f"data/sessions/session_{ts}.csv"


_HEADERS = (
    ["deal", "timestamp_utc", "symbol", "long_exchange", "short_exchange",
     "position_size_usd", "leverage"]
    + [f"{ex.lower()}_balance" for ex in EXCHANGES]
    + ["price_pnl_usd", "funding_pnl_usd", "fees_usd", "net_pnl_usd", "net_balance_change"]
)


class SessionLog:
    def __init__(self):
        self.filepath = _session_filename()
        self._deal_counter = 0
        self._balances: Dict[str, Optional[float]] = {ex: None for ex in EXCHANGES}
        # Write header row
        with open(self.filepath, "w", newline="") as f:
            csv.writer(f).writerow(_HEADERS)

    def update_balances(self, balances: Dict[str, float]):
        """Call with {exchange_name: balance_usd} after fetching from all exchanges."""
        for ex, bal in balances.items():
            if ex in self._balances:
                self._balances[ex] = bal

    def record_opening_balance(self, balances: Dict[str, float]):
        """Write a first row recording the session's starting balances (no trade)."""
        self.update_balances(balances)
        row = (
            ["OPEN",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
             "", "", "", 0, 0]
            + [round(self._balances.get(ex, 0) or 0, 4) for ex in EXCHANGES]
            + [0, 0, 0, 0, 0]
        )
        with open(self.filepath, "a", newline="") as f:
            csv.writer(f).writerow(row)
        print(f"  [LOG] Opening balance row recorded → {self.filepath}")

    def record_trade(
        self,
        symbol: str,
        long_exchange: str,
        short_exchange: str,
        position_size_usd: float,
        leverage: int,
        price_pnl_usd: float,
        funding_pnl_usd: float,
        fees_usd: float,
        net_pnl_usd: float,
        post_balances: Optional[Dict[str, float]] = None,
    ):
        """Append one completed trade row to the CSV."""
        self._deal_counter += 1
        if post_balances:
            self.update_balances(post_balances)

        # Net balance change = net P&L (approximate — real change from balance delta if available)
        net_change = net_pnl_usd

        row = (
            [self._deal_counter,
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
             symbol, long_exchange, short_exchange,
             round(position_size_usd, 2), leverage]
            + [round(self._balances.get(ex, 0) or 0, 4) for ex in EXCHANGES]
            + [round(price_pnl_usd, 4), round(funding_pnl_usd, 4),
               round(fees_usd, 4), round(net_pnl_usd, 4), round(net_change, 4)]
        )

        with open(self.filepath, "a", newline="") as f:
            csv.writer(f).writerow(row)

        print(f"  [LOG] Trade #{self._deal_counter} written → {self.filepath}")
        return self._deal_counter

    @property
    def deal_count(self) -> int:
        return self._deal_counter
