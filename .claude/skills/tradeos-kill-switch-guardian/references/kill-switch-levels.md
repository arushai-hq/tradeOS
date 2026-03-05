# Kill Switch Levels — TradeOS D1

## Pre-Order Gate — Mode Safety Assertion

This gate is **zeroth** — it runs before the kill switch check, before any Zerodha API call, before any signal reaches `order_queue`. It is not a kill switch level. It is a hard assertion that crashes intentionally if the system is misconfigured.

```python
def assert_paper_mode(config: dict) -> None:
    """
    Zeroth gate. Called at signal_processor task startup (once) and before
    any order is placed. Hard AssertionError if not in paper mode —
    intentional crash, not graceful degradation.
    D6's resilient_task crash recovery catches AssertionError → escalates to Level 3.
    """
    assert config["system"]["mode"] == "paper", (
        f"LIVE ORDER BLOCKED: mode is '{config['system']['mode']}'. "
        f"Manual promotion to live mode required via config/settings.yaml"
    )
```

**Why AssertionError and not a kill switch trigger?**
- Misconfigured mode is a deployment error, not a runtime trading event
- The kill switch handles market-hours risk events — not config faults
- A hard crash on wrong mode is the correct signal: something is wrong at the infra level
- D6's `resilient_task` wrapper catches the `AssertionError` and escalates to Level 3

**Order of gates in signal_processor (strictly sequential):**
```
0. assert_paper_mode(config)                                  ← config fault — crashes hard
1. kill_switch.is_trading_allowed()                           ← risk gate — blocks gracefully
2. if shared_state["recon_in_progress"]: skip signal          ← reconciliation gate
3. if symbol in shared_state["locked_instruments"]: skip      ← instrument lock gate
→ only then: await order_queue.put(signal)
```

---

## Level 0 — Inactive (Normal State)

- `kill_switch_state = {"level": 0, "active": False, "reason": "", "triggered_at": None}`
- `is_trading_allowed()` returns `True`
- All 5 async tasks operate normally

---

## Level 1 — Trade Stop

**Effect:** Stop all new signal processing. Existing open positions are left untouched.

**State transition:**
```python
kill_switch_state["level"] = 1
kill_switch_state["active"] = True
kill_switch_state["reason"] = reason  # e.g. "3_consecutive_losses"
kill_switch_state["triggered_at"] = datetime.now(tz=IST)
```

**Actions:**
- Set `stop_new_signals = True` (checked by Signal Processor task)
- Do NOT cancel existing orders
- Do NOT close open positions
- Log CRITICAL via structlog
- Send Telegram alert

**`is_trading_allowed()` returns:** `False`

**Auto-escalation:**
- If Level 1 state persists > 5 minutes without manual reset → auto-trigger Level 2
- Check: `(datetime.now(tz=IST) - kill_switch_state["triggered_at"]).seconds > 300`
- Run this check inside the Risk Watchdog task (runs every 1s)

---

## Level 2 — Position Stop

**Effect:** Cancel all open orders, close all positions, stop new signals.

**State transition:**
```python
kill_switch_state["level"] = 2
kill_switch_state["active"] = True
kill_switch_state["reason"] = reason
kill_switch_state["triggered_at"] = datetime.now(tz=IST)
```

**Actions (execute in this order):**
1. Set `stop_new_signals = True`
2. Cancel ALL open orders via `kite.cancel_order()` for each open order
3. Close ALL open positions via market sell orders (or cover for short)
4. Log CRITICAL via structlog with position count and order count
5. Send Telegram alert immediately

**`is_trading_allowed()` returns:** `False`

**Error handling during Level 2:**
- If cancel/close fails for a specific instrument: log error, lock the instrument, continue with others
- Never block Level 2 execution for a single instrument failure

---

## Level 3 — System Stop

**Effect:** Nuclear option. Execute Level 2 first, then halt the entire asyncio event loop.

**State transition:**
```python
kill_switch_state["level"] = 3
kill_switch_state["active"] = True
kill_switch_state["reason"] = reason
kill_switch_state["triggered_at"] = datetime.now(tz=IST)
```

**Actions (execute in strict order):**
1. **Execute all Level 2 actions first** — cancel orders, close positions
2. Log CRITICAL: `log.critical("system_stop_level3", reason=reason)`
3. Send Telegram alert: "SYSTEM STOP — TradeOS halted. Manual restart required."
4. Stop the asyncio event loop: `asyncio.get_event_loop().stop()`

**Critical rule:** Level 3 without Level 2 is a bug. Always run Level 2 actions first.

**`is_trading_allowed()` returns:** `False`

---

## Reset Protocol

```python
def reset_kill_switch(kill_switch_state: dict) -> bool:
    """Reset only allowed manually, never during market hours."""
    now = datetime.now(tz=IST).time()
    market_open = time(9, 15)
    market_close = time(15, 30)

    if market_open <= now <= market_close:
        log.warning("kill_switch_reset_rejected", reason="market_hours_active")
        return False  # Cannot reset during market hours

    kill_switch_state["level"] = 0
    kill_switch_state["active"] = False
    kill_switch_state["reason"] = ""
    kill_switch_state["triggered_at"] = None
    log.info("kill_switch_reset", operator="manual")
    return True
```

**Never** auto-reset from within the event loop, a watchdog, or any automated task.

---

## Escalation Logic (in Risk Watchdog)

```python
async def check_level1_escalation(kill_switch_state: dict) -> None:
    """Called inside Risk Watchdog every 1 second."""
    if kill_switch_state["level"] == 1 and kill_switch_state["triggered_at"]:
        elapsed = (datetime.now(tz=IST) - kill_switch_state["triggered_at"]).seconds
        if elapsed > 300:  # 5 minutes
            log.critical("kill_switch_escalating", from_level=1, to_level=2,
                         elapsed_seconds=elapsed)
            await trigger_kill_switch(kill_switch_state, level=2,
                                      reason="level1_persisted_5min")
```
