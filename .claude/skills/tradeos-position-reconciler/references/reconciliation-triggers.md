# Reconciliation Triggers

Four events trigger a position reconciliation in TradeOS. Each has different urgency and handling.

## Trigger 1: Startup Reconciliation

**When**: Before `asyncio.create_task()` is called for any trading task.
**Purpose**: Ensure local state matches broker before any signals can be generated.

```python
async def reconcile_on_startup(shared_state: dict, kite) -> None:
    """
    Blocking startup check. Raises ReconciliationMismatchError if mismatches found.
    Called with 'await' — startup halts until this returns cleanly.
    """
    log.info("reconciliation_start", trigger="startup")
    is_clean = await reconcile_positions(shared_state, kite, mode="startup")
    if not is_clean:
        log.critical("reconciliation_mismatch_on_startup",
                     reason="Position mismatch found. Aborting startup.")
        raise ReconciliationMismatchError("Cannot start trading with unverified positions")
    log.info("reconciliation_clean", trigger="startup")
```

**On mismatch**: Raises `ReconciliationMismatchError`. The `main()` coroutine must catch this and exit cleanly — no tasks are started.

**Performance budget**: The startup reconciliation uses `asyncio.to_thread(kite.positions)` — the blocking HTTP call is offloaded to a thread. Startup typically completes within 1–2 seconds.

---

## Trigger 2: Scheduled Reconciliation (Every 30 Minutes)

**When**: Periodic timer, background task.
**Purpose**: Catch drift between broker and local state during normal operation.

```python
async def scheduled_reconciler(shared_state: dict, kite) -> None:
    """Runs continuously in the background, reconciling every 30 minutes."""
    while True:
        await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)  # 1800
        try:
            await reconcile_positions(shared_state, kite, mode="scheduled")
        except Exception as e:
            log.error("scheduled_reconciliation_failed", error=str(e))
            # DO NOT crash — just log and wait for next cycle
```

**Constants**:
```python
RECONCILIATION_INTERVAL_SECONDS = 1800  # 30 minutes — never reduce below 5 minutes
```

**On mismatch**: Locks the affected instrument(s). Does NOT halt trading for other instruments.

---

## Trigger 3: Post-Disruption Reconciliation

**When**: After a WebSocket reconnect or API error recovery.
**Purpose**: Ticks may have been missed during the outage — positions must be re-verified.

```python
async def post_disruption_reconcile(shared_state: dict, kite) -> None:
    """
    Called by ws_listener after reconnect, or by order_monitor after API recovery.
    Pauses signal_processor during the reconciliation window.
    """
    shared_state["recon_in_progress"] = True
    log.warning("post_disruption_reconciliation_start")

    try:
        await reconcile_positions(shared_state, kite, mode="post_disruption")
    finally:
        shared_state["recon_in_progress"] = False
        log.info("post_disruption_reconciliation_complete")
```

**signal_processor gate**: The signal_processor must check `shared_state["recon_in_progress"]` and skip processing ticks while True:

```python
async def signal_processor_fn(shared_state, tick_queue, order_queue):
    while True:
        tick = await tick_queue.get()
        if shared_state.get("recon_in_progress", False):
            log.debug("signal_skipped_during_recon")
            continue  # discard tick, do not generate signal
        # ... normal signal processing
```

---

## Trigger 4: Manual Reconciliation (Future / Phase 2)

**When**: Operator sends command via Telegram bot or admin API.
**Purpose**: Human-initiated check after manual broker intervention.

Not implemented in Phase 1. Placeholder:

```python
# In operator command handler:
async def handle_recon_command(shared_state, kite):
    await reconcile_positions(shared_state, kite, mode="manual")
```

---

## Shared State Keys Used by Reconciler

```python
shared_state["recon_in_progress"]  = False   # bool — signal_processor gate
shared_state["position_state"]     = {}      # dict[token, PositionRecord]
shared_state["locked_instruments"] = set()   # set[instrument_token] — see instrument-lock.md
```
