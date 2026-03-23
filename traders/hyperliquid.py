"""
Hyperliquid trading client.
Uses EIP-712 signed JSON actions posted to https://api.hyperliquid.xyz/exchange
Requires: pip install eth-account

HYPERLIQUID_API env var should be the wallet private key (hex, with or without 0x prefix).
"""
import json
import math
import time
import requests
from typing import Dict, Optional

from .base import BaseTrader, TradeResult

INFO_URL = "https://api.hyperliquid.xyz/info"
EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"

# EIP-712 domain for Hyperliquid on Arbitrum
DOMAIN = {
    "name": "HyperliquidSignTransaction",
    "version": "1",
    "chainId": 42161,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}


def _load_eth_account():
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        return Account
    except ImportError:
        raise ImportError(
            "eth-account is required for Hyperliquid trading.\n"
            "Install it: pip install eth-account"
        )


class HyperliquidTrader(BaseTrader):
    exchange_name = "Hyperliquid"

    def __init__(self, private_key: str):
        Account = _load_eth_account()
        private_key = private_key.strip().strip('"').strip("'")
        if not private_key.lower().startswith("0x"):
            private_key = "0x" + private_key
        self._account = Account.from_key(private_key)
        self.api_wallet_address = self._account.address
        print(f"  [Hyperliquid] API wallet address: {self.api_wallet_address}")
        self._asset_cache: Dict[str, int] = {}   # symbol → asset index
        self._sz_decimals: Dict[str, int] = {}   # symbol → size decimals

        # API wallet trades on behalf of a main account — resolve the parent address.
        # If HYPERLIQUID_ADDRESS is set in env, use it directly (most reliable).
        import os
        explicit = os.getenv("HYPERLIQUID_ADDRESS", "").strip()
        self.address = explicit if explicit else self._resolve_main_account()

        # Verify the resolved address has an active account. Catches expired/missing
        # API wallet authorizations at startup rather than mid-trade.
        self._verify_account()

    def fmt_symbol(self, symbol: str) -> str:
        return symbol  # Hyperliquid uses bare symbols like "BTC"

    # ── Info helpers ───────────────────────────────────────────────────────────

    def _resolve_main_account(self) -> str:
        """Look up the main (parent) account address this API wallet is authorized for."""
        try:
            data = requests.post(INFO_URL, json={
                "type": "agentState",
                "user": self.api_wallet_address,
            }, timeout=10).json()
            parent = data.get("parentAccount") or data.get("parent")
            if parent:
                return parent
        except Exception:
            pass
        # Fall back to the API wallet address itself (works if using main wallet key)
        return self.api_wallet_address

    def _verify_account(self):
        """
        Confirm the API wallet is registered and authorized on Hyperliquid.
        Raises RuntimeError at init time if the wallet has expired or was never set up.
        Hyperliquid API wallet authorizations expire after ~90 days — re-authorize at
        https://app.hyperliquid.xyz under Settings → API Wallets.
        """
        try:
            data = requests.post(INFO_URL, json={
                "type": "agentState",
                "user": self.api_wallet_address,
            }, timeout=10).json()
            # A registered API wallet has a non-empty parentAccount
            parent = data.get("parentAccount") or data.get("parent")
            if not parent:
                raise RuntimeError(
                    f"Hyperliquid API wallet {self.api_wallet_address} is not authorized "
                    f"for any account. Go to https://app.hyperliquid.xyz → Settings → "
                    f"API Wallets and re-authorize it (authorizations expire after ~90 days)."
                )
        except RuntimeError:
            raise
        except Exception:
            pass  # Network failure at init — let it proceed, error will surface on first order

    def _info(self, payload: dict) -> dict:
        resp = requests.post(INFO_URL, json=payload, timeout=10)
        return resp.json()

    def _load_meta(self):
        if self._asset_cache:
            return
        meta = self._info({"type": "meta"})
        for i, asset in enumerate(meta.get("universe", [])):
            name = asset["name"]
            self._asset_cache[name] = i
            self._sz_decimals[name] = asset.get("szDecimals", 2)

    def _asset_index(self, symbol: str) -> int:
        self._load_meta()
        if symbol not in self._asset_cache:
            raise RuntimeError(f"Hyperliquid: unknown symbol {symbol}")
        return self._asset_cache[symbol]

    def _round_sz(self, symbol: str, qty: float) -> float:
        self._load_meta()
        decimals = self._sz_decimals.get(symbol, 2)
        factor = 10 ** decimals
        return int(qty * factor) / factor

    # ── EIP-712 signing ────────────────────────────────────────────────────────

    def _sign_action(self, action: dict, nonce: int) -> dict:
        """
        Sign a Hyperliquid L1 action using EIP-712 Agent struct.

        Per the Hyperliquid open-source SDK:
          1. connection_id = keccak256(msgpack(action) || uint64_be(nonce) || 0x00)
          2. Sign Agent typed data: { source: "a", connectionId: bytes32 }
             with domain: Exchange / version 1 / chainId 1337 / zero address
        """
        try:
            import msgpack as _msgpack
        except ImportError:
            raise ImportError("msgpack is required for Hyperliquid. Install: pip install msgpack")

        from eth_hash.auto import keccak

        # 1. connection_id = keccak256(msgpack(action) || uint64_be(nonce) || 0x00)
        raw = _msgpack.packb(action, use_bin_type=True)
        raw += nonce.to_bytes(8, "big")
        raw += b"\x00"  # no vault address
        connection_id = keccak(raw)  # bytes32

        # 2. EIP-712 sign — matches official Hyperliquid SDK exactly.
        # EIP712Domain field ORDER matters for the type hash; must be chainId, name,
        # verifyingContract, version (alphabetical per the SDK, not insertion order).
        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(full_message={
            "domain": {
                "chainId": 1337,
                "name": "Exchange",
                "verifyingContract": "0x0000000000000000000000000000000000000000",
                "version": "1",
            },
            "message": {
                "source": "a",
                "connectionId": connection_id,
            },
            "primaryType": "Agent",
            "types": {
                "EIP712Domain": [
                    {"name": "chainId",           "type": "uint256"},
                    {"name": "name",              "type": "string"},
                    {"name": "verifyingContract", "type": "address"},
                    {"name": "version",           "type": "string"},
                ],
                "Agent": [
                    {"name": "source",       "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
            },
        })
        signed = self._account.sign_message(signable)

        return {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}

    def _send_order(self, action: dict) -> dict:
        nonce = int(time.time() * 1000)
        sig = self._sign_action(action, nonce)
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": sig,
            "vaultAddress": None,
        }
        resp = requests.post(EXCHANGE_URL, json=payload, timeout=10)
        data = resp.json()
        if data.get("status") == "err":
            raise RuntimeError(f"Hyperliquid order error: {data.get('response')}")
        return data

    # ── Order helpers ──────────────────────────────────────────────────────────

    def _market_order(self, symbol: str, is_buy: bool, sz: float,
                      mark_price: float, reduce_only: bool = False) -> dict:
        """
        Hyperliquid market orders are IOC limit orders with 2% slippage.
        """
        asset = self._asset_index(symbol)
        slippage = 1.02 if is_buy else 0.98
        raw_px = mark_price * slippage
        # Hyperliquid wire format: 5 significant figures, no trailing zeros
        sig_figs = 5
        magnitude = int(math.floor(math.log10(abs(raw_px)))) if raw_px > 0 else 0
        limit_px = round(raw_px, sig_figs - 1 - magnitude)
        px_str = f"{limit_px:g}"

        order = {
            "a": asset,
            "b": is_buy,
            "p": px_str,
            "s": str(sz),
            "r": reduce_only,
            "t": {"limit": {"tif": "Ioc"}},
        }
        action = {
            "type": "order",
            "orders": [order],
            "grouping": "na",
        }
        return self._send_order(action)

    # ── BaseTrader ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        # Check perps account first
        perps = self._info({"type": "clearinghouseState", "user": self.address})
        perps_val = float(perps.get("marginSummary", {}).get("accountValue", 0))
        if perps_val > 0:
            return perps_val

        # Fall back to spot account (USDC)
        spot = self._info({"type": "spotClearinghouseState", "user": self.address})
        for bal in spot.get("balances", []):
            if bal.get("coin") == "USDC":
                return float(bal.get("total", 0))
        return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> int:
        asset = self._asset_index(symbol)
        for lev in [leverage, 5]:
            try:
                nonce = int(time.time() * 1000)
                action = {
                    "type": "updateLeverage",
                    "asset": asset,
                    "isCross": True,
                    "leverage": lev,
                }
                sig = self._sign_action(action, nonce)
                payload = {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": None}
                resp = requests.post(EXCHANGE_URL, json=payload, timeout=10).json()
                if resp.get("status") != "err":
                    return lev
            except Exception:
                continue
        return 5

    def _place(self, symbol: str, is_buy: bool, notional_usd: float,
               mark_price: float, leverage: int, reduce_only: bool = False,
               close_qty: float = 0.0) -> TradeResult:
        raw = symbol
        if reduce_only:
            sz = self._round_sz(symbol, close_qty)
        else:
            sz = self._round_sz(symbol, notional_usd / mark_price)

        if sz <= 0:
            return TradeResult(self.exchange_name, symbol, raw, "buy" if is_buy else "sell",
                               0, 0, notional_usd, leverage, error="qty too small")

        resp = self._market_order(symbol, is_buy, sz, mark_price, reduce_only)
        # Extract fill from response
        fills = resp.get("response", {}).get("data", {}).get("statuses", [{}])
        fill_px = mark_price
        oid = ""
        side_str = "buy" if is_buy else "sell"
        if fills and isinstance(fills[0], dict):
            status = fills[0]
            # Explicit error inside the order status (signing wrong, bad address, etc.)
            if "error" in status:
                return TradeResult(self.exchange_name, symbol, raw, side_str,
                                   0, 0, notional_usd, leverage,
                                   error=f"HL order rejected: {status['error']}")
            filled = status.get("filled", {})
            if filled:
                fill_px = float(filled.get("avgPx", mark_price))
                oid = str(filled.get("oid", ""))
            elif not reduce_only:
                # IOC order didn't fill — likely unfilled or wrong params
                return TradeResult(self.exchange_name, symbol, raw, side_str,
                                   0, 0, notional_usd, leverage,
                                   error=f"HL IOC order did not fill (status: {status})")

        return TradeResult(self.exchange_name, symbol, raw, side_str, sz, fill_px,
                           sz * fill_px, leverage, order_id=oid)

    def open_long(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, True, notional_usd, mark_price, leverage)

    def open_short(self, symbol, notional_usd, mark_price, leverage):
        return self._place(symbol, False, notional_usd, mark_price, leverage)

    def close_long(self, symbol, qty):
        return self._place(symbol, False, 0, self._get_mark(symbol), 1,
                           reduce_only=True, close_qty=qty)

    def close_short(self, symbol, qty):
        return self._place(symbol, True, 0, self._get_mark(symbol), 1,
                           reduce_only=True, close_qty=qty)

    def _get_mark(self, symbol: str) -> float:
        try:
            data = self._info({"type": "allMids"})
            return float(data.get(symbol, 1))
        except Exception:
            return 1.0

    def fetch_order_fills(self, symbol: str, order_id: str, since_ms: int, until_ms: int):
        try:
            data = self._info({"type": "userFills", "user": self.address, "startTime": since_ms})
            if not isinstance(data, list):
                return None, None
            fills = [x for x in data
                     if x.get("coin") == symbol
                     and since_ms <= x.get("time", 0) <= until_ms
                     and (not order_id or str(x.get("oid")) == str(order_id))]
            if not fills:
                return None, None
            total_sz = sum(float(x.get("sz", 0)) for x in fills)
            if total_sz <= 0:
                return None, None
            avg_px = sum(float(x["px"]) * float(x["sz"]) for x in fills) / total_sz
            total_fee = sum(abs(float(x.get("fee", 0))) for x in fills)
            return avg_px, total_fee
        except Exception:
            return None, None

    def fetch_funding_payment(self, symbol: str, since_ms: int, until_ms: int):
        try:
            data = self._info({"type": "userFunding", "user": self.address, "startTime": since_ms})
            if not isinstance(data, list):
                return None
            total = 0.0
            found = False
            for item in data:
                ts = item.get("time", 0)
                if ts > until_ms:
                    continue
                delta = item.get("delta", {})
                if delta.get("coin") == symbol:
                    total += float(delta.get("usdc", 0))
                    found = True
            return total if found else None
        except Exception:
            return None
