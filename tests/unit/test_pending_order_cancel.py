"""
Tests for pending order cancellation at hard_exit.

(c) Pending orders in CREATED/SUBMITTED/ACKNOWLEDGED cancelled; FILLED untouched
(d) No error if zero pending orders at hard_exit
"""
from __future__ import annotations

from decimal import Decimal

from execution_engine.state_machine import (
    OrderState,
    OrderStateMachine,
    TERMINAL_STATES,
)


def _make_osm() -> OrderStateMachine:
    return OrderStateMachine(shared_state={})


def _create_order(osm: OrderStateMachine, order_id: str, symbol: str = "RELIANCE") -> str:
    """Create an ENTRY order in CREATED state."""
    osm.create_order(
        order_id=order_id,
        symbol=symbol,
        instrument_token=738561,
        direction="LONG",
        order_type="ENTRY",
        qty=10,
        price=Decimal("2500"),
    )
    return order_id


def test_pending_orders_cancelled_before_hard_exit():
    """
    Orders in CREATED, SUBMITTED, ACKNOWLEDGED states are cancelled.
    A FILLED order is untouched.
    Mirrors the hard_exit logic in risk_watchdog.
    """
    osm = _make_osm()

    # Order 1: CREATED (not yet submitted)
    _create_order(osm, "ORD-CREATED", "RELIANCE")

    # Order 2: SUBMITTED (sent to broker, awaiting ack)
    _create_order(osm, "ORD-SUBMITTED", "INFY")
    osm.transition("ORD-SUBMITTED", OrderState.SUBMITTED)

    # Order 3: ACKNOWLEDGED (broker accepted, awaiting fill)
    _create_order(osm, "ORD-ACKED", "TCS")
    osm.transition("ORD-ACKED", OrderState.SUBMITTED)
    osm.transition("ORD-ACKED", OrderState.ACKNOWLEDGED)

    # Order 4: FILLED (already complete — must NOT be cancelled)
    _create_order(osm, "ORD-FILLED", "WIPRO")
    osm.transition("ORD-FILLED", OrderState.SUBMITTED)
    osm.transition("ORD-FILLED", OrderState.ACKNOWLEDGED)
    osm.transition("ORD-FILLED", OrderState.FILLED, fill_price=Decimal("400"))

    # --- Simulate hard_exit pending order cancellation ---
    pending = [
        o for o in osm.get_active_orders()
        if o.state != OrderState.FILLED
    ]
    for order in pending:
        osm.transition(order.order_id, OrderState.CANCELLED)

    # Verify: 3 orders cancelled
    assert len(pending) == 3

    # Verify final states
    assert osm._orders["ORD-CREATED"].state == OrderState.CANCELLED
    assert osm._orders["ORD-SUBMITTED"].state == OrderState.CANCELLED
    assert osm._orders["ORD-ACKED"].state == OrderState.CANCELLED
    assert osm._orders["ORD-FILLED"].state == OrderState.FILLED  # untouched

    # No active (non-terminal) orders remain except FILLED
    active = osm.get_active_orders()
    assert len(active) == 0  # FILLED is terminal


def test_no_error_zero_pending_orders():
    """
    When all orders are already in terminal states (FILLED, CANCELLED, etc.),
    the cancellation logic finds zero pending orders — no error, no crash.
    """
    osm = _make_osm()

    # One FILLED order
    _create_order(osm, "ORD-FILLED", "RELIANCE")
    osm.transition("ORD-FILLED", OrderState.SUBMITTED)
    osm.transition("ORD-FILLED", OrderState.ACKNOWLEDGED)
    osm.transition("ORD-FILLED", OrderState.FILLED, fill_price=Decimal("2500"))

    # --- Simulate hard_exit pending order cancellation ---
    pending = [
        o for o in osm.get_active_orders()
        if o.state != OrderState.FILLED
    ]
    for order in pending:
        osm.transition(order.order_id, OrderState.CANCELLED)

    # Zero pending — no error
    assert len(pending) == 0
    assert osm._orders["ORD-FILLED"].state == OrderState.FILLED
