# Order State Definitions — TradeOS D2

## All 8 States

| State | Meaning | Terminal? |
|-------|---------|-----------|
| CREATED | Order object built locally, not yet sent to broker | No |
| SUBMITTED | Sent to Zerodha API, awaiting acknowledgement | No |
| ACKNOWLEDGED | Zerodha accepted the order, in queue | No |
| PARTIALLY_FILLED | Some qty filled, remainder still open | No |
| FILLED | Full qty filled — position is open | Yes |
| REJECTED | Zerodha rejected the order | Yes |
| PENDING_CANCEL | Cancel request sent, awaiting confirmation | No |
| CANCELLED | Order cancelled — no fill | Yes |
| PENDING_UPDATE | Modify request sent, awaiting confirmation | No |
| EXPIRED | Order expired (end of day, validity elapsed) | Yes |
| UNKNOWN | Found on Zerodha at restart but not in local state | Yes* |

*UNKNOWN is terminal in the sense that no automated transitions are allowed — only manual resolution.

## Valid Transition Table

```python
VALID_TRANSITIONS = {
    "CREATED":          {"SUBMITTED"},
    "SUBMITTED":        {"ACKNOWLEDGED", "REJECTED"},
    "ACKNOWLEDGED":     {"PARTIALLY_FILLED", "FILLED", "REJECTED",
                         "PENDING_CANCEL", "PENDING_UPDATE", "EXPIRED"},
    "PARTIALLY_FILLED": {"FILLED", "PENDING_CANCEL", "CANCELLED"},
    "PENDING_CANCEL":   {"CANCELLED"},
    "PENDING_UPDATE":   {"ACKNOWLEDGED"},
    "FILLED":           set(),   # terminal
    "CANCELLED":        set(),   # terminal
    "REJECTED":         set(),   # terminal
    "EXPIRED":          set(),   # terminal
    "UNKNOWN":          set(),   # terminal — manual resolution only
}
```

## OrderStateMachine Implementation

```python
from python_statemachine import StateMachine, State
import structlog
from datetime import datetime
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


class InvalidStateTransition(Exception):
    """Raised when an illegal state transition is attempted."""
    pass


class OrderStateMachine(StateMachine):
    """TradeOS D2 order state machine. One instance per order_id."""

    # States
    CREATED         = State(initial=True)
    SUBMITTED       = State()
    ACKNOWLEDGED    = State()
    PARTIALLY_FILLED = State()
    FILLED          = State(final=True)
    REJECTED        = State(final=True)
    PENDING_CANCEL  = State()
    CANCELLED       = State(final=True)
    PENDING_UPDATE  = State()
    EXPIRED         = State(final=True)
    UNKNOWN         = State(final=True)

    # Transitions (named after trigger events)
    submit          = CREATED         >> SUBMITTED
    acknowledge     = SUBMITTED       >> ACKNOWLEDGED
    partial_fill    = ACKNOWLEDGED    >> PARTIALLY_FILLED
    fill            = (ACKNOWLEDGED | PARTIALLY_FILLED) >> FILLED
    reject          = (SUBMITTED | ACKNOWLEDGED) >> REJECTED
    request_cancel  = (ACKNOWLEDGED | PARTIALLY_FILLED) >> PENDING_CANCEL
    confirm_cancel  = PENDING_CANCEL  >> CANCELLED
    request_update  = ACKNOWLEDGED    >> PENDING_UPDATE
    confirm_update  = PENDING_UPDATE  >> ACKNOWLEDGED
    expire          = ACKNOWLEDGED    >> EXPIRED
    mark_unknown    = CREATED         >> UNKNOWN  # restart artifact

    def __init__(self, order_id: str, symbol: str, strategy: str):
        self.order_id = order_id
        self.symbol = symbol
        self.strategy = strategy
        self._prev_state: str = "CREATED"
        super().__init__()

    def on_enter_state(self, event: str, state: State) -> None:
        """Log every state transition via structlog."""
        log.info(
            "order_state_transition",
            order_id=self.order_id,
            symbol=self.symbol,
            strategy=self.strategy,
            from_state=self._prev_state,
            to_state=state.id,
            event=event,
            timestamp=datetime.now(tz=IST).isoformat(),
        )
        self._prev_state = state.id

    def on_enter_FILLED(self) -> None:
        """Trigger P&L calculation on fill."""
        log.info("order_filled_pnl_trigger",
                 order_id=self.order_id, symbol=self.symbol)
        # Import here to avoid circular imports
        from risk_manager.pnl_tracker import calculate_pnl
        calculate_pnl(self.order_id)

    def on_enter_REJECTED(self) -> None:
        """Check kill switch rejection threshold on every rejection."""
        log.warning("order_rejected",
                    order_id=self.order_id, symbol=self.symbol)
        from risk_manager.kill_switch import kill_switch
        kill_switch.check_rejection_threshold(self.symbol)

    def on_enter_CANCELLED(self) -> None:
        """Release instrument lock when order is cancelled."""
        log.info("order_cancelled_lock_released",
                 order_id=self.order_id, symbol=self.symbol)
        from execution_engine.instrument_lock import release_lock
        release_lock(self.symbol)

    def on_enter_UNKNOWN(self) -> None:
        """Lock instrument and alert on unknown order at restart."""
        log.critical("unknown_order_restart",
                     order_id=self.order_id, symbol=self.symbol)
        from execution_engine.instrument_lock import lock_instrument
        from risk_manager.notifier import send_telegram
        lock_instrument(self.symbol, reason=f"unknown_order_{self.order_id}")
        # Telegram alert is fire-and-forget — do not await in sync context
        import asyncio
        asyncio.create_task(
            send_telegram(f"UNKNOWN order found: {self.order_id} {self.symbol}")
        )
```

## Custom Transition Guard

```python
def safe_transition(order: OrderStateMachine, trigger_name: str, **kwargs) -> None:
    """
    Execute a transition with full validation and logging.
    Raises InvalidStateTransition (never silently ignores).
    """
    current = order.current_state.id
    trigger = getattr(order, trigger_name, None)

    if trigger is None:
        raise InvalidStateTransition(
            f"Unknown trigger '{trigger_name}' for order {order.order_id}"
        )

    try:
        trigger(**kwargs)
    except Exception as e:
        log.critical(
            "invalid_state_transition",
            order_id=order.order_id,
            symbol=order.symbol,
            current_state=current,
            attempted_trigger=trigger_name,
            error=str(e),
        )
        raise InvalidStateTransition(
            f"Cannot trigger '{trigger_name}' from state '{current}' "
            f"for order {order.order_id}"
        ) from e
```
