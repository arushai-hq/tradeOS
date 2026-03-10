"""
TradeOS — Unit tests for B5 lifecycle logging.

Verifies that the 7 lifecycle log events fire with correct fields:
  signal_accepted  — gate pass → order_queue
  signal_rejected  — gate fail with correct gate_name + gate_number
  order_filled     — entry fill in OrderMonitor
  exit_filled      — exit fill in OrderMonitor (position_closed logged by PnlTracker)

Tests:
  (a) test_signal_accepted_logged_on_gate_pass
  (b) test_signal_rejected_logged_on_gate_fail_max_positions
  (c) test_signal_rejected_gate_number_matches_reason
  (d) test_order_filled_logged_on_entry_fill
  (e) test_exit_fill_calls_on_close_no_ghost
  (f) test_exit_fill_long_calls_on_close
  (g) test_exit_fill_short_calls_on_close
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz
import structlog.testing

from strategy_engine.candle_builder import Candle
from strategy_engine.signal_generator import Signal

IST = pytz.timezone("Asia/Kolkata")
BASE_TIME = datetime(2026, 3, 9, 10, 30, tzinfo=IST)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    symbol: str = "RELIANCE",
    direction: str = "LONG",
    rsi: float = 62.0,
    volume_ratio: float = 1.6,
) -> Signal:
    return Signal(
        symbol=symbol,
        instrument_token=738561,
        direction=direction,
        signal_time=BASE_TIME,
        candle_time=BASE_TIME,
        theoretical_entry=Decimal("2450"),
        stop_loss=Decimal("2420"),
        target=Decimal("2510"),
        ema9=Decimal("2445"),
        ema21=Decimal("2440"),
        rsi=Decimal(str(rsi)),
        vwap=Decimal("2430"),
        volume_ratio=Decimal(str(volume_ratio)),
    )


def _make_strategy_engine(gate_result: tuple[bool, str] = (True, "OK")):
    """Build a minimal StrategyEngine with all dependencies mocked."""
    from strategy_engine import StrategyEngine
    from strategy_engine.candle_builder import Candle

    engine = StrategyEngine.__new__(StrategyEngine)

    mock_candle = MagicMock(spec=Candle)
    mock_candle.symbol = "RELIANCE"
    mock_candle.instrument_token = 738561
    mock_candle.candle_time = MagicMock()
    mock_candle.candle_time.isoformat.return_value = "2026-03-09T10:30:00+05:30"

    mock_builder = MagicMock()
    mock_builder.process_tick.return_value = mock_candle

    mock_ind_engine = MagicMock()
    mock_ind_engine.update.return_value = MagicMock()  # non-None indicators

    mock_signal_gen = MagicMock()
    mock_signal_gen.evaluate.return_value = _make_signal()

    mock_risk_gate = MagicMock()
    mock_risk_gate.check.return_value = gate_result

    engine._candle_builders = {738561: mock_builder}
    engine._indicator_engines = {738561: mock_ind_engine}
    engine._signal_generator = mock_signal_gen
    engine._risk_gate = mock_risk_gate
    engine._regime_detector = None
    engine._shared_state = {
        "accepting_signals": True,
        "signals_generated_today": 0,
    }
    engine._config = {"system": {"mode": "paper"}}
    engine._order_queue = asyncio.Queue()
    engine._signals_generated = 0

    return engine


def _tick() -> dict:
    return {
        "instrument_token": 738561,
        "last_price": 2000.0,
        "volume_traded": 5000,
        "exchange_timestamp": None,
    }


# ---------------------------------------------------------------------------
# (a) signal_accepted fires on gate pass
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_accepted_logged_on_gate_pass():
    """
    (a) signal_accepted must fire when all risk gates pass.
    Fields verified: symbol, direction, entry, stop, target, rsi, volume_ratio,
                     regime, gates_passed.
    """
    engine = _make_strategy_engine(gate_result=(True, "OK"))

    with (
        structlog.testing.capture_logs() as cap_logs,
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_candle", new=AsyncMock()
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_signal", new=AsyncMock()
        ),
    ):
        await engine._process_tick(_tick())

    accepted = [e for e in cap_logs if e.get("event") == "signal_accepted"]
    assert len(accepted) == 1, f"Expected 1 signal_accepted event, got: {[e['event'] for e in cap_logs]}"

    evt = accepted[0]
    assert evt["symbol"] == "RELIANCE"
    assert evt["direction"] == "LONG"
    assert "entry" in evt
    assert "stop" in evt
    assert "target" in evt
    assert "rsi" in evt
    assert "volume_ratio" in evt
    assert "regime" in evt
    assert evt["gates_passed"] == "all"


# ---------------------------------------------------------------------------
# (b) signal_rejected fires on gate fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_rejected_logged_on_gate_fail_max_positions():
    """
    (b) signal_rejected must fire when risk gate returns MAX_POSITIONS_REACHED.
    gate_number must be 4, gate_name must be 'max_positions'.
    """
    engine = _make_strategy_engine(gate_result=(False, "MAX_POSITIONS_REACHED"))

    with (
        structlog.testing.capture_logs() as cap_logs,
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_candle", new=AsyncMock()
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_signal", new=AsyncMock()
        ),
    ):
        await engine._process_tick(_tick())

    rejected = [e for e in cap_logs if e.get("event") == "signal_rejected"]
    assert len(rejected) == 1, f"Expected signal_rejected, got: {[e['event'] for e in cap_logs]}"

    evt = rejected[0]
    assert evt["gate_number"] == 4
    assert evt["gate_name"] == "max_positions"
    assert evt["rejection_reason"] == "MAX_POSITIONS_REACHED"
    assert "rsi" in evt
    assert "volume_ratio" in evt


# ---------------------------------------------------------------------------
# (c) gate_number mapping covers all 7 gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected_gate,expected_name", [
    ("KILL_SWITCH_LEVEL_1",        1, "kill_switch"),
    ("RECON_IN_PROGRESS",          2, "recon_in_progress"),
    ("INSTRUMENT_LOCKED",          3, "instrument_locked"),
    ("MAX_POSITIONS_REACHED",      4, "max_positions"),
    ("HARD_EXIT_TIME_REACHED",     5, "hard_exit_time"),
    ("DUPLICATE_SIGNAL",           6, "duplicate_signal"),
    ("REGIME_BLOCKED_BEAR_TREND",  7, "regime_check"),
])
async def test_signal_rejected_gate_number_matches_reason(
    reason: str, expected_gate: int, expected_name: str
):
    """(c) Gate number and name must map correctly for all 7 rejection reasons."""
    engine = _make_strategy_engine(gate_result=(False, reason))

    with (
        structlog.testing.capture_logs() as cap_logs,
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_candle", new=AsyncMock()
        ),
        __import__("unittest.mock", fromlist=["patch"]).patch.object(
            engine, "_write_signal", new=AsyncMock()
        ),
    ):
        await engine._process_tick(_tick())

    rejected = [e for e in cap_logs if e.get("event") == "signal_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["gate_number"] == expected_gate
    assert rejected[0]["gate_name"] == expected_name


# ---------------------------------------------------------------------------
# (d) order_filled fires on entry fill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_filled_logged_on_entry_fill():
    """
    (d) order_filled must fire in OrderMonitor._on_entry_fill with
    required fields: symbol, direction, fill_price, quantity, position_id, mode.
    """
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order, OrderState

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._is_paper = True
    monitor._processed_order_ids = set()
    monitor._shared_state = {"fills_today": 0}
    monitor._risk_manager = AsyncMock()
    monitor._exit_manager = AsyncMock()

    order = MagicMock(spec=Order)
    order.order_id = "PAPER-ABCD1234"
    order.symbol = "RELIANCE"
    order.direction = "LONG"
    order.qty = 5
    order.fill_price = Decimal("2450.0")
    order.price = Decimal("2450.0")
    order.stop_loss = Decimal("2420.0")
    order.target = Decimal("2510.0")
    order.signal_id = 0

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_entry_fill(order)

    filled = [e for e in cap_logs if e.get("event") == "order_filled"]
    assert len(filled) == 1, f"Expected order_filled event, got: {[e['event'] for e in cap_logs]}"

    evt = filled[0]
    assert evt["symbol"] == "RELIANCE"
    assert evt["direction"] == "LONG"
    assert evt["fill_price"] == 2450.0
    assert evt["quantity"] == 5
    assert evt["position_id"] == "PAPER-ABCD1234"
    assert evt["mode"] == "paper"


# ---------------------------------------------------------------------------
# (e) exit fill calls on_close, no ghost position_closed from OrderMonitor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exit_fill_calls_on_close_no_ghost():
    """
    (e) B8 fix: _on_exit_fill must call risk_manager.on_close() and log
    exit_filled. It must NOT log its own position_closed (PnlTracker does that).
    """
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order, OrderState

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._shared_state = {
        "open_positions": {
            "RELIANCE": {
                "qty": 5,
                "avg_price": 2450.0,
                "side": "BUY",
                "entry_time": datetime.now(IST) - timedelta(minutes=45),
            }
        }
    }
    monitor._risk_manager = AsyncMock()
    monitor._notifier = None

    order = MagicMock(spec=Order)
    order.order_id = "PAPER-EXIT-XYZ"
    order.symbol = "RELIANCE"
    order.fill_price = Decimal("2420.0")
    order.price = Decimal("2420.0")
    order.exit_type = "STOP"

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order)

    # exit_filled must be logged
    exit_filled = [e for e in cap_logs if e.get("event") == "exit_filled"]
    assert len(exit_filled) == 1
    assert exit_filled[0]["symbol"] == "RELIANCE"

    # on_close must be called
    monitor._risk_manager.on_close.assert_called_once()

    # NO position_closed from OrderMonitor (PnlTracker handles it)
    ghost = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(ghost) == 0, f"Ghost position_closed logged: {ghost}"


# ---------------------------------------------------------------------------
# (f) exit fill — LONG position calls on_close correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exit_fill_long_calls_on_close():
    """
    (f) LONG position exit: on_close called with correct symbol and exit_price.
    """
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._shared_state = {
        "open_positions": {
            "TCS": {
                "qty": 3,
                "avg_price": 2450.0,
                "side": "BUY",
                "entry_time": datetime.now(IST) - timedelta(minutes=30),
            }
        }
    }
    monitor._risk_manager = AsyncMock()
    monitor._notifier = None

    order = MagicMock(spec=Order)
    order.order_id = "PAPER-EXIT-TCS"
    order.symbol = "TCS"
    order.fill_price = Decimal("2510.0")
    order.price = Decimal("2510.0")
    order.exit_type = "TARGET"

    await monitor._on_exit_fill(order)

    monitor._risk_manager.on_close.assert_called_once_with(
        symbol="TCS",
        exit_price=Decimal("2510.0"),
        exit_reason="TARGET_HIT",
        exit_order_id="PAPER-EXIT-TCS",
    )


# ---------------------------------------------------------------------------
# (g) exit fill — SHORT position calls on_close correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exit_fill_short_calls_on_close():
    """
    (g) SHORT position exit: on_close called, no ghost position_closed.
    """
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._shared_state = {
        "open_positions": {
            "INFY": {
                "qty": 4,
                "avg_price": 1500.0,
                "side": "SELL",   # SHORT
                "entry_time": datetime.now(IST) - timedelta(minutes=60),
            }
        }
    }
    monitor._risk_manager = AsyncMock()
    monitor._notifier = None

    order = MagicMock(spec=Order)
    order.order_id = "PAPER-EXIT-INFY"
    order.symbol = "INFY"
    order.fill_price = Decimal("1450.0")
    order.price = Decimal("1450.0")
    order.exit_type = "TARGET"

    with structlog.testing.capture_logs() as cap_logs:
        await monitor._on_exit_fill(order)

    # on_close called with correct params
    monitor._risk_manager.on_close.assert_called_once_with(
        symbol="INFY",
        exit_price=Decimal("1450.0"),
        exit_reason="TARGET_HIT",
        exit_order_id="PAPER-EXIT-INFY",
    )

    # Zero ghost position_closed from OrderMonitor
    ghost = [e for e in cap_logs if e.get("event") == "position_closed"]
    assert len(ghost) == 0
