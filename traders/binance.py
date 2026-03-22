"""
Binance USDT-M Futures trading client.
Docs: https://binance-docs.github.io/apidocs/futures/en/
"""
import hashlib
import hmac
import math
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://fapi.binance.com"


class BinanceTrader(BaseTrader):
    exchange_name = "Binance"

    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key
        self.secret = api_secret
        self._step_cache: Dict[str, float] = {}   # symbol → stepSize
        self._min_cache: Dict[str, float] = {}    # symbol → minQty
        self._time_offset_ms: int = 0
        self._sync_time()

    def _sync_time(self):
        try:
            resp = requests.get(f"{BASE}/fapi/v1/time", timeout=5).json()
            server_ms = int(resp["serverTime"])
            self._time_offset_ms = server_ms - int(time.time() * 1000)
        except Exception:
            self._time_offset_ms = 0

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.key}

    def _req(self, method: str, path: str, params: dict) -> dict:
        params["timestamp"] = self._now_ms()
        params.setdefault("recvWindow", 10000)
        url = f"{BASE}{path}?{self._sign(params)}"
        resp = requests.request(method, url, headers=self._headers(), timeout=10)
        data = resp.json()
        if isinstance(data, dict) and int(data.get("code", 0)) < 0:
            raise RuntimeError(f"Binance {data.get('code')}: {data.get('msg')}")
        return data

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_info(self, raw: str):
        if raw in self._step_cache:
            return
        info = requests.get(f"{BASE}/fapi/v1/exchangeInfo", timeout=10).json()
        for sym in info.get("symbols", []):
            if sym["symbol"] != raw:
                continue
            for f in sym.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    self._step_cache[raw] = float(f["stepSize"])
                    self._min_cache[raw] = float(f["minQty"])
            break

    def _round_qty(self, raw: str, qty: float) -> float:
        step = self._step_cache.get(raw, 1.0)
        return self.round_step(qty, step)

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
        min_qty = self._min_cache.get(raw, 0.0)
        if qty < min_qty:
            return f"qty {qty} < minQty {min_qty} for {raw}"
        return None

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._req("GET", "/fapi/v2/balance", {})
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        raw = self.fmt_symbol(symbol)
        for lev in [leverage, 5]:
            try:
                self._req("POST", "/fapi/v1/leverage", {"symbol": raw, "leverage": lev})
                return lev
            except RuntimeError as e:
                msg = str(e)
                if "-4028" in msg:   # already at this leverage value
                    return lev
                if "-4055" in msg:   # open position exists — can't change, use as-is
                    return lev
                continue
        raise RuntimeError(f"Binance: could not set leverage for {raw}")

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int, reduce_only: bool = False,
               close_qty: float = 0.0) -> TradeResult:
        raw = self.fmt_symbol(symbol)
        self._load_lot_info(raw)

        if reduce_only:
            qty = self._round_qty(raw, close_qty)
        else:
            qty = self._round_qty(raw, notional_usd / mark_price)

        err = self._check_min(raw, qty)
        if err:
            return TradeResult(self.exchange_name, symbol, raw, side, 0, 0, notional_usd, leverage, error=err)

        params = {
            "symbol": raw,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": qty,
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        resp = self._req("POST", "/fapi/v1/order", params)
        avg   = float(resp.get("avgPrice") or 0)
        price = float(resp.get("price") or 0)
        fill  = avg if avg > 0 else (price if price > 0 else mark_price)
        oid = str(resp.get("orderId", ""))
        return TradeResult(self.exchange_name, symbol, raw, side, qty, fill,
                           qty * fill, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "BUY", notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "SELL", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, "SELL", 0, 0, 1, reduce_only=True, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, "BUY", 0, 0, 1, reduce_only=True, close_qty=qty)

    def fetch_order_fills(self, symbol: str, order_id: str, since_ms: int, until_ms: int):
        raw = self.fmt_symbol(symbol)
        try:
            params = {"symbol": raw, "startTime": since_ms, "endTime": until_ms, "limit": 100}
            if order_id:
                params["orderId"] = int(order_id)
            trades = self._req("GET", "/fapi/v1/userTrades", params)
            if not isinstance(trades, list) or not trades:
                return None, None
            total_qty = sum(float(t.get("qty", 0)) for t in trades)
            if total_qty <= 0:
                return None, None
            avg_px = sum(float(t["price"]) * float(t["qty"]) for t in trades) / total_qty
            total_fee = sum(float(t.get("commission", 0)) for t in trades
                            if t.get("commissionAsset") == "USDT")
            return avg_px, total_fee
        except Exception:
            return None, None

    def fetch_funding_payment(self, symbol: str, since_ms: int, until_ms: int):
        raw = self.fmt_symbol(symbol)
        try:
            data = self._req("GET", "/fapi/v1/income", {
                "symbol": raw, "incomeType": "FUNDING_FEE",
                "startTime": since_ms, "endTime": until_ms, "limit": 100,
            })
            if not isinstance(data, list) or not data:
                return None
            return sum(float(x.get("income", 0)) for x in data)
        except Exception:
            return None
