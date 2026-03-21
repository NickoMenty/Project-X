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

BASE = "https://fapi3.asterdex.com"

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
        self._nonce_sec = 0
        self._nonce_i   = 0

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
        url = f"{BASE}{path}?{param_str}&signature={sig}"
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
        try:
            info = requests.get(f"{BASE}/fapi/v1/exchangeInfo", timeout=10).json()
        except Exception:
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
            break
        if raw not in self._step_cache:
            self._step_cache[raw] = 1.0
            self._min_cache[raw]  = 0.0

    def _round_qty(self, raw: str, qty: float) -> float:
        return self.round_step(qty, self._step_cache.get(raw, 1.0))

    def _check_min(self, raw: str, qty: float) -> Optional[str]:
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
        for lev in [leverage, 5]:
            try:
                self._req("POST", "/fapi/v3/leverage", {"symbol": raw, "leverage": lev})
                return lev
            except RuntimeError as e:
                msg = str(e)
                if "-4028" in msg or "-4055" in msg:
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
            "symbol":   raw,
            "side":     side.upper(),
            "type":     "MARKET",
            "quantity": str(qty),
        }
        if reduce_only:
            params["reduceOnly"] = "true"

        resp = self._req("POST", "/fapi/v3/order", params)
        avg  = resp.get("avgPrice", "0")
        fill = float(avg) if avg and float(avg) > 0 else float(resp.get("price") or mark_price)
        oid  = str(resp.get("orderId", ""))
        return TradeResult(self.exchange_name, symbol, raw, side, qty, fill,
                           qty * fill, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "BUY",  notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, "SELL", notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, "SELL", 0, 0, 1, reduce_only=True, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, "BUY",  0, 0, 1, reduce_only=True, close_qty=qty)
