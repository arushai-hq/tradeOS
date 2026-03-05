# Phase 1 — Startup Sequence

## Overview

Phase 1 begins immediately after `run_pre_market_gate()` returns a validated `KiteConnect` instance.
`asyncio.run(main(kite, shared_state))` is called. The `main()` coroutine runs the 11-step sequence
before any market data flows or signals are processed.

The critical constraint: **all 11 steps must complete successfully before `system_ready = True`**.
`signal_processor` checks `system_ready` before processing any tick — it will discard ticks until
the flag is set.

---

## The 11-Step Sequence

```
Step 1  → Initialize shared state (all 19 keys to default values)
Step 2  → Set session metadata (date, start_time)
Step 3  → Configure structlog with IST timestamps
Step 4  → Run D7 startup reconciliation (BLOCKING — stale positions = sys.exit(1))
Step 5  → Start risk_watchdog task
Step 6  → Start order_monitor task
Step 7  → Connect WebSocket (D3)
Step 8  → Wait for WS CONNECTED (30s timeout — sys.exit(1) on timeout)
Step 9  → Start signal_processor (only after WS confirmed)
Step 10 → Start heartbeat task
Step 11 → Set system_ready = True, send Telegram "✅ TradeOS LIVE"
```

Tasks start in dependency order: watchdog and monitor are needed before signals can flow;
WebSocket must be connected before signal_processor starts consuming ticks.

---

## Step 4 — D7 Startup Reconciliation (The Critical Blocker)

This step prevents trading over stale positions from a prior session crash. It is the only
startup step that can produce `sys.exit(1)` — all other steps either succeed or propagate
exceptions to the resilient_task wrapper.

```python
async def _run_startup_reconciliation(kite: KiteConnect, shared_state: dict) -> None:
    """
    Calls D7 reconciliation and blocks startup if any prior-session positions exist.

    The scenario this prevents:
      - TradeOS crashed at 14:30 with 1 open position
      - Zerodha auto-squared off the position at 15:20 (MIS)
      - Operator restarts TradeOS next morning
      - If positions from the crash are still in shared_state (they shouldn't be),
        or if Zerodha shows non-zero positions for any reason, startup must halt

    After reconciliation, open_positions should be empty — it's a new trading day.
    Any non-empty result means something is wrong and requires human review.
    """
    log.info("startup_reconciliation_begin")

    # Run D7 reconciliation (from tradeos-position-reconciler skill)
    await run_reconciliation(kite, shared_state)

    # After reconciliation, broker is source of truth — check the result
    open_positions = shared_state.get("open_positions", {})
    if open_positions:
        position_list = list(open_positions.keys())
        log.critical("startup_blocked_stale_positions",
                     positions=position_list,
                     count=len(position_list))
        _send_startup_alert_sync(
            shared_state["_secrets"],  # stored during Phase 0
            f"⛔ TradeOS: Prior session positions found at startup.\n"
            f"Positions: {', '.join(position_list)}\n"
            f"Manual review required before restarting."
        )
        sys.exit(1)

    log.info("startup_reconciliation_clean", message="No prior positions — safe to proceed")
```

**Why `sys.exit(1)` and not a kill switch trigger?**
At this point in startup, the kill switch hierarchy isn't running yet (tasks haven't started).
The system has no running event loop to cancel. `sys.exit(1)` is the correct termination path.

---

## Full `main()` Coroutine

```python
import asyncio
import sys
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

async def main(kite: KiteConnect, shared_state: dict) -> None:
    """
    Phase 1 startup sequence — runs before any trading activity.
    All steps must complete before system_ready = True.
    """
    # Step 1: Initialize shared state
    _init_shared_state(shared_state)

    # Step 2: Session metadata
    now_ist = datetime.now(IST)
    shared_state["session_date"] = now_ist.date().isoformat()
    shared_state["session_start_time"] = now_ist

    # Step 3: Configure logging
    _configure_structlog()

    log.info("startup_phase1_begin",
             session_date=shared_state["session_date"],
             mode=shared_state["config"]["system"]["mode"])

    # Step 4: D7 startup reconciliation (BLOCKING — exits on stale positions)
    await _run_startup_reconciliation(kite, shared_state)

    # Step 5: Start risk_watchdog (must be first — it guards everything)
    risk_task = asyncio.create_task(
        resilient_task("risk_watchdog", risk_watchdog_fn, shared_state),
        name="risk_watchdog"
    )
    shared_state["tasks_alive"]["risk_watchdog"] = True

    # Step 6: Start order_monitor
    monitor_task = asyncio.create_task(
        resilient_task("order_monitor", order_monitor_fn, shared_state, kite),
        name="order_monitor"
    )
    shared_state["tasks_alive"]["order_monitor"] = True

    # Step 7: Connect WebSocket
    _connect_websocket(kite, shared_state)

    # Step 8: Wait for WS CONNECTED (30s timeout)
    try:
        await asyncio.wait_for(_wait_for_ws_connected(shared_state), timeout=30.0)
    except asyncio.TimeoutError:
        log.critical("startup_ws_timeout", timeout_seconds=30)
        sys.exit(1)

    # Step 9: Start signal_processor (WS confirmed — safe to start)
    signal_task = asyncio.create_task(
        resilient_task("signal_processor", signal_processor_fn, shared_state,
                       shared_state["config"]),
        name="signal_processor"
    )
    shared_state["tasks_alive"]["signal_processor"] = True

    # Step 10: Start heartbeat
    heartbeat_task = asyncio.create_task(
        resilient_task("heartbeat", heartbeat_fn, shared_state),
        name="heartbeat"
    )
    shared_state["tasks_alive"]["heartbeat"] = True

    # Step 11: System ready
    shared_state["system_ready"] = True
    log.info("startup_system_ready",
             session_date=shared_state["session_date"],
             capital=shared_state["config"]["capital"]["s1_allocation"])
    await send_critical_alert(
        "system_start",
        {"Mode": shared_state["config"]["system"]["mode"],
         "Capital": f"₹{shared_state['config']['capital']['s1_allocation']:,}",
         "Session": shared_state["session_date"]},
        shared_state=shared_state
    )

    # Run all tasks concurrently until any exits
    await asyncio.gather(risk_task, monitor_task, signal_task, heartbeat_task,
                         return_exceptions=True)
```

---

## Shared State Initialization

```python
def _init_shared_state(shared_state: dict) -> None:
    """
    Initializes all 19 shared state keys to their default values.
    Must be called at the very start of Phase 1 (step 1).
    See tradeos-async-architecture references/shared-state-contract.md for full schema.
    """
    shared_state.update({
        # Session metadata (set in Step 2)
        "session_date": None,
        "session_start_time": None,

        # System state
        "system_ready": False,
        "accepting_signals": True,
        "pre_market_gate_passed": shared_state.get("pre_market_gate_passed", False),
        "telegram_active": shared_state.get("telegram_active", True),
        "zerodha_user_id": shared_state.get("zerodha_user_id", ""),

        # Trading state
        "ws_connected": False,
        "kill_switch_level": 0,
        "recon_in_progress": False,
        "locked_instruments": set(),

        # Position and order tracking
        "open_positions": {},
        "open_orders": {},
        "fills_today": [],

        # Counters
        "daily_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "signals_generated_today": 0,

        # Queues (created fresh each session)
        "tick_queue": asyncio.Queue(maxsize=1000),
        "order_queue": asyncio.Queue(maxsize=100),

        # Task health
        "tasks_alive": {},

        # Internal — not part of public contract
        "last_signal": None,
        "reconnect_requested": False,
    })
```
