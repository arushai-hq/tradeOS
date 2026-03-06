"""
Unit tests for execution_engine.state_machine.

D8 mandatory test catalogue (10 cases):
  test_happy_path_created_to_filled
  test_invalid_transition_raises_exception
  test_created_to_filled_direct_is_invalid
  test_duplicate_order_same_symbol_rejected
  test_partial_fill_not_treated_as_complete
  test_rejected_increments_consecutive_counter
  test_zerodha_status_open_maps_to_acknowledged
  test_zerodha_status_complete_maps_to_filled
  test_unknown_zerodha_status_no_transition
  test_startup_reconciliation_blocks_on_unknown
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from execution_engine.state_machine import (
    DuplicateOrderError,
    InvalidStateTransition,
    Order,
    OrderState,
    OrderStateMachine,
    TERMINAL_STATES,
    map_zerodha_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_osm(shared_state: dict | None = None) -> OrderStateMachine:
    return OrderStateMachine(shared_state=shared_state or {})


def _create_entry(osm: OrderStateMachine, symbol: str = "RELIANCE") -> str:
    """Create and return an ENTRY order_id in CREATED state."""
    order_id = f"TEST-{symbol}-001"
    osm.create_order(
        order_id=order_id,
        symbol=symbol,
        instrument_token=738561,
        direction="LONG",
        order_type="ENTRY",
        qty=10,
        price=Decimal("2500"),
        signal_id=1,
        stop_loss=Decimal("2450"),
        target=Decimal("2600"),
    )
    return order_id


# ---------------------------------------------------------------------------
# 1. Happy path: CREATED → SUBMITTED → ACKNOWLEDGED → FILLED
# ---------------------------------------------------------------------------

def test_happy_path_created_to_filled():
    """Full happy-path transition chain for an ENTRY order."""
    osm = _make_osm()
    order_id = _create_entry(osm)

    osm.transition(order_id, OrderState.SUBMITTED)
    assert osm.get_order(order_id).state == OrderState.SUBMITTED

    osm.transition(order_id, OrderState.ACKNOWLEDGED)
    assert osm.get_order(order_id).state == OrderState.ACKNOWLEDGED

    order = osm.transition(order_id, OrderState.FILLED, fill_price=Decimal("2501"))
    assert order.state == OrderState.FILLED
    assert order.fill_price == Decimal("2501")
    assert order.filled_at is not None
    assert order.state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# 2. Invalid transition raises InvalidStateTransition
# ---------------------------------------------------------------------------

def test_invalid_transition_raises_exception():
    """SUBMITTED → FILLED is not in VALID_TRANSITIONS — must raise."""
    osm = _make_osm()
    order_id = _create_entry(osm)
    osm.transition(order_id, OrderState.SUBMITTED)

    with pytest.raises(InvalidStateTransition) as exc_info:
        osm.transition(order_id, OrderState.FILLED)  # SUBMITTED → FILLED is invalid

    assert "SUBMITTED" in str(exc_info.value)
    assert "FILLED" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. CREATED → FILLED directly is invalid
# ---------------------------------------------------------------------------

def test_created_to_filled_direct_is_invalid():
    """CREATED → FILLED bypasses required intermediate states — must raise."""
    osm = _make_osm()
    order_id = _create_entry(osm)

    with pytest.raises(InvalidStateTransition) as exc_info:
        osm.transition(order_id, OrderState.FILLED)

    assert "CREATED" in str(exc_info.value)
    assert "FILLED" in str(exc_info.value)

    # Order must remain in CREATED — state not corrupted
    assert osm.get_order(order_id).state == OrderState.CREATED


# ---------------------------------------------------------------------------
# 4. Duplicate ENTRY order for same symbol raises DuplicateOrderError
# ---------------------------------------------------------------------------

def test_duplicate_order_same_symbol_rejected():
    """Two ENTRY orders for the same symbol — second must be blocked."""
    osm = _make_osm()
    order_id = _create_entry(osm, "RELIANCE")
    osm.transition(order_id, OrderState.SUBMITTED)
    # SUBMITTED is NOT terminal → active

    with pytest.raises(DuplicateOrderError) as exc_info:
        # Attempt second ENTRY for same symbol
        osm.create_order(
            order_id="TEST-RELIANCE-002",
            symbol="RELIANCE",
            instrument_token=738561,
            direction="LONG",
            order_type="ENTRY",
            qty=5,
            price=Decimal("2500"),
        )

    assert "RELIANCE" in str(exc_info.value)
    assert "TEST-RELIANCE-001" in str(exc_info.value)


def test_duplicate_allowed_after_terminal_state():
    """After FILLED, a new ENTRY for the same symbol is allowed."""
    osm = _make_osm()
    order_id = _create_entry(osm, "INFY")
    osm.transition(order_id, OrderState.SUBMITTED)
    osm.transition(order_id, OrderState.ACKNOWLEDGED)
    osm.transition(order_id, OrderState.FILLED)

    # Should not raise — FILLED is terminal
    new_order_id = "TEST-INFY-002"
    order = osm.create_order(
        order_id=new_order_id,
        symbol="INFY",
        instrument_token=408065,
        direction="SHORT",
        order_type="ENTRY",
        qty=5,
        price=Decimal("1700"),
    )
    assert order.state == OrderState.CREATED


# ---------------------------------------------------------------------------
# 5. PARTIALLY_FILLED is NOT terminal — not treated as complete
# ---------------------------------------------------------------------------

def test_partial_fill_not_treated_as_complete():
    """PARTIALLY_FILLED must not be in TERMINAL_STATES."""
    assert OrderState.PARTIALLY_FILLED not in TERMINAL_STATES

    osm = _make_osm()
    order_id = _create_entry(osm)
    osm.transition(order_id, OrderState.SUBMITTED)
    osm.transition(order_id, OrderState.ACKNOWLEDGED)
    osm.transition(order_id, OrderState.PARTIALLY_FILLED)

    order = osm.get_order(order_id)
    assert order.state == OrderState.PARTIALLY_FILLED
    assert order.state not in TERMINAL_STATES

    # Order should still appear in active orders
    active = osm.get_active_orders()
    assert any(o.order_id == order_id for o in active)

    # DuplicateOrderError still fires (instrument still locked)
    with pytest.raises(DuplicateOrderError):
        osm.create_order(
            order_id="SECOND-ORDER",
            symbol="RELIANCE",
            instrument_token=738561,
            direction="LONG",
            order_type="ENTRY",
            qty=5,
            price=Decimal("2500"),
        )


# ---------------------------------------------------------------------------
# 6. REJECTED increments shared_state["consecutive_losses"] for ENTRY orders
# ---------------------------------------------------------------------------

def test_rejected_increments_consecutive_counter():
    """ENTRY order rejection increments consecutive_losses in shared_state."""
    shared_state = {"consecutive_losses": 0}
    osm = _make_osm(shared_state)

    order_id = _create_entry(osm, "TCS")
    osm.transition(order_id, OrderState.SUBMITTED)
    osm.transition(order_id, OrderState.REJECTED, reject_reason="RMS:Insufficient funds")

    assert shared_state["consecutive_losses"] == 1

    # Second ENTRY rejected → counter increments again
    osm.create_order(
        order_id="TCS-002",
        symbol="TCS",
        instrument_token=2953217,
        direction="LONG",
        order_type="ENTRY",
        qty=5,
        price=Decimal("3500"),
    )
    osm.transition("TCS-002", OrderState.SUBMITTED)
    osm.transition("TCS-002", OrderState.REJECTED, reject_reason="RMS:Limit exceeded")

    assert shared_state["consecutive_losses"] == 2


def test_rejected_exit_does_not_increment_counter():
    """Rejected EXIT order must NOT increment consecutive_losses."""
    shared_state = {"consecutive_losses": 0}
    osm = _make_osm(shared_state)

    # Create EXIT order directly
    osm.create_order(
        order_id="EXIT-001",
        symbol="WIPRO",
        instrument_token=969473,
        direction="SELL",
        order_type="EXIT",
        qty=10,
        price=Decimal("420"),
    )
    osm.transition("EXIT-001", OrderState.SUBMITTED)
    osm.transition("EXIT-001", OrderState.REJECTED, reject_reason="Market closed")

    assert shared_state["consecutive_losses"] == 0  # EXIT rejection does NOT count


# ---------------------------------------------------------------------------
# 7. Zerodha "OPEN" maps to ACKNOWLEDGED
# ---------------------------------------------------------------------------

def test_zerodha_status_open_maps_to_acknowledged():
    """Zerodha 'OPEN' status must map to OrderState.ACKNOWLEDGED."""
    result = map_zerodha_status("OPEN", "ORD001", "RELIANCE")
    assert result == OrderState.ACKNOWLEDGED


def test_zerodha_status_open_lowercase_maps_to_acknowledged():
    """Mapping is case-insensitive."""
    result = map_zerodha_status("open", "ORD001", "RELIANCE")
    assert result == OrderState.ACKNOWLEDGED


# ---------------------------------------------------------------------------
# 8. Zerodha "COMPLETE" maps to FILLED
# ---------------------------------------------------------------------------

def test_zerodha_status_complete_maps_to_filled():
    """Zerodha 'COMPLETE' status must map to OrderState.FILLED."""
    result = map_zerodha_status("COMPLETE", "ORD001", "INFY")
    assert result == OrderState.FILLED


# ---------------------------------------------------------------------------
# 9. Unknown Zerodha status → return None (do not transition)
# ---------------------------------------------------------------------------

def test_unknown_zerodha_status_no_transition():
    """Unknown Zerodha status must return None — do not transition per D2 spec."""
    result = map_zerodha_status("SOME_WEIRD_STATUS", "ORD001", "TCS")
    assert result is None


def test_empty_zerodha_status_no_transition():
    """Empty status string must return None."""
    result = map_zerodha_status("", "ORD001", "TCS")
    assert result is None


# ---------------------------------------------------------------------------
# 10. Startup reconciliation: UNKNOWN marks lock — blocks new orders for symbol
# ---------------------------------------------------------------------------

def test_startup_reconciliation_blocks_on_unknown():
    """
    mark_unknown() locks the instrument in shared_state["locked_instruments"].
    Subsequent create_order() for that symbol is NOT blocked by OSM duplicate check
    (UNKNOWN is terminal), but is blocked by the locked_instruments gate in OrderPlacer.
    """
    shared_state = {"locked_instruments": set(), "consecutive_losses": 0}
    osm = _make_osm(shared_state)

    # Simulate startup: Zerodha has an order not in local OSM
    unknown_order = osm.mark_unknown("ZERODHA-UNKNOWN-001", "RELIANCE")

    # UNKNOWN is stored in OSM
    assert osm.get_order("ZERODHA-UNKNOWN-001") is not None
    assert unknown_order.state == OrderState.UNKNOWN
    assert unknown_order.state in TERMINAL_STATES

    # Instrument is locked in shared_state
    assert "RELIANCE" in shared_state["locked_instruments"]


def test_mark_unknown_order_is_terminal():
    """UNKNOWN state has no valid outgoing transitions — truly terminal."""
    osm = _make_osm()
    osm.mark_unknown("ORD-UNK", "SBIN")

    with pytest.raises(InvalidStateTransition):
        osm.transition("ORD-UNK", OrderState.ACKNOWLEDGED)


def test_get_active_orders_excludes_unknown():
    """UNKNOWN orders must NOT appear in get_active_orders()."""
    osm = _make_osm()
    osm.mark_unknown("UNK-001", "AXISBANK")
    # Create a live active order
    _create_entry(osm, "RELIANCE")

    active = osm.get_active_orders()
    assert all(o.state != OrderState.UNKNOWN for o in active)
    assert any(o.symbol == "RELIANCE" for o in active)
    assert not any(o.symbol == "AXISBANK" for o in active)
