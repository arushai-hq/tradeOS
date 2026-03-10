"""
Tests for B8: exit fill handler must not create ghost positions.

Root cause: OrderMonitor._on_exit_fill() called risk_manager.on_close()
which deleted the position from shared_state, THEN read shared_state
to compute P&L for a second position_closed log — but got empty dict,
defaulting to direction=LONG, entry_price=0.0, qty=0, logging a ghost.

Fix: snapshot position data BEFORE on_close(). Remove duplicate
position_closed log from OrderMonitor (PnlTracker already logs it).

Tests:
  (a) SHORT closed by BUY exit → no ghost LONG position_closed
  (b) LONG closed by SELL exit → no ghost SHORT position_closed
  (c) Kill switch emergency_exit_all → only real closures, zero ghosts
  (d) position_closed event count matches actual position count
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
import structlog.testing

IST = pytz.timezone("Asia/Kolkata")


def _make_monitor(shared_state: dict):
    """Create a minimal OrderMonitor for testing _on_exit_fill."""
    from execution_engine.order_monitor import OrderMonitor

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._shared_state = shared_state
    monitor._risk_manager = AsyncMock()
    monitor._notifier = None
    return monitor


def _make_exit_order(symbol: str, fill_price: float, exit_type: str = "KILL_SWITCH"):
    """Create a mock exit Order."""
    from execution_engine.state_machine import Order

    order = MagicMock(spec=Order)
    order.order_id = f"PAPER-EXIT-{symbol}"
    order.symbol = symbol
    order.fill_price = Decimal(str(fill_price))
    order.price = Decimal(str(fill_price))
    order.exit_type = exit_type
    return order


# ---------------------------------------------------------------------------
# (a) SHORT closed by BUY exit → no ghost LONG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b8_short_exit_no_ghost_long():
    """
    Session 04 reproduction: LT SHORT closed by BUY exit order.
    Before fix: ghost position_closed with direction=LONG, entry_price=0.0.
    After fix: only exit_filled logged, zero position_closed from OrderMonitor.
    """
    shared_state = {
        "open_positions": {
            "LT": {
                "qty": -51,           # ExitManager schema: negative for SHORT
                "avg_price": 3883.1,
                "side": "SELL",
                "entry_time": datetime.now(IST) - timedelta(seconds=30),
            }
        }
    }

    monitor = _make_monitor(shared_state)
    # Simulate on_close deleting the position (as PnlTracker does)
    async def mock_on_close(**kwargs):
        shared_state["open_positions"].pop(kwargs["symbol"], None)
    monitor._risk_manager.on_close = mock_on_close

    order = _make_exit_order("LT", 3883.1)

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order)

    # exit_filled must be logged
    exit_events = [e for e in cap_logs if e.get("event") == "exit_filled"]
    assert len(exit_events) == 1
    assert exit_events[0]["symbol"] == "LT"

    # ZERO ghost position_closed with direction=LONG
    ghost = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(ghost) == 0, (
        f"Ghost position_closed detected: {ghost}"
    )


# ---------------------------------------------------------------------------
# (b) LONG closed by SELL exit → no ghost SHORT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b8_long_exit_no_ghost_short():
    """
    RELIANCE LONG closed by SELL exit order.
    Must not create ghost position_closed with direction=SHORT.
    """
    shared_state = {
        "open_positions": {
            "RELIANCE": {
                "qty": 5,
                "avg_price": 2450.0,
                "side": "BUY",
                "entry_time": datetime.now(IST) - timedelta(minutes=45),
            }
        }
    }

    monitor = _make_monitor(shared_state)
    async def mock_on_close(**kwargs):
        shared_state["open_positions"].pop(kwargs["symbol"], None)
    monitor._risk_manager.on_close = mock_on_close

    order = _make_exit_order("RELIANCE", 2420.0, "STOP")

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order)

    # ZERO ghost position_closed
    ghost = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(ghost) == 0


# ---------------------------------------------------------------------------
# (c) Kill switch emergency_exit_all → zero ghosts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b8_kill_switch_exit_all_zero_ghosts():
    """
    Session 04 scenario: 2 SHORT positions (LT, AXISBANK) killed by kill switch.
    emergency_exit_all places BUY exit orders → OrderMonitor processes fills.
    Must produce zero ghost position_closed events from OrderMonitor.
    """
    shared_state = {
        "open_positions": {
            "LT": {
                "qty": -51,
                "avg_price": 3883.1,
                "side": "SELL",
                "entry_time": datetime.now(IST) - timedelta(seconds=30),
            },
            "AXISBANK": {
                "qty": -155,
                "avg_price": 1288.9,
                "side": "SELL",
                "entry_time": datetime.now(IST) - timedelta(seconds=30),
            },
        }
    }

    monitor = _make_monitor(shared_state)
    # Simulate on_close deleting positions one by one
    async def mock_on_close(**kwargs):
        shared_state["open_positions"].pop(kwargs["symbol"], None)
    monitor._risk_manager.on_close = mock_on_close

    order_lt = _make_exit_order("LT", 3883.1)
    order_axisbank = _make_exit_order("AXISBANK", 1288.9)

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order_lt)
        await monitor._on_exit_fill(order_axisbank)

    # Should have exactly 2 exit_filled events
    exit_events = [e for e in cap_logs if e.get("event") == "exit_filled"]
    assert len(exit_events) == 2

    # ZERO ghost position_closed from OrderMonitor
    ghost = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(ghost) == 0, (
        f"Ghost events detected after kill switch exit: {ghost}"
    )

    # Both positions removed from shared_state
    assert len(shared_state["open_positions"]) == 0


# ---------------------------------------------------------------------------
# (d) position_closed count = real position count (no doubles)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b8_position_closed_count_matches():
    """
    With a real PnlTracker mock that logs position_closed,
    verify exactly 1 position_closed per position (not 2).
    """
    shared_state = {
        "open_positions": {
            "INFY": {
                "qty": -4,
                "avg_price": 1500.0,
                "side": "SELL",
                "entry_time": datetime.now(IST) - timedelta(minutes=60),
            },
        }
    }

    monitor = _make_monitor(shared_state)

    # Mock on_close to both log position_closed (like PnlTracker) and delete
    async def mock_on_close(**kwargs):
        structlog.get_logger().info(
            "position_closed",
            symbol=kwargs["symbol"],
            direction="SHORT",
            exit_reason=kwargs["exit_reason"],
        )
        shared_state["open_positions"].pop(kwargs["symbol"], None)
    monitor._risk_manager.on_close = mock_on_close

    order = _make_exit_order("INFY", 1450.0, "TARGET")

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order)

    # Exactly 1 position_closed (from PnlTracker mock), not 2
    closed_events = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(closed_events) == 1, (
        f"Expected exactly 1 position_closed, got {len(closed_events)}: {closed_events}"
    )
    assert closed_events[0]["direction"] == "SHORT"
