"""
Exchange trading clients — Milestone 4 (Real Trading)
Each client implements BaseTrader: set_leverage, open_long/short, close_long/short, get_balance.
"""
from .base import BaseTrader, TradeResult

__all__ = ["BaseTrader", "TradeResult"]
