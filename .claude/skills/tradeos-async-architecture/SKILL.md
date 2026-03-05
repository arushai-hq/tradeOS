---
name: tradeos-async-architecture
description: >
  TradeOS D6 async architecture enforcer — the exact 5-task concurrent structure,
  shared state contract, queue specifications, blocking I/O rules, and
  startup/shutdown sequences for the TradeOS asyncio event loop.
  Use this skill whenever implementing: the main() event loop coroutine,
  any of the 5 named tasks (ws_listener, signal_processor, order_monitor,
  risk_watchdog, heartbeat), the shared state dict and its ownership rules,
  asyncio.Queue configurations, crash recovery wrappers, graceful shutdown
  on SIGTERM/SIGINT, or the startup sequence that enforces reconciliation
  before trading begins.
  Invoke for tasks like: "write the main asyncio event loop for TradeOS",
  "implement the 5 concurrent trading tasks", "set up shared state dict
  between tasks", "write the risk watchdog task", "implement graceful
  shutdown for the trading system", "bridge KiteConnect thread to asyncio
  tasks", "write the crash recovery wrapper", "what order do tasks start",
  "write the heartbeat task", "implement order_monitor polling loop".
  Do NOT invoke for: general Python async patterns outside TradeOS,
  FastAPI or Django async views, asyncio for web scrapers or data pipelines,
  async database queries, or any async code that is not the 5-task TradeOS
  event loop.
related-skills: python-pro, tradeos-kill-switch-guardian, tradeos-websocket-resilience, tradeos-tick-validator, tradeos-observability
---

# TradeOS D6 — Async Architecture

## The 5-Task Architecture (Fixed)

Phase 1 runs exactly 5 concurrent asyncio tasks inside ONE event loop.
Task names are fixed identifiers used in logs, monitoring, and the shared state.

| Task | Responsibility | Input → Output | Interval |
|------|---------------|----------------|----------|
| `ws_listener` | Receive ticks from KiteConnect thread bridge, validate via TickValidator, push to tick_queue | KiteConnect thread → tick_queue | event-driven |
| `signal_processor` | Consume tick_queue, run S1 strategy, check kill switch, push approved signals to order_queue | tick_queue → order_queue | event-driven |
| `order_monitor` | Poll kite.orders() every 5s, update OrderStateMachine | Zerodha REST → shared state | 5s |
| `risk_watchdog` | Check drawdown + kill switch every 1s | shared state → kill_switch.trigger() | 1s |
| `heartbeat` | Emit alive log every 30s, detect dead tasks | shared state → structlog + Telegram | 30s |

## Quick Reference — Critical Rules

1. **signal_processor NEVER starts before ws_listener is CONNECTED**
2. **Reconciliation MUST complete before any trading task starts**
3. **All Zerodha API calls use `asyncio.to_thread()`** — never call synchronously in the loop
4. **Tasks communicate ONLY via shared state dict or queues** — no direct calls
5. **`asyncio.CancelledError` is NEVER suppressed** — always re-raise it
6. **risk_watchdog crash → Level 3 kill switch** — never silently restart it

## Reference Files

Read these for implementation details:

| What you're building | Read this |
|----------------------|-----------|
| Any of the 5 tasks | `references/five-task-definitions.md` |
| Shared state dict (all skills) | `references/shared-state-contract.md` ← **canonical for all D1–D7 keys** |
| tick_queue or order_queue | `references/queue-specifications.md` |
| asyncio.to_thread() rules | `references/blocking-io-rules.md` |
| Startup or shutdown sequence | `references/startup-shutdown-sequences.md` |

`shared-state-contract.md` is the authoritative reference for every key in `shared_state`
across all 8 skills (D1–D7). Any component that reads or writes `shared_state` must
use only keys listed there. It also documents the `kill_switch_level` atomic-write
pattern (D1) and the `reconnect_requested` heartbeat-signal pattern (D3).

## Main Entry Point Pattern

```python
async def main(config: dict, secrets: dict) -> None:
    shared_state = _init_shared_state()

    # Step 1: Blocking reconciliation before any trading
    await reconcile_on_startup(shared_state)

    # Step 2: Create tasks in startup order (risk first, signal last)
    tasks = [
        asyncio.create_task(resilient_task("risk_watchdog",  risk_watchdog_fn,  shared_state)),
        asyncio.create_task(resilient_task("order_monitor",  order_monitor_fn,  shared_state)),
        asyncio.create_task(resilient_task("ws_listener",    ws_listener_fn,    shared_state)),
        asyncio.create_task(resilient_task("signal_processor", signal_processor_fn, shared_state)),
        asyncio.create_task(resilient_task("heartbeat",      heartbeat_fn,      shared_state)),
    ]

    await asyncio.gather(*tasks, return_exceptions=True)
```
