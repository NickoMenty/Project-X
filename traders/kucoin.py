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
    FUNDING_SETTLE_WAIT_S = 30  # KuCoin settles funding with ~30s delay after epoch

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.key = api_key
        self.secret = api_secret
        # For KC-API-KEY-VERSION=2 the passphrase header must itself be signed
        self.passphrase_signed = base64.b64encode(
            hmac.new(api_secret.encode(), passphrase.encode(), hashlib.sha256).digest()
        ).decode()
        self._lot_cache: Dict[str, float] = {}
        self._max_lev_cache: Dict[str, int] = {}

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
            self._max_lev_cache[kc_sym] = int(data.get("maxLeverage", 100) or 100)
            return mult
        except Exception:
            self._lot_cache[kc_sym] = 1.0
            self._max_lev_cache[kc_sym] = 100
            return 1.0

    def _get_max_leverage(self, kc_sym: str) -> int:
        if kc_sym not in self._max_lev_cache:
            self._load_lot_size(kc_sym)
        return self._max_lev_cache.get(kc_sym, 100)

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
        # KuCoin leverage is set per-order via the "leverage" field in the payload.
        # There is no global set-leverage endpoint — return the requested value directly.
        return leverage

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int,
               reduce_only: bool = False, close_lots: int = 0) -> TradeResult:
        kc_sym = self._kc_symbol(symbol)
        lots = close_lots if close_lots else self._lots(symbol, notional_usd, mark_price)

        actual_leverage = leverage

        payload: dict = {
            "clientOid":  str(uuid.uuid4()),
            "symbol":     kc_sym,
            "side":       side,
            "type":       "market",
            "leverage":   str(actual_leverage),
            "size":       lots,
            "marginMode": "CROSS",
        }
        if reduce_only:
            payload["reduceOnly"] = True

        # 330008 = margin too tight for this quantity. Retry with progressively smaller
        # lots until the order fits (e.g. KuCoin's effective margin rate for the symbol
        # may be higher than 1/leverage, or available balance is partially consumed).
        last_err: Optional[str] = None
        for scale in (1.0, 0.75, 0.5, 0.25):
            scaled_lots = max(1, int(lots * scale))
            payload["clientOid"] = str(uuid.uuid4())  # fresh ID each attempt
            payload["size"] = scaled_lots
            try:
                resp = self._post("/api/v1/orders", payload)
                oid = resp.get("orderId", "")
                mult = self._load_lot_size(kc_sym)
                qty = scaled_lots * mult
                if scale < 1.0:
                    print(f"  [KuCoin] placed at {int(scale*100)}% size ({scaled_lots} lots) after margin retry")
                return TradeResult(self.exchange_name, symbol, kc_sym, side,
                                   qty, mark_price, qty * mark_price, actual_leverage, order_id=str(oid))
            except RuntimeError as e:
                last_err = str(e)
                if "330008" not in last_err or reduce_only:
                    raise  # don't retry on other errors or close orders

        raise RuntimeError(last_err or "KuCoin: all size retries exhausted")

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

    def fetch_order_fills(self, symbol: str, order_id: str, since_ms: int, until_ms: int):
        kc_sym = self._kc_symbol(symbol)
        try:
            params = {"symbol": kc_sym}
            if order_id:
                params["orderId"] = order_id
            data = self._get("/api/v1/fills", params)
            items = data.get("items", [])
            if not order_id:
                items = [x for x in items
                         if since_ms <= int(x.get("createdAt", 0)) <= until_ms]
            if not items:
                return None, None
            mult = self._load_lot_size(kc_sym)
            total_size = sum(float(x.get("size", 0)) for x in items)  # size in lots
            if total_size <= 0:
                return None, None
            avg_px = sum(float(x["price"]) * float(x["size"]) for x in items) / total_size
            total_fee = sum(abs(float(x.get("fee", 0))) for x in items)
            return avg_px, total_fee
        except Exception:
            return None, None

    def fetch_funding_payment(self, symbol: str, since_ms: int, until_ms: int):
        kc_sym = self._kc_symbol(symbol)
        try:
            data = self._get("/api/v1/funding-history", {
                "symbol": kc_sym,
                "startAt": since_ms,
                "endAt": until_ms,
                "maxCount": 50,
            })
            items = data.get("dataList", [])
            if not items:
                return None
            return sum(float(x.get("funding", 0)) for x in items)
        except Exception:
            return None
