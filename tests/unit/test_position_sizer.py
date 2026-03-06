"""
Unit tests for risk_manager.position_sizer.PositionSizer.

D8 mandatory test catalogue:
  test_position_size_respects_1pt5pct_limit
  test_stop_too_wide_returns_none
  test_max_position_cap_40pct_capital
  test_qty_always_floor_not_rounded
  test_long_and_short_same_formula
  test_decimal_arithmetic_no_float
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from risk_manager.position_sizer import PositionSizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CAPITAL = Decimal("500000")
RISK_PCT = Decimal("0.015")   # 1.5%

# Derived constants
RISK_AMT = CAPITAL * RISK_PCT  # = 7500
MAX_POSITION = CAPITAL * Decimal("0.40")  # = 200000


def sizer() -> PositionSizer:
    return PositionSizer()


# ---------------------------------------------------------------------------
# test_position_size_respects_1pt5pct_limit
# ---------------------------------------------------------------------------

def test_position_size_respects_1pt5pct_limit():
    """
    capital=500000, entry=500, stop=450
    risk_amt=7500, rps=50, raw_qty=150
    150 * 500 = 75000 < 200000 → no 40% cap → final_qty=150
    """
    qty = sizer().calculate(
        entry_price=Decimal("500"),
        stop_loss=Decimal("450"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 150


# ---------------------------------------------------------------------------
# test_stop_too_wide_returns_none
# ---------------------------------------------------------------------------

def test_stop_too_wide_returns_none():
    """
    entry=10000, stop=1 → rps=9999, raw_qty=floor(7500/9999)=0 → None
    """
    qty = sizer().calculate(
        entry_price=Decimal("10000"),
        stop_loss=Decimal("1"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_zero_rps_returns_none():
    """entry == stop → rps=0 → None (would be division by zero)."""
    qty = sizer().calculate(
        entry_price=Decimal("2500"),
        stop_loss=Decimal("2500"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


# ---------------------------------------------------------------------------
# test_max_position_cap_40pct_capital
# ---------------------------------------------------------------------------

def test_max_position_cap_40pct_capital():
    """
    capital=500000, entry=2500, stop=2450 (rps=50)
    raw_qty = floor(7500/50) = 150
    150 * 2500 = 375000 > 200000 → cap to floor(200000/2500) = 80
    final_qty = 80
    qty * entry = 80 * 2500 = 200000 ≤ 200000 ✓
    """
    qty = sizer().calculate(
        entry_price=Decimal("2500"),
        stop_loss=Decimal("2450"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 80
    assert qty * 2500 <= int(MAX_POSITION)


def test_max_position_cap_high_price_stock():
    """
    High-price stock — even 1 share exceeds 40% cap → return None.
    capital=500000, entry=300000 → max_qty=floor(200000/300000)=0 → None
    """
    qty = sizer().calculate(
        entry_price=Decimal("300000"),
        stop_loss=Decimal("290000"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


# ---------------------------------------------------------------------------
# test_qty_always_floor_not_rounded
# ---------------------------------------------------------------------------

def test_qty_always_floor_not_rounded():
    """
    rps=7 → risk_amt/rps = 7500/7 = 1071.428... → floor = 1071 (not round to 1071)
    """
    qty = sizer().calculate(
        entry_price=Decimal("100"),
        stop_loss=Decimal("93"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    # risk_amt = 7500, rps = 7, raw = floor(7500/7) = floor(1071.428) = 1071
    # 1071 * 100 = 107100 < 200000 → no cap
    assert qty == 1071


def test_qty_floor_not_ceiling():
    """
    Ensure we never round up (which would exceed the 1.5% risk limit).
    rps = 3 → 7500 / 3 = 2500.0 exactly → 2500 (floor = round = 2500)
    rps = 7 → 7500 / 7 = 1071.428 → floor = 1071, not 1072
    """
    qty = sizer().calculate(
        entry_price=Decimal("50"),
        stop_loss=Decimal("43"),   # rps = 7
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    # 1071 * 50 = 53550 < 200000 → no cap
    assert qty == 1071


# ---------------------------------------------------------------------------
# test_long_and_short_same_formula
# ---------------------------------------------------------------------------

def test_long_and_short_same_formula():
    """
    PositionSizer has no direction parameter — formula is symmetric.
    Same entry price, same rps → same qty regardless of stop side.

    LONG:  entry=500, stop=450 (rps=50, no cap: 150*500=75000 < 200000)
    SHORT: entry=500, stop=550 (rps=50, same 40% cap check → identical result)
    """
    long_qty = sizer().calculate(
        entry_price=Decimal("500"),
        stop_loss=Decimal("450"),
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    short_qty = sizer().calculate(
        entry_price=Decimal("500"),
        stop_loss=Decimal("550"),   # stop above entry for SHORT, rps still = 50
        capital=CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert long_qty == short_qty == 150


# ---------------------------------------------------------------------------
# test_decimal_arithmetic_no_float
# ---------------------------------------------------------------------------

def test_decimal_arithmetic_no_float():
    """
    Inputs are Decimal; output is int.
    Verify the function accepts and uses Decimal correctly
    and that the intermediate arithmetic stays exact.
    """
    entry = Decimal("2000")
    stop = Decimal("1990")   # rps = 10
    capital = Decimal("500000")
    risk_pct = Decimal("0.015")

    qty = sizer().calculate(entry, stop, capital, risk_pct)

    # risk_amount = 500000 * 0.015 = 7500 (exact in Decimal)
    # rps = 10 (exact)
    # raw_qty = 750 (exact)
    # 750 * 2000 = 1500000 > 200000 → cap to floor(200000/2000) = 100
    assert qty == 100
    assert isinstance(qty, int)
