# Shared State Contract — TradeOS (Canonical Reference)

## Authority

This file is the **SINGLE CANONICAL SOURCE** for all `shared_state` keys used
across D1–D7. Every skill that reads or writes `shared_state` must use only
keys defined here. Do not introduce new keys in other reference files without
adding them here first with a designated owner.

## The Single Communication Mechanism

All 5 tasks communicate via **ONE shared dict**. No other inter-task mechanism is
permitted in Phase 1. No `asyncio.Queue` for non-tick/order data. No global variables.
No direct task-to-task calls.

```python
shared_state: dict  # passed to every task at startup
```

## Key Ownership Table

Each key has exactly ONE writer. All other tasks may read freely.
"Reconciler" refers to the D7 position reconciliation component (runs as a
background scheduled task and is called from ws_listener after reconnect).

| Key | Type | Owner (single writer) | Initial Value | Discipline |
|-----|----|---------------------|---------------|------------|
| `ws_connected` | `bool` | `ws_listener` | `False` | D3 |
| `last_tick_timestamp` | `datetime \| None` | `ws_listener` | `None` | D3 |
| `reconnect_attempt` | `int` | `ws_listener` | `0` | D3 |
| `disconnect_timestamp` | `datetime \| None` | `ws_listener` | `None` | D3 |
| `reconnect_requested` | `bool` | `heartbeat` | `False` | D3 |
| `last_signal` | `dict \| None` | `signal_processor` | `None` | D6 |
| `signals_generated_today` | `int` | `signal_processor` | `0` | D6 |
| `open_orders` | `dict[str, dict]` | `order_monitor` | `{}` | D2/D6 |
| `open_positions` | `dict[str, dict]` | `order_monitor` | `{}` | D2/D6 |
| `fills_today` | `int` | `order_monitor` | `0` | D6 |
| `daily_pnl_pct` | `float` | `risk_watchdog` | `0.0` | D6 |
| `daily_pnl_rs` | `float` | `risk_watchdog` | `0.0` | D6 |
| `consecutive_losses` | `int` | `risk_watchdog` | `0` | D6 |
| `kill_switch_level` | `int` | `KillSwitch.trigger()` (D1) | `0` | D1 |
| `system_start_time` | `datetime` | `heartbeat` | `datetime.now(IST)` | D6 |
| `tasks_alive` | `dict[str, bool]` | `heartbeat` | see below | D6 |
| `position_state` | `dict[int, dict]` | `reconciler` | `{}` | D7 |
| `recon_in_progress` | `bool` | `reconciler` | `False` | D7 |
| `locked_instruments` | `set[int]` | `reconciler` | `set()` | D7 |
| `tick_queue` | `asyncio.Queue` | startup | `asyncio.Queue(maxsize=1000)` | D6 |
| `order_queue` | `asyncio.Queue` | startup | `asyncio.Queue(maxsize=100)` | D6 |

**`tasks_alive` initial value:**
```python
{"ws_listener": True, "signal_processor": True, "order_monitor": True,
 "risk_watchdog": True, "heartbeat": True}
```

---

## Special Cases

### kill_switch_level — read cache, never write directly

`shared_state["kill_switch_level"]` is a **read cache** for display and logging.
The authoritative kill switch state lives inside the `KillSwitch` object (D1).

The write happens atomically inside `KillSwitch.trigger()` — which holds its own
`asyncio.Lock` while updating both the internal state dict and shared_state:

```python
async def trigger(self, level: int, reason: str) -> None:
    async with self._lock:
        self.state["level"] = level
        self.state["active"] = True
        self.state["reason"] = reason
        self.state["triggered_at"] = datetime.now(tz=IST)
        # Atomically mirror to shared_state read cache (same lock)
        self._shared_state["kill_switch_level"] = level
```

**Never write `kill_switch_level` directly.** Always call `kill_switch.trigger()`.
For order gating, always call `kill_switch.is_trading_allowed()` — never gate on
the shared_state key, which could be stale if the lock timing is unfavourable.

### reconnect_requested — heartbeat → ws_listener signal

This key exists so heartbeat can signal a silent disconnect without violating
ws_listener's single-writer ownership of `ws_connected`.

Protocol:
1. heartbeat detects tick silence > 30s during market hours
2. heartbeat writes `shared_state["reconnect_requested"] = True`
3. ws_listener's reconnect loop sees `reconnect_requested == True` and:
   - Writes `shared_state["ws_connected"] = False` (ws_listener owns this)
   - Writes `shared_state["disconnect_timestamp"] = datetime.now(IST)`
   - Calls `kws.reconnect()` via `asyncio.to_thread`
   - Writes `shared_state["reconnect_requested"] = False`

heartbeat **never** writes `ws_connected` or `disconnect_timestamp`.

### position_state vs open_positions — two different purposes

| Key | Owner | Purpose |
|-----|-------|---------|
| `open_positions` | `order_monitor` | Real-time in-flight view. Updated on every order fill event. Used by risk_watchdog for position-count gating. |
| `position_state` | `reconciler` | Verified-against-broker view. Updated during each reconciliation cycle. Used as the comparison baseline for the next cycle. |

These keys represent the same underlying reality from two different vantage points.
After a reconciliation that adjusts `position_state`, order_monitor must refresh
`open_positions` to match. The reconciler's view is authoritative after any disruption.

---

## Initialization

```python
def _init_shared_state() -> dict:
    import asyncio
    from datetime import datetime
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    return {
        # D3 — ws_listener owns all four WS state keys
        "ws_connected": False,
        "last_tick_timestamp": None,
        "reconnect_attempt": 0,
        "disconnect_timestamp": None,
        # D3 — heartbeat writes, ws_listener reads and clears
        "reconnect_requested": False,
        # D6 — signal_processor
        "last_signal": None,
        "signals_generated_today": 0,
        # D2/D6 — order_monitor
        "open_orders": {},
        "open_positions": {},
        "fills_today": 0,
        # D6 — risk_watchdog (kill_switch_level written by KillSwitch.trigger() only)
        "daily_pnl_pct": 0.0,
        "daily_pnl_rs": 0.0,
        "consecutive_losses": 0,
        "kill_switch_level": 0,
        # D6 — heartbeat
        "system_start_time": datetime.now(IST),
        "tasks_alive": {
            "ws_listener": True, "signal_processor": True,
            "order_monitor": True, "risk_watchdog": True, "heartbeat": True,
        },
        # D7 — reconciler (position reconciliation component)
        "position_state": {},
        "recon_in_progress": False,
        "locked_instruments": set(),
        # D6 — queues (also stored here for heartbeat queue-depth reporting)
        "tick_queue": asyncio.Queue(maxsize=1000),
        "order_queue": asyncio.Queue(maxsize=100),
    }
```

---

## Locking Rules

**Do NOT use `asyncio.Lock` for simple reads.** Python's GIL protects dict reads.

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
    # Note: do NOT update kill_switch_level here — call kill_switch.trigger()

# ✅ Simple dict write — no lock needed (single assignment is atomic in CPython)
shared_state["ws_connected"] = True

# ✅ Simple dict read — no lock needed
pnl = shared_state["daily_pnl_pct"]

# ❌ Over-locking — wastes CPU on read-only access
async with lock:
    pnl = shared_state["daily_pnl_pct"]  # unnecessary
```

---

## What Must NEVER Be in Shared State

```python
# ❌ Never store credentials
shared_state["api_key"] = "..."
shared_state["access_token"] = "..."

# ❌ Never store large objects that are frequently read-modified
# Use task-local variables instead; copy minimal data into shared_state

# ❌ Never store callback functions
shared_state["on_fill"] = some_function  # use task communication instead

# ❌ Never write kill_switch_level directly — atomic update happens inside trigger()
shared_state["kill_switch_level"] = 2  # WRONG — call kill_switch.trigger(2, reason)

# ❌ Never introduce new keys outside this file
# If a new key is needed, add it here first with an owner
```
