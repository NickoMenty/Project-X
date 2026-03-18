"""
Telegram Reporter
=================
Sends paper trade reports to a Telegram chat via Bot API.

Setup
-----
1. Create a bot via @BotFather on Telegram — copy the token.
2. Start a chat with the bot (or add it to a group).
3. Get your chat ID:
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     (send the bot any message first, then check the JSON)
4. Add to .env:
     TELEGRAM_BOT_TOKEN=123456:ABC-your-token
     TELEGRAM_CHAT_ID=123456789

If either variable is missing the reporter silently does nothing.
"""

import os
from typing import Optional

import requests
from dotenv import load_dotenv

from paper_trader import SimulatedTrade
from spike_scorer import SpikeOpportunity
from typing import List

load_dotenv()

_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
_API_URL  = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram message length limit
_MAX_LEN = 4096


def _configured() -> bool:
    return bool(_TOKEN and _CHAT_IDS)


def _send(text: str) -> None:
    """POST a message to all configured Telegram chat IDs. Silently swallows errors."""
    if not _configured():
        return
    for chat_id in _CHAT_IDS:
        try:
            requests.post(
                _API_URL.format(token=_TOKEN),
                json={
                    "chat_id":    chat_id,
                    "text":       text[:_MAX_LEN],
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception:
            pass  # never crash the main loop over a Telegram failure


def send_trade_report(trade: SimulatedTrade) -> None:
    """
    Format and send a completed SimulatedTrade report to Telegram.
    Safe to call even when Telegram is not configured — does nothing.
    """
    if not _configured():
        return

    result = "✅ PROFIT" if trade.net_pnl_usd >= 0 else "❌ LOSS"
    sign   = "+" if trade.net_pnl_usd >= 0 else ""

    from datetime import datetime, timezone
    def _ts(unix: float) -> str:
        return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%H:%M:%S UTC")

    long_pct  = (trade.exit_long_price  - trade.entry_long_price)  / trade.entry_long_price  * 100
    short_pct = (trade.entry_short_price - trade.exit_short_price) / trade.entry_short_price * 100
    price_combined = trade.long_price_pnl_usd + trade.short_price_pnl_usd

    lines = [
        f"<b>[SIM] PAPER TRADE — {trade.symbol}</b>",
        "",
        f"<b>ENTRY</b>  [{_ts(trade.entry_ts)}]",
        f"  Long   <code>{trade.long_exchange}</code>  ${trade.entry_long_price:,.4f}",
        f"  Short  <code>{trade.short_exchange}</code>  ${trade.entry_short_price:,.4f}",
        f"  Size: ${trade.position_size_usd:.2f} per leg",
        "",
        f"<b>FUNDING EPOCH</b>  [{_ts(trade.funding_ts_ms / 1000)}]",
        f"  Short {trade.short_exchange}  {trade.short_rate_pct:+.4f}%  →  ${trade.funding_collected_usd:+.4f}  (collected)",
        f"  Long  {trade.long_exchange}   {trade.long_rate_pct:+.4f}%  →  ${trade.funding_paid_usd:+.4f}  (paid)",
        f"  Net funding: <b>${trade.net_funding_usd:+.4f}</b>",
        "",
        f"<b>EXIT</b>  [{_ts(trade.exit_ts)}]",
        f"  Long   {trade.long_exchange}  ${trade.entry_long_price:,.4f} → ${trade.exit_long_price:,.4f}  ({long_pct:+.4f}%)  ${trade.long_price_pnl_usd:+.4f}",
        f"  Short  {trade.short_exchange}  ${trade.entry_short_price:,.4f} → ${trade.exit_short_price:,.4f}  ({short_pct:+.4f}%)  ${trade.short_price_pnl_usd:+.4f}",
        "",
        f"<b>FEES</b>",
        f"  Long  {trade.long_exchange}  {trade.long_fee_pct:.3f}% × 2  =  -${trade.position_size_usd * trade.long_fee_pct * 2 / 100:.4f}",
        f"  Short {trade.short_exchange}  {trade.short_fee_pct:.3f}% × 2  =  -${trade.position_size_usd * trade.short_fee_pct * 2 / 100:.4f}",
        f"  Total fees: -${trade.total_fee_usd:.4f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  Price P&L:    ${price_combined:+.4f}",
        f"  Net funding:  ${trade.net_funding_usd:+.4f}",
        f"  Total fees:  -${trade.total_fee_usd:.4f}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>NET P&L:  ${sign}{trade.net_pnl_usd:.4f}  {result}</b>",
    ]

    _send("\n".join(lines))


def send_spike_opportunities(
    passing: "List[SpikeOpportunity]",
    failing: "List[SpikeOpportunity]",
    seconds_to_epoch: float,
) -> None:
    """
    Send the pre-epoch spike opportunity table to Telegram.
    Shows all passing pairs ranked by score, plus top 3 near-misses.
    """
    if not _configured():
        return

    ts_str = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).strftime("%H:%M:%S UTC")

    lines = [
        f"<b>⚡ PRE-EPOCH SCAN  |  T-{seconds_to_epoch:.0f}s  |  {ts_str}</b>",
        f"<b>{len(passing)} passing  |  {len(passing) + len(failing)} scored</b>",
    ]

    if not passing:
        lines.append("\nNo opportunities pass all checks.")
    else:
        import datetime as _dt

        def _fmt_ts(ts_ms: int) -> str:
            if not ts_ms:
                return "?"
            return _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc).strftime("%H:%M:%S UTC")

        lines.append("")
        for i, opp in enumerate(passing, 1):
            secs = opp.seconds_to_funding
            time_str = f"T-{int(secs//60)}m{int(secs%60)}s" if secs < 3600 else f"T-{secs/3600:.1f}h"

            # Assign timestamps to long/short legs
            if opp.primary_is_short:
                long_ts, short_ts = opp.hedge_funding_ts, opp.next_funding_ts
            else:
                long_ts, short_ts = opp.next_funding_ts, opp.hedge_funding_ts

            lines.append(
                f"<b>#{i} {opp.symbol}</b>  ({time_str})\n"
                f"  L: <code>{opp.long_exchange}</code>  {opp.long_rate_pct:+.4f}%  🕐 {_fmt_ts(long_ts)}\n"
                f"  S: <code>{opp.short_exchange}</code>  {opp.short_rate_pct:+.4f}%  🕐 {_fmt_ts(short_ts)}\n"
                f"  Spread: {opp.funding_spread_pct:.4f}%  Fees: {opp.total_fee_pct:.4f}%\n"
                f"  Score: <b>{opp.score:+.4f}%</b>  Est: <b>${opp.estimated_profit_usd:.4f}</b>"
            )

    near_misses = [o for o in failing if o.funding_spread_pct > 0][:3]
    if near_misses:
        lines.append("\n<b>Near-misses:</b>")
        for opp in near_misses:
            reason = opp.fail_reasons[0] if opp.fail_reasons else "?"
            lines.append(
                f"  {opp.symbol}  {opp.long_exchange}/{opp.short_exchange}  "
                f"spread={opp.funding_spread_pct:.4f}%  → {reason}"
            )

    _send("\n".join(lines))


def send_alert(message: str) -> None:
    """Send a plain-text alert (errors, warnings, status updates)."""
    _send(message)
