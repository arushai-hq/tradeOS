# Duplicate Order Prevention — TradeOS D2

## The Rule

Before calling `kite.place_order()`, check if an ACTIVE order already exists for the same `tradingsymbol`. If one exists, raise `DuplicateOrderError` — do NOT place the second order silently.

"Active" means any order whose state is NOT in `TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"}`.

## Implementation

```python
class DuplicateOrderError(Exception):
    """Raised when attempting to place an order for a symbol with an active order."""
    pass


# TERMINAL_STATES — orders in these states do NOT block new orders
TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"}


def get_active_order_for_symbol(
    symbol: str,
    order_registry: dict,
) -> "OrderStateMachine | None":
    """
    Returns the active OrderStateMachine for a symbol, or None.
    An order is active if its current state is NOT in TERMINAL_STATES.
    """
    for order_sm in order_registry.values():
        if order_sm.symbol == symbol:
            current = order_sm.current_state.id
            if current not in TERMINAL_STATES:
                return order_sm
    return None


def check_no_duplicate_order(
    symbol: str,
    order_registry: dict,
) -> None:
    """
    Gate check — call BEFORE kite.place_order().
    Raises DuplicateOrderError if an active order exists for this symbol.
    """
    existing = get_active_order_for_symbol(symbol, order_registry)
    if existing is not None:
        raise DuplicateOrderError(
            f"Active order {existing.order_id} already exists for {symbol} "
            f"in state {existing.current_state.id}. "
            f"Cannot place a second order until the first is terminal."
        )
```

## Integration in Order Placement

```python
import structlog
from execution_engine.order_registry import (
    OrderStateMachine,
    check_no_duplicate_order,
    DuplicateOrderError,
)
from risk_manager.kill_switch import assert_system_ready

log = structlog.get_logger()


async def place_order(
    kite,
    symbol: str,
    qty: int,
    transaction_type: str,
    strategy: str,
    order_registry: dict,
) -> str:
    """
    Full pre-order gate sequence:
    1. assert_system_ready()         — startup reconciliation passed
    2. check_no_duplicate_order()    — no active order for this symbol
    3. Create OrderStateMachine      — register CREATED state
    4. kite.place_order()            — submit to Zerodha
    5. transition to SUBMITTED       — after successful API call
    """
    # Gate 1: Startup reconciliation
    assert_system_ready()

    # Gate 2: No duplicate
    try:
        check_no_duplicate_order(symbol, order_registry)
    except DuplicateOrderError as e:
        log.warning(
            "duplicate_order_blocked",
            symbol=symbol,
            strategy=strategy,
            reason=str(e),
        )
        raise

    # Create state machine entry in CREATED state
    order_sm = OrderStateMachine(
        order_id=None,  # Not yet assigned by broker
        symbol=symbol,
        strategy=strategy,
    )
    # Note: register after getting order_id from Zerodha

    # Place order (may block — use asyncio.to_thread)
    try:
        order_id = await asyncio.to_thread(
            kite.place_order,
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=qty,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
        )
    except Exception as e:
        log.critical(
            "order_placement_failed",
            symbol=symbol,
            strategy=strategy,
            error=str(e),
        )
        raise

    # Assign order_id and register
    order_sm.order_id = order_id
    order_registry[order_id] = order_sm

    # Transition to SUBMITTED
    order_sm.submit()

    log.info(
        "order_placed",
        order_id=order_id,
        symbol=symbol,
        strategy=strategy,
        qty=qty,
        transaction_type=transaction_type,
    )

    return order_id
```

## Instrument Lock Relationship

Duplicate prevention and instrument locks are complementary:

```python
# instrument_locks: set[str] — symbols currently locked
#
# LOCK is acquired when:
#   - An order transitions to SUBMITTED (via on_enter_SUBMITTED callback)
#   - An UNKNOWN order is discovered on restart
#
# LOCK is released when:
#   - An order transitions to FILLED (fully)
#   - An order is CANCELLED or REJECTED
#   - An EXPIRED order is cleaned up
#
# check_no_duplicate_order() is the APPLICATION-LEVEL guard
# instrument_locks is the SYSTEM-LEVEL guard (also prevents race conditions)

def check_instrument_not_locked(
    symbol: str,
    instrument_locks: set,
) -> None:
    if symbol in instrument_locks:
        raise DuplicateOrderError(
            f"{symbol} is locked — active order in progress. "
            f"Wait for the current order to reach a terminal state."
        )
```

## Edge Cases

### PARTIALLY_FILLED — does NOT release lock

```python
# In OrderStateMachine callbacks:
def on_enter_PARTIALLY_FILLED(self):
    # DO NOT release instrument lock
    # DO NOT allow new orders for this symbol
    # Log the partial fill qty for position tracking
    log.info(
        "order_partially_filled",
        order_id=self.order_id,
        symbol=self.symbol,
        filled_qty=self.filled_qty,
        remaining_qty=self.remaining_qty,
    )
    # Instrument lock REMAINS — duplicate check still blocks new orders
```

### PENDING_CANCEL — treat as active

```python
# A PENDING_CANCEL order is still live — Zerodha has not confirmed cancellation.
# The duplicate check MUST block new orders while an order is PENDING_CANCEL.
#
# This prevents the race condition:
#   1. Cancel requested (state = PENDING_CANCEL)
#   2. New order placed for same symbol
#   3. Zerodha executes BOTH orders because cancel was slow
#
# PENDING_CANCEL is NOT in TERMINAL_STATES — it is caught by check_no_duplicate_order()
```

### Race condition in async context

```python
# Multiple async tasks may attempt to place orders concurrently.
# Use asyncio.Lock per symbol to serialize access:

symbol_locks: dict[str, asyncio.Lock] = {}

async def get_symbol_lock(symbol: str) -> asyncio.Lock:
    if symbol not in symbol_locks:
        symbol_locks[symbol] = asyncio.Lock()
    return symbol_locks[symbol]


async def place_order_safe(symbol: str, ...):
    lock = await get_symbol_lock(symbol)
    async with lock:
        check_no_duplicate_order(symbol, order_registry)
        # ... rest of placement ...
```

## Testing Duplicate Prevention

```python
import pytest
from execution_engine.order_registry import (
    OrderStateMachine, check_no_duplicate_order, DuplicateOrderError
)


def test_duplicate_blocked_when_active_order_exists():
    """Active order for RELIANCE must block a second order."""
    registry = {}
    sm = OrderStateMachine(order_id="ORD001", symbol="RELIANCE", strategy="s1")
    sm.submit()  # SUBMITTED is active
    registry["ORD001"] = sm

    with pytest.raises(DuplicateOrderError) as exc_info:
        check_no_duplicate_order("RELIANCE", registry)
    assert "ORD001" in str(exc_info.value)


def test_duplicate_allowed_after_fill():
    """After FILLED, a new order for the same symbol is allowed."""
    registry = {}
    sm = OrderStateMachine(order_id="ORD001", symbol="RELIANCE", strategy="s1")
    sm.submit()
    sm.acknowledge()
    sm.fill()  # FILLED is terminal
    registry["ORD001"] = sm

    # Should not raise
    check_no_duplicate_order("RELIANCE", registry)


def test_partially_filled_blocks_duplicate():
    """PARTIALLY_FILLED is NOT terminal — duplicate must be blocked."""
    registry = {}
    sm = OrderStateMachine(order_id="ORD001", symbol="INFY", strategy="s1")
    sm.submit()
    sm.acknowledge()
    sm.partially_fill()  # Still active
    registry["ORD001"] = sm

    with pytest.raises(DuplicateOrderError):
        check_no_duplicate_order("INFY", registry)


def test_pending_cancel_blocks_duplicate():
    """PENDING_CANCEL is NOT terminal — duplicate must be blocked."""
    registry = {}
    sm = OrderStateMachine(order_id="ORD001", symbol="TCS", strategy="s1")
    sm.submit()
    sm.acknowledge()
    sm.request_cancel()  # PENDING_CANCEL
    registry["ORD001"] = sm

    with pytest.raises(DuplicateOrderError):
        check_no_duplicate_order("TCS", registry)
```
