"""
MEXC Futures (Contract) trading client.
API base: https://contract.mexc.com
Auth: ApiKey + Request-Time + Signature (HMAC-SHA256 of apiKey+timestamp+body)
"""
import hashlib
import hmac
import json
import math
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://contract.mexc.com"


class MEXCTrader(BaseTrader):
    exchange_name = "MEXC"

    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key
        self.secret = api_secret
        self._step_cache: Dict[str, float] = {}   # raw_symbol → stepSize (vol step)
        self._contract_size_cache: Dict[str, float] = {}

    def fmt_symbol(self, symbol: str) -> str:
        return f"{symbol}_USDT"

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign(self, ts: str, body_str: str = "") -> str:
        msg = self.key + ts + body_str
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _headers(self, ts: str, body_str: str = "") -> dict:
        return {
            "ApiKey": self.key,
            "Request-Time": ts,
            "Signature": self._sign(ts, body_str),
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        last_err = None
        for _ in range(retries):
            try:
                ts = str(int(time.time() * 1000))
                body = json.dumps(payload)
                resp = requests.post(f"{BASE}{path}", headers=self._headers(ts, body),
                                     data=body, timeout=10)
                if not resp.text:
                    raise RuntimeError("Empty response body")
                data = resp.json()
                if not data.get("success", True):
                    raise RuntimeError(f"MEXC error: {data.get('message', data)}")
                return data.get("data", {}) or {}
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"MEXC POST {path} failed after {retries} attempts: {last_err}")

    def _get(self, path: str, params: dict = {}, retries: int = 3) -> dict:
        last_err = None
        for _ in range(retries):
            try:
                ts = str(int(time.time() * 1000))
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                resp = requests.get(f"{BASE}{path}?{qs}", headers=self._headers(ts), timeout=10)
                if not resp.text:
                    raise RuntimeError("Empty response body")
                data = resp.json()
                if not data.get("success", True):
                    raise RuntimeError(f"MEXC error: {data.get('message', data)}")
                return data.get("data", {}) or {}
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"MEXC GET {path} failed after {retries} attempts: {last_err}")

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_contract_info(self, raw: str):
        if raw in self._step_cache:
            return
        data = requests.get(f"{BASE}/api/v1/contract/detail", timeout=10).json()
        for c in data.get("data", []):
            if c["symbol"] == raw:
                self._step_cache[raw] = float(c.get("volUnit", 1))
                self._contract_size_cache[raw] = float(c.get("contractSize", 1))
                break
        if raw not in self._step_cache:
            self._step_cache[raw] = 1.0
            self._contract_size_cache[raw] = 1.0

    def _qty_to_vol(self, raw: str, notional_usd: float, mark_price: float) -> int:
        """Convert notional USD to MEXC contract vol (integer contracts)."""
        contract_size = self._contract_size_cache.get(raw, 1.0)
        step = self._step_cache.get(raw, 1.0)
        qty = notional_usd / mark_price / contract_size
        vol = max(1, int(math.floor(qty / step) * step))
        return vol

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("/api/v1/private/account/assets")
        if isinstance(data, list):
            for asset in data:
                if asset.get("currency") == "USDT":
                    return float(asset.get("availableBalance", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        raw = self.fmt_symbol(symbol)
        for lev in [leverage, 5]:
            try:
                self._post("/api/v1/private/position/change_leverage", {
                    "symbol": raw,
                    "leverage": lev,
                    "openType": 1,  # isolated
                    "positionType": 1,  # long side (set both via separate call if needed)
                })
                return lev
            except RuntimeError:
                continue
        return 5

    def _place(self, symbol: str, side_val: int, notional_usd: float,
               mark_price: float, leverage: int, close_vol: int = 0) -> TradeResult:
        """
        MEXC order sides:
          1 = open long, 2 = close short, 3 = open short, 4 = close long
        """
        raw = self.fmt_symbol(symbol)
        self._load_contract_info(raw)

        if close_vol:
            vol = close_vol
        else:
            vol = self._qty_to_vol(raw, notional_usd, mark_price)

        side_str = {1: "buy", 2: "buy", 3: "sell", 4: "sell"}[side_val]

        payload = {
            "symbol": raw,
            "side": side_val,
            "openType": 1,
            "type": 5,  # market
            "vol": vol,
            "leverage": leverage,
        }
        resp = self._post("/api/v1/private/order/submit", payload)
        oid = str(resp) if isinstance(resp, (int, str)) else str(resp.get("orderId", ""))
        contract_size = self._contract_size_cache.get(raw, 1.0)
        qty = vol * contract_size
        return TradeResult(self.exchange_name, symbol, raw, side_str, qty,
                           mark_price, qty * mark_price, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, 1, notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, 3, notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        raw = self.fmt_symbol(symbol)
        self._load_contract_info(raw)
        contract_size = self._contract_size_cache.get(raw, 1.0)
        vol = max(1, int(round(qty / contract_size)))
        return self._place(symbol, 4, 0, 0, 1, close_vol=vol)

    def close_short(self, symbol, qty):
        raw = self.fmt_symbol(symbol)
        self._load_contract_info(raw)
        contract_size = self._contract_size_cache.get(raw, 1.0)
        vol = max(1, int(round(qty / contract_size)))
        return self._place(symbol, 2, 0, 0, 1, close_vol=vol)
