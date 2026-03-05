# Restart Safety Protocol — TradeOS D2

## The Rule

`system_ready = False` until Zerodha reconciliation passes with **zero UNKNOWN orders**.
No new order may be placed — at any layer — until `system_ready = True`.

This is called at every system startup, before any async tasks generate signals.

## Startup Sequence

```
1. Set system_ready = False
2. Call kite.orders()  → fetch all open orders from Zerodha
3. Call kite.positions() → fetch all open positions from Zerodha
4. Reconcile each Zerodha order against local order_registry
5. For each order in Zerodha NOT in local state → create UNKNOWN entry + lock instrument
6. For each order in local state NOT in Zerodha → mark as EXPIRED (order disappeared)
7. If UNKNOWN count == 0 → set system_ready = True, allow order placement
8. If UNKNOWN count > 0 → remain system_ready = False, Telegram alert, await manual resolution
```

## Implementation

```python
import asyncio
import structlog
from datetime import datetime
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Module-level flag — checked by all order placement paths
system_ready: bool = False


async def run_startup_reconciliation(
    kite,
    order_registry: dict,
    instrument_locks: set,
) -> bool:
    """
    Full startup reconciliation sequence.
    Returns True if system_ready, False if UNKNOWN orders exist.
    Must be awaited before any other async task starts.
    """
    global system_ready
    system_ready = False

    log.info("startup_reconciliation_starting")

    # Step 1: Fetch from Zerodha
    try:
        zerodha_orders = await asyncio.to_thread(kite.orders)
        zerodha_positions = await asyncio.to_thread(kite.positions)
    except Exception as e:
        log.critical("startup_reconciliation_fetch_failed", error=str(e))
        raise

    # Step 2: Build Zerodha order_id set
    zerodha_order_ids = {o["order_id"] for o in zerodha_orders}
    local_order_ids = set(order_registry.keys())

    unknown_count = 0

    # Step 3: Orders on Zerodha but NOT in local state → UNKNOWN
    for order in zerodha_orders:
        order_id = order["order_id"]
        symbol = order.get("tradingsymbol", "UNKNOWN")
        status = order.get("status", "")

        # Only care about non-terminal orders
        if status in ("COMPLETE", "CANCELLED", "REJECTED"):
            continue

        if order_id not in order_registry:
            log.critical(
                "unknown_order_found_on_restart",
                order_id=order_id,
                symbol=symbol,
                zerodha_status=status,
            )
            # Create UNKNOWN state machine entry
            from execution_engine.order_registry import OrderStateMachine
            unknown_sm = OrderStateMachine(
                order_id=order_id,
                symbol=symbol,
                strategy="UNKNOWN",
            )
            unknown_sm.mark_unknown()  # Triggers on_enter_UNKNOWN → lock + alert
            order_registry[order_id] = unknown_sm
            instrument_locks.add(symbol)
            unknown_count += 1

    # Step 4: Orders in local state NOT on Zerodha → mark EXPIRED
    for order_id, order_sm in list(order_registry.items()):
        if order_id not in zerodha_order_ids:
            current = order_sm.current_state.id
            if current not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"}:
                log.warning(
                    "local_order_missing_from_zerodha",
                    order_id=order_id,
                    symbol=order_sm.symbol,
                    current_state=current,
                )
                order_sm.expire()

    if unknown_count == 0:
        system_ready = True
        log.info("startup_reconciliation_passed",
                 system_ready=True,
                 orders_reconciled=len(zerodha_order_ids))
    else:
        log.critical(
            "startup_reconciliation_failed",
            system_ready=False,
            unknown_orders=unknown_count,
        )
        from risk_manager.notifier import send_telegram
        await send_telegram(
            f"STARTUP BLOCKED: {unknown_count} UNKNOWN order(s) found. "
            f"Manual resolution required before trading resumes."
        )

    return system_ready


def assert_system_ready() -> None:
    """
    Gate check — call at the top of every order placement path.
    Raises RuntimeError if reconciliation has not passed.
    """
    if not system_ready:
        raise RuntimeError(
            "Order placement blocked: startup reconciliation not complete. "
            "Resolve UNKNOWN orders before trading."
        )
```

## Integration Point

```python
# In main.py — called BEFORE any tasks start
async def main():
    global order_registry, instrument_locks

    success = await run_startup_reconciliation(
        kite=kite,
        order_registry=order_registry,
        instrument_locks=instrument_locks,
    )

    if not success:
        log.critical("system_startup_aborted_unknown_orders")
        return  # Do not start trading tasks

    # Only start tasks after reconciliation passes
    await asyncio.gather(
        websocket_listener_task(kill_switch),
        signal_processor_task(kill_switch),
        order_monitor_task(kill_switch, order_registry),
        risk_watchdog_task(kill_switch),
        heartbeat_task(kill_switch),
    )
```

## Manual Resolution Flow

When `system_ready = False` due to UNKNOWN orders:

1. Operator reviews UNKNOWN orders in Zerodha dashboard
2. For each UNKNOWN order:
   - If actually filled → manually create FILLED entry in `order_registry`
   - If actually cancelled → remove from `order_registry`, release instrument lock
3. Call `reset_unknown_and_retry()` which re-runs reconciliation
4. Only after `system_ready = True` does the system allow new orders
