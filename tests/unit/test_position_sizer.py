"""
Unit tests for risk_manager.position_sizer.PositionSizer.

Section A — Core mechanics (updated for slot-based sizing):
  test_risk_based_sizing_normal
  test_stop_too_wide_returns_none
  test_zero_stop_distance_returns_none
  test_slot_capital_cap_scales_down
  test_slot_too_small_returns_none
  test_qty_always_floor_not_rounded
  test_long_and_short_same_formula
  test_decimal_arithmetic_no_float

Section B — 7 slot-based sizing scenarios:
  test_slot_normal_case
  test_slot_scale_down
  test_slot_risk_floor_reject
  test_slot_position_value_reject
  test_slot_capital_auto_adjusts
  test_slot_edge_one_share
  test_slot_cheap_stock_scale_down
"""
from __future__ import annotations

from decimal import Decimal

from core.risk_manager.position_sizer import PositionSizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Scenario D: ₹5L total, 70% S1 allocation, 4 slots → ₹87,500 per slot
SLOT_CAPITAL = Decimal("87500")
RISK_PCT = Decimal("0.015")   # 1.5%

# Derived: risk_amount = 87500 * 0.015 = 1312.50
RISK_AMT = SLOT_CAPITAL * RISK_PCT  # = 1312.50


def sizer() -> PositionSizer:
    return PositionSizer()


# ===========================================================================
# Section A — Core mechanics
# ===========================================================================

def test_risk_based_sizing_normal():
    """
    slot_capital=87500, entry=1500, stop=1470 (rps=30)
    risk_amount=1312.5, shares=floor(1312.5/30)=43
    capital_needed=43*1500=64500 < 87500 → no scale-down
    actual_risk=43*30=1290 ≥ 1000, position_value=64500 ≥ 15000 → OK
    """
    qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 43


def test_stop_too_wide_returns_none():
    """
    entry=10000, stop=1 → rps=9999, risk_amt/rps = 1312.5/9999 ≈ 0.13 → 0 → None
    """
    qty = sizer().calculate(
        entry_price=Decimal("10000"),
        stop_loss=Decimal("1"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_zero_stop_distance_returns_none():
    """entry == stop → stop_distance=0 → None (would be division by zero)."""
    qty = sizer().calculate(
        entry_price=Decimal("2500"),
        stop_loss=Decimal("2500"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_slot_capital_cap_scales_down():
    """
    slot_capital=87500, entry=2000, stop=1970 (rps=30)
    risk_amount=1312.5, shares=floor(1312.5/30)=43
    capital_needed=43*2000=86000 < 87500 → no scale-down → 43

    Now with tighter stop: entry=2000, stop=1995 (rps=5)
    shares=floor(1312.5/5)=262
    capital_needed=262*2000=524000 > 87500 → scale-down!
    new shares=floor(87500/2000)=43
    actual_risk=43*5=215 < 1000 → rejected (risk floor)
    """
    # No scale-down case
    qty1 = sizer().calculate(
        entry_price=Decimal("2000"),
        stop_loss=Decimal("1970"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty1 == 43

    # Scale-down + risk floor rejection
    qty2 = sizer().calculate(
        entry_price=Decimal("2000"),
        stop_loss=Decimal("1995"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty2 is None


def test_slot_too_small_returns_none():
    """
    Entry price exceeds slot_capital → even 1 share won't fit.
    entry=100000 > slot_capital=87500 → floor(87500/100000)=0 → None
    """
    qty = sizer().calculate(
        entry_price=Decimal("100000"),
        stop_loss=Decimal("90000"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_qty_always_floor_not_rounded():
    """
    rps=7 → risk_amt/rps = 1312.5/7 = 187.5 → floor = 187
    capital_needed=187*100=18700 < 87500, actual_risk=187*7=1309 ≥ 1000
    """
    qty = sizer().calculate(
        entry_price=Decimal("100"),
        stop_loss=Decimal("93"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 187


def test_long_and_short_same_formula():
    """
    PositionSizer has no direction parameter — formula is symmetric.
    Same entry price, same rps → same qty regardless of stop side.
    """
    long_qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    short_qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1530"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert long_qty == short_qty == 43


def test_decimal_arithmetic_no_float():
    """
    Inputs are Decimal; output is int.
    entry=2000, stop=1970, slot_capital=87500 → shares=43
    """
    entry = Decimal("2000")
    stop = Decimal("1970")

    qty = sizer().calculate(entry, stop, SLOT_CAPITAL, RISK_PCT)

    assert qty == 43
    assert isinstance(qty, int)


# ===========================================================================
# Section B — 7 slot-based sizing scenarios
# ===========================================================================

def test_slot_normal_case():
    """
    Typical NSE mid-cap: HCLTECH at ₹1500, stop ₹1470.
    slot_capital = ₹87,500 (500000 × 0.70 ÷ 4)
    risk_amount = 87500 × 0.015 = ₹1,312.50
    shares = floor(1312.5 / 30) = 43
    capital_needed = 43 × 1500 = ₹64,500 (< slot_capital)
    actual_risk = 43 × 30 = ₹1,290 (≥ ₹1,000 floor)
    position_value = ₹64,500 (≥ ₹15,000 floor)
    """
    qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 43
    assert qty * 1500 == 64500  # within slot


def test_slot_scale_down():
    """
    Scale-down: shares × entry > slot_capital → reduce shares.
    entry=1000, stop=986 (rps=14)
    risk_amount=1312.5, shares=floor(1312.5/14)=93
    capital_needed=93*1000=93000 > 87500 → scale-down!
    new shares=floor(87500/1000)=87
    actual_risk=87*14=1218 ≥ 1000 → OK
    position_value=87*1000=87000 ≥ 15000 → OK
    """
    qty = sizer().calculate(
        entry_price=Decimal("1000"),
        stop_loss=Decimal("986"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 87
    assert qty * 1000 <= int(SLOT_CAPITAL)


def test_slot_risk_floor_reject():
    """
    After scale-down, actual_risk < ₹1,000 → reject.
    entry=5000, stop=4990 (rps=10)
    shares=floor(1312.5/10)=131
    capital_needed=131*5000=655000 > 87500 → scale-down!
    new shares=floor(87500/5000)=17
    actual_risk=17*10=170 < 1000 → REJECTED
    """
    qty = sizer().calculate(
        entry_price=Decimal("5000"),
        stop_loss=Decimal("4990"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_slot_position_value_reject():
    """
    Position value < ₹15,000 → reject.
    entry=100, stop=60 (rps=40)
    shares=floor(1312.5/40)=32
    capital_needed=32*100=3200 < 87500 → no scale-down
    actual_risk=32*40=1280 ≥ 1000 → OK
    position_value=3200 < 15000 → REJECTED
    """
    qty = sizer().calculate(
        entry_price=Decimal("100"),
        stop_loss=Decimal("60"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty is None


def test_slot_capital_auto_adjusts():
    """
    Changing slot_capital (via allocation or max_positions) auto-adjusts sizing.

    Same stock (entry=1500, stop=1470, rps=30):
    - slot_capital=87500  → risk=1312.5 → shares=43 (actual_risk=1290)
    - slot_capital=75000  → risk=1125   → shares=37 (actual_risk=1110)
    - slot_capital=175000 → risk=2625   → shares=87 (actual_risk=2610)
    """
    base_qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=Decimal("87500"),   # 500k × 0.70 / 4
        risk_pct=RISK_PCT,
    )
    assert base_qty == 43

    # Less capital (e.g. 60% allocation, 4 slots → 75000)
    smaller_qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=Decimal("75000"),   # 500k × 0.60 / 4
        risk_pct=RISK_PCT,
    )
    assert smaller_qty == 37
    assert smaller_qty < base_qty

    # More capital (e.g. 70% allocation, 2 slots → 175000)
    larger_qty = sizer().calculate(
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1470"),
        slot_capital=Decimal("175000"),  # 500k × 0.70 / 2
        risk_pct=RISK_PCT,
    )
    assert larger_qty == 87
    assert larger_qty > base_qty


def test_slot_edge_one_share():
    """
    Edge case: exactly 1 share is the minimum viable position.
    entry=50000, stop=48750 (rps=1250)
    shares=floor(1312.5/1250)=1
    capital_needed=1*50000=50000 < 87500 → no scale-down
    actual_risk=1*1250=1250 ≥ 1000 → OK
    position_value=50000 ≥ 15000 → OK
    """
    qty = sizer().calculate(
        entry_price=Decimal("50000"),
        stop_loss=Decimal("48750"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 1


def test_slot_cheap_stock_scale_down():
    """
    Cheap stock with moderate stop triggers scale-down but passes viability.
    entry=250, stop=247 (rps=3)
    shares=floor(1312.5/3)=437
    capital_needed=437*250=109250 > 87500 → scale-down!
    new shares=floor(87500/250)=350
    actual_risk=350*3=1050 ≥ 1000 → OK
    position_value=350*250=87500 ≥ 15000 → OK
    """
    qty = sizer().calculate(
        entry_price=Decimal("250"),
        stop_loss=Decimal("247"),
        slot_capital=SLOT_CAPITAL,
        risk_pct=RISK_PCT,
    )
    assert qty == 350
    assert qty * 250 <= int(SLOT_CAPITAL)
