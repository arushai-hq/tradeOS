---
name: tradeos-position-reconciler
description: >
  TradeOS D7 position reconciliation enforcer — Zerodha is always the source of truth.
  Use this skill whenever implementing: startup reconciliation that blocks trading until
  clean, the position comparison algorithm (broker vs local state), mismatch handling
  and per-instrument locking, resolution modes (manual / auto-adjust / ghost close),
  scheduled reconciliation every 30 minutes, post-disruption reconciliation after
  WebSocket reconnect or API error recovery, or the instrument lock mechanism that
  prevents orders on mismatched instruments.
  Invoke for tasks like: "block startup until positions match broker", "compare Zerodha
  positions to local state", "lock instrument on position mismatch", "auto-adjust local
  state to match broker", "detect and handle ghost positions", "schedule reconciliation
  every 30 minutes", "run reconciliation after WebSocket reconnect", "implement
  position reconciliation for TradeOS", "mismatch resolution modes", "unlock instrument
  after successful reconciliation", "post-disruption reconciliation pause".
  Do NOT invoke for: kill switch logic, order state machine, tick validation, WebSocket
  reconnect/backoff, Prometheus metrics, general position sizing, or non-TradeOS
  reconciliation systems.
related-skills: tradeos-kill-switch-guardian, tradeos-async-architecture, tradeos-observability
---

# TradeOS D7 — Position Reconciliation

## Cardinal Rule

**Zerodha is always the source of truth.**

When broker state and local state disagree, local state is wrong. Always.
Never assume local `open_positions` is correct after a disruption. Always verify against broker.

## The 4 Reconciliation Triggers

| Trigger | When | Action |
|---------|------|--------|
| Startup | Before any trading task starts | Blocks boot until clean — mismatch aborts startup |
| Scheduled | Every 30 minutes | Non-blocking background check |
| Post-disruption | After WS reconnect or API error recovery | Pause signal_processor, recon, then resume |
| Manual | Operator command (future) | Same as scheduled |

## Quick Reference — Critical Rules

1. **Startup recon MUST complete before any `asyncio.create_task()` call** — trading never starts with unverified state
2. **Mismatch → lock that instrument only** — other instruments continue trading
3. **Auto-adjust is DISABLED by default** — mismatches require human confirmation unless explicitly enabled
4. **Ghost positions are NEVER automatically closed** — log CRITICAL, alert Telegram, wait for human
5. **Post-disruption recon pauses signal_processor** — no signals during recon window
6. **Instrument lock persists across reconnects** — only cleared by successful recon or manual unlock

## Reference Files

Read these for implementation details:

| What you're building | Read this |
|----------------------|-----------|
| Reconciliation triggers and timing | `references/reconciliation-triggers.md` |
| Position comparison algorithm | `references/comparison-algorithm.md` |
| Mismatch resolution modes | `references/mismatch-resolution-modes.md` |
| Instrument lock mechanism | `references/instrument-lock-mechanism.md` |
| Zerodha positions API payload | `references/zerodha-positions-payload.md` |

## Entry Point Pattern

> **Position local map is built from `shared_state["open_positions"]` owned by
> `order_monitor` (D6). D7 never writes this key directly.**
> Mismatch corrections in auto-adjust mode go via `reconciler.apply_fix(symbol, qty)`.

```python
async def reconcile_positions(shared_state: dict, kite, mode: str = "startup") -> bool:
    """
    Returns True if clean (no mismatches), False if mismatches found.
    On startup: raises ReconciliationMismatchError if mismatches found.
    On scheduled/post-disruption: locks affected instruments, returns False.
    """
    broker_positions = await asyncio.to_thread(kite.positions)
    local_positions = shared_state["open_positions"]

    mismatches = _compare_positions(broker_positions["net"], local_positions)

    if not mismatches:
        log.info("reconciliation_clean", trigger=mode, instrument_count=len(broker_positions["net"]))
        return True

    for mismatch in mismatches:
        _handle_mismatch(shared_state, mismatch, mode)

    return False
```
