# Five Task Definitions — TradeOS D6

## Overview

All 5 tasks run concurrently inside ONE asyncio event loop.
They are created with `asyncio.create_task()` and collected into `asyncio.gather()`.
Each task is wrapped in `resilient_task()` for crash recovery (see `startup-shutdown-sequences.md`).

---

## TASK 1 — ws_listener

**Responsibility:** Receive raw ticks from the KiteConnect WebSocket (which runs in a thread),
validate each tick through the 5-gate `TickValidator` (D5), and push valid ticks to `tick_queue`.

**Input:** KiteConnect thread via `loop.call_soon_threadsafe()`
**Output:** `tick_queue: asyncio.Queue(maxsize=1000)`
**Max queue size:** 1000 (backpressure protection — see queue-specifications.md)

**On bad tick:** Discard, increment bad-tick counter, continue — never crash.
**On queue full:** Drop oldest tick, push new tick, log WARNING "queue_overflow".

```python
def _on_ticks_callback(ticks: list, shared_state: dict, loop, tick_queue):
    """Called from KiteConnect thread — bridges to asyncio."""
    loop.call_soon_threadsafe(
        asyncio.ensure_future,
        _process_ticks(ticks, shared_state, tick_queue)
    )

async def ws_listener_fn(shared_state: dict) -> None:
    """Manages KiteConnect WebSocket connection state."""
    # KiteConnect's on_ticks callback is registered at startup.
    # This coroutine monitors ws_connected and handles reconnect signals.
    while True:
        await asyncio.sleep(30)  # heartbeat interval
        if not shared_state.get("ws_connected"):
            log.warning("ws_listener_disconnected")
```

---

## TASK 2 — signal_processor

**Responsibility:** Consume ticks from `tick_queue`, run the S1 strategy logic,
check `kill_switch.is_trading_allowed()`, and push approved signals to `order_queue`.

**Input:** `tick_queue: asyncio.Queue`
**Output:** `order_queue: asyncio.Queue(maxsize=100)`

**Critical rule:** Check kill switch BEFORE every `order_queue.put()`.
If `kill_switch.is_trading_allowed()` returns False → drop signal, log INFO "signal_blocked_by_kill_switch".

**On exception in strategy logic:** Log ERROR with full traceback, continue — one bad tick does not stop processing.

```python
async def signal_processor_fn(shared_state: dict) -> None:
    tick_queue = shared_state["tick_queue"]
    order_queue = shared_state["order_queue"]

    while True:
        tick = await tick_queue.get()
        try:
            signal = strategy.on_tick(tick)
            if signal and kill_switch.is_trading_allowed():
                await order_queue.put(signal)
                shared_state["last_signal"] = signal
                shared_state["signals_generated_today"] += 1
        except Exception as e:
            log.error("signal_processor_error", error=str(e), exc_info=True)
        finally:
            tick_queue.task_done()
```

---

## TASK 3 — order_monitor

**Responsibility:** Poll Zerodha `kite.orders()` every 5 seconds, update `OrderStateMachine`
states, detect fills, rejections, and cancellations. Update `shared_state["open_orders"]`
and `shared_state["open_positions"]`.

**Poll interval:** Exactly 5 seconds (`asyncio.sleep(5)`)
**Input:** Zerodha REST API via `asyncio.to_thread(kite.orders)` — MUST use to_thread
**Output:** Updates `open_orders`, `open_positions`, `fills_today` in shared state

**On API error:** Log ERROR, continue polling — never stop monitoring.
**On order state change:** Trigger OrderStateMachine transition, log INFO "order_state_changed".

```python
async def order_monitor_fn(shared_state: dict) -> None:
    while True:
        try:
            orders = await asyncio.to_thread(kite.orders)  # ← MUST use to_thread
            for order in orders:
                state_machine.transition(order["order_id"], order["status"])
            shared_state["open_orders"] = {o["order_id"]: o for o in orders}
        except Exception as e:
            log.error("order_monitor_error", error=str(e))
        await asyncio.sleep(5)
```

---

## TASK 4 — risk_watchdog

**Responsibility:** Check drawdown and kill switch conditions every 1 second.
This is the fastest-running task and the most critical.

**Check interval:** Exactly 1 second (`asyncio.sleep(1)`)
**Checks on every cycle:**
- `daily_pnl_pct` vs `MAX_DAILY_LOSS_PCT` (0.03) → Level 2 if breached
- `consecutive_losses` vs threshold (3) → Level 1 if breached
- `ws_connected` during market hours → Level 1 if disconnected > 60s
- `open_positions` count vs `MAX_OPEN_POSITIONS` (3)

**On threshold breach:** Call `kill_switch.trigger(level=N)` immediately.
**On crash:** Log CRITICAL + trigger Level 3 immediately. **Never silently restart.**

```python
async def risk_watchdog_fn(shared_state: dict) -> None:
    while True:
        try:
            if shared_state["daily_pnl_pct"] <= -MAX_DAILY_LOSS_PCT:
                kill_switch.trigger(level=2, reason="daily_loss_exceeded")

            if shared_state["consecutive_losses"] >= 3:
                kill_switch.trigger(level=1, reason="consecutive_losses")
        except Exception as e:
            log.critical("risk_watchdog_crashed", error=str(e))
            kill_switch.trigger(level=3, reason="risk_watchdog_crashed")
            raise  # re-raise — handled by resilient_task variant
        await asyncio.sleep(1)
```

**IMPORTANT:** The `resilient_task` wrapper for `risk_watchdog` does NOT auto-restart.
It calls `kill_switch.trigger(level=3)` and cancels the task instead.

---

## TASK 5 — heartbeat

**Responsibility:** Emit a system-alive structured log every 30 seconds.
Checks that all 4 other tasks are still running by inspecting `tasks_alive` dict.

**Interval:** Exactly 30 seconds (`asyncio.sleep(30)`)

**Log format:**
```python
log.info("system_heartbeat",
    tasks_alive=list(shared_state["tasks_alive"].keys()),
    ws_connected=shared_state["ws_connected"],
    kill_switch_level=shared_state["kill_switch_level"],
    daily_pnl_pct=shared_state["daily_pnl_pct"],
    open_positions=len(shared_state["open_positions"]),
    queue_depths={
        "tick_q": shared_state["tick_queue"].qsize(),
        "order_q": shared_state["order_queue"].qsize(),
    }
)
```

**On dead task detected:** Log CRITICAL "task_not_alive" + send Telegram WARNING alert.
The heartbeat itself does NOT restart dead tasks — it only detects and alerts.

```python
async def heartbeat_fn(shared_state: dict) -> None:
    while True:
        await asyncio.sleep(30)
        for task_name, alive in shared_state["tasks_alive"].items():
            if not alive:
                log.critical("task_not_alive", task=task_name)
                await send_warning_alert("task_not_alive", ...)
        # Emit heartbeat log
        log.info("system_heartbeat", ...)
        shared_state["tasks_alive"]["heartbeat"] = True
```
