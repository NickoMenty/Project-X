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

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.key}

    def _req(self, method: str, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
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
            except RuntimeError:
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
        fill = float(resp.get("avgPrice") or resp.get("price") or mark_price)
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
