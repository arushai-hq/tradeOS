# KiteTicker Integration — TradeOS D3

## Library

```python
from kiteconnect import KiteTicker
```

KiteTicker runs in its **own thread** — not the asyncio event loop. All five callbacks fire in the KiteTicker thread. Never block in them.

## Five Callbacks

```python
kws = KiteTicker(api_key, access_token)

kws.on_ticks = on_ticks           # fires when tick data arrives
kws.on_connect = on_connect       # fires on successful (re)connect
kws.on_close = on_close           # fires on graceful close
kws.on_error = on_error           # fires on error before reconnect
kws.on_reconnect = on_reconnect   # fires on each reconnect attempt
```

## The Critical Rule: on_ticks Must Not Block

```python
# WRONG — processes inside callback, blocks KiteTicker thread
def on_ticks(ws, ticks):
    for tick in ticks:
        validate(tick)          # DON'T
        run_strategy(tick)      # DON'T
        place_order(tick)       # DON'T

# CORRECT — push to asyncio queue immediately, return
def on_ticks(ws, ticks):
    loop.call_soon_threadsafe(queue.put_nowait, ticks)
```

## Thread-to-Asyncio Bridge

KiteTicker lives in a thread. The signal processor lives in the asyncio event loop. Bridge them with `loop.call_soon_threadsafe()`:

```python
import asyncio
import structlog
from kiteconnect import KiteTicker
from datetime import datetime
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


def build_on_ticks(loop: asyncio.AbstractEventLoop, tick_queue: asyncio.Queue, ws_state: dict):
    """Factory that closes over loop + queue — avoids globals."""
    def on_ticks(ws, ticks):
        # Update last_tick_timestamp from KiteTicker thread
        # Use call_soon_threadsafe so asyncio tasks can read it safely
        now = datetime.now(IST)
        loop.call_soon_threadsafe(ws_state.__setitem__, "last_tick_timestamp", now)
        # Push ticks to asyncio queue — non-blocking
        loop.call_soon_threadsafe(tick_queue.put_nowait, ticks)
    return on_ticks


def build_on_connect(loop: asyncio.AbstractEventLoop, ws_state: dict, watchlist: list[str]):
    """on_connect callback — fires after every successful connect/reconnect."""
    def on_connect(ws, response):
        log.info(
            "kiteticker_connected",
            instruments=len(watchlist),
            reconnect_attempt=ws_state.get("reconnect_attempt", 0),
        )
        # Subscribe to instruments in QUOTE mode (OHLC — sufficient for EMA/VWAP/RSI)
        ws.subscribe(watchlist)
        ws.set_mode(ws.MODE_QUOTE, watchlist)
        # Update shared state from KiteTicker thread
        loop.call_soon_threadsafe(ws_state.update, {
            "ws_connected": True,
            "reconnect_attempt": 0,
            "disconnect_timestamp": None,
            "instruments_subscribed": list(watchlist),
        })
    return on_connect


def build_on_close(loop: asyncio.AbstractEventLoop, ws_state: dict):
    """on_close callback — fires on graceful or unexpected disconnect."""
    def on_close(ws, code, reason):
        from datetime import datetime
        log.warning(
            "kiteticker_disconnected",
            code=code,
            reason=reason,
        )
        loop.call_soon_threadsafe(ws_state.update, {
            "ws_connected": False,
            "disconnect_timestamp": datetime.now(IST),
        })
    return on_close


def build_on_error(loop: asyncio.AbstractEventLoop, ws_state: dict):
    """on_error callback — fires on connection errors."""
    def on_error(ws, code, reason):
        log.error(
            "kiteticker_error",
            code=code,
            reason=reason,
        )
        # Disconnect timestamp is set here if not already set
        if not ws_state.get("disconnect_timestamp"):
            loop.call_soon_threadsafe(
                ws_state.__setitem__,
                "disconnect_timestamp",
                datetime.now(IST),
            )
    return on_error
```

## Wiring KiteTicker

```python
async def start_websocket_listener(
    api_key: str,
    access_token: str,
    watchlist: list[str],
    tick_queue: asyncio.Queue,
    ws_state: dict,
    kill_switch: dict,
) -> None:
    """
    Entry point for the WebSocket listener asyncio task.
    KiteTicker.connect(threaded=True) runs in its own thread.
    This coroutine manages reconnect logic.
    """
    loop = asyncio.get_running_loop()

    kws = KiteTicker(api_key, access_token)
    kws.on_ticks = build_on_ticks(loop, tick_queue, ws_state)
    kws.on_connect = build_on_connect(loop, ws_state, watchlist)
    kws.on_close = build_on_close(loop, ws_state)
    kws.on_error = build_on_error(loop, ws_state)

    # Start in threaded mode — doesn't block asyncio
    kws.connect(threaded=True)

    # Monitor and handle reconnects in asyncio context
    await manage_reconnect_loop(kws, ws_state, kill_switch)
```

## Subscribe Modes

| Mode | Data | Use case |
|------|------|----------|
| `MODE_FULL` | OHLC + market depth + OI | Phase 2 only |
| `MODE_QUOTE` | OHLC + volume + OI | **Phase 1 — S1/S2 strategies** |
| `MODE_LTP` | Last traded price only | Not sufficient for EMA/VWAP |

**Always use `MODE_QUOTE` for Phase 1.** OHLC fields are required for VWAP and RSI calculations.
