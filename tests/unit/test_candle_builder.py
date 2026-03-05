"""
Unit tests for strategy_engine/candle_builder.py

Tests the 15-minute candle building logic including boundary detection,
VWAP assignment, gap detection, and volume delta calculation.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytz
import pytest

from strategy_engine.candle_builder import Candle, CandleBuilder

IST = pytz.timezone("Asia/Kolkata")


def _ts(h: int, m: int, s: int = 0) -> datetime:
    """Helper: create IST-aware datetime for today at given time."""
    return datetime(2026, 3, 5, h, m, s, tzinfo=IST)


def _tick(
    token: int = 738561,
    symbol: str = "RELIANCE",
    price: float = 2450.0,
    volume: int = 100_000,
    avg: float = 2440.0,
    ts: datetime | None = None,
) -> dict:
    """Build a minimal tick dict."""
    return {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "last_price": price,
        "volume_traded": volume,
        "average_price": avg,
        "exchange_timestamp": ts or _ts(9, 15),
    }


# ---------------------------------------------------------------------------
# test_tick_within_candle_returns_none
# ---------------------------------------------------------------------------

def test_tick_within_candle_returns_none():
    """A tick that does not cross a 15-min boundary should return None."""
    builder = CandleBuilder(738561, "RELIANCE")
    # First tick — starts candle
    result = builder.process_tick(_tick(ts=_ts(9, 15)))
    assert result is None

    # Second tick at 09:22 — same candle
    result = builder.process_tick(_tick(ts=_ts(9, 22)))
    assert result is None


# ---------------------------------------------------------------------------
# test_tick_on_boundary_closes_candle
# ---------------------------------------------------------------------------

def test_tick_on_boundary_closes_candle():
    """
    A tick exactly at 09:30:00 must close (and return) the 09:15 candle.
    """
    builder = CandleBuilder(738561, "RELIANCE")
    builder.process_tick(_tick(price=2450.0, ts=_ts(9, 15)))
    builder.process_tick(_tick(price=2460.0, ts=_ts(9, 28)))

    # Boundary tick — should close the 09:15 candle
    candle = builder.process_tick(_tick(price=2455.0, ts=_ts(9, 30, 0)))

    assert candle is not None
    assert candle.symbol == "RELIANCE"
    assert candle.candle_time.hour == 9
    assert candle.candle_time.minute == 15
    assert candle.close == Decimal("2460.0")  # close = last price in the 09:15 candle
    assert candle.high == Decimal("2460.0")
    assert candle.low == Decimal("2450.0")


# ---------------------------------------------------------------------------
# test_tick_on_boundary_starts_new_candle
# ---------------------------------------------------------------------------

def test_tick_on_boundary_starts_new_candle():
    """
    The tick that triggers a boundary close must start a NEW candle at 09:30.
    """
    builder = CandleBuilder(738561, "RELIANCE")
    builder.process_tick(_tick(price=2450.0, ts=_ts(9, 15)))

    # Boundary tick
    builder.process_tick(_tick(price=2455.0, ts=_ts(9, 30)))

    # Next tick in the 09:30 candle
    candle2 = builder.process_tick(_tick(price=2480.0, ts=_ts(9, 45)))

    assert candle2 is not None
    # candle2 is the 09:30 candle
    assert candle2.candle_time.minute == 30
    # Open of 09:30 candle = first tick (2455), close = last (2455)
    assert candle2.open == Decimal("2455.0")


# ---------------------------------------------------------------------------
# test_vwap_taken_from_tick_average_price
# ---------------------------------------------------------------------------

def test_vwap_taken_from_tick_average_price():
    """VWAP in the completed candle equals tick['average_price'] at candle close."""
    builder = CandleBuilder(738561, "RELIANCE")
    builder.process_tick(_tick(price=2450.0, avg=2440.0, ts=_ts(9, 15)))
    builder.process_tick(_tick(price=2455.0, avg=2443.0, ts=_ts(9, 28)))
    # avg_price at close = 2443.0

    # Boundary tick closes 09:15 candle
    candle = builder.process_tick(_tick(price=2460.0, avg=2450.0, ts=_ts(9, 30)))

    assert candle is not None
    # VWAP should be the average_price of the LAST tick in the 09:15 candle (09:28)
    assert candle.vwap == Decimal("2443.0")


# ---------------------------------------------------------------------------
# test_gap_candle_not_synthesised
# ---------------------------------------------------------------------------

def test_gap_candle_not_synthesised(capsys):
    """
    If ticks jump over a 15-min window (gap), no candle is synthesised.
    A WARNING is logged per missed boundary, and only one candle is returned.
    """
    builder = CandleBuilder(738561, "RELIANCE")
    builder.process_tick(_tick(ts=_ts(9, 15)))    # start 09:15 candle
    builder.process_tick(_tick(ts=_ts(9, 28)))    # still in 09:15

    # Jump to 10:00 — skips 09:30 and 09:45 boundaries
    candle = builder.process_tick(_tick(ts=_ts(10, 0)))

    assert candle is not None   # 09:15 candle is returned
    assert candle.candle_time.minute == 15

    # Two gap warnings: 09:30 and 09:45 (structlog writes to stdout)
    captured = capsys.readouterr()
    gap_count = captured.out.count("gap_candle_detected")
    assert gap_count == 2


# ---------------------------------------------------------------------------
# test_first_tick_of_session_starts_fresh
# ---------------------------------------------------------------------------

def test_first_tick_of_session_starts_fresh():
    """
    The first tick of the session (09:15:00) starts a fresh candle.
    No state from a prior session should carry over (builder is per-session).
    """
    builder = CandleBuilder(738561, "RELIANCE")
    result = builder.process_tick(_tick(price=2500.0, ts=_ts(9, 15, 0)))
    assert result is None  # first tick — candle started, not yet complete

    # The open of this candle should be 2500.0
    # Confirm by completing the candle at the next boundary
    candle = builder.process_tick(_tick(price=2510.0, ts=_ts(9, 30)))
    assert candle is not None
    assert candle.open == Decimal("2500.0")


# ---------------------------------------------------------------------------
# test_candle_boundary_calculation_correct
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("input_time,expected_boundary", [
    ((9, 15, 0),  (9, 15)),   # exactly on boundary → 09:15
    ((9, 22, 30), (9, 15)),   # mid-candle → 09:15
    ((9, 29, 59), (9, 15)),   # last second of 09:15 candle → 09:15
    ((9, 30, 0),  (9, 30)),   # exactly on boundary → 09:30 (new candle)
    ((9, 31, 0),  (9, 30)),   # into 09:30 candle
    ((14, 45, 0), (14, 45)),  # last boundary
    ((14, 59, 59), (14, 45)), # last second of 14:45 candle
    ((15, 0, 0),  (15, 0)),   # 15:00 boundary
])
def test_candle_boundary_calculation_correct(input_time, expected_boundary):
    """_candle_boundary must round down to the correct 15-min slot."""
    builder = CandleBuilder(738561, "RELIANCE")
    h, m, s = input_time
    ts = datetime(2026, 3, 5, h, m, s, tzinfo=IST)
    result = builder._candle_boundary(ts)
    assert result.hour == expected_boundary[0]
    assert result.minute == expected_boundary[1]
    assert result.second == 0
    assert result.microsecond == 0
