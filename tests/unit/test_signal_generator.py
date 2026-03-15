"""
Unit tests for strategy_engine/signal_generator.py

10 mandatory S1 tests from D8 layer1-unit-test-catalogue plus
deduplication and RSI boundary parametrized tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytz

from core.strategy_engine.candle_builder import Candle
from core.strategy_engine.indicators import Indicators
from core.strategy_engine.signal_generator import (
    DEFAULT_MIN_STOP_PCT,
    LONG_RSI_MAX,
    LONG_RSI_MIN,
    MIN_VOLUME_RATIO,
    SHORT_RSI_MAX,
    SHORT_RSI_MIN,
    Signal,
    SignalGenerator,
)

IST = pytz.timezone("Asia/Kolkata")
BASE_TIME = datetime(2026, 3, 5, 9, 30, tzinfo=IST)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _candle(close: float = 2450.0, symbol: str = "RELIANCE") -> Candle:
    c = Decimal(str(close))
    return Candle(
        instrument_token=738561,
        symbol=symbol,
        open=c,
        high=c + Decimal("10"),
        low=c - Decimal("10"),
        close=c,
        volume=50_000,
        vwap=Decimal("2430.0"),   # below close → LONG conditions easier to meet
        candle_time=BASE_TIME,
        session_date=BASE_TIME.date(),
        tick_count=10,
    )


def _indicators(
    ema9: float = 2445.0,
    ema21: float = 2440.0,
    rsi: float = 62.0,
    volume_ratio: float = 1.6,
    swing_high: float = 2460.0,
    swing_low: float = 2420.0,
    vwap: float = 2430.0,
) -> Indicators:
    return Indicators(
        ema9=Decimal(str(ema9)),
        ema21=Decimal(str(ema21)),
        rsi=Decimal(str(rsi)),
        volume_ratio=Decimal(str(volume_ratio)),
        swing_high=Decimal(str(swing_high)),
        swing_low=Decimal(str(swing_low)),
        vwap=Decimal(str(vwap)),
        candle_time=BASE_TIME,
        symbol="RELIANCE",
    )


# ---------------------------------------------------------------------------
# LONG signal tests (tests 1-4)
# ---------------------------------------------------------------------------

def test_long_signal_requires_ema9_above_ema21():
    """LONG: no signal when ema9 <= ema21 (bearish cross)."""
    gen = SignalGenerator()
    ind = _indicators(ema9=2430.0, ema21=2440.0)  # ema9 < ema21
    result = gen.evaluate(_candle(), ind)
    assert result is None


def test_long_signal_requires_price_above_vwap():
    """LONG: no signal when candle.close is below VWAP."""
    gen = SignalGenerator()
    # price = 2420 < vwap = 2430
    result = gen.evaluate(_candle(close=2420.0), _indicators(vwap=2430.0))
    assert result is None


def test_long_signal_requires_rsi_between_55_and_70():
    """LONG: signal only when 55 <= RSI <= 70."""
    gen = SignalGenerator()
    # RSI = 54 → no signal
    result = gen.evaluate(_candle(), _indicators(rsi=54.0))
    assert result is None

    # RSI = 71 → no signal
    gen2 = SignalGenerator()
    result = gen2.evaluate(_candle(), _indicators(rsi=71.0))
    assert result is None

    # RSI = 62 → signal
    gen3 = SignalGenerator()
    result = gen3.evaluate(_candle(), _indicators(rsi=62.0))
    assert result is not None
    assert result.direction == "LONG"


def test_long_signal_requires_volume_1pt5x_average():
    """LONG: no signal when volume_ratio < 1.5."""
    gen = SignalGenerator()
    result = gen.evaluate(_candle(), _indicators(volume_ratio=1.4))
    assert result is None

    # Exactly 1.5 → signal
    gen2 = SignalGenerator()
    result = gen2.evaluate(_candle(), _indicators(volume_ratio=1.5))
    assert result is not None
    assert result.direction == "LONG"


# ---------------------------------------------------------------------------
# SHORT signal tests (tests 5-6)
# ---------------------------------------------------------------------------

def _short_candle(close: float = 2420.0) -> Candle:
    """Candle below VWAP (2430) for SHORT conditions."""
    c = Decimal(str(close))
    return Candle(
        instrument_token=738561,
        symbol="RELIANCE",
        open=c,
        high=c + Decimal("5"),
        low=c - Decimal("5"),
        close=c,
        volume=50_000,
        vwap=Decimal("2430.0"),
        candle_time=BASE_TIME,
        session_date=BASE_TIME.date(),
        tick_count=10,
    )


def _short_indicators(
    rsi: float = 38.0,
    volume_ratio: float = 1.6,
    vwap: float = 2430.0,
) -> Indicators:
    return Indicators(
        ema9=Decimal("2435.0"),   # ema9 > ema21 → NOT a SHORT setup; need ema9 < ema21
        ema21=Decimal("2440.0"),  # ema9 < ema21 for SHORT
        rsi=Decimal(str(rsi)),
        volume_ratio=Decimal(str(volume_ratio)),
        swing_high=Decimal("2445.0"),
        swing_low=Decimal("2415.0"),
        vwap=Decimal(str(vwap)),
        candle_time=BASE_TIME,
        symbol="RELIANCE",
    )


def test_short_signal_requires_ema9_below_ema21():
    """SHORT: no signal when ema9 >= ema21."""
    gen = SignalGenerator()
    # ema9 > ema21 → would be LONG territory; with price < vwap no LONG either
    ind = _indicators(ema9=2445.0, ema21=2440.0, vwap=2460.0)  # price 2450 < vwap 2460? no
    # Build a scenario: close < vwap but ema9 > ema21
    candle = _short_candle(close=2420.0)
    result = gen.evaluate(candle, ind)
    # Neither LONG (vwap 2430 > close 2420) nor SHORT (ema9 > ema21) → None
    assert result is None


def test_short_signal_requires_price_below_vwap():
    """SHORT: no signal when candle.close >= VWAP."""
    gen = SignalGenerator()
    # close 2450 >= vwap 2430 → no SHORT
    candle = _candle(close=2450.0)   # vwap = 2430 in candle's vwap field
    ind = _short_indicators(vwap=2430.0)
    # ema9 < ema21 satisfied; rsi in range; volume ok; but close > vwap
    result = gen.evaluate(candle, ind)
    # No SHORT because close > vwap; no LONG because ema9 < ema21
    assert result is None


# ---------------------------------------------------------------------------
# test_no_signal_when_conditions_partially_met
# ---------------------------------------------------------------------------

def test_no_signal_when_conditions_partially_met():
    """
    If only 3 of 4 S1 conditions are met (e.g. good EMA + good RSI + good vol
    but close < VWAP for LONG), no signal is produced.
    """
    gen = SignalGenerator()
    # All LONG conditions except price is below VWAP
    ind = _indicators(
        ema9=2445.0, ema21=2440.0,    # ema9 > ema21 ✓
        rsi=62.0,                      # 55 <= rsi <= 70 ✓
        volume_ratio=1.6,              # >= 1.5 ✓
        vwap=2460.0,                   # price below vwap ✗
    )
    result = gen.evaluate(_candle(close=2450.0), ind)
    assert result is None


# ---------------------------------------------------------------------------
# test_signal_respects_kill_switch_gate (via RiskGate)
# ---------------------------------------------------------------------------

def test_signal_respects_kill_switch_gate():
    """
    When kill_switch_level > 0, the RiskGate must block the signal.
    SignalGenerator generates a valid signal; RiskGate returns (False, reason).
    """
    from core.strategy_engine.risk_gate import RiskGate

    gen = SignalGenerator()
    gate = RiskGate(kill_switch=None)  # fallback to shared_state

    # All LONG conditions met → signal generated
    signal = gen.evaluate(_candle(), _indicators())
    assert signal is not None
    assert signal.direction == "LONG"

    # Kill switch at level 1
    shared_state = {
        "kill_switch_level": 1,
        "recon_in_progress": False,
        "locked_instruments": set(),
        "open_positions": {},
    }
    config = {"system": {"mode": "paper"}, "risk": {"max_open_positions": 3}}

    allowed, reason = gate.check(signal, shared_state, config)
    assert not allowed
    assert "KILL_SWITCH" in reason


# ---------------------------------------------------------------------------
# test_1_to_2_rr_target_calculated_correctly
# ---------------------------------------------------------------------------

def test_1_to_2_rr_target_calculated_correctly():
    """
    LONG: target = entry + 2 * (entry - stop_loss)
    e.g. entry=2450, stop=2420 → risk=30 → target=2450+60=2510
    """
    gen = SignalGenerator()
    candle = Candle(
        instrument_token=738561,
        symbol="RELIANCE",
        open=Decimal("2450"),
        high=Decimal("2460"),
        low=Decimal("2440"),
        close=Decimal("2450"),
        volume=50_000,
        vwap=Decimal("2430"),  # below close
        candle_time=BASE_TIME,
        session_date=BASE_TIME.date(),
        tick_count=10,
    )
    ind = _indicators(
        ema9=2445.0, ema21=2440.0,
        rsi=62.0, volume_ratio=1.6,
        swing_low=2420.0,  # stop = 2420
        vwap=2430.0,
    )
    signal = gen.evaluate(candle, ind)
    assert signal is not None
    assert signal.direction == "LONG"

    entry = signal.theoretical_entry   # 2450
    stop = signal.stop_loss            # 2420
    target = signal.target

    risk = entry - stop                # 30
    expected_target = entry + Decimal("2") * risk  # 2510
    assert target == expected_target, f"Expected {expected_target}, got {target}"


# ---------------------------------------------------------------------------
# test_stop_loss_at_previous_swing_low
# ---------------------------------------------------------------------------

def test_stop_loss_at_previous_swing_low():
    """LONG stop_loss must equal indicators.swing_low when wider than 2% floor."""
    gen = SignalGenerator()
    # swing_low=2390 → distance = 60/2450 = 2.45% > 2% floor → swing stop used as-is
    ind = _indicators(swing_low=2390.0, vwap=2430.0)
    signal = gen.evaluate(_candle(), ind)
    assert signal is not None
    assert signal.stop_loss == Decimal("2390.0")


# ---------------------------------------------------------------------------
# test_signal_dedup_same_direction_same_session
# ---------------------------------------------------------------------------

def test_signal_dedup_same_direction_same_session():
    """
    Second LONG signal for the same symbol in the same session must be dropped.
    Reset state after reset_session() allows a new signal.
    """
    gen = SignalGenerator()
    candle = _candle()
    ind = _indicators()

    # First call → signal generated
    signal1 = gen.evaluate(candle, ind)
    assert signal1 is not None

    # Second call for same symbol+direction → deduplicated
    signal2 = gen.evaluate(candle, ind)
    assert signal2 is None

    # After session reset → signal allowed again
    gen.reset_session()
    signal3 = gen.evaluate(candle, ind)
    assert signal3 is not None


# ---------------------------------------------------------------------------
# test_rsi_boundary_values (parametrized)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rsi,expect_signal", [
    (54.9, False),  # just below LONG_RSI_MIN (55)
    (55.0, True),   # exactly at LONG_RSI_MIN → signal
    (70.0, True),   # exactly at LONG_RSI_MAX → signal
    (70.1, False),  # just above LONG_RSI_MAX
])
def test_rsi_boundary_values(rsi: float, expect_signal: bool):
    """LONG RSI gate: 55 (inclusive) ≤ RSI ≤ 70 (inclusive)."""
    gen = SignalGenerator()
    result = gen.evaluate(_candle(), _indicators(rsi=rsi))
    if expect_signal:
        assert result is not None, f"Expected signal at RSI={rsi}"
        assert result.direction == "LONG"
    else:
        assert result is None, f"Expected no signal at RSI={rsi}"


# ---------------------------------------------------------------------------
# Stop floor tests (minimum stop distance)
# ---------------------------------------------------------------------------

def test_long_swing_stop_wider_than_floor_unchanged():
    """LONG: swing stop already > 2% from entry → no floor override."""
    gen = SignalGenerator()  # default 2% floor
    # close=2450, swing_low=2380 → distance = 70/2450 = 2.86% > 2%
    ind = _indicators(swing_low=2380.0, vwap=2430.0)
    signal = gen.evaluate(_candle(close=2450.0), ind)
    assert signal is not None
    assert signal.stop_loss == Decimal("2380.0")  # swing stop unchanged


def test_long_swing_stop_tighter_than_floor_widened():
    """LONG: swing stop < 2% from entry → floor applied, stop widened down."""
    gen = SignalGenerator()  # default 2% floor
    # close=2450, swing_low=2445 → distance = 5/2450 = 0.20% < 2%
    # floor stop = 2450 * 0.98 = 2401.0
    ind = _indicators(swing_low=2445.0, vwap=2430.0)
    signal = gen.evaluate(_candle(close=2450.0), ind)
    assert signal is not None
    expected_floor = Decimal("2450.0") * (Decimal("1") - Decimal("0.02"))
    assert signal.stop_loss == expected_floor  # 2401.00
    assert signal.stop_loss < Decimal("2445.0")  # widened (lower)


def test_short_swing_stop_wider_than_floor_unchanged():
    """SHORT: swing stop already > 2% from entry → no floor override."""
    gen = SignalGenerator()  # default 2% floor
    # close=2420, swing_high=2500 → distance = 80/2420 = 3.31% > 2%
    ind = _short_indicators()
    ind = Indicators(
        ema9=Decimal("2435.0"), ema21=Decimal("2440.0"),
        rsi=Decimal("45"), volume_ratio=Decimal("1.6"),
        swing_high=Decimal("2500.0"), swing_low=Decimal("2415.0"),
        vwap=Decimal("2430.0"), candle_time=BASE_TIME, symbol="RELIANCE",
    )
    signal = gen.evaluate(_short_candle(close=2420.0), ind)
    assert signal is not None
    assert signal.stop_loss == Decimal("2500.0")  # swing stop unchanged


def test_short_swing_stop_tighter_than_floor_widened():
    """SHORT: swing stop < 2% from entry → floor applied, stop widened up."""
    gen = SignalGenerator()  # default 2% floor
    # close=2420, swing_high=2425 → distance = 5/2420 = 0.21% < 2%
    # floor stop = 2420 * 1.02 = 2468.40
    ind = Indicators(
        ema9=Decimal("2435.0"), ema21=Decimal("2440.0"),
        rsi=Decimal("45"), volume_ratio=Decimal("1.6"),
        swing_high=Decimal("2425.0"), swing_low=Decimal("2415.0"),
        vwap=Decimal("2430.0"), candle_time=BASE_TIME, symbol="RELIANCE",
    )
    signal = gen.evaluate(_short_candle(close=2420.0), ind)
    assert signal is not None
    expected_floor = Decimal("2420.0") * (Decimal("1") + Decimal("0.02"))
    assert signal.stop_loss == expected_floor  # 2468.40
    assert signal.stop_loss > Decimal("2425.0")  # widened (higher)


def test_target_recalculated_after_floor_widened_stop():
    """Target uses the widened stop distance for 2R calculation."""
    gen = SignalGenerator()
    # LONG: close=2450, swing_low=2445 (tight) → floor stop = 2401.00
    ind = _indicators(swing_low=2445.0, vwap=2430.0)
    signal = gen.evaluate(_candle(close=2450.0), ind)
    assert signal is not None

    entry = signal.theoretical_entry  # 2450
    stop = signal.stop_loss           # 2401.00
    risk = entry - stop               # 49.00
    expected_target = entry + Decimal("2") * risk  # 2450 + 98 = 2548.00
    assert signal.target == expected_target


def test_config_min_stop_pct_respected():
    """Custom min_stop_pct value overrides default."""
    # Use 5% floor → swing stop at 1% should be overridden
    gen = SignalGenerator(s1_config={"min_stop_pct": 0.05})
    # close=2450, swing_low=2430 → distance = 20/2450 = 0.82% < 5%
    # floor stop = 2450 * 0.95 = 2327.50
    ind = _indicators(swing_low=2430.0, vwap=2430.0)
    candle = _candle(close=2450.0)
    signal = gen.evaluate(candle, ind)
    assert signal is not None
    expected_floor = Decimal("2450.0") * (Decimal("1") - Decimal("0.05"))
    assert signal.stop_loss == expected_floor  # 2327.50


def test_long_floor_stop_is_below_entry():
    """LONG: floor stop must always be below entry price."""
    gen = SignalGenerator()
    ind = _indicators(swing_low=2445.0, vwap=2430.0)
    signal = gen.evaluate(_candle(close=2450.0), ind)
    assert signal is not None
    assert signal.stop_loss < signal.theoretical_entry


def test_short_floor_stop_is_above_entry():
    """SHORT: floor stop must always be above entry price."""
    gen = SignalGenerator()
    ind = Indicators(
        ema9=Decimal("2435.0"), ema21=Decimal("2440.0"),
        rsi=Decimal("45"), volume_ratio=Decimal("1.6"),
        swing_high=Decimal("2425.0"), swing_low=Decimal("2415.0"),
        vwap=Decimal("2430.0"), candle_time=BASE_TIME, symbol="RELIANCE",
    )
    signal = gen.evaluate(_short_candle(close=2420.0), ind)
    assert signal is not None
    assert signal.stop_loss > signal.theoretical_entry


# ---------------------------------------------------------------------------
# S1 config loading tests
# ---------------------------------------------------------------------------

def test_s1_config_loads_all_defaults_when_empty():
    """SignalGenerator() with no config uses all current code defaults."""
    gen = SignalGenerator()
    assert gen._rsi_long_min == Decimal("55")
    assert gen._rsi_long_max == Decimal("70")
    assert gen._rsi_short_min == Decimal("45")
    assert gen._volume_ratio_min == Decimal("1.5")
    assert gen._rr_ratio == Decimal("2")
    assert gen._min_stop_pct == Decimal("0.02")


def test_s1_config_custom_values_override_defaults():
    """Custom config values override all defaults."""
    cfg = {
        "rsi_long_min": 40,
        "rsi_long_max": 80,
        "rsi_short_min": 35,
        "volume_ratio_min": 2.0,
        "rr_ratio": 3.0,
        "min_stop_pct": 0.03,
    }
    gen = SignalGenerator(s1_config=cfg)
    assert gen._rsi_long_min == Decimal("40")
    assert gen._rsi_long_max == Decimal("80")
    assert gen._rsi_short_min == Decimal("35")
    assert gen._volume_ratio_min == Decimal("2.0")
    assert gen._rr_ratio == Decimal("3.0")
    assert gen._min_stop_pct == Decimal("0.03")


def test_s1_config_rr_ratio_3_produces_wider_target():
    """Custom rr_ratio=3 produces a 3R target instead of 2R."""
    gen = SignalGenerator(s1_config={"rr_ratio": 3.0})
    ind = _indicators(swing_low=2390.0, vwap=2430.0)
    signal = gen.evaluate(_candle(close=2450.0), ind)
    assert signal is not None

    entry = signal.theoretical_entry  # 2450
    stop = signal.stop_loss           # 2390 (wider than 2% floor)
    risk = entry - stop               # 60
    expected_target = entry + Decimal("3") * risk  # 2450 + 180 = 2630
    assert signal.target == expected_target


def test_s1_config_missing_key_uses_default():
    """Partial config — missing keys silently fall back to defaults."""
    cfg = {"rr_ratio": 2.5}  # only override one param
    gen = SignalGenerator(s1_config=cfg)
    assert gen._rr_ratio == Decimal("2.5")
    assert gen._rsi_long_min == Decimal("55")  # default
    assert gen._min_stop_pct == Decimal("0.02")  # default


def test_s1_config_loaded_log_emitted():
    """s1_config_loaded log event is emitted at init."""
    import io
    import re
    import structlog

    output = io.StringIO()
    structlog.configure(
        processors=[structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(file=output),
    )
    try:
        SignalGenerator(s1_config={"rr_ratio": 2.5})
        # Strip ANSI escape codes for assertion
        raw = re.sub(r"\x1b\[[0-9;]*m", "", output.getvalue())
        assert "s1_config_loaded" in raw
        assert "rr_ratio=2.5" in raw
        assert "rsi_long_min=55" in raw  # default logged
    finally:
        structlog.reset_defaults()
