"""
Bybit Linear Perpetuals trading client (V5 API).
Docs: https://bybit-exchange.github.io/docs/v5/
"""
import hashlib
import hmac
import json
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE = "https://api.bybit.com"
RECV_WINDOW = "5000"


class BybitTrader(BaseTrader):
    exchange_name = "Bybit"

    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key
        self.secret = api_secret
        self._step_cache: Dict[str, float] = {}
        self._min_cache: Dict[str, float] = {}

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _sign_post(self, ts: str, body_str: str) -> str:
        msg = ts + self.key + RECV_WINDOW + body_str
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _sign_get(self, ts: str, qs: str) -> str:
        msg = ts + self.key + RECV_WINDOW + qs
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _headers(self, ts: str, sig: str) -> dict:
        return {
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-SIGN": sig,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        ts = str(int(time.time() * 1000))
        body = json.dumps(payload)
        sig = self._sign_post(ts, body)
        resp = requests.post(f"{BASE}{path}", headers=self._headers(ts, sig),
                             data=body, timeout=10)
        data = resp.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {})

    def _get(self, path: str, params: dict) -> dict:
        ts = str(int(time.time() * 1000))
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sig = self._sign_get(ts, qs)
        resp = requests.get(f"{BASE}{path}?{qs}", headers=self._headers(ts, sig), timeout=10)
        data = resp.json()
        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {})

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_info(self, raw: str):
        if raw in self._step_cache:
            return
        result = self._get("/v5/market/instruments-info", {"category": "linear", "symbol": raw})
        for item in result.get("list", []):
            lot = item.get("lotSizeFilter", {})
            self._step_cache[raw] = float(lot.get("qtyStep", 1))
            self._min_cache[raw] = float(lot.get("minOrderQty", 0))

    def _round_qty(self, raw: str, qty: float) -> float:
        return self.round_step(qty, self._step_cache.get(raw, 1.0))

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
        min_qty = self._min_cache.get(raw, 0.0)
        if qty < min_qty:
            return f"qty {qty} < minOrderQty {min_qty} for {raw}"
        return None

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        # Try UNIFIED first (modern accounts), fall back to CONTRACT (classic)
        for account_type in ["UNIFIED", "CONTRACT"]:
            try:
                result = self._get("/v5/account/wallet-balance", {"accountType": account_type})
                for acct in result.get("list", []):
                    for coin in acct.get("coin", []):
                        if coin.get("coin") == "USDT":
                            val = float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0)
                            if val > 0:
                                return val
            except Exception:
                continue
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        raw = self.fmt_symbol(symbol)
        for lev in [leverage, 5]:
            try:
                self._post("/v5/position/set-leverage", {
                    "category": "linear",
                    "symbol": raw,
                    "buyLeverage": str(lev),
                    "sellLeverage": str(lev),
                })
                return lev
            except RuntimeError as e:
                if "leverage not modified" in str(e).lower() or "110043" in str(e):
                    return lev  # already set to this value
                continue
        raise RuntimeError(f"Bybit: could not set leverage for {raw}")

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int, reduce_only: bool = False,
               close_qty: float = 0.0) -> TradeResult:
        raw = self.fmt_symbol(symbol)
        self._load_lot_info(raw)

        qty = self._round_qty(raw, close_qty if reduce_only else notional_usd / mark_price)
        err = self._check_min(raw, qty)
        if err:
            return TradeResult(self.exchange_name, symbol, raw, side, 0, 0, notional_usd, leverage, error=err)

        payload = {
            "category": "linear",
            "symbol": raw,
            "side": "Buy" if side == "buy" else "Sell",
            "orderType": "Market",
            "qty": str(qty),
            "reduceOnly": reduce_only,
        }
        result = self._post("/v5/order/create", payload)
        oid = result.get("orderId", "")
        # Bybit market orders fill async; use mark_price as estimate
        fill = mark_price if not reduce_only else mark_price
        return TradeResult(self.exchange_name, symbol, raw, side, qty, fill,
                           qty * fill, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "buy", notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "sell", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, "sell", 0, 0, 1, reduce_only=True, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, "buy", 0, 0, 1, reduce_only=True, close_qty=qty)
