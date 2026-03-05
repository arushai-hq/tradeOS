# Reconnect Backoff Patterns — TradeOS D3

## Exact Backoff Sequence

```
Attempt 1: wait 2s   → reconnect
Attempt 2: wait 4s   → reconnect
Attempt 3: wait 8s   → reconnect
Attempt 4: wait 16s  → reconnect
Attempt 5: wait 30s  → reconnect + Telegram alert
Attempt 6: wait 30s  → reconnect + Telegram alert
... (30s cap forever until reconnect succeeds or market closes)
```

**Formula:** `min(2 ** attempt, 30)` — gives exactly 2, 4, 8, 16, 30, 30...

## Full Reconnect Loop Implementation

```python
import asyncio
import structlog
from datetime import datetime, time as dtime
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
BACKOFF_CAP = 30
KILL_SWITCH_TIMEOUT_SECONDS = 60


def is_market_hours() -> bool:
    """True if current IST time is within 09:15–15:30."""
    now_ist = datetime.now(IST).time()
    return MARKET_OPEN <= now_ist <= MARKET_CLOSE


async def manage_reconnect_loop(
    kws,
    ws_state: dict,
    kill_switch: dict,
) -> None:
    """
    Async task — monitors ws_connected, drives reconnect with backoff.
    Stops when market closes or Level 3 kill switch is active.
    """
    from risk_manager.notifier import send_telegram

    # Monitor loop
    while not kill_switch.get("level3_active", False):
        if ws_state.get("ws_connected", False):
            await asyncio.sleep(1)  # All good — check again in 1s
            continue

        # Disconnected — start reconnect sequence
        if not is_market_hours():
            log.info("ws_reconnect_stopped_market_closed")
            return

        attempt = ws_state.get("reconnect_attempt", 0) + 1
        ws_state["reconnect_attempt"] = attempt

        wait_seconds = min(2 ** attempt, BACKOFF_CAP)

        log.warning(
            "ws_reconnect_attempt",
            attempt=attempt,
            wait_seconds=wait_seconds,
            reason="disconnected",
        )

        # Send Telegram alert from attempt 5 onward
        if attempt >= 5:
            await send_telegram(
                f"TradeOS WS DISCONNECTED — attempt {attempt}, "
                f"retrying in {wait_seconds}s"
            )

        await asyncio.sleep(wait_seconds)

        # Attempt reconnect
        if not ws_state.get("ws_connected", False):
            try:
                kws.reconnect()
                log.info("ws_reconnect_triggered", attempt=attempt)
            except Exception as e:
                log.error("ws_reconnect_failed", attempt=attempt, error=str(e))


async def watch_for_60s_kill_switch(
    ws_state: dict,
    kill_switch: dict,
) -> None:
    """
    Separate task — triggers Level 2 kill switch if WS is disconnected
    for more than 60 seconds during market hours.
    """
    while not kill_switch.get("level3_active", False):
        if ws_state.get("ws_connected", True):
            await asyncio.sleep(1)
            continue

        disconnect_ts = ws_state.get("disconnect_timestamp")
        if disconnect_ts is None:
            await asyncio.sleep(1)
            continue

        elapsed = (datetime.now(IST) - disconnect_ts).total_seconds()

        if elapsed >= KILL_SWITCH_TIMEOUT_SECONDS and is_market_hours():
            log.critical(
                "ws_timeout_60s_kill_switch_level2",
                elapsed_seconds=elapsed,
                disconnect_timestamp=disconnect_ts.isoformat(),
            )
            from risk_manager.kill_switch import trigger_kill_switch
            await trigger_kill_switch(
                kill_switch,
                level=2,
                reason="ws_timeout_60s",
            )
            return  # Stop watching after trigger

        await asyncio.sleep(1)
```

## Market Hours Guard

```python
def is_market_hours() -> bool:
    """09:15–15:30 IST. Any time outside this range → reconnect is unnecessary."""
    now_ist = datetime.now(IST).time()
    return MARKET_OPEN <= now_ist <= MARKET_CLOSE

# In reconnect loop — stop gracefully after market close
if not is_market_hours():
    log.info(
        "ws_reconnect_stopped_market_closed",
        time=datetime.now(IST).isoformat(),
    )
    return
```

## Rules

1. **Never stop retrying during market hours** — even after 10 attempts
2. **Always use `asyncio.sleep()`** — `time.sleep()` blocks the event loop
3. **Reset attempt counter on successful reconnect** — `ws_state["reconnect_attempt"] = 0`
4. **Log every attempt** — `attempt number`, `wait_seconds`, `reason`
5. **Telegram alert from attempt 5 onward** — every attempt, not just the first
6. **Stop after market close** — `15:30 IST` — no retry needed overnight
