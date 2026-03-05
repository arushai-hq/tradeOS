# Instrument Lock Mechanism

When a position mismatch is detected, only the affected instrument is locked. All other instruments continue trading normally.

## The Lock — Data Structure

The lock lives in `shared_state["locked_instruments"]`, a Python `set` of `instrument_token` integers:

```python
# In _init_shared_state():
shared_state["locked_instruments"] = set()  # set[int] — instrument tokens
```

Using a `set` gives O(1) lookup. Every order gate checks `token in shared_state["locked_instruments"]`.

## How to Lock an Instrument

```python
def lock_instrument(shared_state: dict, token: int, symbol: str, reason: str) -> None:
    shared_state["locked_instruments"].add(token)
    log.warning(
        "instrument_locked",
        instrument_token=token,
        tradingsymbol=symbol,
        reason=reason,
    )
```

Called by:
- `_handle_mismatch_manual()` — on any mismatch
- `_handle_ghost_position()` — on ghost position detection

## The Order Gate

**signal_processor** must check the lock before pushing any signal to order_queue:

```python
async def signal_processor_fn(shared_state, tick_queue, order_queue):
    while True:
        tick = await tick_queue.get()

        # Gate: recon in progress
        if shared_state.get("recon_in_progress", False):
            continue

        # Gate: instrument locked
        token = tick.get("instrument_token")
        if token in shared_state["locked_instruments"]:
            log.debug("signal_rejected_instrument_locked", instrument_token=token)
            continue

        # ... generate signal, push to order_queue
```

**execution_engine** / **order_monitor** must also check the lock before placing orders:

```python
if order.instrument_token in shared_state["locked_instruments"]:
    log.warning("order_rejected_instrument_locked",
                order_id=order.order_id,
                instrument_token=order.instrument_token)
    return  # do not place this order
```

## Lock Persistence Across Reconnects

The lock set lives in `shared_state` which is initialised once in `main()` and never reset during reconnects or task restarts. A WebSocket reconnect does NOT clear locks.

**This is intentional.** A mismatch detected before a reconnect is still unresolved after reconnect — the lock must remain until a fresh reconciliation confirms clean state.

The only ways to unlock an instrument:
1. **Successful reconciliation** clears the lock if positions now match:
   ```python
   def _clear_lock_if_resolved(shared_state: dict, token: int) -> None:
       if token in shared_state["locked_instruments"]:
           shared_state["locked_instruments"].discard(token)
           log.info("instrument_unlocked", instrument_token=token,
                    reason="reconciliation_clean")
   ```

2. **Manual operator command** (Phase 2):
   ```python
   # In operator command handler:
   shared_state["locked_instruments"].discard(token)
   ```

## Heartbeat Monitoring

The heartbeat task logs the current locked instruments count every 30 seconds:

```python
log.info(
    "system_heartbeat",
    locked_instruments=list(shared_state["locked_instruments"]),
    locked_count=len(shared_state["locked_instruments"]),
    # ... other fields
)
```

If `locked_count > 0`, the heartbeat sends a Telegram WARNING so the operator knows instruments are being held out of trading.

## What Lock Does NOT Do

- Does NOT cancel existing open orders for the locked instrument (that's kill switch Level 2 work)
- Does NOT close existing positions (that's the ghost close protocol — manual only)
- Does NOT stop other instruments from trading
- Does NOT affect the kill switch state
