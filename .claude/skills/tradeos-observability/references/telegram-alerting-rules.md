# Telegram Alerting Rules — TradeOS D4

## Alert Taxonomy

Only specific event types trigger Telegram alerts. Do NOT send all logs to
Telegram — the goal is signal-to-noise ratio. A trader tuning out alerts
because of noise is worse than no alerts.

### CRITICAL Alerts (immediate, bypass rate limiting)
These fire instantly whenever the event occurs:

| Event | Trigger condition |
|-------|-----------------|
| `kill_switch_triggered` | Any level (1, 2, or 3) |
| `position_mismatch` | Any mismatch found during reconciliation |
| `system_start` | Only when `mode="live"` (not paper) |
| Unhandled exception | Any uncaught exception in main event loop |

### WARNING Alerts (batched — max 1 per 5 min per type)
```python
WARN_RATE_LIMIT_SECONDS = 300  # 5 minutes
```

| Event | Trigger condition |
|-------|-----------------|
| `ws_disconnected` | Only during market hours (09:15–15:30 IST) |
| `daily_loss_warning` | When drawdown > 2% |
| `order_rejected` | When `consecutive_rejections >= 3` |

### INFO Alerts (daily summary only)
Sent once per day at **15:35 IST** — never during trading hours:
- `daily_pnl_summary`: trades, winners, losers, total_pnl_rs, max_drawdown

---

## Message Format

All Telegram messages use a human-readable multi-line format.
Never send raw JSON — a trader on mobile must read this instantly.

### CRITICAL Template
```
🔴 CRITICAL | TradeOS
Event: kill_switch_triggered
Level: 2 | Reason: daily_loss_exceeded
Daily PnL: -3.1% | Positions: 2 open
Time: 11:32:04 IST
```

### WARNING Template
```
🟡 WARNING | TradeOS
Event: ws_disconnected
Reconnect attempt: 3 | Market hours: Yes
Time: 11:45:22 IST
```

### Daily Summary Template
```
📊 Daily Summary | TradeOS
Date: 2026-03-05 | Mode: paper
Trades: 4 (3W / 1L) | Win rate: 75%
Total PnL: +₹1,247 (+0.25%)
Max drawdown: -0.8%
Time: 15:35:00 IST
```

---

## Implementation

```python
import asyncio
from datetime import datetime, time as dtime
from typing import Optional
import httpx
import pytz
import structlog

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Rate limiting state — module-level dict keyed by alert_type
_last_sent: dict[str, datetime] = {}
WARN_RATE_LIMIT_SECONDS = 300


async def send_telegram(
    message: str,
    bot_token: str,
    chat_id: str,
    critical: bool = False,
) -> None:
    """
    Send a Telegram message via Bot API using httpx (async — not requests).

    Rate limiting applies for non-critical messages: if the same alert_type
    was sent within 5 minutes, the message is silently dropped.

    Never call this directly — use the typed helpers below.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Never let Telegram failure crash the trading system
        log.error("telegram_send_failed", error=str(exc))


async def send_critical_alert(
    event: str,
    fields: dict,
    bot_token: str,
    chat_id: str,
) -> None:
    """Send a CRITICAL alert immediately, bypassing rate limiting."""
    now_ist = datetime.now(IST).strftime("%H:%M:%S IST")
    lines = [f"🔴 CRITICAL | TradeOS", f"Event: {event}"]
    for k, v in fields.items():
        lines.append(f"{k.replace('_', ' ').title()}: {v}")
    lines.append(f"Time: {now_ist}")
    await send_telegram("\n".join(lines), bot_token, chat_id, critical=True)


async def send_warning_alert(
    alert_type: str,
    event: str,
    fields: dict,
    bot_token: str,
    chat_id: str,
) -> None:
    """
    Send a WARNING alert, subject to 5-minute rate limiting per alert_type.

    alert_type is the key for rate limiting (e.g. "ws_disconnected").
    Different alert types have independent 5-minute windows.
    """
    now = datetime.now(IST)
    last = _last_sent.get(alert_type)

    if last is not None:
        elapsed = (now - last).total_seconds()
        if elapsed < WARN_RATE_LIMIT_SECONDS:
            log.debug("telegram_warning_rate_limited",
                      alert_type=alert_type, elapsed_seconds=round(elapsed, 1))
            return

    _last_sent[alert_type] = now
    now_str = now.strftime("%H:%M:%S IST")
    lines = [f"🟡 WARNING | TradeOS", f"Event: {event}"]
    for k, v in fields.items():
        lines.append(f"{k.replace('_', ' ').title()}: {v}")
    lines.append(f"Time: {now_str}")
    await send_telegram("\n".join(lines), bot_token, chat_id)
```

---

## Daily Summary Scheduler

```python
async def schedule_daily_summary(
    session_stats: dict,
    bot_token: str,
    chat_id: str,
) -> None:
    """
    Async task that waits until 15:35 IST then sends the daily summary.
    Spawn this once at startup alongside the main trading tasks.

    session_stats is a shared dict updated throughout the day:
      {trades: int, winners: int, losers: int, total_pnl_rs: float,
       max_drawdown_pct: float, mode: str}
    """
    target_time = dtime(15, 35, 0)

    while True:
        now = datetime.now(IST)
        target_today = now.replace(
            hour=target_time.hour,
            minute=target_time.minute,
            second=0,
            microsecond=0,
        )
        if now >= target_today:
            # Already past 15:35 — wait for tomorrow
            wait_seconds = 86400 - (now - target_today).total_seconds()
        else:
            wait_seconds = (target_today - now).total_seconds()

        await asyncio.sleep(wait_seconds)

        # Build summary
        s = session_stats
        total = s.get("trades", 0)
        winners = s.get("winners", 0)
        losers = s.get("losers", 0)
        pnl = s.get("total_pnl_rs", 0.0)
        drawdown = s.get("max_drawdown_pct", 0.0)
        win_rate = round(winners / total * 100) if total > 0 else 0
        date_str = datetime.now(IST).strftime("%Y-%m-%d")

        message = (
            f"📊 Daily Summary | TradeOS\n"
            f"Date: {date_str} | Mode: {s.get('mode', 'paper')}\n"
            f"Trades: {total} ({winners}W / {losers}L) | Win rate: {win_rate}%\n"
            f"Total PnL: {'+' if pnl >= 0 else ''}{pnl:,.0f} ({pnl/5000*100:+.2f}%)\n"
            f"Max drawdown: {drawdown:+.1f}%\n"
            f"Time: {datetime.now(IST).strftime('%H:%M:%S IST')}"
        )
        await send_telegram(message, bot_token, chat_id)
        log.info("daily_summary_sent", trades=total, pnl_rs=pnl)
```

---

## Rules Summary

- Use `httpx` (async) — never `requests` (synchronous, would block event loop)
- Rate limit key is the `alert_type` string, not the event payload
- CRITICAL alerts (`send_critical_alert`) always bypass rate limiting
- Never send API keys, account numbers, or tokens in Telegram messages
- Maximum 1 message per second (Telegram rate limit) — add `asyncio.sleep(1)`
  between bursts if sending multiple alerts in quick succession
- `send_telegram` must never raise — catch all HTTP exceptions and log them
