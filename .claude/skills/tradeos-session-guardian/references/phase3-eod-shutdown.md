# Phase 3 — EOD Shutdown (15:00–15:30 IST)

## Overview

Phase 3 is a **scheduled, orderly shutdown** — not a kill switch event.
S1 is intraday MIS; all positions must be closed before 15:30 IST or Zerodha auto-squares them.
The Phase 3 sequence ensures TradeOS closes positions intentionally, with reconciliation confirmation,
before cleanly exiting the process.

**Critical distinction:** Phase 3 is initiated by the clock, not by a risk event. Do not trigger
D1 kill switch levels for Phase 3 events. The kill switch is for anomalies; 15:00 EOD is expected.

---

## Timeline

```
15:00 IST  ── Hard exit signal
               signal_processor stops accepting new signals
               accepting_signals = False
               tick_queue drained without processing

15:00–15:10 ── Position exit window
               All S1 positions should be closed by strategy exit logic
               (stop-loss hit, target hit, or EOD exit signal)

15:10–15:15 ── Position check + manual close if needed
               If any open position remains → WARNING + Telegram
               Close remaining positions via market order

15:20 IST  ── Final D7 reconciliation
               Verify all positions = 0, all orders in terminal state
               Any mismatch → CRITICAL + Telegram

15:25 IST  ── Daily summary (D4)
               Telegram daily summary: trades, P&L, win rate, max drawdown

15:30 IST  ── Clean shutdown
               Cancel all asyncio tasks (reverse startup order)
               Disconnect WebSocket
               Final log write
               sys.exit(0)
```

---

## 15:00 — Hard Exit Signal

The hard exit trigger runs inside `risk_watchdog_fn` on every 1-second cycle.
When 15:00 is crossed, it sets `accepting_signals = False`, which stops `signal_processor`
from enqueuing new signals.

This is NOT a kill switch trigger — open positions stay open, order_monitor stays running,
the event loop continues to handle fills and cancellations.

```python
# Inside risk_watchdog_fn, in the 1s check loop:
from datetime import time as dtime
import pytz

IST = pytz.timezone("Asia/Kolkata")
HARD_EXIT_TIME = dtime(15, 0)

async def risk_watchdog_fn(shared_state: dict) -> None:
    hard_exit_triggered = False

    while True:
        try:
            now_ist = datetime.now(IST).time()

            # Phase 3: hard exit at 15:00 (runs once)
            if not hard_exit_triggered and now_ist >= HARD_EXIT_TIME:
                hard_exit_triggered = True
                shared_state["accepting_signals"] = False

                # Drain the tick queue without processing
                tick_queue = shared_state["tick_queue"]
                while not tick_queue.empty():
                    try:
                        tick_queue.get_nowait()
                        tick_queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                open_count = len(shared_state["open_positions"])
                log.info("hard_exit_triggered",
                         time="15:00 IST",
                         open_positions=open_count,
                         note="Scheduled EOD — not a kill switch event")
                # Do NOT trigger kill_switch here — this is scheduled, not anomalous

            # --- Standard risk checks continue until positions are closed ---
            if shared_state["daily_pnl_pct"] <= -MAX_DAILY_LOSS_PCT:
                kill_switch.trigger(level=2, reason="daily_loss_exceeded")

        except Exception as e:
            log.critical("risk_watchdog_crashed", error=str(e))
            kill_switch.trigger(level=3, reason="risk_watchdog_crashed")
            raise

        await asyncio.sleep(1)
```

`signal_processor` checks `accepting_signals` before processing each tick:
```python
# In signal_processor_fn, after kill switch gate:
if not shared_state.get("accepting_signals", True):
    log.debug("signal_blocked_accepting_signals_false")
    continue
```

---

## 15:10–15:15 — Position Check

If any position remains open at 15:10, S1's exit logic hasn't fired. Close manually via market order.
This check runs in `order_monitor_fn` at the 15:10 boundary:

```python
# Inside order_monitor_fn:
POSITION_CHECK_TIME = dtime(15, 10)
position_check_done = False

# In the polling loop:
now_ist = datetime.now(IST).time()
if not position_check_done and now_ist >= POSITION_CHECK_TIME:
    position_check_done = True
    open_positions = shared_state["open_positions"]
    if open_positions:
        for symbol in list(open_positions.keys()):
            log.warning("position_open_at_1510",
                        symbol=symbol,
                        qty=open_positions[symbol]["qty"])
            await send_warning_alert(
                f"position_open_1510_{symbol}",
                "position_open_at_1510",
                {"Symbol": symbol,
                 "Qty": open_positions[symbol]["qty"],
                 "Action": "Closing via market order"},
                shared_state=shared_state
            )
            # Place market exit order
            await _place_exit_order(kite, symbol, open_positions[symbol], shared_state)
```

---

## 15:20 — Final Reconciliation

D7 reconciliation confirms all positions and orders are in terminal state.
Expected result: `open_positions = {}`, all orders in FILLED / CANCELLED / REJECTED.

```python
# In a scheduled coroutine triggered at 15:20:
async def _run_eod_reconciliation(kite: KiteConnect, shared_state: dict) -> None:
    """
    Final D7 reconciliation at 15:20.
    All positions expected to be zero. Any mismatch is CRITICAL.
    """
    log.info("eod_reconciliation_begin")
    await run_reconciliation(kite, shared_state)

    open_positions = shared_state.get("open_positions", {})
    if open_positions:
        log.critical("eod_reconciliation_positions_remaining",
                     positions=list(open_positions.keys()))
        await send_critical_alert(
            "eod_positions_not_closed",
            {"Positions": str(list(open_positions.keys())),
             "Action": "Manual intervention required — Zerodha auto-square at 15:20"},
            shared_state=shared_state
        )
    else:
        log.info("eod_reconciliation_clean", message="All positions closed — clean EOD")
```

---

## 15:25 — Daily Summary

D4's `schedule_daily_summary()` coroutine fires at 15:35 by default (see D4 references).
For Phase 3, trigger it explicitly at 15:25 to ensure it runs before 15:30 shutdown:

```python
async def _send_eod_daily_summary(shared_state: dict) -> None:
    """
    Sends the daily P&L summary before clean shutdown.
    Uses the same D4 format as the scheduled daily summary.
    """
    session_stats = {
        "trades": len(shared_state.get("fills_today", [])),
        "winners": sum(1 for f in shared_state.get("fills_today", []) if f.get("pnl", 0) > 0),
        "losers": sum(1 for f in shared_state.get("fills_today", []) if f.get("pnl", 0) <= 0),
        "total_pnl_rs": sum(f.get("pnl", 0) for f in shared_state.get("fills_today", [])),
        "max_drawdown_pct": shared_state.get("daily_pnl_pct", 0.0),
        "mode": shared_state["config"]["system"]["mode"],
    }
    await send_daily_summary(session_stats, shared_state=shared_state)
```

---

## 15:30 — Clean Shutdown

Task cancellation runs in **reverse startup order** (heartbeat first, risk_watchdog last).
This mirrors the D6 graceful shutdown sequence.

```python
async def _clean_shutdown(tasks: dict, kite: KiteConnect, shared_state: dict) -> None:
    """
    Orderly shutdown at 15:30 IST.
    Cancels tasks in reverse startup order, disconnects WebSocket, exits cleanly.
    """
    shared_state["system_ready"] = False
    log.info("shutdown_begin", time="15:30 IST")

    # Cancel in reverse startup order
    shutdown_order = ["heartbeat", "signal_processor", "ws_listener",
                      "order_monitor", "risk_watchdog"]
    for task_name in shutdown_order:
        task = tasks.get(task_name)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            log.info("task_cancelled", task=task_name)

    # Disconnect WebSocket
    try:
        kite.close()
        log.info("websocket_disconnected")
    except Exception as e:
        log.warning("websocket_close_error", error=str(e))

    # Final log
    session_date = shared_state.get("session_date", "unknown")
    log.info("session_complete",
             date=session_date,
             fills=len(shared_state.get("fills_today", [])),
             final_pnl_pct=shared_state.get("daily_pnl_pct", 0.0))

    await send_critical_alert(
        "session_complete",
        {"Date": session_date,
         "Mode": shared_state["config"]["system"]["mode"],
         "Status": "Clean shutdown"},
        shared_state=shared_state
    )

    sys.exit(0)
```

---

## What Must NOT Happen in Phase 3

| Action | Why it's wrong |
|--------|---------------|
| Triggering kill switch Level 2 for 15:00 EOD | Kill switch is for anomalies; 15:00 is scheduled |
| Placing new entry orders after 15:00 | `accepting_signals = False` must prevent this |
| Skipping reconciliation at 15:20 | Could leave positions mismatched in Zerodha records |
| Deleting `fills_today` before daily summary | Summary reads from this list |
| Calling `sys.exit(1)` for clean shutdown | 15:30 is success; use `sys.exit(0)` |
