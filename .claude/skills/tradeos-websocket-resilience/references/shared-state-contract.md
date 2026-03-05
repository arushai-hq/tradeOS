# Shared State Contract — TradeOS D3

## The ws_state Dict

The WebSocket module owns these keys in the shared state dict. They are READ by D1 (kill switch), D2 (order state machine), and D6 (async task orchestration).

```python
ws_state: dict = {
    "ws_connected": False,                    # bool — is KiteTicker connected?
    "last_tick_timestamp": None,              # datetime | None — last tick received
    "disconnect_timestamp": None,             # datetime | None — when we went down
    "reconnect_attempt": 0,                   # int — current backoff attempt count
    "instruments_subscribed": [],             # list[str] — currently subscribed tokens
}
```

## Who Reads / Writes Each Key

| Key | Written by | Read by | Notes |
|-----|-----------|---------|-------|
| `ws_connected` | `on_connect`, `on_close`, heartbeat | kill switch, risk watchdog | Core flag |
| `last_tick_timestamp` | `on_ticks` (via `call_soon_threadsafe`) | heartbeat monitor | Updated every tick |
| `disconnect_timestamp` | `on_close`, `on_error`, heartbeat | 60s kill switch watcher | IST datetime |
| `reconnect_attempt` | reconnect loop | reconnect loop, Telegram alerter | Reset on success |
| `instruments_subscribed` | `on_connect` | order monitor, reconciliation | Token list |

## D1 Integration (Kill Switch reads ws_connected)

The kill switch watchdog checks `ws_connected` to decide if a Level 2 trigger is warranted:

```python
# In risk_manager/kill_switch.py
async def risk_watchdog_task(kill_switch: dict, ws_state: dict) -> None:
    while True:
        # D1-T2: WebSocket down for 60s → Level 2
        if not ws_state.get("ws_connected", True):
            disconnect_ts = ws_state.get("disconnect_timestamp")
            if disconnect_ts:
                elapsed = (datetime.now(IST) - disconnect_ts).total_seconds()
                if elapsed >= 60 and is_market_hours():
                    await trigger_kill_switch(
                        kill_switch, level=2, reason="ws_timeout_60s"
                    )
        await asyncio.sleep(1)
```

## D6 Integration (Async Architecture)

Five concurrent asyncio tasks share this state dict:

```python
# In main.py — shared across all 5 tasks
ws_state: dict = {
    "ws_connected": False,
    "last_tick_timestamp": None,
    "disconnect_timestamp": None,
    "reconnect_attempt": 0,
    "instruments_subscribed": [],
}

await asyncio.gather(
    websocket_listener_task(kws, tick_queue, ws_state, kill_switch),
    signal_processor_task(tick_queue, ws_state, kill_switch),
    order_monitor_task(kill_switch, order_registry),
    risk_watchdog_task(kill_switch, ws_state),
    heartbeat_monitor_task(ws_state, kws, kill_switch),
)
```

## Thread Safety

`ws_state` is a regular Python dict accessed from both:
- **KiteTicker thread** — `on_ticks`, `on_connect`, `on_close` callbacks
- **asyncio event loop** — heartbeat monitor, reconnect loop, risk watchdog

Writes from the KiteTicker thread **must** use `loop.call_soon_threadsafe()`:

```python
# CORRECT — write from KiteTicker thread
loop.call_soon_threadsafe(ws_state.__setitem__, "ws_connected", True)
loop.call_soon_threadsafe(ws_state.update, {"ws_connected": True, "reconnect_attempt": 0})

# WRONG — direct dict write from thread (race condition)
ws_state["ws_connected"] = True  # Never do this from on_ticks/on_connect
```

Reads from asyncio tasks are safe — Python dict reads are atomic for simple types.

## IST Timestamps

All timestamps in `ws_state` must be IST-aware:

```python
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

# CORRECT
ws_state["disconnect_timestamp"] = datetime.now(IST)
ws_state["last_tick_timestamp"] = datetime.now(IST)

# WRONG — naive datetime
ws_state["disconnect_timestamp"] = datetime.now()
```

## Watchlist → Instrument Tokens

`instruments_subscribed` stores Zerodha instrument tokens (integers), not symbol strings:

```python
# Lookup tokens from settings.yaml watchlist
# instruments.json or kite.instruments("NSE") maps symbol → token
instrument_tokens = [256265, 408065, ...]  # RELIANCE, INFY, etc.
ws_state["instruments_subscribed"] = instrument_tokens

# Subscribe with these tokens
kws.subscribe(instrument_tokens)
kws.set_mode(kws.MODE_QUOTE, instrument_tokens)
```
