"""
AsterDex Perpetuals trading client (Binance-compatible API).
Uses same signing and endpoints as Binance Futures with a different base URL.
"""
import hashlib
import hmac
import math
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE_URLS = [
    "https://api.asterdex.com",
    "https://www.asterdex.com",
]


class AsterDexTrader(BaseTrader):
    exchange_name = "AsterDex"

    def __init__(self, api_key: str, api_secret: str):
        self.key = api_key
        self.secret = api_secret
        self._base: Optional[str] = None
        self._step_cache: Dict[str, float] = {}
        self._min_cache: Dict[str, float] = {}
        self._time_offset_ms: int = 0
        self._sync_time()

    def _sync_time(self):
        try:
            base = self._get_base()
            resp = requests.get(f"{base}/fapi/v1/time", timeout=5).json()
            server_ms = int(resp["serverTime"])
            self._time_offset_ms = server_ms - int(time.time() * 1000)
        except Exception:
            self._time_offset_ms = 0

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _get_base(self) -> str:
        if self._base:
            return self._base
        for url in BASE_URLS:
            try:
                r = requests.get(f"{url}/fapi/v1/time", timeout=5)
                if r.status_code == 200:
                    self._base = url
                    return url
            except Exception:
                continue
        raise RuntimeError("AsterDex: all base URLs unreachable")

    # ── Auth (same as Binance) ─────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        return f"{qs}&signature={sig}"

    def _headers(self) -> dict:
        return {
            "X-MBX-APIKEY": self.key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    def _req(self, method: str, path: str, params: dict, retries: int = 3) -> dict:
        last_err = None
        # Try all base URLs on each attempt to work around per-URL blocks
        bases_to_try = BASE_URLS if self._base is None else [self._base] + [u for u in BASE_URLS if u != self._base]
        for attempt in range(retries):
            base = bases_to_try[attempt % len(bases_to_try)]
            try:
                params["timestamp"] = self._now_ms()
                params.setdefault("recvWindow", 10000)
                url = f"{base}{path}?{self._sign(params)}"
                resp = requests.request(method, url, headers=self._headers(), timeout=10)
                text = resp.text.strip()
                if not text:
                    raise RuntimeError(f"Empty response (HTTP {resp.status_code})")
                if resp.status_code == 403:
                    raise RuntimeError(f"HTTP 403 from {base}: {text[:200]}")
                try:
                    data = resp.json()
                except Exception:
                    raise RuntimeError(f"Non-JSON response (HTTP {resp.status_code}): {text[:300]}")
                if isinstance(data, dict) and int(data.get("code", 0)) < 0:
                    raise RuntimeError(f"AsterDex {data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"AsterDex {method} {path} failed after {retries} attempts: {last_err}")

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_info(self, raw: str):
        if raw in self._step_cache:
            return
        base = self._get_base()
        info = requests.get(f"{base}/fapi/v1/exchangeInfo", timeout=10).json()
        for sym in info.get("symbols", []):
            if sym["symbol"] != raw:
                continue
            for f in sym.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    self._step_cache[raw] = float(f["stepSize"])
                    self._min_cache[raw] = float(f["minQty"])
            break
        if raw not in self._step_cache:
            self._step_cache[raw] = 1.0
            self._min_cache[raw] = 0.0

    def _round_qty(self, raw: str, qty: float) -> float:
        return self.round_step(qty, self._step_cache.get(raw, 1.0))

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
        min_qty = self._min_cache.get(raw, 0.0)
        if qty < min_qty:
            return f"qty {qty} < minQty {min_qty} for {raw}"
        return None

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._req("GET", "/fapi/v2/balance", {})
        if isinstance(data, list):
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
                if "-4028" in msg:  # already at this leverage value
                    return lev
                if "-4055" in msg:  # open position exists — use as-is
                    return lev
                continue
        raise RuntimeError(f"AsterDex: could not set leverage for {raw}")

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int, reduce_only: bool = False,
               close_qty: float = 0.0) -> TradeResult:
        raw = self.fmt_symbol(symbol)
        self._load_lot_info(raw)

        qty = self._round_qty(raw, close_qty if reduce_only else notional_usd / mark_price)
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
        avg = resp.get("avgPrice", "0")
        fill = float(avg) if avg and float(avg) > 0 else float(resp.get("price") or mark_price)
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
