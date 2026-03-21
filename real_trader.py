"""
Real Trader — Milestone 4
=========================
Executes real orders on live exchanges using the traders/ package.

Execution sequence
------------------
  T-10s  real_entry()   — set leverage, place market orders on both legs simultaneously
  T+0    funding epoch fires
  T+0    real_exit()    — close both legs simultaneously

Position sizing
---------------
  POSITION_SIZE_USD  = 50 USD notional per leg (from spike_scorer)
  TARGET_LEVERAGE    = 10x (falls back to 5x if exchange rejects 10x)
  Margin per leg     = 50 / 10 = $5 USDT
"""

import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, Tuple

from colorama import Fore, Style

from spike_scorer import SpikeOpportunity, POSITION_SIZE_USD
from traders.base import TradeResult

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_LEVERAGE = 10
FALLBACK_LEVERAGE = 5
EXIT_WAIT_SECONDS = 0  # close immediately after funding epoch

# Set to True to log all actions but NOT place real orders (safe test mode)
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")


# ── Trade record ──────────────────────────────────────────────────────────────

@dataclass
class RealTrade:
    # Identity
    symbol: str
    long_exchange: str
    short_exchange: str
    position_size_usd: float

    # Rates at pre-scan
    long_rate_pct: float
    short_rate_pct: float
    long_fee_pct: float
    short_fee_pct: float

    # Epoch
    funding_ts_ms: int
    primary_is_short: bool
    hedge_credits: bool

    # Entry
    entry_ts: float = 0.0
    leverage_used: int = TARGET_LEVERAGE
    long_entry_result: Optional[TradeResult] = None
    short_entry_result: Optional[TradeResult] = None

    # Exit
    exit_ts: float = 0.0
    long_exit_result: Optional[TradeResult] = None
    short_exit_result: Optional[TradeResult] = None

    # P&L (populated after exit)
    long_price_pnl_usd: float = 0.0
    short_price_pnl_usd: float = 0.0
    funding_collected_usd: float = 0.0
    funding_paid_usd: float = 0.0
    net_funding_usd: float = 0.0
    total_fee_usd: float = 0.0
    net_pnl_usd: float = 0.0

    completed: bool = False
    error: str = ""
    dry_run: bool = DRY_RUN
    pre_balances: Dict[str, float] = field(default_factory=dict)
    post_balances: Dict[str, float] = field(default_factory=dict)

    @property
    def entry_long_price(self) -> float:
        return self.long_entry_result.fill_price if self.long_entry_result else 0.0

    @property
    def entry_short_price(self) -> float:
        return self.short_entry_result.fill_price if self.short_entry_result else 0.0

    @property
    def exit_long_price(self) -> float:
        return self.long_exit_result.fill_price if self.long_exit_result else 0.0

    @property
    def exit_short_price(self) -> float:
        return self.short_exit_result.fill_price if self.short_exit_result else 0.0

    @property
    def long_qty(self) -> float:
        return self.long_entry_result.qty if self.long_entry_result else 0.0

    @property
    def short_qty(self) -> float:
        return self.short_entry_result.qty if self.short_entry_result else 0.0


# ── Trader registry ───────────────────────────────────────────────────────────

_traders: Dict[str, object] = {}


def init_traders():
    """
    Initialise all exchange trading clients from environment variables.
    Call once at startup. Returns {exchange_name: trader_instance}.
    Only initialises exchanges that are enabled in settings.py.
    """
    from dotenv import load_dotenv
    load_dotenv()
    from settings import active_exchanges

    from traders.binance import BinanceTrader
    from traders.bybit import BybitTrader
    from traders.bitget import BitgetTrader
    from traders.asterdex import AsterDexTrader
    from traders.hyperliquid import HyperliquidTrader
    from traders.okx import OKXTrader
    from traders.kucoin import KuCoinTrader

    enabled = set(active_exchanges())
    loaded = {}

    def _try(name, factory):
        if name not in enabled:
            print(f"  {name:14s} {Fore.YELLOW}— disabled in settings.py{Style.RESET_ALL}")
            return
        try:
            t = factory()
            loaded[name] = t
            print(f"  {name:14s} {Fore.GREEN}✓ trader ready{Style.RESET_ALL}")
        except Exception as e:
            print(f"  {name:14s} {Fore.RED}✗ {e}{Style.RESET_ALL}")

    _try("Binance", lambda: BinanceTrader(
        os.environ["BINANCE_KEY"], os.environ["BINANCE_SECRET"]))

    _try("Bybit", lambda: BybitTrader(
        os.environ["BYBIT_KEY"], os.environ["BYBIT_SECRET"]))

    _try("Bitget", lambda: BitgetTrader(
        os.environ["BITGET_KEY"], os.environ["BITGET_SECRET"], os.environ["BITGET_PASS"]))

    _try("AsterDex", lambda: AsterDexTrader(
        os.environ["ASTER_WALLET"], os.environ["ASTER_PRIVATE"]))

    if "Hyperliquid" in enabled:
        try:
            import eth_account  # noqa
            _try("Hyperliquid", lambda: HyperliquidTrader(os.environ["HYPERLIQUID_API"]))
        except ImportError:
            print(f"  {'Hyperliquid':14s} {Fore.YELLOW}⚠ eth-account not installed — run: pip install eth-account{Style.RESET_ALL}")

    _try("OKX", lambda: OKXTrader(
        os.environ["OKX_KEY"], os.environ["OKX_SECRET"], os.environ["OKX_PASS"]))

    _try("KuCoin", lambda: KuCoinTrader(
        os.environ["KUCOIN_KEY"], os.environ["KUCOIN_SECRET"], os.environ["KUCOIN_PASS"]))

    _traders.update(loaded)
    return loaded


def get_all_balances_dict() -> Dict[str, float]:
    """Fetch balances from all traders concurrently, silently. Returns {exchange: balance}."""
    result: Dict[str, float] = {}

    def _fetch(name, trader):
        try:
            result[name] = trader.get_balance()
        except Exception:
            result[name] = 0.0

    threads = [threading.Thread(target=_fetch, args=(n, t)) for n, t in _traders.items()]
    for t in threads: t.start()
    for t in threads: t.join()
    return result


def fetch_all_balances() -> Dict[str, object]:
    """
    Fetch USDT balance from all initialised traders concurrently.
    Returns {exchange: float} for successes, {exchange: None} for failures.
    """
    balances: Dict[str, object] = {}
    errors: Dict[str, str] = {}

    def _fetch(name, trader):
        try:
            if DRY_RUN:
                balances[name] = 100.0
            else:
                balances[name] = trader.get_balance()
        except Exception as e:
            errors[name] = str(e)
            balances[name] = None

    threads = [threading.Thread(target=_fetch, args=(n, t)) for n, t in _traders.items()]
    for t in threads: t.start()
    for t in threads: t.join()

    # Print results — include exchanges that failed to init too
    from session_log import EXCHANGES
    for name in EXCHANGES:
        if name not in _traders:
            print(f"  {name:14s} {Fore.YELLOW}⚠ trader not initialised{Style.RESET_ALL}")
        elif name in errors:
            print(f"  {name:14s} {Fore.RED}✗ {errors[name]}{Style.RESET_ALL}")
        else:
            print(f"  {name:14s} {Fore.GREEN}${balances.get(name, 0):.2f}{Style.RESET_ALL}")

    total = sum(v for k, v in balances.items() if k != "_errors" and isinstance(v, (int, float)))
    print(f"\n  {'':14s}  {'─'*20}")
    print(f"  {'TOTAL USDT':14s}  {Fore.CYAN}{Style.BRIGHT}${total:.2f}{Style.RESET_ALL}")

    # Store errors so callers can show them in TG
    balances["_errors"] = errors
    return balances


def get_trader(exchange: str):
    if exchange not in _traders:
        raise RuntimeError(f"No trader initialised for {exchange}. Check API keys in .env")
    return _traders[exchange]


# ── Entry / Exit ──────────────────────────────────────────────────────────────

def real_entry(opportunity: SpikeOpportunity, event_ts_ms: int) -> RealTrade:
    """
    Place real orders on both legs simultaneously.
    Returns a RealTrade record with entry fill prices.
    """
    trade = RealTrade(
        symbol=opportunity.symbol,
        long_exchange=opportunity.long_exchange,
        short_exchange=opportunity.short_exchange,
        position_size_usd=POSITION_SIZE_USD,
        long_rate_pct=opportunity.long_rate_pct,
        short_rate_pct=opportunity.short_rate_pct,
        long_fee_pct=opportunity.long_fee_pct,
        short_fee_pct=opportunity.short_fee_pct,
        funding_ts_ms=event_ts_ms,
        primary_is_short=opportunity.primary_is_short,
        hedge_credits=opportunity.hedge_credits,
        entry_ts=time.time(),
    )

    long_trader = get_trader(opportunity.long_exchange)
    short_trader = get_trader(opportunity.short_exchange)

    long_result: Optional[TradeResult] = None
    short_result: Optional[TradeResult] = None

    # Set leverage on both legs (do this before order placement)
    lev_long, lev_short = TARGET_LEVERAGE, TARGET_LEVERAGE
    try:
        lev_long = long_trader.set_leverage(opportunity.symbol, TARGET_LEVERAGE)
    except Exception as e:
        print(f"  [WARN] {opportunity.long_exchange} set_leverage failed: {e}")
    try:
        lev_short = short_trader.set_leverage(opportunity.symbol, TARGET_LEVERAGE)
    except Exception as e:
        print(f"  [WARN] {opportunity.short_exchange} set_leverage failed: {e}")

    leverage_used = min(lev_long, lev_short)
    trade.leverage_used = leverage_used

    if DRY_RUN:
        if not opportunity.long_price or not opportunity.short_price:
            trade.error = f"Cannot enter: missing price (long={opportunity.long_price}, short={opportunity.short_price})"
            return trade
        print(f"  [DRY-RUN] Would open LONG  {opportunity.long_exchange}  {opportunity.symbol}"
              f"  ${POSITION_SIZE_USD} @ {lev_long}x  price≈{opportunity.long_price:.4f}")
        print(f"  [DRY-RUN] Would open SHORT {opportunity.short_exchange}  {opportunity.symbol}"
              f"  ${POSITION_SIZE_USD} @ {lev_short}x  price≈{opportunity.short_price:.4f}")
        # Fake results for dry-run
        from traders.base import TradeResult as TR
        trade.long_entry_result = TR(opportunity.long_exchange, opportunity.symbol,
                                     opportunity.symbol + "USDT", "buy",
                                     POSITION_SIZE_USD / opportunity.long_price,
                                     opportunity.long_price, POSITION_SIZE_USD, lev_long)
        trade.short_entry_result = TR(opportunity.short_exchange, opportunity.symbol,
                                      opportunity.symbol + "USDT", "sell",
                                      POSITION_SIZE_USD / opportunity.short_price,
                                      opportunity.short_price, POSITION_SIZE_USD, lev_short)
        return trade

    from traders.base import TradeResult as TR

    def _open_long():
        nonlocal long_result
        try:
            long_result = long_trader.open_long(
                opportunity.symbol, POSITION_SIZE_USD, opportunity.long_price, lev_long)
        except Exception as e:
            long_result = TR(opportunity.long_exchange, opportunity.symbol,
                             opportunity.symbol + "USDT", "buy", 0, 0, 0, lev_long, error=str(e))

    def _open_short():
        nonlocal short_result
        try:
            short_result = short_trader.open_short(
                opportunity.symbol, POSITION_SIZE_USD, opportunity.short_price, lev_short)
        except Exception as e:
            short_result = TR(opportunity.short_exchange, opportunity.symbol,
                              opportunity.symbol + "USDT", "sell", 0, 0, 0, lev_short, error=str(e))

    t1 = threading.Thread(target=_open_long)
    t2 = threading.Thread(target=_open_short)
    t1.start(); t2.start()
    t1.join(); t2.join()

    trade.long_entry_result = long_result
    trade.short_entry_result = short_result

    long_ok  = long_result  and long_result.success
    short_ok = short_result and short_result.success

    if not long_ok:
        trade.error = f"Long entry failed ({opportunity.long_exchange}): {long_result.error if long_result else 'no response'}"
    if not short_ok:
        trade.error += f"{' | ' if trade.error else ''}Short entry failed ({opportunity.short_exchange}): {short_result.error if short_result else 'no response'}"

    # Partial fill: one leg opened, the other didn't — close the opened leg immediately
    # to avoid a naked directional exposure.
    if long_ok and not short_ok:
        print(f"  {Fore.RED}[PARTIAL ENTRY] Short leg failed — closing long on {opportunity.long_exchange} to flatten{Style.RESET_ALL}")
        try:
            long_trader.close_long(opportunity.symbol, long_result.qty)
        except Exception as e:
            trade.error += f" | EMERGENCY CLOSE FAILED: {e}"
    elif short_ok and not long_ok:
        print(f"  {Fore.RED}[PARTIAL ENTRY] Long leg failed — closing short on {opportunity.short_exchange} to flatten{Style.RESET_ALL}")
        try:
            short_trader.close_short(opportunity.symbol, short_result.qty)
        except Exception as e:
            trade.error += f" | EMERGENCY CLOSE FAILED: {e}"

    return trade


def real_exit(trade: RealTrade, exit_long_price: float, exit_short_price: float) -> RealTrade:
    """
    Close both legs simultaneously and compute full P&L.
    exit_long_price / exit_short_price are the mark prices at exit time (used for P&L).
    Actual fill prices come from the exchange responses.
    """
    trade.exit_ts = time.time()

    long_trader = get_trader(trade.long_exchange)
    short_trader = get_trader(trade.short_exchange)

    long_exit: Optional[TradeResult] = None
    short_exit: Optional[TradeResult] = None

    if DRY_RUN:
        print(f"  [DRY-RUN] Would close LONG  {trade.long_exchange}  {trade.symbol}"
              f"  qty={trade.long_qty:.4f}  price≈{exit_long_price:.4f}")
        print(f"  [DRY-RUN] Would close SHORT {trade.short_exchange}  {trade.symbol}"
              f"  qty={trade.short_qty:.4f}  price≈{exit_short_price:.4f}")
        from traders.base import TradeResult as TR
        long_exit = TR(trade.long_exchange, trade.symbol, trade.symbol + "USDT", "sell",
                       trade.long_qty, exit_long_price, trade.long_qty * exit_long_price, 1)
        short_exit = TR(trade.short_exchange, trade.symbol, trade.symbol + "USDT", "buy",
                        trade.short_qty, exit_short_price, trade.short_qty * exit_short_price, 1)
    else:
        from traders.base import TradeResult as TR

        def _close_long():
            nonlocal long_exit
            try:
                long_exit = long_trader.close_long(trade.symbol, trade.long_qty)
            except Exception as e:
                long_exit = TR(trade.long_exchange, trade.symbol,
                               trade.symbol + "USDT", "sell", 0, 0, 0, 1, error=str(e))

        def _close_short():
            nonlocal short_exit
            try:
                short_exit = short_trader.close_short(trade.symbol, trade.short_qty)
            except Exception as e:
                short_exit = TR(trade.short_exchange, trade.symbol,
                                trade.symbol + "USDT", "buy", 0, 0, 0, 1, error=str(e))

        t1 = threading.Thread(target=_close_long)
        t2 = threading.Thread(target=_close_short)
        t1.start(); t2.start()
        t1.join(); t2.join()

    # Patch fill_price=0: exchange responses for market close orders are often async.
    # Use exit mark price (fetched at T+0) as the actual fill — it's the best available.
    if long_exit and long_exit.success and long_exit.fill_price == 0 and exit_long_price > 0:
        long_exit.fill_price = exit_long_price
        long_exit.notional_usd = long_exit.qty * exit_long_price
    if short_exit and short_exit.success and short_exit.fill_price == 0 and exit_short_price > 0:
        short_exit.fill_price = exit_short_price
        short_exit.notional_usd = short_exit.qty * exit_short_price

    trade.long_exit_result = long_exit
    trade.short_exit_result = short_exit

    # Use actual fill prices if available, else fall back to mark prices
    actual_exit_long = (long_exit.fill_price if long_exit and long_exit.success
                        else exit_long_price)
    actual_exit_short = (short_exit.fill_price if short_exit and short_exit.success
                         else exit_short_price)

    size = trade.position_size_usd

    # ── Price P&L ──────────────────────────────────────────────────────────────
    long_pnl_pct = ((actual_exit_long - trade.entry_long_price) / trade.entry_long_price * 100
                    if trade.entry_long_price else 0)
    short_pnl_pct = ((trade.entry_short_price - actual_exit_short) / trade.entry_short_price * 100
                     if trade.entry_short_price else 0)
    trade.long_price_pnl_usd  = size * long_pnl_pct  / 100
    trade.short_price_pnl_usd = size * short_pnl_pct / 100

    # ── Funding P&L ────────────────────────────────────────────────────────────
    short_funding = size * trade.short_rate_pct / 100
    long_funding  = -size * trade.long_rate_pct / 100

    if trade.primary_is_short:
        trade.funding_collected_usd = short_funding
        trade.funding_paid_usd      = long_funding if trade.hedge_credits else 0.0
    else:
        trade.funding_collected_usd = short_funding if trade.hedge_credits else 0.0
        trade.funding_paid_usd      = long_funding

    trade.net_funding_usd = trade.funding_collected_usd + trade.funding_paid_usd

    # ── Fees ───────────────────────────────────────────────────────────────────
    trade.total_fee_usd = size * (trade.long_fee_pct * 2 + trade.short_fee_pct * 2) / 100

    # ── Net ────────────────────────────────────────────────────────────────────
    trade.net_pnl_usd = (trade.long_price_pnl_usd + trade.short_price_pnl_usd
                         + trade.net_funding_usd - trade.total_fee_usd)
    trade.completed = True
    return trade


# ── Order connectivity test ───────────────────────────────────────────────────

TEST_ORDER_NOTIONAL = 30.0   # USD notional for the round-trip connectivity test
TEST_ORDER_SYMBOL   = "ETH"  # ETH ~$2500: $30 ≈ 0.012 ETH, above min qty on all exchanges


def run_order_connectivity_test(all_data: Dict[str, list]) -> Dict[str, Tuple[bool, str]]:
    """
    For each initialised trader, open an $11 long and immediately close it.
    Used at startup to verify API keys have order-placement permission.

    Only runs in LIVE mode — returns an empty dict when DRY_RUN is True.
    Returns {exchange: (success, message)}.
    """
    if DRY_RUN:
        return {}

    results: Dict[str, Tuple[bool, str]] = {}

    for name, trader in _traders.items():
        # Find mark price for BTC from the already-fetched rate data
        mark_price = 0.0
        for fd in all_data.get(name, []):
            if fd.symbol == TEST_ORDER_SYMBOL and fd.mark_price and fd.mark_price > 0:
                mark_price = fd.mark_price
                break

        if mark_price <= 0:
            results[name] = (False, f"no {TEST_ORDER_SYMBOL} price from {name} — skipped")
            continue

        try:
            try:
                trader.set_leverage(TEST_ORDER_SYMBOL, TARGET_LEVERAGE)
            except Exception:
                pass
            entry = trader.open_long(TEST_ORDER_SYMBOL, TEST_ORDER_NOTIONAL, mark_price, TARGET_LEVERAGE)
            if not entry.success:
                results[name] = (False, f"open failed: {entry.error}")
                continue
            time.sleep(2)  # wait for async order to settle before closing
            close = trader.close_long(TEST_ORDER_SYMBOL, entry.qty)
            if not close.success:
                results[name] = (
                    False,
                    f"⚠️ OPEN OK (qty={entry.qty:.6f}) but CLOSE FAILED: {close.error} — "
                    f"manual close required on {name}"
                )
            else:
                results[name] = (
                    True,
                    f"open {entry.qty:.6f} @ ${entry.fill_price:,.2f}  →  "
                    f"close @ ${close.fill_price:,.2f}"
                )
        except Exception as e:
            results[name] = (False, repr(e) or type(e).__name__)

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def _ts(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%H:%M:%S.%f UTC")


def _pnl_color(val: float) -> str:
    color = Fore.GREEN if val >= 0 else Fore.RED
    return color + Style.BRIGHT + f"${val:+.4f}" + Style.RESET_ALL


def print_trade_report(trade: RealTrade) -> None:
    W = 72
    sep  = Style.BRIGHT + "─" * W + Style.RESET_ALL
    sep2 = Style.BRIGHT + "═" * W + Style.RESET_ALL
    mode = "[DRY-RUN]" if DRY_RUN else "[LIVE]"

    print()
    print(sep2)
    print(Style.BRIGHT + f"  {mode} TRADE REPORT — {trade.symbol}" + Style.RESET_ALL)
    print(sep2)

    # ── Entry ─────────────────────────────────────────────────────────────────
    print(f"\n  ENTRY  [{_ts(trade.entry_ts)}]  (T-10s)  leverage: {trade.leverage_used}x")
    print(f"    Long   {trade.long_exchange:<14}  qty: {trade.long_qty:.6f}  "
          f"fill: ${trade.entry_long_price:,.4f}")
    print(f"    Short  {trade.short_exchange:<14}  qty: {trade.short_qty:.6f}  "
          f"fill: ${trade.entry_short_price:,.4f}")
    print(f"    Position size: ${trade.position_size_usd:.2f} per leg")

    # ── Funding ───────────────────────────────────────────────────────────────
    epoch_str = _ts(trade.funding_ts_ms / 1000)
    print(f"\n  FUNDING EPOCH  [{epoch_str}]")
    size = trade.position_size_usd
    short_fires = trade.primary_is_short or trade.hedge_credits
    long_fires  = (not trade.primary_is_short) or trade.hedge_credits

    def _leg(usd, fires, lbl_pos, lbl_neg):
        if not fires:
            return f"{Fore.YELLOW}(not fired — hedge only){Style.RESET_ALL}"
        c = Fore.GREEN if usd >= 0 else Fore.RED
        lbl = lbl_pos if usd >= 0 else lbl_neg
        return f"{c}{Style.BRIGHT}${usd:+.4f}{Style.RESET_ALL}  ({lbl})"

    short_usd =  size * trade.short_rate_pct / 100
    long_usd  = -size * trade.long_rate_pct  / 100
    print(f"    Short  {trade.short_exchange:<14}  rate: {trade.short_rate_pct:+.4f}%  "
          f"→  {_leg(short_usd, short_fires, 'collected', 'paid')}")
    print(f"    Long   {trade.long_exchange:<14}  rate: {trade.long_rate_pct:+.4f}%  "
          f"→  {_leg(long_usd, long_fires, 'collected', 'paid')}")
    print(f"    {'Net funding':38}  {_pnl_color(trade.net_funding_usd)}")

    # ── Exit ──────────────────────────────────────────────────────────────────
    print(f"\n  EXIT   [{_ts(trade.exit_ts)}]  ({EXIT_WAIT_SECONDS}s post-epoch)")
    if trade.entry_long_price:
        lp = (trade.exit_long_price - trade.entry_long_price) / trade.entry_long_price * 100
    else:
        lp = 0
    if trade.entry_short_price:
        sp = (trade.entry_short_price - trade.exit_short_price) / trade.entry_short_price * 100
    else:
        sp = 0
    print(f"    Long   {trade.long_exchange:<14}  "
          f"${trade.entry_long_price:,.4f} → ${trade.exit_long_price:,.4f}  "
          f"({lp:+.4f}%)  {_pnl_color(trade.long_price_pnl_usd)}")
    print(f"    Short  {trade.short_exchange:<14}  "
          f"${trade.entry_short_price:,.4f} → ${trade.exit_short_price:,.4f}  "
          f"({sp:+.4f}%)  {_pnl_color(trade.short_price_pnl_usd)}")
    print(f"    {'Price P&L combined':38}  "
          f"{_pnl_color(trade.long_price_pnl_usd + trade.short_price_pnl_usd)}")

    # ── Fees ──────────────────────────────────────────────────────────────────
    print(f"\n  FEES")
    print(f"    Long   {trade.long_exchange:<14}  {trade.long_fee_pct:.3f}% × 2 (o+c) = "
          f"{Fore.RED}${size * trade.long_fee_pct * 2 / 100:.4f}{Style.RESET_ALL}")
    print(f"    Short  {trade.short_exchange:<14}  {trade.short_fee_pct:.3f}% × 2 (o+c) = "
          f"{Fore.RED}${size * trade.short_fee_pct * 2 / 100:.4f}{Style.RESET_ALL}")
    print(f"    {'Total fees':38}  {Fore.RED}${trade.total_fee_usd:.4f}{Style.RESET_ALL}")

    # ── Balances ───────────────────────────────────────────────────────────────
    if trade.pre_balances or trade.post_balances:
        print(f"\n  BALANCES  (pre-entry → post-exit)")
        all_exch = sorted(set(list(trade.pre_balances) + list(trade.post_balances)))
        total_pre  = sum(trade.pre_balances.values())
        total_post = sum(trade.post_balances.values())
        for ex in all_exch:
            pre  = trade.pre_balances.get(ex, 0.0)
            post = trade.post_balances.get(ex, 0.0)
            delta = post - pre
            dc = Fore.GREEN if delta >= 0 else Fore.RED
            print(f"    {ex:<14}  ${pre:.2f} → ${post:.2f}  "
                  f"({dc}{delta:+.4f}{Style.RESET_ALL})")
        td = total_post - total_pre
        tc = Fore.GREEN if td >= 0 else Fore.RED
        print(f"    {'─'*50}")
        print(f"    {'TOTAL USDT':<14}  ${total_pre:.2f} → ${total_post:.2f}  "
              f"({tc}{Style.BRIGHT}{td:+.4f}{Style.RESET_ALL})")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(sep)
    price_combined = trade.long_price_pnl_usd + trade.short_price_pnl_usd
    print(f"  {'Price P&L (long + short):':<38}  {_pnl_color(price_combined)}")
    print(f"  {'Net funding received:':<38}  {_pnl_color(trade.net_funding_usd)}")
    print(f"  {'Total fees paid:':<38}  {Fore.RED}${trade.total_fee_usd:.4f}{Style.RESET_ALL}")
    print(sep)
    net_color = Fore.GREEN if trade.net_pnl_usd >= 0 else Fore.RED
    result = "PROFIT" if trade.net_pnl_usd >= 0 else "LOSS"
    print(f"  {'NET P&L:':<38}  "
          f"{net_color}{Style.BRIGHT}${trade.net_pnl_usd:+.4f}  [{result}]{Style.RESET_ALL}")
    if trade.error:
        print(f"  {Fore.RED}ERRORS: {trade.error}{Style.RESET_ALL}")
    print(sep2)
    print()
