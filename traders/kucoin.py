"""
KuCoin Futures USDT-M trading client (v1 API).
Docs: https://docs.kucoin.com/futures/
Auth: KC-API-KEY + KC-API-SIGN (Base64 HMAC-SHA256) + KC-API-TIMESTAMP
      + KC-API-PASSPHRASE (Base64 HMAC-SHA256 of passphrase with secret)
      + KC-API-KEY-VERSION=2

Position mode: one-way; use reduceOnly=true to close.
Contract size: size in lots; 1 lot = multiplier base asset.
  e.g. XBTUSDTM multiplier = 0.001 BTC, ETHUSDTM multiplier = 0.01 ETH
"""
import base64
import hashlib
import hmac
import json
import math
import time
import uuid
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://api-futures.kucoin.com"

# KuCoin uses XBT for Bitcoin in symbol names
_SYMBOL_MAP = {
    "BTC": "XBTUSDTM",
}


class KuCoinTrader(BaseTrader):
    exchange_name = "KuCoin"

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.key = api_key
        self.secret = api_secret
        # For KC-API-KEY-VERSION=2 the passphrase header must itself be signed
        self.passphrase_signed = base64.b64encode(
            hmac.new(api_secret.encode(), passphrase.encode(), hashlib.sha256).digest()
        ).decode()
        self._lot_cache: Dict[str, float] = {}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _kc_symbol(self, symbol: str) -> str:
        """Map normalised symbol to KuCoin futures symbol, e.g. BTC -> XBTUSDTM."""
        if symbol in _SYMBOL_MAP:
            return _SYMBOL_MAP[symbol]
        return f"{symbol}USDTM"

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign(self, ts: str, method: str, endpoint: str, body: str = "") -> str:
        msg = ts + method.upper() + endpoint + body
        raw = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(raw).decode()

    def _headers(self, method: str, endpoint: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "KC-API-KEY":          self.key,
            "KC-API-SIGN":         self._sign(ts, method, endpoint, body),
            "KC-API-TIMESTAMP":    ts,
            "KC-API-PASSPHRASE":   self.passphrase_signed,
            "KC-API-KEY-VERSION":  "2",
            "Content-Type":        "application/json",
        }

    def _post(self, endpoint: str, payload: dict) -> dict:
        body = json.dumps(payload)
        resp = requests.post(f"{BASE}{endpoint}", headers=self._headers("POST", endpoint, body),
                             data=body, timeout=10)
        data = resp.json()
        if data.get("code") != "200000":
            raise RuntimeError(f"KuCoin {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    def _get(self, endpoint: str, params: dict = {}) -> dict:
        qs = ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))) if params else ""
        full = endpoint + qs
        resp = requests.get(f"{BASE}{full}", headers=self._headers("GET", full), timeout=10)
        data = resp.json()
        if data.get("code") != "200000":
            raise RuntimeError(f"KuCoin {data.get('code')}: {data.get('msg')}")
        return data.get("data", {}) or {}

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_size(self, kc_sym: str) -> float:
        """Return the multiplier (base asset per lot) for the symbol."""
        if kc_sym in self._lot_cache:
            return self._lot_cache[kc_sym]
        try:
            data = self._get(f"/api/v1/contracts/{kc_sym}")
            mult = float(data.get("multiplier", 1) or 1)
            self._lot_cache[kc_sym] = mult
            return mult
        except Exception:
            self._lot_cache[kc_sym] = 1.0
            return 1.0

    def _lots(self, symbol: str, notional_usd: float, mark_price: float) -> int:
        kc_sym = self._kc_symbol(symbol)
        mult = self._load_lot_size(kc_sym)
        lots = notional_usd / (mark_price * mult)
        return max(1, math.floor(lots))

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._get("/api/v1/account-overview", {"currency": "USDT"})
        return float(data.get("availableBalance", 0))

    def set_leverage(self, symbol: str, leverage: int) -> int:
        kc_sym = self._kc_symbol(symbol)
        for lev in [leverage, 5]:
            try:
                self._post("/api/v1/position/risk-limit-level/change", {
                    "symbol": kc_sym,
                })
                # KuCoin sets leverage per order; nothing to set globally
                return lev
            except Exception:
                continue
        return 5

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int,
               reduce_only: bool = False, close_lots: int = 0) -> TradeResult:
        kc_sym = self._kc_symbol(symbol)
        lots = close_lots if close_lots else self._lots(symbol, notional_usd, mark_price)

        payload: dict = {
            "clientOid": str(uuid.uuid4()),
            "symbol":    kc_sym,
            "side":      side,
            "type":      "market",
            "leverage":  str(leverage),
            "size":      lots,
        }
        if reduce_only:
            payload["reduceOnly"] = True

        resp = self._post("/api/v1/orders", payload)
        oid = resp.get("orderId", "")

        mult = self._load_lot_size(kc_sym)
        qty = lots * mult
        return TradeResult(self.exchange_name, symbol, kc_sym, side,
                           qty, mark_price, qty * mark_price, leverage, order_id=str(oid))

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "buy", notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "sell", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        kc_sym = self._kc_symbol(symbol)
        mult = self._load_lot_size(kc_sym)
        lots = max(1, round(qty / mult))
        return self._place(symbol, "sell", 0, 0, 1, reduce_only=True, close_lots=lots)

    def close_short(self, symbol, qty):
        kc_sym = self._kc_symbol(symbol)
        mult = self._load_lot_size(kc_sym)
        lots = max(1, round(qty / mult))
        return self._place(symbol, "buy", 0, 0, 1, reduce_only=True, close_lots=lots)

    def fmt_symbol(self, symbol: str) -> str:
        return self._kc_symbol(symbol)
