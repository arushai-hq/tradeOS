# Async State Machine Patterns — TradeOS D2

## Library: python-statemachine

TradeOS uses `python-statemachine` (not `transitions`, not `pytransitions`).

```bash
pip install python-statemachine
```

```python
from statemachine import StateMachine, State
```

## Core Pattern: Non-blocking Callbacks

Transition callbacks (`on_enter_*`) run synchronously within python-statemachine. In an asyncio context, these callbacks MUST NOT block the event loop. Use these patterns:

### Pattern 1: Schedule async work with `asyncio.create_task()`

```python
import asyncio
import structlog
from statemachine import StateMachine, State

log = structlog.get_logger()


class OrderStateMachine(StateMachine):
    # States
    CREATED = State(initial=True)
    SUBMITTED = State()
    ACKNOWLEDGED = State()
    PARTIALLY_FILLED = State()
    FILLED = State(final=True)
    REJECTED = State(final=True)
    PENDING_CANCEL = State()
    CANCELLED = State(final=True)
    PENDING_UPDATE = State()
    EXPIRED = State(final=True)
    UNKNOWN = State(final=True)

    # Transitions (abbreviated — see state-definitions.md for full list)
    submit = CREATED.to(SUBMITTED)
    acknowledge = SUBMITTED.to(ACKNOWLEDGED)
    fill = ACKNOWLEDGED.to(FILLED) | PARTIALLY_FILLED.to(FILLED)
    partially_fill = ACKNOWLEDGED.to(PARTIALLY_FILLED)
    reject = SUBMITTED.to(REJECTED) | ACKNOWLEDGED.to(REJECTED)
    request_cancel = ACKNOWLEDGED.to(PENDING_CANCEL) | PARTIALLY_FILLED.to(PENDING_CANCEL)
    cancel = PENDING_CANCEL.to(CANCELLED)
    request_update = ACKNOWLEDGED.to(PENDING_UPDATE)
    reacknowledge = PENDING_UPDATE.to(ACKNOWLEDGED)
    expire = SUBMITTED.to(EXPIRED) | ACKNOWLEDGED.to(EXPIRED) | PARTIALLY_FILLED.to(EXPIRED)
    mark_unknown = CREATED.to(UNKNOWN)

    def __init__(self, order_id: str, symbol: str, strategy: str):
        super().__init__()
        self.order_id = order_id
        self.symbol = symbol
        self.strategy = strategy
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get running event loop for scheduling async tasks."""
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop

    def on_enter_FILLED(self):
        """Called synchronously by python-statemachine on FILLED entry."""
        log.info(
            "order_filled",
            order_id=self.order_id,
            symbol=self.symbol,
            strategy=self.strategy,
        )
        # Schedule async cleanup without blocking
        loop = self._get_loop()
        loop.call_soon_threadsafe(
            loop.create_task,
            self._async_on_filled(),
        )

    async def _async_on_filled(self):
        """Async actions after fill — release locks, update position tracking."""
        from risk_manager.position_tracker import release_instrument_lock
        await release_instrument_lock(self.symbol)
        log.info(
            "instrument_lock_released",
            order_id=self.order_id,
            symbol=self.symbol,
        )
```

### Pattern 2: Use `asyncio.to_thread()` for blocking callbacks

When the callback itself needs to call a blocking function (e.g., sending Telegram):

```python
    def on_enter_REJECTED(self):
        log.error(
            "order_rejected",
            order_id=self.order_id,
            symbol=self.symbol,
            strategy=self.strategy,
        )
        # Don't call send_telegram() directly — it may block
        # Schedule it as a coroutine instead
        loop = self._get_loop()
        loop.call_soon_threadsafe(
            loop.create_task,
            self._notify_rejection(),
        )

    async def _notify_rejection(self):
        from risk_manager.notifier import send_telegram
        await send_telegram(
            f"Order REJECTED: {self.order_id} for {self.symbol} "
            f"(strategy: {self.strategy})"
        )
```

### Pattern 3: Callbacks that only need synchronous work

For log-only callbacks, no async scheduling needed:

```python
    def on_enter_SUBMITTED(self):
        log.info(
            "order_submitted",
            order_id=self.order_id,
            symbol=self.symbol,
            strategy=self.strategy,
            from_state="CREATED",
            to_state="SUBMITTED",
        )

    def on_enter_UNKNOWN(self):
        log.critical(
            "order_marked_unknown",
            order_id=self.order_id,
            symbol=self.symbol,
        )
        # Instrument lock is added by the caller (startup reconciliation)
        # This callback only logs — no async needed
```

## Processing Zerodha Postbacks Asynchronously

Zerodha sends order updates via WebSocket postbacks. Process them without blocking:

```python
async def handle_order_postback(
    postback: dict,
    order_registry: dict,
    instrument_locks: set,
) -> None:
    """
    Called when Zerodha sends an order update via kws.on_order_update.
    Maps Zerodha status to TradeOS state transition.
    """
    order_id = postback.get("order_id")
    zerodha_status = postback.get("status", "")

    if order_id not in order_registry:
        log.warning(
            "postback_for_unknown_order",
            order_id=order_id,
            zerodha_status=zerodha_status,
        )
        return

    order_sm = order_registry[order_id]

    # Map Zerodha status → transition method
    # Blocking map_zerodha_status() returns the method name, not the result
    from execution_engine.order_registry import map_zerodha_status
    transition_name = map_zerodha_status(zerodha_status, order_sm)

    if transition_name:
        try:
            # Transitions are synchronous in python-statemachine
            # Run in thread to avoid blocking event loop if callbacks have I/O
            await asyncio.to_thread(getattr(order_sm, transition_name))
        except Exception as e:
            log.critical(
                "postback_transition_failed",
                order_id=order_id,
                zerodha_status=zerodha_status,
                transition=transition_name,
                error=str(e),
            )
```

## Order Monitor Task

The order monitor polls Zerodha every N seconds to sync order states:

```python
async def order_monitor_task(
    kill_switch,
    order_registry: dict,
    poll_interval: int = 5,
) -> None:
    """
    Async task — runs every poll_interval seconds during market hours.
    Syncs order states from Zerodha API.
    """
    import pytz
    from datetime import datetime

    IST = pytz.timezone("Asia/Kolkata")
    log.info("order_monitor_started", poll_interval=poll_interval)

    while not kill_switch.get("level3_active", False):
        try:
            # Fetch all orders from Zerodha (blocking call)
            zerodha_orders = await asyncio.to_thread(kite.orders)

            for z_order in zerodha_orders:
                order_id = z_order["order_id"]
                if order_id not in order_registry:
                    continue

                order_sm = order_registry[order_id]
                current_state = order_sm.current_state.id

                # Skip terminal states
                if current_state in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"}:
                    continue

                # Sync from Zerodha
                from execution_engine.order_registry import sync_order_from_zerodha
                await asyncio.to_thread(
                    sync_order_from_zerodha,
                    order_sm,
                    z_order,
                )

        except Exception as e:
            log.error("order_monitor_poll_failed", error=str(e))
            # Don't raise — let the monitor keep running

        await asyncio.sleep(poll_interval)

    log.info("order_monitor_stopped")
```

## InvalidStateTransition Handling in Async Context

```python
from statemachine.exceptions import InvalidDefinition
from execution_engine.order_registry import InvalidStateTransition


async def safe_transition_async(
    order_sm: "OrderStateMachine",
    transition_name: str,
    **kwargs,
) -> bool:
    """
    Attempt a transition. Returns True if successful, False if blocked.
    Never raises — always logs.
    """
    try:
        await asyncio.to_thread(
            getattr(order_sm, transition_name),
            **kwargs,
        )
        log.info(
            "order_transition_success",
            order_id=order_sm.order_id,
            symbol=order_sm.symbol,
            transition=transition_name,
            new_state=order_sm.current_state.id,
        )
        return True
    except (InvalidStateTransition, Exception) as e:
        log.critical(
            "order_transition_failed",
            order_id=order_sm.order_id,
            symbol=order_sm.symbol,
            transition=transition_name,
            current_state=order_sm.current_state.id,
            error=str(e),
        )
        return False
```

## Testing Async Callbacks

```python
import pytest
import asyncio
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_filled_callback_releases_lock():
    """on_enter_FILLED must schedule lock release without blocking."""
    with patch(
        "risk_manager.position_tracker.release_instrument_lock",
        new_callable=AsyncMock,
    ) as mock_release:
        from execution_engine.order_registry import OrderStateMachine

        sm = OrderStateMachine(order_id="ORD001", symbol="RELIANCE", strategy="s1")
        sm.submit()
        sm.acknowledge()

        # fill() triggers on_enter_FILLED
        sm.fill()

        # Allow scheduled tasks to run
        await asyncio.sleep(0)

        mock_release.assert_called_once_with("RELIANCE")


@pytest.mark.asyncio
async def test_postback_handler_routes_complete_to_fill():
    """Zerodha COMPLETE postback must trigger fill() transition."""
    registry = {}
    sm = OrderStateMachine(order_id="ORD001", symbol="INFY", strategy="s1")
    sm.submit()
    sm.acknowledge()
    registry["ORD001"] = sm

    postback = {"order_id": "ORD001", "status": "COMPLETE"}
    await handle_order_postback(postback, registry, set())

    assert registry["ORD001"].current_state.id == "FILLED"
```

## Key Rules for Async + python-statemachine

1. **Never `await` inside `on_enter_*` callbacks** — they are synchronous
2. **Use `loop.call_soon_threadsafe(loop.create_task, coro)` to schedule async work from callbacks**
3. **Use `asyncio.to_thread()` for any blocking Zerodha API call** (kite.orders, kite.positions)
4. **The state machine itself is not thread-safe** — access `order_sm` only from the asyncio event loop thread
5. **Each `OrderStateMachine` instance is single-owner** — never share across threads without a lock
