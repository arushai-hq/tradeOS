"""
Unit tests for strategy_engine/indicators.py

Tests EMA9/21, RSI14, Volume Ratio, and Swing High/Low calculations.
Uses the `ta` library under the hood — tests verify correctness of output,
not internal implementation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytz

from strategy_engine.candle_builder import Candle
from strategy_engine.indicators import IndicatorEngine, MIN_CANDLES

IST = pytz.timezone("Asia/Kolkata")
BASE_TIME = datetime(2026, 3, 5, 9, 15, tzinfo=IST)


def _candle(
    idx: int,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: int = 10_000,
    vwap: float | None = None,
) -> Candle:
    """Create a synthetic 15-min candle."""
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c + Decimal("5")
    l = Decimal(str(low)) if low is not None else c - Decimal("5")
    v = Decimal(str(vwap)) if vwap is not None else c
    return Candle(
        instrument_token=738561,
        symbol="RELIANCE",
        open=c,
        high=h,
        low=l,
        close=c,
        volume=volume,
        vwap=v,
        candle_time=BASE_TIME + timedelta(minutes=15 * idx),
        session_date=BASE_TIME.date(),
        tick_count=5,
    )


def _make_candles(closes: list[float], volumes: list[int] | None = None) -> list[Candle]:
    """Build a list of synthetic candles from close prices."""
    vols = volumes or [10_000] * len(closes)
    return [_candle(i, c, volume=v) for i, (c, v) in enumerate(zip(closes, vols))]


# ---------------------------------------------------------------------------
# test_returns_none_below_21_candles
# ---------------------------------------------------------------------------

def test_returns_none_below_21_candles():
    """
    IndicatorEngine must return None when fewer than MIN_CANDLES (21) are
    available — not enough history to compute EMA21.
    """
    engine = IndicatorEngine([])
    for i in range(MIN_CANDLES - 1):  # 20 candles
        result = engine.update(_candle(i, 100.0))
    assert result is None


def test_returns_indicators_at_exactly_21_candles():
    """At exactly 21 candles the engine must return an Indicators object."""
    engine = IndicatorEngine([])
    result = None
    for i in range(MIN_CANDLES):
        result = engine.update(_candle(i, 100.0 + i))
    assert result is not None


# ---------------------------------------------------------------------------
# test_ema9_greater_than_ema21_in_uptrend
# ---------------------------------------------------------------------------

def test_ema9_greater_than_ema21_in_uptrend():
    """
    In a clear uptrend (steadily increasing closes), EMA9 should be greater
    than EMA21 because the shorter EMA reacts faster to recent price action.
    """
    # 60 candles with prices 100, 101, ..., 159
    closes = [100.0 + i for i in range(60)]
    engine = IndicatorEngine(_make_candles(closes))
    result = engine.update(_candle(60, 160.0))
    assert result is not None
    assert result.ema9 > result.ema21, (
        f"Expected ema9 ({result.ema9}) > ema21 ({result.ema21}) in uptrend"
    )


# ---------------------------------------------------------------------------
# test_ema9_less_than_ema21_in_downtrend
# ---------------------------------------------------------------------------

def test_ema9_less_than_ema21_in_downtrend():
    """
    In a clear downtrend (steadily decreasing closes), EMA9 should be less
    than EMA21 because the shorter EMA reflects the decline faster.
    """
    # 60 candles with prices 160, 159, ..., 101
    closes = [160.0 - i for i in range(60)]
    engine = IndicatorEngine(_make_candles(closes))
    result = engine.update(_candle(60, 100.0))
    assert result is not None
    assert result.ema9 < result.ema21, (
        f"Expected ema9 ({result.ema9}) < ema21 ({result.ema21}) in downtrend"
    )


# ---------------------------------------------------------------------------
# test_rsi_between_0_and_100
# ---------------------------------------------------------------------------

def test_rsi_between_0_and_100():
    """RSI must always be in [0, 100] — a mathematical invariant."""
    closes = [100.0 + (i % 5) * 2 for i in range(60)]  # oscillating prices
    engine = IndicatorEngine(_make_candles(closes))
    result = engine.update(_candle(60, 108.0))
    assert result is not None
    assert Decimal("0") <= result.rsi <= Decimal("100"), (
        f"RSI out of range: {result.rsi}"
    )


# ---------------------------------------------------------------------------
# test_volume_ratio_calculation_correct
# ---------------------------------------------------------------------------

def test_volume_ratio_calculation_correct():
    """
    volume_ratio = current_candle_volume / 20-period rolling mean.
    With constant volume, the ratio should be approximately 1.0.
    """
    candles = _make_candles([100.0] * 60, volumes=[10_000] * 60)
    engine = IndicatorEngine(candles)
    result = engine.update(_candle(60, 100.0, volume=10_000))
    assert result is not None
    assert abs(float(result.volume_ratio) - 1.0) < 0.01, (
        f"Expected volume_ratio ≈ 1.0 with constant volume, got {result.volume_ratio}"
    )


def test_volume_ratio_doubles_when_volume_doubles():
    """When current volume is 2x the 20-period average, ratio ≈ 2.0."""
    candles = _make_candles([100.0] * 60, volumes=[10_000] * 60)
    engine = IndicatorEngine(candles)
    result = engine.update(_candle(60, 100.0, volume=20_000))
    assert result is not None
    assert abs(float(result.volume_ratio) - 2.0) < 0.1, (
        f"Expected volume_ratio ≈ 2.0, got {result.volume_ratio}"
    )


# ---------------------------------------------------------------------------
# test_swing_low_uses_last_5_candles_only
# ---------------------------------------------------------------------------

def test_swing_low_uses_last_5_candles_only():
    """
    Swing low must be the minimum low of the last 5 candles only —
    NOT the historical minimum.
    """
    # Build 60 candles where earlier candles have very low lows
    candles = []
    for i in range(55):
        # First 55 candles have low of 50.0 (very low — must NOT appear in swing)
        candles.append(_candle(i, close=100.0, low=50.0))

    # Last 5 candles have a higher but known low of 90.0
    for i in range(55, 60):
        candles.append(_candle(i, close=100.0, low=90.0))

    engine = IndicatorEngine(candles)
    result = engine.update(_candle(60, 100.0, low=92.0))
    assert result is not None
    # Swing low uses last 5 candles (lows: 90, 90, 90, 90, 90 + 92 from current = last 5 of deque)
    # The current candle (low=92) and previous 4 (low=90) → min = 90
    assert result.swing_low == Decimal("90"), (
        f"swing_low should be 90 (last 5 candles), got {result.swing_low}"
    )


# ---------------------------------------------------------------------------
# test_swing_high_uses_last_5_candles_only
# ---------------------------------------------------------------------------

def test_swing_high_uses_last_5_candles_only():
    """
    Swing high must be the maximum high of the last 5 candles only —
    NOT the historical maximum.
    """
    # Build 60 candles where earlier candles have very high highs
    candles = []
    for i in range(55):
        # First 55 candles have high of 500.0 (very high — must NOT appear in swing)
        candles.append(_candle(i, close=100.0, high=500.0))

    # Last 5 candles have a lower but known high of 110.0
    for i in range(55, 60):
        candles.append(_candle(i, close=100.0, high=110.0))

    engine = IndicatorEngine(candles)
    result = engine.update(_candle(60, 100.0, high=108.0))
    assert result is not None
    # Swing high: last 5 candles → max of (110, 110, 110, 110, 108) = 110
    assert result.swing_high == Decimal("110"), (
        f"swing_high should be 110 (last 5 candles), got {result.swing_high}"
    )
