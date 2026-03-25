#!/usr/bin/env python3
"""
fetch_history.py — Last 10 closed perp trade PnL reports per exchange.

Usage:
    python fetch_history.py                    # all exchanges
    python fetch_history.py --exchange Bybit   # single exchange
    python fetch_history.py --limit 20         # more rows

Columns: Date | Exchange | Token | Qty | Notional USD | Entry | Exit | Fees | Funding | Position PnL
"""

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests
from tabulate import tabulate

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ClosedTrade:
    exchange: str
    date: str           # ISO datetime
    token: str
    qty: float          # amount in token
    notional_usd: float # amount in USD (qty × entry_price)
    entry_price: float
    exit_price: float
    fees_usd: float
    funding_usd: float
    position_pnl: float  # price move PnL only (before fees/funding)

    # Display helpers
    def net_pnl(self) -> float:
        return self.position_pnl + self.funding_usd - self.fees_usd

    def fmt(self, v: float, decimals: int = 4, prefix: str = "") -> str:
        if math.isnan(v):
            return "—"
        return f"{prefix}{v:+.{decimals}f}" if prefix else f"{v:.{decimals}f}"


_NA = float("nan")


def _ts_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ── Binance ───────────────────────────────────────────────────────────────────

class BinanceFetcher:
    BASE = "https://fapi.binance.com"

    def __init__(self, key: str, secret: str):
        self.key    = key
        self.secret = secret
        self._offset = self._sync_time()

    def _sync_time(self) -> int:
        try:
            r = requests.get(f"{self.BASE}/fapi/v1/time", timeout=5).json()
            return int(r["serverTime"]) - int(time.time() * 1000)
        except Exception:
            return 0

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._offset

    def _sign(self, params: dict) -> str:
        qs  = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _get(self, path: str, params: dict) -> list | dict:
        params["timestamp"] = self._now_ms()
        url = f"{self.BASE}{path}?{self._sign(params)}"
        r   = requests.get(url, headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        # Get recent REALIZED_PNL income events (one per position close)
        since_ms = self._now_ms() - 90 * 24 * 3600 * 1000
        pnl_events = self._get("/fapi/v1/income", {
            "incomeType": "REALIZED_PNL", "limit": min(limit, 100),
            "startTime": since_ms,
        })
        if not isinstance(pnl_events, list) or not pnl_events:
            return []

        # Collect symbols and time windows from those events
        records: List[ClosedTrade] = []
        for ev in pnl_events[:limit]:
            sym      = ev.get("symbol", "")
            pnl      = float(ev.get("income", 0))
            ts_ms    = int(ev.get("time", 0))
            trade_id = str(ev.get("tradeId", ""))

            # Fetch the closing trade fill for this event
            fills = []
            if sym:
                try:
                    fills = self._get("/fapi/v1/userTrades", {
                        "symbol": sym, "fromId": trade_id,
                        "limit": 1,
                    }) or []
                except Exception:
                    pass

            # Fetch fees: sum COMMISSION events near this trade
            fees = 0.0
            try:
                comm = self._get("/fapi/v1/income", {
                    "symbol": sym, "incomeType": "COMMISSION",
                    "startTime": ts_ms - 60_000, "endTime": ts_ms + 60_000,
                })
                fees = sum(abs(float(c.get("income", 0))) for c in (comm or []))
            except Exception:
                pass

            close_price = float(fills[0].get("price", 0)) if fills else _NA
            qty         = float(fills[0].get("qty", 0))   if fills else _NA
            fee_fill    = float(fills[0].get("commission", 0)) if fills else 0.0
            fees        = fees if fees else fee_fill

            token = sym.replace("USDT", "").replace("PERP", "")
            records.append(ClosedTrade(
                exchange="Binance",
                date=_ts_to_date(ts_ms),
                token=token,
                qty=qty,
                notional_usd=qty * close_price if not math.isnan(qty * close_price) else _NA,
                entry_price=_NA,    # Binance income API doesn't expose entry price
                exit_price=close_price,
                fees_usd=fees,
                funding_usd=_NA,
                position_pnl=pnl,
            ))
        return records


# ── Bybit ─────────────────────────────────────────────────────────────────────

class BybitFetcher:
    BASE = "https://api.bybit.com"
    RECV = "10000"

    def __init__(self, key: str, secret: str):
        self.key    = key
        self.secret = secret
        self._offset = self._sync_time()

    def _sync_time(self) -> int:
        try:
            r = requests.get(f"{self.BASE}/v5/market/time", timeout=5).json()
            return int(r["result"]["timeNano"]) // 1_000_000 - int(time.time() * 1000)
        except Exception:
            return 0

    def _now_ms(self) -> str:
        return str(int(time.time() * 1000) + self._offset)

    def _sign(self, ts: str, qs: str) -> str:
        msg = ts + self.key + self.RECV + qs
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _get(self, path: str, params: dict) -> dict:
        ts  = self._now_ms()
        qs  = "&".join(f"{k}={v}" for k, v in params.items())
        sig = self._sign(ts, qs)
        hdrs = {
            "X-BAPI-API-KEY": self.key, "X-BAPI-SIGN": sig,
            "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.RECV,
        }
        r = requests.get(f"{self.BASE}{path}?{qs}", headers=hdrs, timeout=10)
        r.raise_for_status()
        return r.json()

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        data = self._get("/v5/position/closed-pnl", {
            "category": "linear", "limit": min(limit, 100),
        })
        items = data.get("result", {}).get("list", [])

        records: List[ClosedTrade] = []
        for it in items[:limit]:
            sym        = it.get("symbol", "")
            qty        = float(it.get("closedSize") or it.get("qty") or 0)
            entry_px   = float(it.get("avgEntryPrice") or 0)
            exit_px    = float(it.get("avgExitPrice")  or 0)
            pnl        = float(it.get("closedPnl") or 0)
            ts_ms      = int(it.get("updatedTime") or it.get("createdTime") or 0)
            token      = sym.replace("USDT", "").replace("PERP", "")

            # Fetch fees from transaction log for this symbol around close time
            fees     = 0.0
            funding  = 0.0
            if ts_ms:
                try:
                    fee_data = self._get("/v5/account/transaction-log", {
                        "category": "linear", "currency": "USDT", "type": "TRADE",
                        "startTime": ts_ms - 120_000, "endTime": ts_ms + 10_000,
                        "limit": "50",
                    })
                    for f in fee_data.get("result", {}).get("list", []):
                        if f.get("symbol") == sym:
                            fees += abs(float(f.get("fee", 0)))
                except Exception:
                    pass
                try:
                    fund_data = self._get("/v5/account/transaction-log", {
                        "category": "linear", "currency": "USDT", "type": "SETTLEMENT",
                        "startTime": ts_ms - 600_000, "endTime": ts_ms + 10_000,
                        "limit": "50",
                    })
                    for f in fund_data.get("result", {}).get("list", []):
                        if f.get("symbol") == sym:
                            funding += float(f.get("cashFlow", 0))
                except Exception:
                    pass

            records.append(ClosedTrade(
                exchange="Bybit",
                date=_ts_to_date(ts_ms) if ts_ms else "—",
                token=token,
                qty=qty,
                notional_usd=qty * entry_px,
                entry_price=entry_px,
                exit_price=exit_px,
                fees_usd=fees,
                funding_usd=funding,
                position_pnl=pnl,
            ))
        return records


# ── Bitget ────────────────────────────────────────────────────────────────────

class BitgetFetcher:
    BASE = "https://api.bitget.com"

    def __init__(self, key: str, secret: str, passphrase: str):
        self.key        = key
        self.secret     = secret
        self.passphrase = passphrase

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        raw = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def _headers(self, ts: str, method: str, path: str) -> dict:
        return {
            "ACCESS-KEY":        self.key,
            "ACCESS-SIGN":       self._sign(ts, method, path),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":      "application/json",
        }

    def _get(self, path: str, params: dict) -> dict:
        ts  = str(int(time.time() * 1000))
        qs  = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        r   = requests.get(f"{self.BASE}{path}{qs}",
                           headers=self._headers(ts, "GET", path + qs), timeout=10)
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        data  = self._get("/api/v2/mix/position/history-position", {
            "productType": "usdt-futures", "pageSize": str(min(limit, 100)),
        })
        items = data.get("list", []) if isinstance(data, dict) else (data or [])

        records: List[ClosedTrade] = []
        for it in items[:limit]:
            sym       = it.get("symbol", "")
            qty       = float(it.get("openTotalPos") or it.get("holdVolume") or 0)
            entry_px  = float(it.get("openPriceAvg")  or 0)
            exit_px   = float(it.get("closePriceAvg") or 0)
            open_fee  = abs(float(it.get("openFee")  or 0))
            close_fee = abs(float(it.get("closeFee") or 0))
            funding   = float(it.get("totalFunding") or it.get("netProfit", 0)) # fallback
            net_pnl   = float(it.get("netProfit")    or 0)
            # netProfit on Bitget = price PnL + funding - fees
            price_pnl = net_pnl - funding + open_fee + close_fee
            ts_ms     = int(it.get("uTime") or it.get("cTime") or 0)
            token     = sym.replace("USDT", "").replace("_UMCBL", "")

            # Try separate funding amount if available
            try:
                funding = float(it.get("totalFunding") or 0)
            except Exception:
                funding = _NA

            records.append(ClosedTrade(
                exchange="Bitget",
                date=_ts_to_date(ts_ms) if ts_ms else "—",
                token=token,
                qty=qty,
                notional_usd=qty * entry_px,
                entry_price=entry_px,
                exit_price=exit_px,
                fees_usd=open_fee + close_fee,
                funding_usd=funding,
                position_pnl=price_pnl,
            ))
        return records


# ── Hyperliquid ───────────────────────────────────────────────────────────────

class HyperliquidFetcher:
    BASE = "https://api.hyperliquid.xyz"

    def __init__(self, private_key: str):
        from eth_account import Account
        key = private_key.strip().strip('"').strip("'")
        if not key.startswith("0x"):
            key = "0x" + key
        self.address = Account.from_key(key).address

    def _post(self, payload: dict) -> dict | list:
        r = requests.post(f"{self.BASE}/info",
                          json=payload,
                          headers={"Content-Type": "application/json"},
                          timeout=15)
        r.raise_for_status()
        return r.json()

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        # Get recent fills and reconstruct closed positions
        since_ms = int(time.time() * 1000) - 90 * 24 * 3600 * 1000
        fills = self._post({"type": "userFills", "user": self.address, "startTime": since_ms})
        if not isinstance(fills, list):
            return []

        # Group fills into positions: each consecutive run of same dir = one leg
        # Match paired open+close fills by coin and direction reversal
        from collections import defaultdict
        positions: dict[str, list] = defaultdict(list)
        for f in fills:
            positions[f.get("coin", "")].append(f)

        records: List[ClosedTrade] = []
        for coin, coin_fills in positions.items():
            coin_fills.sort(key=lambda x: x.get("time", 0))
            # Walk through fills to find open→close pairs
            open_fills:  List[dict] = []
            close_fills: List[dict] = []
            for f in coin_fills:
                dir_ = f.get("dir", "")
                if "Open" in dir_:
                    open_fills.append(f)
                elif "Close" in dir_:
                    close_fills.append(f)

            # Pair up closes with corresponding opens
            for i, cf in enumerate(close_fills[-limit:]):
                ts_ms     = int(cf.get("time", 0))
                close_px  = float(cf.get("px", 0))
                close_qty = abs(float(cf.get("sz", 0)))
                close_fee = abs(float(cf.get("fee", 0)))

                # Find the open fill closest before this close
                of = open_fills[i] if i < len(open_fills) else {}
                open_px   = float(of.get("px", 0)) if of else _NA
                open_fee  = abs(float(of.get("fee", 0))) if of else 0.0

                pnl = float(cf.get("closedPnl", 0)) if "closedPnl" in cf else _NA

                records.append(ClosedTrade(
                    exchange="Hyperliquid",
                    date=_ts_to_date(ts_ms) if ts_ms else "—",
                    token=coin,
                    qty=close_qty,
                    notional_usd=close_qty * open_px if not math.isnan(open_px) else _NA,
                    entry_price=open_px,
                    exit_price=close_px,
                    fees_usd=open_fee + close_fee,
                    funding_usd=_NA,
                    position_pnl=pnl,
                ))

        # Sort by date desc and return latest N
        records.sort(key=lambda r: r.date, reverse=True)
        return records[:limit]


# ── AsterDex ──────────────────────────────────────────────────────────────────

class AsterDexFetcher:
    """AsterDex uses a Binance-compatible API."""

    def __init__(self, wallet: str, private_key: str):
        # AsterDex signs via wallet address + private key (EIP-712 or simple HMAC)
        # Use the wallet as the API key and private key as secret for income endpoint
        self.wallet = wallet.strip()
        self.key    = private_key.strip().strip('"').strip("'")
        # Derive BASE from asterdex trader
        from traders.asterdex import AsterDexTrader
        trader = AsterDexTrader(wallet, private_key)
        self._trader = trader

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        try:
            data = self._trader._req("GET", "/fapi/v1/income", {
                "incomeType": "REALIZED_PNL", "limit": min(limit, 100),
            })
        except Exception:
            return []

        if not isinstance(data, list):
            return []

        records: List[ClosedTrade] = []
        for ev in data[:limit]:
            sym   = ev.get("symbol", "")
            pnl   = float(ev.get("income", 0))
            ts_ms = int(ev.get("time", 0))
            token = sym.replace("USDT", "").replace("PERP", "")

            # Try to get fill details
            fill_px = _NA
            qty     = _NA
            fees    = _NA
            try:
                trades = self._trader._req("GET", "/fapi/v1/userTrades", {
                    "symbol": sym, "limit": 5,
                })
                if isinstance(trades, list) and trades:
                    t      = trades[0]
                    fill_px = float(t.get("price", 0))
                    qty     = float(t.get("qty", 0))
                    fees    = abs(float(t.get("commission", 0)))
            except Exception:
                pass

            records.append(ClosedTrade(
                exchange="AsterDex",
                date=_ts_to_date(ts_ms) if ts_ms else "—",
                token=token,
                qty=qty,
                notional_usd=qty * fill_px if not (math.isnan(qty) or math.isnan(fill_px)) else _NA,
                entry_price=_NA,
                exit_price=fill_px,
                fees_usd=fees,
                funding_usd=_NA,
                position_pnl=pnl,
            ))
        return records


# ── KuCoin ────────────────────────────────────────────────────────────────────

class KuCoinFetcher:
    BASE = "https://api-futures.kucoin.com"

    def __init__(self, key: str, secret: str, passphrase: str):
        self.key  = key
        self.secret = secret
        self.passphrase_signed = base64.b64encode(
            hmac.new(secret.encode(), passphrase.encode(), hashlib.sha256).digest()
        ).decode()

    def _sign(self, ts: str, method: str, endpoint: str, body: str = "") -> str:
        msg = ts + method.upper() + endpoint + body
        raw = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def _headers(self, method: str, endpoint: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KC-API-KEY":          self.key,
            "KC-API-SIGN":         self._sign(ts, method, endpoint),
            "KC-API-TIMESTAMP":    ts,
            "KC-API-PASSPHRASE":   self.passphrase_signed,
            "KC-API-KEY-VERSION":  "2",
        }

    def _get(self, path: str, params: dict = {}) -> dict:
        qs   = ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))) if params else ""
        full = path + qs
        r    = requests.get(f"{self.BASE}{full}", headers=self._headers("GET", full), timeout=10)
        data = r.json()
        if data.get("code") != "200000":
            raise RuntimeError(f"KuCoin {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    def fetch(self, limit: int = 10) -> List[ClosedTrade]:
        # KuCoin fills give us individual trade fills; group by symbol into positions
        data  = self._get("/api/v1/fills", {"pageSize": "100"})
        fills = data.get("items", [])

        # Group consecutive fills per symbol into position open/close pairs
        from collections import defaultdict
        by_sym: dict[str, list] = defaultdict(list)
        for f in fills:
            by_sym[f.get("symbol", "")].append(f)

        records: List[ClosedTrade] = []
        for kc_sym, sym_fills in by_sym.items():
            sym_fills.sort(key=lambda x: int(x.get("createdAt", 0)), reverse=True)
            token = kc_sym.replace("USDTM", "").replace("XBT", "BTC")

            # Pair up: look for open/close pairs (side reversal)
            opens:  List[dict] = [f for f in sym_fills if not f.get("stop")]
            closes: List[dict] = []

            # KuCoin fills don't distinguish open/close directly — use tradeType
            opens  = [f for f in sym_fills if f.get("tradeType") in ("open", None)]
            closes = [f for f in sym_fills if f.get("tradeType") == "close"]

            if not closes:
                # Fall back: alternate fills as open/close pairs
                opens  = sym_fills[1::2]
                closes = sym_fills[0::2]

            for cf in closes[:limit]:
                ts_ms     = int(cf.get("createdAt", 0))
                close_px  = float(cf.get("price", 0))
                close_qty = float(cf.get("size", 0)) * float(cf.get("matchSize") or 1)
                close_fee = abs(float(cf.get("fee", 0)))

                # Match with corresponding open
                of = opens[closes.index(cf)] if closes.index(cf) < len(opens) else {}
                open_px  = float(of.get("price", 0)) if of else _NA
                open_fee = abs(float(of.get("fee", 0))) if of else 0.0

                # Load lot multiplier for qty calculation
                mult = 1.0
                try:
                    contract = self._get(f"/api/v1/contracts/{kc_sym}")
                    mult = float(contract.get("multiplier", 1) or 1)
                except Exception:
                    pass

                actual_qty = float(cf.get("size", 0)) * mult
                pnl        = _NA   # KuCoin fills don't expose realized PnL per fill

                # Fetch realized PnL from income statement
                try:
                    history = self._get("/api/v1/recentDoneOrders", {"symbol": kc_sym})
                    for order in history.get("items") or []:
                        if order.get("status") == "done":
                            pnl = float(order.get("closePnl", 0) or 0)
                            break
                except Exception:
                    pass

                # Funding in same time window
                funding = _NA
                try:
                    fund_data = self._get("/api/v1/funding-history", {
                        "symbol": kc_sym,
                        "startAt": ts_ms - 8 * 3600 * 1000,
                        "endAt":   ts_ms + 3600 * 1000,
                        "maxCount": 5,
                    })
                    items = fund_data.get("dataList", [])
                    if items:
                        funding = sum(float(x.get("funding", 0)) for x in items)
                except Exception:
                    pass

                records.append(ClosedTrade(
                    exchange="KuCoin",
                    date=_ts_to_date(ts_ms) if ts_ms else "—",
                    token=token,
                    qty=actual_qty,
                    notional_usd=actual_qty * (open_px if not math.isnan(open_px) else close_px),
                    entry_price=open_px,
                    exit_price=close_px,
                    fees_usd=open_fee + close_fee,
                    funding_usd=funding,
                    position_pnl=pnl,
                ))

        records.sort(key=lambda r: r.date, reverse=True)
        return records[:limit]


# ── Display ───────────────────────────────────────────────────────────────────

def _fmt(v: float, prefix: str = "", decimals: int = 4) -> str:
    if math.isnan(v):
        return "—"
    if prefix == "$":
        return f"${v:+.{decimals}f}" if v != 0 else f"$0.{'0'*decimals}"
    return f"{v:.{decimals}f}"


def display(records: List[ClosedTrade]) -> None:
    if not records:
        print("  No records found.")
        return

    rows = []
    for r in records:
        rows.append([
            r.date,
            r.exchange,
            r.token,
            _fmt(r.qty,          decimals=4),
            _fmt(r.notional_usd, prefix="$", decimals=2).lstrip("+"),
            _fmt(r.entry_price,  decimals=4),
            _fmt(r.exit_price,   decimals=4),
            _fmt(r.fees_usd,     prefix="$", decimals=4).lstrip("+"),
            _fmt(r.funding_usd,  prefix="$", decimals=4),
            _fmt(r.position_pnl, prefix="$", decimals=4),
        ])

    headers = ["Date (UTC)", "Exchange", "Token", "Qty", "Notional",
               "Entry", "Exit", "Fees", "Funding", "Price PnL"]
    print(tabulate(rows, headers=headers, tablefmt="simple"))


# ── Main ──────────────────────────────────────────────────────────────────────

FETCHERS = {
    "Binance":     lambda: BinanceFetcher(
                       os.environ["BINANCE_KEY"], os.environ["BINANCE_SECRET"]),
    "Bybit":       lambda: BybitFetcher(
                       os.environ["BYBIT_KEY"], os.environ["BYBIT_SECRET"]),
    "Bitget":      lambda: BitgetFetcher(
                       os.environ["BITGET_KEY"], os.environ["BITGET_SECRET"], os.environ["BITGET_PASS"]),
    "Hyperliquid": lambda: HyperliquidFetcher(os.environ["HYPERLIQUID_API"]),
    "AsterDex":    lambda: AsterDexFetcher(
                       os.environ["ASTER_WALLET"], os.environ["ASTER_PRIVATE"]),
    "KuCoin":      lambda: KuCoinFetcher(
                       os.environ["KUCOIN_KEY"], os.environ["KUCOIN_SECRET"], os.environ["KUCOIN_PASS"]),
}


def main():
    ap = argparse.ArgumentParser(description="Fetch last N closed perp trades per exchange.")
    ap.add_argument("--exchange", help="Single exchange name (Binance, Bybit, Bitget, Hyperliquid, AsterDex, KuCoin)")
    ap.add_argument("--limit", type=int, default=10, help="Rows per exchange (default: 10)")
    ap.add_argument("--all-in-one", action="store_true", help="Combine all exchanges into one sorted table")
    args = ap.parse_args()

    targets = [args.exchange] if args.exchange else list(FETCHERS.keys())
    all_records: List[ClosedTrade] = []

    for name in targets:
        if name not in FETCHERS:
            print(f"Unknown exchange: {name}. Options: {', '.join(FETCHERS)}")
            continue
        print(f"\nFetching {name}...")
        try:
            fetcher  = FETCHERS[name]()
            records  = fetcher.fetch(limit=args.limit)
            all_records.extend(records)
            if not args.all_in_one:
                print(f"\n{'═'*80}")
                print(f"  {name}  —  last {len(records)} closed trades")
                print(f"{'═'*80}")
                display(records)
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    if args.all_in_one and all_records:
        all_records.sort(key=lambda r: r.date, reverse=True)
        print(f"\n{'═'*80}")
        print(f"  All exchanges — {len(all_records)} records")
        print(f"{'═'*80}")
        display(all_records)


if __name__ == "__main__":
    main()
