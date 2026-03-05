# Shared State Contract — TradeOS D6

## The Single Communication Mechanism

All 5 tasks communicate via **ONE shared dict**. No other inter-task mechanism is
permitted in Phase 1. No `asyncio.Queue` for non-tick/order data. No global variables.
No direct task-to-task calls.

```python
shared_state: dict  # passed to every task at startup
```

## Key Ownership Table

Each key has exactly ONE writer task. All other tasks may read freely.

| Key | Type | Owner | Initial Value |
|-----|------|-------|---------------|
| `ws_connected` | `bool` | `ws_listener` | `False` |
| `last_tick_timestamp` | `datetime \| None` | `ws_listener` | `None` |
| `reconnect_attempt` | `int` | `ws_listener` | `0` |
| `last_signal` | `dict \| None` | `signal_processor` | `None` |
| `signals_generated_today` | `int` | `signal_processor` | `0` |
| `open_orders` | `dict[str, dict]` | `order_monitor` | `{}` |
| `open_positions` | `dict[str, dict]` | `order_monitor` | `{}` |
| `fills_today` | `int` | `order_monitor` | `0` |
| `daily_pnl_pct` | `float` | `risk_watchdog` | `0.0` |
| `daily_pnl_rs` | `float` | `risk_watchdog` | `0.0` |
| `consecutive_losses` | `int` | `risk_watchdog` | `0` |
| `kill_switch_level` | `int` | `risk_watchdog` | `0` |
| `system_start_time` | `datetime` | `heartbeat` | `datetime.now(IST)` |
| `tasks_alive` | `dict[str, bool]` | `heartbeat` | see below |
| `tick_queue` | `asyncio.Queue` | startup | `asyncio.Queue(maxsize=1000)` |
| `order_queue` | `asyncio.Queue` | startup | `asyncio.Queue(maxsize=100)` |

**`tasks_alive` initial value:**
```python
{"ws_listener": True, "signal_processor": True, "order_monitor": True,
 "risk_watchdog": True, "heartbeat": True}
```

## Initialization

```python
def _init_shared_state() -> dict:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    return {
        # ws_listener
        "ws_connected": False,
        "last_tick_timestamp": None,
        "reconnect_attempt": 0,
        # signal_processor
        "last_signal": None,
        "signals_generated_today": 0,
        # order_monitor
        "open_orders": {},
        "open_positions": {},
        "fills_today": 0,
        # risk_watchdog
        "daily_pnl_pct": 0.0,
        "daily_pnl_rs": 0.0,
        "consecutive_losses": 0,
        "kill_switch_level": 0,
        # heartbeat
        "system_start_time": datetime.now(IST),
        "tasks_alive": {
            "ws_listener": True, "signal_processor": True,
            "order_monitor": True, "risk_watchdog": True, "heartbeat": True
        },
        # queues (also stored here for heartbeat queue depth reporting)
        "tick_queue": asyncio.Queue(maxsize=1000),
        "order_queue": asyncio.Queue(maxsize=100),
    }
```

## Locking Rules

**Do NOT use `asyncio.Lock` for simple reads.** Python's GIL protects dict reads.
Redundant locking wastes CPU and can cause subtle bugs.

**Use `asyncio.Lock` ONLY for atomic read-modify-write cycles:**

```python
# ✅ Lock needed: read → compute → write must be atomic
async with lock:
    current = shared_state["consecutive_losses"]
    shared_state["consecutive_losses"] = current + 1

# ✅ Lock needed: coordinated multi-key update
async with lock:
    shared_state["daily_pnl_pct"] = new_pct
    shared_state["daily_pnl_rs"] = new_rs
    shared_state["kill_switch_level"] = 2

# ✅ Simple dict write — no lock needed (single assignment is atomic in CPython)
shared_state["ws_connected"] = True

# ✅ Simple dict read — no lock needed
pnl = shared_state["daily_pnl_pct"]

# ❌ Over-locking — wastes CPU on read-only access
async with lock:
    pnl = shared_state["daily_pnl_pct"]  # unnecessary
```

## What Must NEVER Be in Shared State

```python
# ❌ Never store credentials
shared_state["api_key"] = "..."
shared_state["access_token"] = "..."

# ❌ Never store large objects that are frequently read-modified
# Use task-local variables instead; copy minimal data into shared_state

# ❌ Never store callback functions
shared_state["on_fill"] = some_function  # use task communication instead
```
