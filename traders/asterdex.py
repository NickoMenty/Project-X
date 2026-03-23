"""
AsterDex Perpetuals trading client (v3 API — EIP-712 private key auth).

Auth: EIP-712 structured data signing (name=AsterSignTransaction, chainId=1666)
Nonce: microseconds (time.time() * 1_000_000)
Params: user (main wallet), signer (API wallet derived from private key), nonce, signature
Docs: https://github.com/asterdex/api-docs/blob/master/aster-finance-futures-api-v3.md
"""
import copy
import time
import urllib.parse
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

BASE_URLS = [
    "https://fapi.asterdex.com",
    "https://fapi3.asterdex.com",
    "https://api.asterdex.com",
]

_TYPED_DATA = {
    "types": {
        "EIP712Domain": [
            {"name": "name",             "type": "string"},
            {"name": "version",          "type": "string"},
            {"name": "chainId",          "type": "uint256"},
            {"name": "verifyingContract","type": "address"},
        ],
        "Message": [
            {"name": "msg", "type": "string"},
        ],
    },
    "primaryType": "Message",
    "domain": {
        "name":             "AsterSignTransaction",
        "version":          "1",
        "chainId":          1666,
        "verifyingContract":"0x0000000000000000000000000000000000000000",
    },
    "message": {"msg": ""},
}

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent":   "PythonApp/1.0",
}


class AsterDexTrader(BaseTrader):
    exchange_name = "AsterDex"

    def __init__(self, user: str, private_key: str):
        """
        user        — main account wallet address (ASTER_WALLET)
        private_key — private key of the API signer wallet (ASTER_PRIVATE)
        """
        from eth_account import Account
        self.user = user
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self._account = Account.from_key(private_key)
        self.signer = self._account.address
        self._step_cache: Dict[str, float] = {}
        self._min_cache:  Dict[str, float] = {}
        self._max_cache:  Dict[str, float] = {}
        self._nonce_sec = 0
        self._nonce_i   = 0
        self._base = self._resolve_base()

    def _resolve_base(self) -> str:
        """Pick the first reachable base URL."""
        for url in BASE_URLS:
            try:
                r = requests.get(f"{url}/fapi/v1/exchangeInfo", timeout=5)
                if r.status_code < 500:
                    return url
            except Exception:
                continue
        return BASE_URLS[0]  # fall back to first, let errors surface naturally

    # ── Nonce ──────────────────────────────────────────────────────────────────

    def _get_nonce(self) -> int:
        """Monotonically increasing microsecond nonce (per AsterDex v3 spec)."""
        now = int(time.time())
        if now == self._nonce_sec:
            self._nonce_i += 1
        else:
            self._nonce_sec = now
            self._nonce_i   = 0
        return now * 1_000_000 + self._nonce_i

    # ── Signing ────────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        from eth_account.messages import encode_typed_data
        td = copy.deepcopy(_TYPED_DATA)
        td["message"]["msg"] = urllib.parse.urlencode(params)
        msg = encode_typed_data(full_message=td)
        return self._account.sign_message(msg).signature.hex()

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def _req(self, method: str, path: str, params: dict) -> dict:
        params["nonce"]  = str(self._get_nonce())
        params["user"]   = self.user
        params["signer"] = self.signer
        sig      = self._sign(params)
        param_str = urllib.parse.urlencode(params)
        url = f"{self._base}{path}?{param_str}&signature={sig}"
        resp = requests.request(method, url, headers=_HEADERS, timeout=10)
        text = resp.text.strip()
        if not text:
            raise RuntimeError(f"Empty response (HTTP {resp.status_code})")
        if resp.status_code == 403:
            raise RuntimeError(f"HTTP 403: {text[:200]}")
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON (HTTP {resp.status_code}): {text[:300]}")
        if isinstance(data, dict) and int(data.get("code", 0)) < 0:
            raise RuntimeError(f"AsterDex {data.get('code')}: {data.get('msg')}")
        return data

    # ── Instrument info ────────────────────────────────────────────────────────

    def _load_lot_info(self, raw: str):
        if raw in self._step_cache:
            return
        info = None
        for base in [self._base] + [u for u in BASE_URLS if u != self._base]:
            try:
                info = requests.get(f"{base}/fapi/v1/exchangeInfo", timeout=5).json()
                break
            except Exception:
                continue
        if info is None:
            self._step_cache[raw] = 1.0
            self._min_cache[raw]  = 0.0
            return
        for sym in info.get("symbols", []):
            if sym["symbol"] != raw:
                continue
            for f in sym.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    self._step_cache[raw] = float(f["stepSize"])
                    self._min_cache[raw]  = float(f["minQty"])
                    if f.get("maxQty"):
                        self._max_cache[raw] = float(f["maxQty"])
            break
        if raw not in self._step_cache:
            self._step_cache[raw] = 1.0
            self._min_cache[raw]  = 0.0

    def _round_qty(self, raw: str, qty: float) -> float:
        return self.round_step(qty, self._step_cache.get(raw, 1.0))

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
        if qty <= 0:
            return f"qty rounded to 0 for {raw} (step size too large or price missing)"
        min_qty = self._min_cache.get(raw, 0.0)
        if qty < min_qty:
            return f"qty {qty} < minQty {min_qty} for {raw}"
        return None

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        data = self._req("GET", "/fapi/v3/balance", {})
        if isinstance(data, list):
            for asset in data:
                if asset.get("asset") == "USDT":
                    return float(asset.get("availableBalance", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        raw = self.fmt_symbol(symbol)
        # Try requested leverage then common fallbacks down to 1x.
        # Ensures we always find the highest supported leverage for the symbol.
        candidates = sorted({leverage, 5, 3, 2, 1}, reverse=True)
        for lev in candidates:
            if lev > leverage:
                continue
            try:
                self._req("POST", "/fapi/v3/leverage", {"symbol": raw, "leverage": lev})
                if lev < leverage:
                    print(f"  [AsterDex] {raw}: max leverage is {lev}x (requested {leverage}x)")
                return lev
            except RuntimeError as e:
                msg = str(e)
                if "-4028" in msg or "-4055" in msg:
                    return lev  # already set to this leverage
                continue
        raise RuntimeError(f"AsterDex: could not set leverage for {raw}")

    def _place(self, symbol: str, side: str, notional_usd: float,
               mark_price: float, leverage: int, reduce_only: bool = False,
               close_qty: float = 0.0) -> TradeResult:
        raw = self.fmt_symbol(symbol)
        self._load_lot_info(raw)

        qty = self._round_qty(raw, close_qty if reduce_only else notional_usd / mark_price)
        max_qty = self._max_cache.get(raw, 0.0)
        if max_qty > 0 and qty > max_qty:
            print(f"  [AsterDex] {raw}: qty {qty} capped to maxQty {max_qty}")
            qty = self._round_qty(raw, max_qty)
        err = self._check_min(raw, qty)
        if err:
            return TradeResult(self.exchange_name, symbol, raw, side, 0, 0, notional_usd, leverage, error=err)

        params = {
            "symbol":   raw,
            "side":     side.upper(),
            "type":     "MARKET",
            "quantity": str(qty),
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        # -5018 = max notional limit; -4005 = qty > maxQty per order.
        # Retry with progressively smaller qty for both.
        last_err: Optional[str] = None
        for scale in (1.0, 0.75, 0.5, 0.25):
            scaled_qty = self._round_qty(raw, qty * scale)
            err = self._check_min(raw, scaled_qty)
            if err:
                break
            params["quantity"] = str(scaled_qty)
            try:
                resp = self._req("POST", "/fapi/v3/order", params)
                avg   = float(resp.get("avgPrice") or 0)
                price = float(resp.get("price") or 0)
                fill  = avg if avg > 0 else (price if price > 0 else mark_price)
                oid   = str(resp.get("orderId", ""))
                if scale < 1.0:
                    print(f"  [AsterDex] placed at {int(scale*100)}% size ({scaled_qty}) after size retry")
                return TradeResult(self.exchange_name, symbol, raw, side, scaled_qty, fill,
                                   scaled_qty * fill, leverage, order_id=oid)
            except RuntimeError as e:
                last_err = str(e)
                if ("-5018" not in last_err and "-4005" not in last_err) or reduce_only:
                    raise

        raise RuntimeError(last_err or "AsterDex: all size retries exhausted")

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "BUY",  notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "SELL", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, "SELL", 0, 0, 1, reduce_only=True, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, "BUY",  0, 0, 1, reduce_only=True, close_qty=qty)

    def fetch_order_fills(self, symbol: str, order_id: str, since_ms: int, until_ms: int):
        raw = self.fmt_symbol(symbol)
        try:
            params = {"symbol": raw, "startTime": str(since_ms), "endTime": str(until_ms), "limit": "100"}
            if order_id:
                params["orderId"] = order_id
            data = self._req("GET", "/fapi/v3/userTrades", params)
            if not isinstance(data, list) or not data:
                return None, None
            total_qty = sum(float(t.get("qty", 0)) for t in data)
            if total_qty <= 0:
                return None, None
            avg_px = sum(float(t["price"]) * float(t["qty"]) for t in data) / total_qty
            total_fee = sum(abs(float(t.get("commission", 0))) for t in data)
            return avg_px, total_fee
        except Exception:
            return None, None

    def fetch_funding_payment(self, symbol: str, since_ms: int, until_ms: int):
        raw = self.fmt_symbol(symbol)
        try:
            data = self._req("GET", "/fapi/v3/income", {
                "symbol": raw, "incomeType": "FUNDING_FEE",
                "startTime": str(since_ms), "endTime": str(until_ms),
                "limit": "100",
            })
            if not isinstance(data, list) or not data:
                return None
            return sum(float(x.get("income", 0)) for x in data)
        except Exception:
            return None
