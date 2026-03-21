"""
Project-X settings — edit this file to configure the bot.

ENABLED_EXCHANGES
-----------------
Toggle any exchange on (True) or off (False).

When an exchange is disabled it is excluded from:
  • funding rate fetching & display tables
  • opportunity scoring & pair proposals
  • pre-epoch scan / live trading
  • order connectivity test at startup
  • balance display
  • session CSV logs
"""

# ── Connectivity test ─────────────────────────────────────────────────────────
# Set to False to skip the ETH round-trip order test on every startup.
# Useful once all exchanges are confirmed working.
RUN_CONNECTIVITY_TEST: bool = True

ENABLED_EXCHANGES: dict[str, bool] = {
    "Binance":     True,
    "Bybit":       True,
    "Bitget":      True,
    "Hyperliquid": True,
    "AsterDex":    True,
    "OKX":         True,
    "KuCoin":      True,
}

# ── Helper ────────────────────────────────────────────────────────────────────

def active_exchanges() -> list[str]:
    """Return the list of exchange names that are currently enabled."""
    return [ex for ex, on in ENABLED_EXCHANGES.items() if on]
