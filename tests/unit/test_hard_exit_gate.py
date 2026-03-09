"""
TradeOS — Unit tests for B1/B2 hard exit gate fixes.

B1: risk_watchdog must call emergency_exit_all() when hard_exit fires at 15:00
    with open positions. Positions must not be orphaned until EOD shutdown.

B2: StrategyEngine._process_tick() must skip signal evaluation entirely when
    shared_state["accepting_signals"] is False (set by risk_watchdog at 15:00).
    order_queue must receive nothing after hard_exit.

Test cases:
  B2-1  signal_skipped_when_accepting_signals_false
  B2-2  order_queue_empty_after_hard_exit
  B2-3  signal_evaluated_when_accepting_signals_true
  B1-1  risk_watchdog_calls_emergency_exit_all_at_1500_with_positions
  B1-2  risk_watchdog_no_emergency_exit_when_no_positions
  B1-3  risk_watchdog_no_emergency_exit_when_no_exec_engine
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers — build a minimal StrategyEngine with mocked internals
# ---------------------------------------------------------------------------

def _make_engine(accepting_signals: bool):
    """Construct a StrategyEngine bypassing __init__, mocking all dependencies."""
    from strategy_engine import StrategyEngine
    from strategy_engine.candle_builder import Candle

    engine = StrategyEngine.__new__(StrategyEngine)

    # Mock candle — instrument_token must match builder key
    mock_candle = MagicMock(spec=Candle)
    mock_candle.symbol = "RELIANCE"
    mock_candle.instrument_token = 738561
    mock_candle.candle_time = MagicMock()
    mock_candle.candle_time.isoformat.return_value = "2026-03-09T14:45:00+05:30"

    mock_builder = MagicMock()
    mock_builder.process_tick.return_value = mock_candle

    mock_indicators = MagicMock()
    mock_ind_engine = MagicMock()
    mock_ind_engine.update.return_value = mock_indicators

    mock_signal_gen = MagicMock()
    mock_signal_gen.evaluate.return_value = None   # no signal by default

    mock_risk_gate = MagicMock()
    mock_risk_gate.check.return_value = (True, "OK")

    engine._candle_builders = {738561: mock_builder}
    engine._indicator_engines = {738561: mock_ind_engine}
    engine._signal_generator = mock_signal_gen
    engine._risk_gate = mock_risk_gate
    engine._shared_state = {
        "accepting_signals": accepting_signals,
        "signals_generated_today": 0,
    }
    engine._order_queue = asyncio.Queue()
    engine._signals_generated = 0

    return engine


def _tick() -> dict:
    """Minimal validated tick dict."""
    return {
        "instrument_token": 738561,
        "last_price": 2000.0,
        "volume_traded": 5000,
        "exchange_timestamp": None,
    }


# ---------------------------------------------------------------------------
# B2 — accepting_signals gate in _process_tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b2_signal_skipped_when_accepting_signals_false():
    """
    B2-1: _process_tick must not call signal_generator.evaluate when
    accepting_signals=False. No signal must escape to order_queue.
    """
    engine = _make_engine(accepting_signals=False)

    with patch.object(engine, "_write_candle", new=AsyncMock()):
        await engine._process_tick(_tick())

    engine._signal_generator.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_b2_order_queue_empty_after_hard_exit():
    """
    B2-2: order_queue must remain empty when accepting_signals=False,
    even if signal_generator would have returned a signal.
    """
    from strategy_engine.signal_generator import Signal

    engine = _make_engine(accepting_signals=False)
    # Make evaluate() return a signal — this must never reach order_queue
    engine._signal_generator.evaluate.return_value = MagicMock(spec=Signal)

    with patch.object(engine, "_write_candle", new=AsyncMock()):
        await engine._process_tick(_tick())

    assert engine._order_queue.empty(), (
        "order_queue must be empty — signal evaluation must be skipped "
        "when accepting_signals=False"
    )


@pytest.mark.asyncio
async def test_b2_signal_evaluated_when_accepting_signals_true():
    """
    B2-3: _process_tick must call signal_generator.evaluate when
    accepting_signals=True (normal session state).
    """
    engine = _make_engine(accepting_signals=True)

    with (
        patch.object(engine, "_write_candle", new=AsyncMock()),
        patch.object(engine, "_write_signal", new=AsyncMock()),
    ):
        await engine._process_tick(_tick())

    engine._signal_generator.evaluate.assert_called_once()


# ---------------------------------------------------------------------------
# B1 — risk_watchdog calls emergency_exit_all at 15:00
# ---------------------------------------------------------------------------

def _make_shared_state(open_positions: dict | None = None) -> dict:
    """Build a minimal shared_state dict for risk_watchdog tests."""
    return {
        "system_ready": True,
        "daily_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "kill_switch_level": 0,
        "open_positions": open_positions or {},
        "accepting_signals": True,
        "session_date": None,
        "tick_queue": asyncio.Queue(),
    }


def _time_ist(h: int, m: int) -> datetime:
    """Return a timezone-aware IST datetime at the given hour:minute today."""
    from utils.time_utils import now_ist
    return now_ist().replace(hour=h, minute=m, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_b1_risk_watchdog_calls_emergency_exit_all_at_1500_with_positions():
    """
    B1-1: When risk_watchdog fires at 15:00 and open positions exist,
    exec_engine._exit_manager.emergency_exit_all("hard_exit_1500") must be called.
    """
    from main import risk_watchdog

    mock_exit_manager = AsyncMock()
    mock_exec_engine = MagicMock()
    mock_exec_engine._exit_manager = mock_exit_manager

    shared_state = _make_shared_state(
        open_positions={"RELIANCE": {}, "TCS": {}, "INFY": {}}
    )
    config = {"risk": {"max_daily_loss_pct": 0.03}}
    secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

    t_1500 = _time_ist(15, 0)

    call_count = 0

    async def mock_sleep(_n):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with (
        patch("main.asyncio.sleep", mock_sleep),
        patch("main.now_ist", return_value=t_1500),
    ):
        with pytest.raises(asyncio.CancelledError):
            await risk_watchdog(
                shared_state, config, secrets,
                exec_engine=mock_exec_engine,
                regime_detector=None,
            )

    mock_exit_manager.emergency_exit_all.assert_called_once_with("hard_exit_1500")
    assert shared_state["accepting_signals"] is False


@pytest.mark.asyncio
async def test_b1_risk_watchdog_no_emergency_exit_when_no_positions():
    """
    B1-2: When risk_watchdog fires at 15:00 with no open positions,
    emergency_exit_all must NOT be called.
    """
    from main import risk_watchdog

    mock_exit_manager = AsyncMock()
    mock_exec_engine = MagicMock()
    mock_exec_engine._exit_manager = mock_exit_manager

    shared_state = _make_shared_state(open_positions={})
    config = {"risk": {"max_daily_loss_pct": 0.03}}
    secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

    t_1500 = _time_ist(15, 0)
    call_count = 0

    async def mock_sleep(_n):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with (
        patch("main.asyncio.sleep", mock_sleep),
        patch("main.now_ist", return_value=t_1500),
    ):
        with pytest.raises(asyncio.CancelledError):
            await risk_watchdog(
                shared_state, config, secrets,
                exec_engine=mock_exec_engine,
                regime_detector=None,
            )

    mock_exit_manager.emergency_exit_all.assert_not_called()


@pytest.mark.asyncio
async def test_b1_risk_watchdog_no_emergency_exit_when_no_exec_engine():
    """
    B1-3: When exec_engine is None (e.g. early-stage startup), hard exit at 15:00
    must not raise — position closure is silently skipped.
    """
    from main import risk_watchdog

    shared_state = _make_shared_state(
        open_positions={"RELIANCE": {}}
    )
    config = {"risk": {"max_daily_loss_pct": 0.03}}
    secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

    t_1500 = _time_ist(15, 0)
    call_count = 0

    async def mock_sleep(_n):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError

    with (
        patch("main.asyncio.sleep", mock_sleep),
        patch("main.now_ist", return_value=t_1500),
    ):
        # Must not raise TypeError or AttributeError when exec_engine is None
        with pytest.raises(asyncio.CancelledError):
            await risk_watchdog(
                shared_state, config, secrets,
                exec_engine=None,
                regime_detector=None,
            )

    assert shared_state["accepting_signals"] is False  # flag still set
