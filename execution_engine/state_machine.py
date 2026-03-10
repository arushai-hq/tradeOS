"""
TradeOS — Order State Machine (D2)

Registry-style state machine that manages the full lifecycle of every order.
One OrderStateMachine instance is shared across the entire ExecutionEngine.

8 valid states:
    CREATED, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED,
    FILLED, REJECTED, CANCELLED, EXPIRED

UNKNOWN is a special restart-artifact state set only via mark_unknown().
It is not reachable via transition() — it bypasses normal validation.

D2 Rules enforced here:
  - Invalid transitions raise InvalidStateTransition (logged CRITICAL)
  - Duplicate ENTRY orders for same symbol raise DuplicateOrderError
  - PARTIALLY_FILLED is NOT terminal — instrument lock not released
  - All transitions logged with from_state/to_state via structlog
  - mark_unknown() locks instrument in shared_state["locked_instruments"]
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

class OrderState(str, Enum):
    CREATED          = "CREATED"
    SUBMITTED        = "SUBMITTED"
    ACKNOWLEDGED     = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED           = "FILLED"
    REJECTED         = "REJECTED"
    CANCELLED        = "CANCELLED"
    EXPIRED          = "EXPIRED"
    UNKNOWN          = "UNKNOWN"   # restart artifact — not a normal transition target


# Terminal states: no further transitions allowed
TERMINAL_STATES: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.REJECTED,
    OrderState.CANCELLED,
    OrderState.EXPIRED,
    OrderState.UNKNOWN,
})

# Valid transitions table (D2 contract)
VALID_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({
        OrderState.SUBMITTED,
        OrderState.CANCELLED,       # hard_exit / manual cancellation
    }),
    OrderState.SUBMITTED: frozenset({
        OrderState.ACKNOWLEDGED,
        OrderState.REJECTED,
        OrderState.CANCELLED,       # hard_exit / manual cancellation
    }),
    OrderState.ACKNOWLEDGED: frozenset({
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }),
    OrderState.PARTIALLY_FILLED: frozenset({
        OrderState.FILLED,
        OrderState.CANCELLED,
    }),
    # Terminal states — no valid outgoing transitions
    OrderState.FILLED:    frozenset(),
    OrderState.REJECTED:  frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED:   frozenset(),
    OrderState.UNKNOWN:   frozenset(),
}

# Zerodha API status → TradeOS OrderState
# "do not transition" for unknown status → map_zerodha_status returns None
ZERODHA_STATUS_MAP: dict[str, OrderState] = {
    "OPEN":           OrderState.ACKNOWLEDGED,
    "COMPLETE":       OrderState.FILLED,
    "CANCELLED":      OrderState.CANCELLED,
    "REJECTED":       OrderState.REJECTED,
    "UPDATE":         OrderState.ACKNOWLEDGED,  # order modified
    "TRIGGER PENDING": OrderState.ACKNOWLEDGED,
    "MODIFY PENDING": OrderState.ACKNOWLEDGED,
    "CANCEL PENDING": OrderState.ACKNOWLEDGED,
}


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class InvalidStateTransition(Exception):
    """Raised when an illegal order state transition is attempted."""


class DuplicateOrderError(Exception):
    """Raised when attempting to place an ENTRY order for a symbol with an active order."""


# ---------------------------------------------------------------------------
# Zerodha status mapper
# ---------------------------------------------------------------------------

def map_zerodha_status(
    zerodha_status: str,
    order_id: str = "",
    symbol: str = "",
) -> Optional[OrderState]:
    """
    Map a Zerodha order status string to a TradeOS OrderState.

    Returns None for unknown statuses (log WARNING, do not transition per D2 spec).

    Args:
        zerodha_status: Status string from kite.orders() response.
        order_id:       Order ID for log context.
        symbol:         Symbol for log context.

    Returns:
        OrderState if known, None if unknown.
    """
    state = ZERODHA_STATUS_MAP.get(zerodha_status.upper().strip())
    if state is None:
        log.warning(
            "unknown_zerodha_status_no_transition",
            zerodha_status=zerodha_status,
            order_id=order_id,
            symbol=symbol,
        )
    return state


# ---------------------------------------------------------------------------
# Order dataclass
# ---------------------------------------------------------------------------

@dataclass
class Order:
    """
    Represents a single order in TradeOS.

    Immutable fields set at creation: order_id, symbol, instrument_token,
    direction, order_type, qty, price, signal_id, placed_at.

    Mutable fields updated on state transitions: state, filled_at,
    fill_price, reject_reason.

    exit_type is set on EXIT orders: 'TARGET', 'STOP', 'HARD_EXIT', 'KILL_SWITCH'.
    stop_loss and target are copied from the originating Signal for ENTRY orders.
    """
    order_id: str
    symbol: str
    instrument_token: int
    direction: str              # 'LONG' or 'SHORT'
    order_type: str             # 'ENTRY' or 'EXIT'
    qty: int
    price: Decimal              # theoretical price at placement
    state: OrderState
    signal_id: int              # FK to signals table (0 = no signal)
    placed_at: datetime

    # Set on state transitions
    filled_at: Optional[datetime] = None
    fill_price: Optional[Decimal] = None
    reject_reason: Optional[str] = None

    # EXIT order metadata
    exit_type: Optional[str] = None    # 'TARGET', 'STOP', 'HARD_EXIT', 'KILL_SWITCH'

    # Copied from Signal for ENTRY orders — needed by OrderMonitor for register_position
    stop_loss: Optional[Decimal] = None
    target: Optional[Decimal] = None


# ---------------------------------------------------------------------------
# OrderStateMachine registry
# ---------------------------------------------------------------------------

class OrderStateMachine:
    """
    Registry-style D2 state machine. Manages the lifecycle of all orders.

    One instance per ExecutionEngine, shared across all components.

    Thread/task safety: Single-writer pattern per D6. Only OrderMonitor
    and OrderPlacer write state transitions. Reads are lock-free.

    Args:
        shared_state: D6 shared state dict. Used to:
          - Increment consecutive_losses on ENTRY order rejection
          - Lock instruments via locked_instruments on mark_unknown()
    """

    def __init__(self, shared_state: Optional[dict] = None) -> None:
        self._orders: dict[str, Order] = {}
        self._shared_state: dict = shared_state if shared_state is not None else {}

    # ------------------------------------------------------------------
    # Order creation
    # ------------------------------------------------------------------

    def create_order(
        self,
        order_id: str,
        symbol: str,
        instrument_token: int,
        direction: str,
        order_type: str,
        qty: int,
        price: Decimal,
        signal_id: int = 0,
        stop_loss: Optional[Decimal] = None,
        target: Optional[Decimal] = None,
    ) -> Order:
        """
        Create a new order in CREATED state and register it.

        For ENTRY orders: raises DuplicateOrderError if an active ENTRY
        order already exists for the symbol (D2 duplicate prevention).

        Args:
            order_id:         Broker order ID (or paper simulation ID).
            symbol:           Trading symbol.
            instrument_token: Zerodha instrument token.
            direction:        'LONG' or 'SHORT'.
            order_type:       'ENTRY' or 'EXIT'.
            qty:              Order quantity.
            price:            Theoretical price at placement.
            signal_id:        FK to signals table (0 = no DB signal).
            stop_loss:        Stop loss from Signal (ENTRY only).
            target:           Target from Signal (ENTRY only).

        Returns:
            New Order in CREATED state.

        Raises:
            DuplicateOrderError: Active ENTRY order exists for symbol.
        """
        if order_type == "ENTRY":
            existing = self._get_active_entry_for_symbol(symbol)
            if existing:
                log.warning(
                    "duplicate_order_blocked",
                    symbol=symbol,
                    existing_order_id=existing.order_id,
                    existing_state=existing.state.value,
                )
                raise DuplicateOrderError(
                    f"Active order {existing.order_id} already exists for {symbol} "
                    f"in state {existing.state.value}. "
                    f"Cannot place second order until first is terminal."
                )

        order = Order(
            order_id=order_id,
            symbol=symbol,
            instrument_token=instrument_token,
            direction=direction,
            order_type=order_type,
            qty=qty,
            price=price,
            state=OrderState.CREATED,
            signal_id=signal_id,
            placed_at=datetime.now(IST),
            stop_loss=stop_loss,
            target=target,
        )
        self._orders[order_id] = order

        log.info(
            "order_created",
            order_id=order_id,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            qty=qty,
            price=float(price),
        )
        return order

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def transition(
        self,
        order_id: str,
        new_state: OrderState,
        fill_price: Optional[Decimal] = None,
        reject_reason: Optional[str] = None,
    ) -> Order:
        """
        Transition an order to a new state.

        All transitions are logged. Invalid transitions raise
        InvalidStateTransition (logged CRITICAL) per D2 rule.

        On FILLED: records filled_at + fill_price.
        On REJECTED (ENTRY): increments shared_state["consecutive_losses"].

        Args:
            order_id:      Order to transition.
            new_state:     Target state.
            fill_price:    Fill price (required when transitioning to FILLED).
            reject_reason: Rejection reason (for REJECTED transitions).

        Returns:
            Updated Order.

        Raises:
            KeyError:                Order not found.
            InvalidStateTransition:  Transition not in VALID_TRANSITIONS.
        """
        order = self._get_or_raise(order_id)
        current = order.state

        if new_state not in VALID_TRANSITIONS[current]:
            msg = (
                f"Invalid transition {current.value} → {new_state.value} "
                f"for order {order_id} ({order.symbol})"
            )
            log.critical(
                "invalid_state_transition",
                order_id=order_id,
                symbol=order.symbol,
                from_state=current.value,
                to_state=new_state.value,
            )
            raise InvalidStateTransition(msg)

        order.state = new_state

        if new_state == OrderState.FILLED:
            order.filled_at = datetime.now(IST)
            if fill_price is not None:
                order.fill_price = fill_price

        elif new_state == OrderState.REJECTED:
            order.reject_reason = reject_reason
            # Rejection of ENTRY = failed trade attempt → increment consecutive losses
            if order.order_type == "ENTRY":
                count = self._shared_state.get("consecutive_losses", 0)
                self._shared_state["consecutive_losses"] = count + 1
                log.debug(
                    "consecutive_losses_incremented_on_rejection",
                    symbol=order.symbol,
                    consecutive_losses=count + 1,
                )

        log.info(
            "order_state_transition",
            order_id=order_id,
            symbol=order.symbol,
            direction=order.direction,
            order_type=order.order_type,
            from_state=current.value,
            to_state=new_state.value,
            timestamp=datetime.now(IST).isoformat(),
        )
        return order

    def mark_unknown(self, order_id: str, symbol: str) -> Order:
        """
        Mark an order as UNKNOWN — used for restart artifact detection.

        Bypasses normal transition validation. Locks the instrument in
        shared_state["locked_instruments"] to prevent new orders (D2 restart-safety).

        Args:
            order_id: Broker order ID found on Zerodha but not in local OSM.
            symbol:   Trading symbol.

        Returns:
            New Order in UNKNOWN state.
        """
        order = Order(
            order_id=order_id,
            symbol=symbol,
            instrument_token=0,
            direction="UNKNOWN",
            order_type="UNKNOWN",
            qty=0,
            price=Decimal("0"),
            state=OrderState.UNKNOWN,
            signal_id=0,
            placed_at=datetime.now(IST),
        )
        self._orders[order_id] = order

        # Lock instrument to block new orders for this symbol
        locked = self._shared_state.get("locked_instruments")
        if locked is not None:
            locked.add(symbol)

        log.critical(
            "unknown_order_on_restart",
            order_id=order_id,
            symbol=symbol,
            action="instrument_locked_pending_manual_resolution",
        )
        return order

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Optional[Order]:
        """Return order by ID, or None if not found."""
        return self._orders.get(order_id)

    def get_active_orders(self) -> list[Order]:
        """Return all orders NOT in terminal states."""
        return [o for o in self._orders.values() if o.state not in TERMINAL_STATES]

    def get_all_orders(self) -> list[Order]:
        """Return all orders including terminal states."""
        return list(self._orders.values())

    def get_orders_for_symbol(self, symbol: str) -> list[Order]:
        """Return all orders (including terminal) for a given symbol."""
        return [o for o in self._orders.values() if o.symbol == symbol]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_raise(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found in OSM registry")
        return order

    def _get_active_entry_for_symbol(self, symbol: str) -> Optional[Order]:
        """Return the active ENTRY order for a symbol, or None if none active."""
        for order in self._orders.values():
            if (
                order.symbol == symbol
                and order.order_type == "ENTRY"
                and order.state not in TERMINAL_STATES
            ):
                return order
        return None
