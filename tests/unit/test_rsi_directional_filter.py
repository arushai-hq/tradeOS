"""
TradeOS — Unit tests for B3 RSI directional filter fix.

B3: SHORT signals were firing at RSI ~30 (oversold). The RSI condition
    SHORT_RSI_MIN <= rsi <= SHORT_RSI_MAX checked if RSI IS IN the oversold
    zone [30,45], which is the opposite of a momentum signal. A SHORT momentum
    entry requires RSI to be ABOVE the oversold floor (not exhausted).

Fix: SHORT RSI condition changed from
    SHORT_RSI_MIN <= rsi <= SHORT_RSI_MAX  (RSI in [30,45])
to
    rsi >= SHORT_RSI_MAX                   (RSI >= 45, above oversold zone)

Test cases:
  B3-1  short_rejected_when_rsi_oversold
  B3-2  short_accepted_when_rsi_above_oversold_zone
  B3-2b short_accepted_at_rsi_boundary (RSI exactly at SHORT_RSI_MAX)
  B3-3  long_signals_unaffected_by_fix
  B3-4  session03_bad_short_signals_all_rejected (parametrized)
  B3-5  short_rsi_boundary_parametrized
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
import pytz

from core.strategy_engine.candle_builder import Candle
from core.strategy_engine.indicators import Indicators
from core.strategy_engine.signal_generator import (
    SHORT_RSI_MAX,
    SignalGenerator,
)

IST = pytz.timezone("Asia/Kolkata")
BASE_TIME = datetime(2026, 3, 9, 10, 30, tzinfo=IST)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bearish_candle(close: float = 2420.0, symbol: str = "RELIANCE") -> Candle:
    """Candle with close below VWAP — required for SHORT setups."""
    c = Decimal(str(close))
    return Candle(
        instrument_token=738561,
        symbol=symbol,
        open=c,
        high=c + Decimal("5"),
        low=c - Decimal("5"),
        close=c,
        volume=50_000,
        vwap=Decimal("2430.0"),   # close (2420) < vwap (2430) ✓
        candle_time=BASE_TIME,
        session_date=BASE_TIME.date(),
        tick_count=10,
    )


def _bearish_indicators(rsi: float = 55.0, volume_ratio: float = 1.6) -> Indicators:
    """Bearish indicators: ema9 < ema21 with configurable RSI."""
    return Indicators(
        ema9=Decimal("2430.0"),
        ema21=Decimal("2440.0"),   # ema9 < ema21 → bearish cross ✓
        rsi=Decimal(str(rsi)),
        volume_ratio=Decimal(str(volume_ratio)),
        swing_high=Decimal("2445.0"),
        swing_low=Decimal("2415.0"),
        vwap=Decimal("2430.0"),
        candle_time=BASE_TIME,
        symbol="RELIANCE",
    )


def _bullish_candle(close: float = 2450.0) -> Candle:
    """Candle with close above VWAP — required for LONG setups."""
    c = Decimal(str(close))
    return Candle(
        instrument_token=738561,
        symbol="RELIANCE",
        open=c,
        high=c + Decimal("10"),
        low=c - Decimal("10"),
        close=c,
        volume=50_000,
        vwap=Decimal("2430.0"),   # close (2450) > vwap (2430) ✓
        candle_time=BASE_TIME,
        session_date=BASE_TIME.date(),
        tick_count=10,
    )


def _bullish_indicators(rsi: float = 62.0, volume_ratio: float = 1.6) -> Indicators:
    """Bullish indicators: ema9 > ema21 with configurable RSI."""
    return Indicators(
        ema9=Decimal("2445.0"),
        ema21=Decimal("2440.0"),   # ema9 > ema21 → bullish cross ✓
        rsi=Decimal(str(rsi)),
        volume_ratio=Decimal(str(volume_ratio)),
        swing_high=Decimal("2460.0"),
        swing_low=Decimal("2420.0"),
        vwap=Decimal("2430.0"),
        candle_time=BASE_TIME,
        symbol="RELIANCE",
    )


# ---------------------------------------------------------------------------
# B3-1: SHORT rejected when RSI in oversold zone
# ---------------------------------------------------------------------------

def test_b3_short_rejected_when_rsi_oversold():
    """
    B3-1: SHORT must be rejected when RSI < SHORT_RSI_MAX (oversold zone).
    All other bearish conditions are met — only RSI causes rejection.
    NESTLEIND from Session 03 (RSI 31.5) is the canonical failing case.
    """
    gen = SignalGenerator()
    result = gen.evaluate(_bearish_candle(), _bearish_indicators(rsi=31.5))
    assert result is None, (
        f"RSI 31.5 should be rejected for SHORT (oversold zone, threshold={SHORT_RSI_MAX})"
    )


# ---------------------------------------------------------------------------
# B3-2: SHORT accepted when RSI is above the oversold zone
# ---------------------------------------------------------------------------

def test_b3_short_accepted_when_rsi_above_oversold_zone():
    """
    B3-2: SHORT must be accepted when RSI >= SHORT_RSI_MAX (not oversold).
    All bearish conditions met; RSI shows bearish momentum not yet exhausted.
    """
    gen = SignalGenerator()
    result = gen.evaluate(_bearish_candle(), _bearish_indicators(rsi=55.0))
    assert result is not None, "RSI 55 should generate SHORT signal (above oversold zone)"
    assert result.direction == "SHORT"


def test_b3_short_accepted_at_rsi_boundary():
    """
    B3-2b: SHORT must be accepted at exactly RSI = SHORT_RSI_MAX (45.0).
    Boundary condition — threshold is inclusive.
    """
    gen = SignalGenerator()
    result = gen.evaluate(
        _bearish_candle(),
        _bearish_indicators(rsi=float(SHORT_RSI_MAX)),
    )
    assert result is not None, (
        f"RSI exactly at threshold ({SHORT_RSI_MAX}) should generate SHORT signal"
    )
    assert result.direction == "SHORT"


# ---------------------------------------------------------------------------
# B3-3: LONG signals unaffected by the fix
# ---------------------------------------------------------------------------

def test_b3_long_signals_unaffected_by_fix():
    """
    B3-3: LONG signal logic must be unchanged after the B3 fix.
    RSI 55–70 still generates LONG; RSI outside range still rejected.
    """
    gen = SignalGenerator()

    # RSI 62 — standard LONG signal in momentum zone
    result = gen.evaluate(_bullish_candle(), _bullish_indicators(rsi=62.0))
    assert result is not None
    assert result.direction == "LONG"

    # RSI 54 — just below LONG_RSI_MIN (55), no signal
    gen2 = SignalGenerator()
    result2 = gen2.evaluate(_bullish_candle(), _bullish_indicators(rsi=54.0))
    assert result2 is None, "RSI 54 must not generate LONG (below LONG_RSI_MIN=55)"

    # RSI 71 — just above LONG_RSI_MAX (70), no signal
    gen3 = SignalGenerator()
    result3 = gen3.evaluate(_bullish_candle(), _bullish_indicators(rsi=71.0))
    assert result3 is None, "RSI 71 must not generate LONG (above LONG_RSI_MAX=70)"


# ---------------------------------------------------------------------------
# B3-4: Session 03 bad SHORT signals must all be rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("symbol,rsi", [
    ("NESTLEIND",  31.50),   # fired in Session 03 — must now be rejected
    ("HINDUNILVR", 30.75),   # fired in Session 03 — must now be rejected
    ("KOTAKBANK",  30.19),   # fired in Session 03 — must now be rejected
    ("TCS",        41.55),   # fired in Session 03 — must now be rejected (RSI < 45)
])
def test_b3_session03_shorts_rejected(symbol: str, rsi: float):
    """
    B3-4: All four SHORT signals from Session 03 with RSI < SHORT_RSI_MAX
    must be rejected after the B3 fix.
    """
    gen = SignalGenerator()
    candle = _bearish_candle(symbol=symbol)
    ind = _bearish_indicators(rsi=rsi)
    result = gen.evaluate(candle, ind)
    assert result is None, (
        f"{symbol} SHORT at RSI={rsi} must be rejected "
        f"(threshold: RSI >= {SHORT_RSI_MAX})"
    )


# ---------------------------------------------------------------------------
# B3-5: SHORT RSI boundary — parametrized
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rsi,expect_short", [
    (29.9,  False),   # deeply oversold — reject
    (30.0,  False),   # old SHORT_RSI_MIN boundary — still reject after fix
    (44.9,  False),   # just below SHORT_RSI_MAX threshold — reject
    (45.0,  True),    # exactly at SHORT_RSI_MAX — accept (inclusive)
    (55.0,  True),    # clear bearish momentum zone — accept
    (70.0,  True),    # high RSI with bearish structure — accept
])
def test_b3_short_rsi_boundary_parametrized(rsi: float, expect_short: bool):
    """
    B3-5: Parametrized boundary test for corrected SHORT RSI condition.
    RSI < SHORT_RSI_MAX (45) → reject. RSI >= SHORT_RSI_MAX → accept.
    """
    gen = SignalGenerator()
    result = gen.evaluate(_bearish_candle(), _bearish_indicators(rsi=rsi))
    if expect_short:
        assert result is not None, (
            f"RSI={rsi} should generate SHORT signal (>= {SHORT_RSI_MAX})"
        )
        assert result.direction == "SHORT"
    else:
        assert result is None, (
            f"RSI={rsi} should be rejected for SHORT (threshold={SHORT_RSI_MAX})"
        )
