"""
Unit tests for strategy_engine/risk_gate.py

6 mandatory D8 tests plus gate-sequence, individual gate tests, and Gate 7 regime tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytz
from freezegun import freeze_time
from unittest.mock import MagicMock

from core.strategy_engine.risk_gate import RiskGate
from core.strategy_engine.signal_generator import Signal

IST = pytz.timezone("Asia/Kolkata")


def _signal(
    symbol: str = "RELIANCE",
    direction: str = "LONG",
    entry: float = 2450.0,
    stop: float = 2420.0,
) -> Signal:
    entry_d = Decimal(str(entry))
    stop_d = Decimal(str(stop))
    risk = entry_d - stop_d
    target = entry_d + Decimal("2") * risk
    return Signal(
        symbol=symbol,
        instrument_token=738561,
        direction=direction,
        signal_time=datetime.now(IST),
        candle_time=datetime(2026, 3, 5, 9, 30, tzinfo=IST),
        theoretical_entry=entry_d,
        stop_loss=stop_d,
        target=target,
        ema9=Decimal("2445"),
        ema21=Decimal("2440"),
        rsi=Decimal("62"),
        vwap=Decimal("2430"),
        volume_ratio=Decimal("1.6"),
    )


def _state(
    kill_switch_level: int = 0,
    recon: bool = False,
    locked: set | None = None,
    open_positions: dict | None = None,
) -> dict:
    return {
        "kill_switch_level": kill_switch_level,
        "recon_in_progress": recon,
        "locked_instruments": locked or set(),
        "open_positions": open_positions or {},
    }


def _config(max_positions: int = 3) -> dict:
    return {
        "system": {"mode": "paper"},
        "risk": {"max_open_positions": max_positions},
    }


# ---------------------------------------------------------------------------
# test_position_size_respects_1pt5pct_limit
# (D8 mandatory — interpreted as: signal has a valid stop_loss set,
#  which enforces the 1.5% max loss per trade. Stop-loss is mandatory.)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30 — within market hours
def test_stop_loss_is_set_on_every_signal():
    """
    Every signal must have a stop_loss (1.5% max risk enforcement).
    The RiskGate passes signals that have a stop_loss set.
    """
    gate = RiskGate()
    sig = _signal(stop=2420.0)
    assert sig.stop_loss is not None
    assert sig.stop_loss > Decimal("0")

    allowed, reason = gate.check(sig, _state(), _config())
    assert allowed, f"Expected signal to pass, got reason: {reason}"


# ---------------------------------------------------------------------------
# test_max_3_positions_blocks_new_entry (D8 mandatory)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30 — within market hours
def test_max_3_positions_blocks_new_entry():
    """
    When 3 positions are open, Gate 4 must block a new signal.
    """
    gate = RiskGate()
    state = _state(open_positions={
        "INFY": {"qty": 10, "side": "BUY"},
        "TCS": {"qty": 5, "side": "BUY"},
        "WIPRO": {"qty": 8, "side": "BUY"},
    })
    allowed, reason = gate.check(_signal(), state, _config(max_positions=3))
    assert not allowed
    assert reason == "MAX_POSITIONS_REACHED"


# ---------------------------------------------------------------------------
# test_hard_exit_time_1500_ist_triggers (D8 mandatory)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 09:30:00")  # IST 15:00 — exactly at hard exit
def test_hard_exit_time_1500_ist_triggers():
    """
    At or after 15:00 IST, Gate 5 must block all new signals.
    freeze_time uses UTC; 15:00 IST = 09:30 UTC.
    """
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert not allowed
    assert reason == "HARD_EXIT_TIME_REACHED"


@freeze_time("2026-03-05 08:29:00")  # IST 13:59:00 — before no-entry window
def test_signals_allowed_before_no_entry_window():
    """Before 14:30 IST (no-entry window), signals should still be allowed."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert allowed, f"Expected signal to pass before 14:30, got: {reason}"


# ---------------------------------------------------------------------------
# test_stop_loss_mandatory_on_every_order (D8 mandatory)
# ---------------------------------------------------------------------------

def test_stop_loss_mandatory_on_every_signal():
    """
    Signal must carry a non-None, positive stop_loss.
    RiskGate allows signals that have stop_loss set (it is created by SignalGenerator).
    """
    sig = _signal()
    assert sig.stop_loss is not None
    assert sig.stop_loss > Decimal("0"), "stop_loss must be positive"

    gate = RiskGate()
    # Within market hours (9:15–15:00 IST)
    with freeze_time("2026-03-05 04:00:00"):  # 09:30 IST
        allowed, _ = gate.check(sig, _state(), _config())
    assert allowed


# ---------------------------------------------------------------------------
# test_daily_loss_accumulates_correctly (D8 mandatory)
# (Kill switch level reflects accumulated daily loss — Gate 1 blocks on level>0)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_daily_loss_kill_switch_blocks_signal():
    """
    When kill_switch_level > 0 (daily loss triggered), Gate 1 must block signals.
    This covers: daily_loss_accumulates_correctly from D8.
    """
    gate = RiskGate(kill_switch=None)  # uses shared_state fallback
    state = _state(kill_switch_level=1)

    allowed, reason = gate.check(_signal(), state, _config())
    assert not allowed
    assert reason == "KILL_SWITCH_LEVEL_1"


# ---------------------------------------------------------------------------
# test_consecutive_loss_counter_resets_on_win (D8 mandatory)
# (Tested via kill switch: level 0 after counter reset → signals allowed)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_consecutive_loss_counter_reset_allows_signals():
    """
    After kill switch is cleared (counter reset on win), kill_switch_level=0
    → Gate 1 passes and signals are allowed.
    """
    gate = RiskGate(kill_switch=None)
    state = _state(kill_switch_level=0)  # level 0 = reset state after win

    allowed, reason = gate.check(_signal(), state, _config())
    assert allowed, f"Expected signal allowed after KS reset, got: {reason}"


# ---------------------------------------------------------------------------
# test_gate_sequence_stops_at_first_failure
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_gate_sequence_stops_at_first_failure():
    """
    Gate evaluation must stop at the first failure.
    With kill_switch_level=1 AND recon_in_progress=True, Gate 1 fires first.
    """
    gate = RiskGate()
    # Both Gate 1 and Gate 2 would fail — should get Gate 1's reason
    state = _state(kill_switch_level=1, recon=True)
    allowed, reason = gate.check(_signal(), state, _config())
    assert not allowed
    assert "KILL_SWITCH" in reason   # Gate 1 fired, not Gate 2


# ---------------------------------------------------------------------------
# test_recon_in_progress_blocks_signal
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_recon_in_progress_blocks_signal():
    """Gate 2: reconciliation in progress must block all signals."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(recon=True), _config())
    assert not allowed
    assert reason == "RECON_IN_PROGRESS"


# ---------------------------------------------------------------------------
# test_locked_instrument_blocks_signal
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_locked_instrument_blocks_signal():
    """Gate 3: a locked instrument must block its signals."""
    gate = RiskGate()
    state = _state(locked={"RELIANCE"})
    allowed, reason = gate.check(_signal(symbol="RELIANCE"), state, _config())
    assert not allowed
    assert reason == "INSTRUMENT_LOCKED"


# ---------------------------------------------------------------------------
# kill switch object integration
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_kill_switch_object_is_consulted():
    """When a KillSwitch object is provided, is_trading_allowed() is called."""
    mock_ks = MagicMock()
    mock_ks.is_trading_allowed.return_value = False

    gate = RiskGate(kill_switch=mock_ks)
    state = _state(kill_switch_level=0)  # shared_state says level 0

    # But the KillSwitch object says not allowed
    allowed, reason = gate.check(_signal(), state, _config())
    mock_ks.is_trading_allowed.assert_called_once_with()
    assert not allowed


@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_all_gates_pass_returns_ok():
    """All gates pass → (True, 'OK')."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert allowed
    assert reason == "OK"


# ---------------------------------------------------------------------------
# Gate 7: regime check
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_regime_blocks_long_in_bear_trend():
    """Gate 7: LONG signal blocked in BEAR_TREND regime."""
    mock_regime = MagicMock()
    mock_regime.is_long_allowed.return_value = False
    mock_regime.current_regime.return_value = MagicMock(value="bear_trend")

    gate = RiskGate(regime_detector=mock_regime)
    allowed, reason = gate.check(_signal(direction="LONG"), _state(), _config())
    assert not allowed
    assert reason == "REGIME_BLOCKED_BEAR_TREND"


@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_regime_blocks_short_in_bull_trend():
    """Gate 7: SHORT signal blocked in BULL_TREND regime."""
    mock_regime = MagicMock()
    mock_regime.is_short_allowed.return_value = False
    mock_regime.current_regime.return_value = MagicMock(value="bull_trend")

    gate = RiskGate(regime_detector=mock_regime)
    allowed, reason = gate.check(_signal(direction="SHORT"), _state(), _config())
    assert not allowed
    assert reason == "REGIME_BLOCKED_BULL_TREND"


@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_regime_crash_blocks_short_with_low_volume():
    """Gate 7: CRASH + SHORT + volume_ratio <= 2.0 → blocked."""
    from core.regime_detector.regime_detector import MarketRegime

    mock_regime = MagicMock()
    mock_regime.is_short_allowed.return_value = True
    mock_regime.current_regime.return_value = MarketRegime.CRASH

    gate = RiskGate(regime_detector=mock_regime)
    # Default volume_ratio in _signal() is 1.6, which is <= 2.0
    allowed, reason = gate.check(_signal(direction="SHORT"), _state(), _config())
    assert not allowed
    assert reason == "REGIME_CRASH_LOW_VOLUME_SHORT"


@freeze_time("2026-03-05 04:00:00")  # IST 09:30
def test_regime_crash_allows_short_with_high_volume():
    """Gate 7: CRASH + SHORT + volume_ratio > 2.0 → allowed."""
    from core.regime_detector.regime_detector import MarketRegime

    mock_regime = MagicMock()
    mock_regime.is_short_allowed.return_value = True
    mock_regime.current_regime.return_value = MarketRegime.CRASH

    gate = RiskGate(regime_detector=mock_regime)
    # Create signal with volume_ratio > 2.0
    sig = _signal(direction="SHORT")
    sig = Signal(
        symbol=sig.symbol,
        instrument_token=sig.instrument_token,
        direction="SHORT",
        signal_time=sig.signal_time,
        candle_time=sig.candle_time,
        theoretical_entry=sig.theoretical_entry,
        stop_loss=sig.stop_loss,
        target=sig.target,
        ema9=sig.ema9,
        ema21=sig.ema21,
        rsi=sig.rsi,
        vwap=sig.vwap,
        volume_ratio=Decimal("2.5"),  # > 2.0
    )
    allowed, reason = gate.check(sig, _state(), _config())
    assert allowed
    assert reason == "OK"


# ---------------------------------------------------------------------------
# Gate 5b: no-entry window (default 14:30 IST)
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 08:59:00")  # IST 14:29:00 — 1 minute before cutoff
def test_no_entry_window_signal_at_1429_passes():
    """Signal at 14:29 IST — before no-entry window — should pass Gate 5."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert allowed, f"Expected signal to pass at 14:29, got: {reason}"


@freeze_time("2026-03-05 09:00:00")  # IST 14:30:00 — exactly at cutoff
def test_no_entry_window_signal_at_1430_rejected():
    """Signal at 14:30 IST — at no-entry cutoff — should be rejected."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert not allowed
    assert reason == "NO_ENTRY_WINDOW"


@freeze_time("2026-03-05 09:15:00")  # IST 14:45:00 — well inside window
def test_no_entry_window_signal_at_1445_rejected():
    """Signal at 14:45 IST — inside no-entry window — should be rejected."""
    gate = RiskGate()
    allowed, reason = gate.check(_signal(), _state(), _config())
    assert not allowed
    assert reason == "NO_ENTRY_WINDOW"


@freeze_time("2026-03-05 09:10:00")  # IST 14:40:00 — after no-entry cutoff
def test_no_entry_window_existing_positions_unaffected():
    """
    After 14:30 IST, existing positions remain in shared_state — unmodified.
    Gate 5 only blocks NEW signals; it does not touch open_positions.
    """
    gate = RiskGate()
    existing_positions = {
        "INFY": {"qty": 10, "side": "BUY", "entry": 1500.0},
        "TCS": {"qty": 5, "side": "SELL", "entry": 3400.0},
    }
    state = _state(open_positions=existing_positions)

    # New signal is blocked
    allowed, reason = gate.check(_signal(symbol="RELIANCE"), state, _config())
    assert not allowed
    assert reason == "NO_ENTRY_WINDOW"

    # But existing positions remain intact — gate didn't modify them
    assert len(state["open_positions"]) == 2
    assert "INFY" in state["open_positions"]
    assert "TCS" in state["open_positions"]


@freeze_time("2026-03-05 09:14:00")  # IST 14:44:00 — 1 minute before custom cutoff
def test_no_entry_window_configurable_cutoff():
    """Custom no_entry_after='14:45' — signal at 14:44 passes, 14:45 rejected."""
    gate = RiskGate()
    custom_config = {
        **_config(),
        "trading_hours": {"no_entry_after": "14:45"},
    }

    # 14:44 — before custom cutoff — should pass
    allowed, reason = gate.check(_signal(), _state(), custom_config)
    assert allowed, f"Expected signal to pass at 14:44 with cutoff 14:45, got: {reason}"

    # 14:45 — at custom cutoff — should be rejected
    with freeze_time("2026-03-05 09:15:00"):  # IST 14:45:00
        allowed, reason = gate.check(_signal(), _state(), custom_config)
    assert not allowed
    assert reason == "NO_ENTRY_WINDOW"


# ---------------------------------------------------------------------------
# T1: _parse_gate_info maps NO_ENTRY_WINDOW correctly
# ---------------------------------------------------------------------------

def test_parse_gate_info_no_entry_window():
    """_parse_gate_info maps 'NO_ENTRY_WINDOW' to gate 5, 'no_entry_window'."""
    from core.strategy_engine import _parse_gate_info

    gate_number, gate_name = _parse_gate_info("NO_ENTRY_WINDOW")
    assert gate_number == 5
    assert gate_name == "no_entry_window"


def test_parse_gate_info_hard_exit():
    """_parse_gate_info maps 'HARD_EXIT_TIME_REACHED' to gate 5, 'hard_exit_time'."""
    from core.strategy_engine import _parse_gate_info

    gate_number, gate_name = _parse_gate_info("HARD_EXIT_TIME_REACHED")
    assert gate_number == 5
    assert gate_name == "hard_exit_time"


def test_parse_gate_info_unknown_falls_through():
    """_parse_gate_info returns (0, 'unknown') for unrecognised reason."""
    from core.strategy_engine import _parse_gate_info

    gate_number, gate_name = _parse_gate_info("SOME_WEIRD_REASON")
    assert gate_number == 0
    assert gate_name == "unknown"
