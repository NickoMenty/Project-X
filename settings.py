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

# ── Mode ──────────────────────────────────────────────────────────────────────
# Set DRY_RUN = True to test the full pipeline with mock funding rates.
# Orders ARE real in this mode — only the funding data is mocked.
# Configure your test scenario in MOCK_SCENARIO below.
DRY_RUN: bool = True

# ── Mock scenario (used when DRY_RUN = True) ──────────────────────────────────
# Each entry is a dict with these keys:
#   exchange        Exchange name, must match ENABLED_EXCHANGES
#   symbol          Normalised base asset, e.g. "BTC"
#   rate_pct        Funding rate as a percentage, e.g. 0.05 means +0.05%
#   mark_price      (optional) Mark price in USD — omit to use the live price
#   interval_hours  (optional) funding interval in hours, default 8
#
# Tip: opposing rates on the same symbol across two exchanges will score as
# an opportunity (one side collects, the other pays).
MOCK_SCENARIO: list[dict] = [
    {"exchange": "Binance", "symbol": "BTC", "rate_pct":  0.05, "interval_hours": 0.1},
    {"exchange": "Bybit",   "symbol": "BTC", "rate_pct": -0.03, "interval_hours": 0.1},
]

# ── Connectivity test ─────────────────────────────────────────────────────────
# Set to False to skip the ETH round-trip order test on every startup.
# Useful once all exchanges are confirmed working.
RUN_CONNECTIVITY_TEST: bool = False

# ── Trade timing ──────────────────────────────────────────────────────────────
# Seconds to wait after the funding epoch fires before closing both positions.
# 1s gives the funding payment time to register while keeping exposure minimal.
EXIT_WAIT_SECONDS: int = 1

ENABLED_EXCHANGES: dict[str, bool] = {
    "Binance":     True,
    "Bybit":       True,
    "Bitget":      True,
    "Hyperliquid": True,
    "AsterDex":    True,
    "OKX":         False,
    "KuCoin":      True,
}

# ── Helper ────────────────────────────────────────────────────────────────────

def active_exchanges() -> list[str]:
    """Return the list of exchange names that are currently enabled."""
    return [ex for ex, on in ENABLED_EXCHANGES.items() if on]
