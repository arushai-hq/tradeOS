# Heartbeat Monitor — TradeOS D3

## The Problem: Silent Disconnects

Zerodha's WebSocket can enter a "zombie" state — the TCP connection stays open (so `on_close` never fires) but no tick data flows. Common during:
- NSE circuit breakers / market halt
- Zerodha server maintenance
- Network path issues that drop data but not the connection

Without a heartbeat, the system keeps thinking it's connected while trading on stale or empty tick data.

## Solution: Monitor last_tick_timestamp

If the last tick is older than 30 seconds during market hours, treat it as a disconnect and trigger reconnect.

```python
import asyncio
import structlog
from datetime import datetime, time as dtime
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

HEARTBEAT_INTERVAL_SECONDS = 30
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_hours() -> bool:
    now_ist = datetime.now(IST).time()
    return MARKET_OPEN <= now_ist <= MARKET_CLOSE


async def heartbeat_monitor_task(
    ws_state: dict,
    kws,
    kill_switch: dict,
) -> None:
    """
    Independent asyncio task — checks for tick silence every 30 seconds.
    Detects silent disconnects where TCP stays open but no data flows.
    Must run as a separate asyncio.create_task() alongside websocket_listener.
    """
    log.info("heartbeat_monitor_started", interval_seconds=HEARTBEAT_INTERVAL_SECONDS)

    while not kill_switch.get("level3_active", False):
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        # Only check during market hours
        if not is_market_hours():
            continue

        # If already marked disconnected, reconnect loop handles it
        if not ws_state.get("ws_connected", False):
            continue

        last_tick = ws_state.get("last_tick_timestamp")

        if last_tick is None:
            # No tick ever received — may be pre-open
            log.debug("heartbeat_no_tick_received_yet")
            continue

        elapsed = (datetime.now(IST) - last_tick).total_seconds()

        if elapsed > HEARTBEAT_INTERVAL_SECONDS:
            log.warning(
                "heartbeat_silent_disconnect_detected",
                elapsed_seconds=round(elapsed, 1),
                last_tick=last_tick.isoformat(),
                threshold_seconds=HEARTBEAT_INTERVAL_SECONDS,
            )
            # Mark as disconnected — triggers reconnect loop
            from datetime import datetime as dt
            ws_state["ws_connected"] = False
            ws_state["disconnect_timestamp"] = datetime.now(IST)
            # Try to force reconnect
            try:
                kws.reconnect()
            except Exception as e:
                log.error("heartbeat_forced_reconnect_failed", error=str(e))
        else:
            log.debug(
                "heartbeat_ok",
                elapsed_seconds=round(elapsed, 1),
                last_tick=last_tick.isoformat(),
            )

    log.info("heartbeat_monitor_stopped")
```

## Integration in Main

The heartbeat runs as an independent asyncio task alongside the websocket listener:

```python
async def main():
    # Start websocket listener task
    ws_task = asyncio.create_task(
        start_websocket_listener(api_key, access_token, watchlist, tick_queue, ws_state, kill_switch)
    )

    # Start heartbeat monitor task — independent, watching ws_state
    heartbeat_task = asyncio.create_task(
        heartbeat_monitor_task(ws_state, kws, kill_switch)
    )

    await asyncio.gather(ws_task, heartbeat_task, ...)
```

## What Counts as "No Tick"

During normal trading hours (09:15–15:30) with 20 instruments in QUOTE mode, ticks arrive every 1-3 seconds. A 30-second gap indicates:
- Silent disconnect (most common)
- Very low liquidity period (pre-open, circuit limit)
- Server-side issue

**The heartbeat triggers reconnect on silence > 30s.** It does NOT directly trigger the kill switch — that's the job of `watch_for_60s_kill_switch()` which counts elapsed time since `disconnect_timestamp`.

## Key Design Points

1. **Uses `asyncio.sleep(30)` not `time.sleep(30)`** — must not block the event loop
2. **Reads from `ws_state["last_tick_timestamp"]`** — updated by `on_ticks` callback via `call_soon_threadsafe`
3. **Only checks during market hours** — NSE is closed outside 09:15–15:30
4. **Sets `ws_connected = False`** — so the reconnect loop picks it up
5. **Logs WARNING not CRITICAL** — reconnect loop handles the severity escalation
