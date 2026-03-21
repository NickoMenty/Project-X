"""
Bitget USDT-M Futures trading client (V2 API).
Docs: https://www.bitget.com/api-doc/contract/intro
Auth: ACCESS-KEY + ACCESS-SIGN (base64 HMAC-SHA256) + ACCESS-TIMESTAMP + ACCESS-PASSPHRASE
"""
import base64
import hashlib
import hmac
import json
import math
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://api.bitget.com"


class BitgetTrader(BaseTrader):
    exchange_name = "Bitget"

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.key = api_key
        self.secret = api_secret
        self.passphrase = passphrase
        self._step_cache: Dict[str, float] = {}
        self._min_cache: Dict[str, float] = {}

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        raw = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def _headers(self, ts: str, method: str, path: str, body: str = "") -> dict:
        return {
            "ACCESS-KEY": self.key,
            "ACCESS-SIGN": self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        ts = str(int(time.time() * 1000))
        body = json.dumps(payload)
        resp = requests.post(f"{BASE}{path}", headers=self._headers(ts, "POST", path, body),
                             data=body, timeout=10)
        data = resp.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    def _get(self, path: str, params: dict = {}) -> dict:
        ts = str(int(time.time() * 1000))
        qs = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        full_path = path + qs
        resp = requests.get(f"{BASE}{full_path}", headers=self._headers(ts, "GET", full_path),
                            timeout=10)
        data = resp.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_info(self, raw: str):
        if raw in self._step_cache:
            return
        data = self._get("/api/v2/mix/market/contracts",
                         {"productType": "usdt-futures", "symbol": raw})
        items = data if isinstance(data, list) else data.get("data", [])
        for item in items:
            if item.get("symbol") == raw:
                self._step_cache[raw] = float(item.get("sizeMultiplier") or item.get("sizeIncrement") or 1)
                self._min_cache[raw] = float(item.get("minTradeNum", 1))
                break
        if raw not in self._step_cache:
            self._step_cache[raw] = 1.0
            self._min_cache[raw] = 1.0

    def _round_qty(self, raw: str, qty: float) -> float:
        return self.round_step(qty, self._step_cache.get(raw, 1.0))

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
        min_qty = self._min_cache.get(raw, 0.0)
        if qty < min_qty:
            return f"qty {qty} < minTradeNum {min_qty} for {raw}"
        return None

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("/api/v2/mix/account/accounts", {"productType": "usdt-futures"})
        items = data if isinstance(data, list) else []
        for acct in items:
            if acct.get("marginCoin") == "USDT":
                return float(acct.get("available", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        raw = self.fmt_symbol(symbol)
        for lev in [leverage, 5]:
            try:
                self._post("/api/v2/mix/account/set-leverage", {
                    "symbol": raw,
                    "productType": "usdt-futures",
                    "marginCoin": "USDT",
                    "leverage": str(lev),
                    "holdSide": "long",
                })
                self._post("/api/v2/mix/account/set-leverage", {
                    "symbol": raw,
                    "productType": "usdt-futures",
                    "marginCoin": "USDT",
                    "leverage": str(lev),
                    "holdSide": "short",
                })
                return lev
            except RuntimeError:
                continue
        raise RuntimeError(f"Bitget: could not set leverage for {raw}")

    def _place(self, symbol: str, side: str, trade_side: str, notional_usd: float,
               mark_price: float, leverage: int, close_qty: float = 0.0) -> TradeResult:
        """
        side: 'buy' or 'sell'
        trade_side: 'open' or 'close'
        """
        raw = self.fmt_symbol(symbol)
        self._load_lot_info(raw)

        qty = self._round_qty(raw, close_qty if trade_side == "close" else notional_usd / mark_price)
        err = self._check_min(raw, qty)
        if err:
            return TradeResult(self.exchange_name, symbol, raw, side, 0, 0, notional_usd, leverage, error=err)

        payload = {
            "symbol": raw,
            "productType": "usdt-futures",
            "marginCoin": "USDT",
            "size": str(qty),
            "side": side,
            "tradeSide": trade_side,
            "orderType": "market",
        }
        payload["marginMode"] = "isolated"
        resp = self._post("/api/v2/mix/order/place-order", payload)
        oid = str(resp.get("orderId", ""))
        fill = mark_price  # Bitget market orders fill async; use mark as estimate
        return TradeResult(self.exchange_name, symbol, raw, side, qty, fill,
                           qty * fill, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "buy", "open", notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "sell", "open", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, "sell", "close", 0, 0, 1, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, "buy", "close", 0, 0, 1, close_qty=qty)
