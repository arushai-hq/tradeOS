# Heartbeat Monitor — TradeOS D3

## The Problem: Silent Disconnects

Zerodha's WebSocket can enter a "zombie" state — the TCP connection stays open (so `on_close` never fires) but no tick data flows. Common during:
- NSE circuit breakers / market halt
- Zerodha server maintenance
- Network path issues that drop data but not the connection

Without a heartbeat, the system keeps thinking it's connected while trading on stale or empty tick data.

## Solution: Monitor last_tick_timestamp

If the last tick is older than 30 seconds during market hours, set
`reconnect_requested = True` to signal ws_listener to handle the reconnect.

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
    shared_state: dict,
    kill_switch: dict,
) -> None:
    """
    Independent asyncio task — checks for tick silence every 30 seconds.
    Detects silent disconnects where TCP stays open but no data flows.
    Must run as a separate asyncio.create_task() alongside websocket_listener.

    Single-writer rule: heartbeat only writes reconnect_requested.
    It never writes ws_connected or disconnect_timestamp — those keys are
    owned by ws_listener. See shared-state-contract.md for full ownership table.
    """
    log.info("heartbeat_monitor_started", interval_seconds=HEARTBEAT_INTERVAL_SECONDS)

    while not kill_switch.get("level3_active", False):
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

        # Only check during market hours
        if not is_market_hours():
            continue

        # If already marked disconnected, reconnect loop handles it
        if not shared_state.get("ws_connected", False):
            continue

        # If a reconnect is already pending, don't pile on
        if shared_state.get("reconnect_requested", False):
            continue

        last_tick = shared_state.get("last_tick_timestamp")

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
            # Signal ws_listener to handle the reconnect.
            # heartbeat never writes ws_connected or disconnect_timestamp —
            # those keys are owned by ws_listener (shared-state-contract.md).
            shared_state["reconnect_requested"] = True
        else:
            log.debug(
                "heartbeat_ok",
                elapsed_seconds=round(elapsed, 1),
                last_tick=last_tick.isoformat(),
            )

    log.info("heartbeat_monitor_stopped")
```

## ws_listener — Handling reconnect_requested

ws_listener polls `reconnect_requested` in its reconnect loop. When the flag
is set, ws_listener owns the full state transition:

```python
async def _handle_reconnect_request(shared_state: dict, kws) -> None:
    """
    Called by ws_listener when shared_state["reconnect_requested"] is True.
    ws_listener is the single writer of ws_connected and disconnect_timestamp.
    """
    shared_state["ws_connected"] = False
    shared_state["disconnect_timestamp"] = datetime.now(IST)
    shared_state["reconnect_requested"] = False  # clear the flag

    log.warning("ws_listener_reconnect_on_heartbeat_signal")
    try:
        await asyncio.to_thread(kws.reconnect)
    except Exception as e:
        log.error("ws_listener_reconnect_failed", error=str(e))
```

## Integration in Main

The heartbeat runs as an independent asyncio task alongside the websocket listener.
Note: the `kws` (KiteTicker) object is NOT passed to heartbeat — ws_listener owns it.

```python
async def main():
    # Start websocket listener task (owns kws, ws_connected, disconnect_timestamp)
    ws_task = asyncio.create_task(
        start_websocket_listener(api_key, access_token, watchlist, tick_queue, shared_state, kill_switch)
    )

    # Start heartbeat monitor task — reads shared_state, writes reconnect_requested only
    heartbeat_task = asyncio.create_task(
        heartbeat_monitor_task(shared_state, kill_switch)
    )

    await asyncio.gather(ws_task, heartbeat_task, ...)
```

## What Counts as "No Tick"

During normal trading hours (09:15–15:30) with 20 instruments in QUOTE mode, ticks arrive every 1-3 seconds. A 30-second gap indicates:
- Silent disconnect (most common)
- Very low liquidity period (pre-open, circuit limit)
- Server-side issue

**The heartbeat sets `reconnect_requested = True` on silence > 30s.** It does NOT
directly trigger the kill switch and does NOT modify `ws_connected`. The kill switch
trigger comes from `watch_for_60s_kill_switch()` in ws_listener, which counts elapsed
time since `disconnect_timestamp`.

## Key Design Points

1. **Uses `asyncio.sleep(30)` not `time.sleep(30)`** — must not block the event loop
2. **Reads from `shared_state["last_tick_timestamp"]`** — updated by `on_ticks` callback via `call_soon_threadsafe`
3. **Only checks during market hours** — NSE is closed outside 09:15–15:30
4. **Writes `reconnect_requested = True`** — ws_listener reads this and handles the actual `ws_connected` update and `kws.reconnect()` call. heartbeat never writes `ws_connected` directly.
5. **Skips if `reconnect_requested` already set** — avoids redundant signals if ws_listener hasn't cleared the flag yet
6. **Logs WARNING not CRITICAL** — reconnect loop handles the severity escalation
