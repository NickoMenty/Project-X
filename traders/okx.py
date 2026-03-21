"""
OKX USDT-M Perpetual Swap trading client (v5 API).
Docs: https://www.okx.com/docs-v5/en/
Auth: OK-ACCESS-KEY + OK-ACCESS-SIGN (Base64 HMAC-SHA256) + OK-ACCESS-TIMESTAMP + OK-ACCESS-PASSPHRASE

Position mode: hedge (long/short posSide) — set once via set-position-mode.
Contract size: sz in contracts; 1 contract = ctVal base asset (e.g., 0.01 BTC, 0.1 ETH).
"""
import base64
import hashlib
import hmac
import json
import math
import os
import time
import requests
from datetime import datetime, timezone
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://www.okx.com"


class OKXTrader(BaseTrader):
    exchange_name = "OKX"

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.key = api_key
        self.secret = api_secret
        self.passphrase = passphrase
        self._ct_val_cache: Dict[str, float] = {}
        self._set_hedge_mode()

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        raw = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = self._ts()
        return {
            "OK-ACCESS-KEY":        self.key,
            "OK-ACCESS-SIGN":       self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":         "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload)
        resp = requests.post(f"{BASE}{path}", headers=self._headers("POST", path, body),
                             data=body, timeout=10)
        data = resp.json()
        if data.get("code") != "0":
            # Trade endpoints wrap the real error in data[0].sCode/sMsg
            inner = (data.get("data") or [{}])[0]
            s_code = inner.get("sCode", data.get("code"))
            s_msg  = inner.get("sMsg") or data.get("msg")
            raise RuntimeError(f"OKX {s_code}: {s_msg}")
        # Also check inner sCode for trade order responses (code=0 but order rejected)
        inner = (data.get("data") or [{}])[0]
        if inner.get("sCode") and inner["sCode"] != "0":
            raise RuntimeError(f"OKX {inner['sCode']}: {inner.get('sMsg')}")
        return data

    def _get(self, path: str, params: dict = {}) -> dict:
        qs = ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))) if params else ""
        full_path = path + qs
        resp = requests.get(f"{BASE}{full_path}", headers=self._headers("GET", full_path),
                            timeout=10)
        data = resp.json()
        if data.get("code") != "0":
            raise RuntimeError(f"OKX {data.get('code')}: {data.get('msg')}")
        return data

    # ── Instrument info ────────────────────────────────────────────────────────

    def _inst_id(self, symbol: str) -> str:
        return f"{symbol}-USDT-SWAP"

    def _load_ct_val(self, symbol: str) -> float:
        if symbol in self._ct_val_cache:
            return self._ct_val_cache[symbol]
        inst_id = self._inst_id(symbol)
        try:
            data = self._get("/api/v5/public/instruments",
                             {"instType": "SWAP", "instId": inst_id})
            items = data.get("data", [])
            if items:
                ct_val = float(items[0].get("ctVal", 1))
                self._ct_val_cache[symbol] = ct_val
                return ct_val
        except Exception:
            pass
        self._ct_val_cache[symbol] = 1.0
        return 1.0

    def _contracts(self, symbol: str, notional_usd: float, mark_price: float) -> int:
        """Number of whole contracts for a given notional."""
        ct_val = self._load_ct_val(symbol)
        contracts = notional_usd / (mark_price * ct_val)
        return max(1, math.floor(contracts))

    # ── Position mode ──────────────────────────────────────────────────────────

    def _set_hedge_mode(self):
        try:
            self._post("/api/v5/account/set-position-mode", {"posMode": "long_short_mode"})
        except Exception:
            pass  # Already in hedge mode or unsupported — continue

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("/api/v5/account/balance", {"ccy": "USDT"})
        for detail in data.get("data", [{}])[0].get("details", []):
            if detail.get("ccy") == "USDT":
                return float(detail.get("availBal", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        inst_id = self._inst_id(symbol)
        for lev in [leverage, 5]:
            try:
                for pos_side in ("long", "short"):
                    self._post("/api/v5/account/set-leverage", {
                        "instId":  inst_id,
                        "lever":   str(lev),
                        "mgnMode": "cross",
                        "posSide": pos_side,
                    })
                return lev
            except RuntimeError:
                continue
        return 5

    def _place(self, symbol: str, side: str, pos_side: str,
               notional_usd: float, mark_price: float, leverage: int,
               close_qty: int = 0) -> TradeResult:
        inst_id = self._inst_id(symbol)
        sz = close_qty if close_qty else self._contracts(symbol, notional_usd, mark_price)

        payload = {
            "instId":  inst_id,
            "tdMode":  "cross",
            "posSide": pos_side,
            "side":    side,
            "ordType": "market",
            "sz":      str(sz),
        }
        resp = self._post("/api/v5/trade/order", payload)
        items = resp.get("data", [{}])
        oid = items[0].get("ordId", "") if items else ""

        ct_val = self._load_ct_val(symbol)
        qty = sz * ct_val
        return TradeResult(self.exchange_name, symbol, inst_id, side,
                           qty, mark_price, qty * mark_price, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "buy", "long", notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "sell", "short", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        ct_val = self._load_ct_val(symbol)
        contracts = max(1, round(qty / ct_val))
        return self._place(symbol, "sell", "long", 0, 0, 1, close_qty=contracts)

    def close_short(self, symbol, qty):
        ct_val = self._load_ct_val(symbol)
        contracts = max(1, round(qty / ct_val))
        return self._place(symbol, "buy", "short", 0, 0, 1, close_qty=contracts)

    def fmt_symbol(self, symbol: str) -> str:
        return self._inst_id(symbol)
