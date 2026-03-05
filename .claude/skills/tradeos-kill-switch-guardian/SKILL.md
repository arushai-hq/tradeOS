---
name: tradeos-kill-switch-guardian
description: TradeOS D1 kill switch implementation enforcer for the risk_manager module. Use this skill whenever implementing the 3-level kill switch hierarchy (Level 1 Trade Stop / Level 2 Position Stop / Level 3 System Stop), the is_trading_allowed() order gate, circuit breakers for daily loss limits or consecutive trade losses, WebSocket-disconnect-triggered trading halt logic (not reconnect), pybreaker API error circuit breaking, kill switch state sharing across asyncio tasks, or market-hours-aware manual reset restrictions. Invoke for tasks like: "add kill switch check before order placement", "implement 3% daily loss halt", "stop trading when websocket goes down for 60 seconds", "write is_trading_allowed gate", "Level 2 position stop — cancel orders and close positions", "prevent kill switch reset during 09:15–15:30 market hours", "trigger Level 1 after 3 consecutive losses", "Level 3 system stop — halt event loop", "pybreaker for Zerodha API errors". Do NOT invoke for general WebSocket reconnect/backoff logic, TickValidator, order state machine, position reconciliation, Prometheus metrics, or database setup unless the task explicitly involves kill switch triggering or the is_trading_allowed gate.
---

# TradeOS Kill Switch Guardian

The kill switch is TradeOS's primary safety net — the last line of defence before capital loss becomes uncontrolled. Every order path in the system must pass through it. Every trigger condition must be checked automatically. Every state transition must be logged and alerted.

This skill enforces the D1 reliability discipline from the TradeOS architecture.

## Quick Reference

| Level | Name | Actions |
|-------|------|---------|
| 1 | Trade Stop | `stop_new_signals = True`. Positions untouched. |
| 2 | Position Stop | Cancel all open orders + close all positions + `stop_new_signals = True`. |
| 3 | System Stop | Level 2 actions first, then `loop.stop()` + log CRITICAL. |

**The non-negotiable gate:** Every order path calls `kill_switch.is_trading_allowed()` before executing. Returns `False` for levels 1, 2, and 3. Returns `True` only when `level == 0`.

**Reset rule:** Manual only. Never auto-reset during market hours (09:15–15:30 IST).

## Trigger Conditions (canonical — do not substitute values)

| Trigger | Level | Condition | Note |
|---------|-------|-----------|------|
| L1-T1 | 1 | `consecutive_losses >= 5 AND daily_pnl_pct <= -0.015` | Compound. P=3.1% at 50% WR — prevents false fires on S1 variance. |
| L1-T2 | 1 | `daily_pnl_pct <= -0.030` | 3% daily loss hard cap. |
| L2-T1 | 2 | WebSocket disconnected > 60s during market hours | Time-based, hardcoded. |
| L2-T2 | 2 | Zerodha API errors > 5 in rolling 5-minute window | pybreaker circuit breaker. |
| L2-T3 | 2 | Position mismatch detected by D7 reconciliation | D7 integration point. |
| L3-T1 | 3 | Manual `/killswitch3` Telegram command | Human override. |
| L3-T2 | 3 | Unrecoverable exception in core event loop | Always escalates. |

## State Dict (shared across all 5 async tasks)

```python
kill_switch_state: dict = {
    "level": 0,          # 0 = inactive, 1/2/3 = active level
    "active": False,
    "reason": "",
    "triggered_at": None  # datetime | None
}
```

This dict is passed by reference into all 5 asyncio tasks at startup. All tasks read from the same object — never copy it.

## Core Implementation Rules

1. Use `pybreaker` for circuit breaker pattern — read `references/pybreaker-patterns.md`
2. Level 3 MUST execute Level 2 actions before halting the event loop — never skip
3. All state transitions produce a `structlog` CRITICAL log entry + Telegram alert
4. Escalation: if Level 1 persists > 5 minutes → auto-escalate to Level 2
5. Never place live orders — paper mode is always active (mode: paper in settings.yaml)

## Reference Files

Read these when implementing specific components:

| File | When to read |
|------|-------------|
| `references/kill-switch-levels.md` | Implementing any level's behaviour, escalation logic, or state transitions |
| `references/trigger-conditions.md` | Wiring up auto-trigger conditions (losses, drawdown, WS disconnect, API errors) |
| `references/pybreaker-patterns.md` | Using pybreaker, writing `KillSwitch` class, `is_trading_allowed()` method |
| `references/testing-kill-switch.md` | Writing pytest fixtures and test cases for kill switch behaviour |

## What This Skill Prevents

- Order placement code that does not call `is_trading_allowed()` first
- Auto-reset of kill switch during market hours
- Level 3 triggered without Level 2 actions first
- Kill switch state stored as a local variable (not shared across tasks)
- Silent failures — every trigger must log CRITICAL + Telegram alert
- Bare `except:` clauses swallowing kill switch errors
