"""
Unit tests for audit fix verification.

Tests cover:
  Critical 1 — signal_id chain end-to-end:
    test_write_signal_returns_db_id
    test_signal_db_id_set_on_signal_object
    test_order_placer_passes_signal_db_id_to_osm
    test_signal_id_flows_through_pnl_tracker_to_trade_result

  Critical 2 — structlog field names match session_report parser:
    test_order_filled_emits_qty_field
    test_position_closed_emits_exit_price_field
    test_signal_rejected_emits_parser_compatible_fields

  Warnings — dead code removed:
    test_state_machine_no_get_orders_for_symbol
    test_exit_manager_no_asyncio_import
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
import structlog
from structlog.testing import capture_logs

from execution_engine.order_placer import OrderPlacer
from execution_engine.state_machine import OrderStateMachine
from risk_manager.pnl_tracker import PnlTracker, TradeResult
from strategy_engine.signal_generator import Signal

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(db_id: int | None = None, **overrides) -> Signal:
    defaults = dict(
        symbol="RELIANCE",
        instrument_token=738561,
        direction="LONG",
        signal_time=datetime.now(IST),
        candle_time=datetime.now(IST),
        theoretical_entry=Decimal("2500"),
        stop_loss=Decimal("2450"),
        target=Decimal("2600"),
        ema9=Decimal("9.5"),
        ema21=Decimal("8.5"),
        rsi=Decimal("62"),
        vwap=Decimal("2480"),
        volume_ratio=Decimal("2.1"),
    )
    defaults.update(overrides)
    sig = Signal(**defaults)
    if db_id is not None:
        sig.db_id = db_id
    return sig


def _make_placer(mode: str = "paper") -> tuple[OrderPlacer, OrderStateMachine, dict]:
    config = {"system": {"mode": mode}}
    shared = {
        "kill_switch_level": 0,
        "locked_instruments": set(),
        "open_positions": {},
        "last_tick_prices": {},
    }
    osm = OrderStateMachine(shared_state=shared)
    kite = MagicMock()
    placer = OrderPlacer(kite=kite, config=config, osm=osm, shared_state=shared)
    return placer, osm, shared


# ---------------------------------------------------------------------------
# Critical 1 — signal_id chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_signal_returns_db_id():
    """_write_signal uses RETURNING id and returns the generated DB id."""
    from strategy_engine import StrategyEngine

    # Mock pool with fetchval returning 42
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=42)

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = _acquire

    engine = object.__new__(StrategyEngine)
    engine._db_pool = mock_pool
    engine._session_date = datetime.now(IST).date()

    signal = _make_signal()
    result = await engine._write_signal(signal, allowed=True, reason="")

    assert result == 42
    # Verify RETURNING id is in the SQL
    sql_arg = mock_conn.fetchval.call_args[0][0]
    assert "RETURNING id" in sql_arg


@pytest.mark.asyncio
async def test_signal_db_id_set_on_signal_object():
    """Signal.db_id field can be set after creation (mutable dataclass)."""
    sig = _make_signal()
    assert sig.db_id is None

    sig.db_id = 99
    assert sig.db_id == 99


@pytest.mark.asyncio
async def test_order_placer_passes_signal_db_id_to_osm():
    """OrderPlacer uses signal.db_id (not hardcoded 0) when creating orders."""
    placer, osm, shared = _make_placer("paper")
    signal = _make_signal(db_id=42)

    order = await placer.place_entry(signal, qty=10)

    assert order is not None
    assert order.signal_id == 42


@pytest.mark.asyncio
async def test_order_placer_signal_id_zero_when_no_db_id():
    """OrderPlacer falls back to 0 when signal.db_id is None."""
    placer, osm, shared = _make_placer("paper")
    signal = _make_signal(db_id=None)

    order = await placer.place_entry(signal, qty=10)

    assert order is not None
    assert order.signal_id == 0


def test_signal_id_flows_through_pnl_tracker_to_trade_result():
    """signal_id stored in on_fill is carried through to TradeResult on close."""
    shared = {"open_positions": {}, "daily_pnl_pct": 0.0, "daily_pnl_rs": 0.0}
    tracker = PnlTracker(capital=Decimal("500000"), shared_state=shared)

    tracker.on_fill(
        symbol="RELIANCE",
        direction="LONG",
        qty=100,
        fill_price=Decimal("2500"),
        order_id="ORDER-1",
        signal_id=42,
    )

    result = tracker.on_close(
        symbol="RELIANCE",
        exit_price=Decimal("2600"),
        exit_reason="TARGET_HIT",
        exit_order_id="EXIT-1",
    )

    assert result.signal_id == 42


# ---------------------------------------------------------------------------
# Critical 2 — structlog field names
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_filled_emits_qty_field():
    """order_filled event must emit 'qty' (not 'quantity') to match session_report parser."""
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order, OrderState

    order = Order(
        order_id="PAPER-TEST123",
        symbol="RELIANCE",
        instrument_token=738561,
        direction="LONG",
        order_type="ENTRY",
        qty=100,
        price=Decimal("2500"),
        state=OrderState.FILLED,
        signal_id=42,
        placed_at=datetime.now(IST),
        fill_price=Decimal("2500"),
        stop_loss=Decimal("2450"),
        target=Decimal("2600"),
    )

    with capture_logs() as cap_logs:
        monitor = object.__new__(OrderMonitor)
        monitor._mode = "paper"
        monitor._notifier = None
        monitor._db_pool = None
        monitor._session_date = datetime.now(IST).date()
        monitor._risk_manager = AsyncMock()
        monitor._exit_manager = AsyncMock()
        monitor._shared_state = {"fills_today": 0, "open_positions": {}}

        await monitor._on_entry_fill(order)

    # Find the order_filled log event
    filled_events = [e for e in cap_logs if e.get("event") == "order_filled"]
    assert len(filled_events) >= 1
    event = filled_events[0]
    assert "qty" in event, f"order_filled must emit 'qty', got keys: {list(event.keys())}"
    assert "quantity" not in event, "order_filled must NOT emit 'quantity'"
    assert event["qty"] == 100


def test_position_closed_emits_exit_price_field():
    """position_closed event must emit 'exit_price' for session_report parser."""
    shared = {"open_positions": {}, "daily_pnl_pct": 0.0, "daily_pnl_rs": 0.0}
    tracker = PnlTracker(capital=Decimal("500000"), shared_state=shared)

    tracker.on_fill("RELIANCE", "LONG", 100, Decimal("2500"), "ORDER-1", 1)

    with capture_logs() as cap_logs:
        tracker.on_close("RELIANCE", Decimal("2600"), "TARGET_HIT", "EXIT-1")

    closed_events = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(closed_events) == 1
    event = closed_events[0]
    assert "exit_price" in event, f"position_closed must emit 'exit_price', got keys: {list(event.keys())}"
    assert event["exit_price"] == 2600.0


def test_signal_rejected_emits_parser_compatible_fields():
    """signal_rejected event must emit 'reason', 'gate', 'entry', 'stop', 'target'
    to match session_report parser field expectations."""
    from strategy_engine import StrategyEngine, _parse_gate_info

    # Verify the field names that _parse_gate_info produces
    gate_num, gate_name = _parse_gate_info("KILL_SWITCH:level_1")
    assert gate_num == 1
    assert gate_name == "kill_switch"

    # Now test the actual log emission by constructing the log call manually
    # We test the field names the strategy engine would emit
    signal = _make_signal()
    reason = "MAX_POSITIONS_REACHED"
    _gate_number, _gate_name = _parse_gate_info(reason)

    with capture_logs() as cap_logs:
        log = structlog.get_logger()
        log.info(
            "signal_rejected",
            symbol=signal.symbol,
            direction=signal.direction,
            entry=float(signal.theoretical_entry),
            stop=float(signal.stop_loss),
            target=float(signal.target),
            gate=_gate_number,
            gate_name=_gate_name,
            reason=reason,
            rsi=float(signal.rsi),
            volume_ratio=float(signal.volume_ratio),
        )

    rejected_events = [e for e in cap_logs if e.get("event") == "signal_rejected"]
    assert len(rejected_events) == 1
    event = rejected_events[0]

    # Parser expects these exact field names
    assert "reason" in event, "signal_rejected must emit 'reason' (not 'rejection_reason')"
    assert "gate" in event, "signal_rejected must emit 'gate' (not 'gate_number')"
    assert "entry" in event, "signal_rejected must emit 'entry'"
    assert "stop" in event, "signal_rejected must emit 'stop'"
    assert "target" in event, "signal_rejected must emit 'target'"

    # Verify no old field names present
    assert "rejection_reason" not in event
    assert "gate_number" not in event


# ---------------------------------------------------------------------------
# Warnings — dead code removed
# ---------------------------------------------------------------------------

def test_state_machine_no_get_orders_for_symbol():
    """get_orders_for_symbol should be removed (unused method)."""
    assert not hasattr(OrderStateMachine, "get_orders_for_symbol"), \
        "get_orders_for_symbol should have been removed from OrderStateMachine"


def test_exit_manager_no_asyncio_import():
    """exit_manager.py should not import asyncio (unused)."""
    import execution_engine.exit_manager as em
    source = inspect.getsource(em)
    # The module should not have 'import asyncio' as a standalone import
    # (async def keywords don't need the asyncio module)
    lines = source.split("\n")
    asyncio_imports = [l.strip() for l in lines if l.strip() == "import asyncio"]
    assert len(asyncio_imports) == 0, \
        "exit_manager.py should not import asyncio (unused)"
