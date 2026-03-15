"""
Unit tests for risk_manager.charge_calculator.ChargeCalculator.

All charge components are verified independently against manual calculations.
Rates used (Zerodha MIS NSE equity, 2024):
  Brokerage:    min(₹20, 0.03% per leg)
  STT:          0.025% sell-side only
  Exchange txn: 0.00345% both legs
  SEBI:         ₹10 per crore (= 0.000001 of turnover)
  Stamp duty:   0.003% buy-side only
  GST:          18% of (brokerage + exchange_txn + sebi)
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.risk_manager.charge_calculator import ChargeCalculator, ChargeBreakdown


@pytest.fixture
def calc() -> ChargeCalculator:
    return ChargeCalculator()


# ---------------------------------------------------------------------------
# test_long_trade_charge_breakdown
# ---------------------------------------------------------------------------

def test_long_trade_charge_breakdown(calc: ChargeCalculator):
    """
    qty=100, entry=2500, exit=2600, direction=LONG

    Entry (buy) turnover = 100 * 2500 = 250,000
    Exit (sell) turnover = 100 * 2600 = 260,000
    Total turnover       = 510,000

    Brokerage:
      entry leg: min(20, 0.03% * 250000) = min(20, 75) = 20
      exit leg:  min(20, 0.03% * 260000) = min(20, 78) = 20
      total: 40

    STT (sell-side = exit for LONG):  0.025% * 260000 = 65
    Exchange txn (both):              0.00345% * 510000 = 17.595
    SEBI (₹10/crore of total):        510000 * 0.000001 = 0.51
    Stamp duty (buy-side = entry):    0.003% * 250000 = 7.5
    GST (18% on brokerage+exch+sebi): 18% * (40 + 17.595 + 0.51) = 18% * 58.105 = 10.4589
    Total = 40 + 65 + 17.595 + 0.51 + 7.5 + 10.4589 = 141.0639
    """
    result = calc.calculate(
        qty=100,
        entry_price=Decimal("2500"),
        exit_price=Decimal("2600"),
        direction="LONG",
    )

    assert result.brokerage == Decimal("40")
    assert result.stt == Decimal("65")
    assert result.exchange_txn == Decimal("17.595")
    assert result.sebi == Decimal("0.51")
    assert result.stamp_duty == Decimal("7.5")
    assert result.gst == Decimal("10.4589")

    # total must equal exact sum of components
    expected_total = (
        result.brokerage
        + result.stt
        + result.exchange_txn
        + result.sebi
        + result.stamp_duty
        + result.gst
    )
    assert result.total == expected_total


# ---------------------------------------------------------------------------
# test_short_trade_stt_on_entry_leg
# ---------------------------------------------------------------------------

def test_short_trade_stt_on_entry_leg(calc: ChargeCalculator):
    """
    SHORT: entry = sell leg, exit = buy leg.
    STT charged on entry (sell); stamp duty on exit (buy).

    qty=100, entry=2500, exit=2400, direction=SHORT
    Entry (sell) turnover: 100 * 2500 = 250,000
    Exit (buy) turnover:   100 * 2400 = 240,000

    STT on entry (sell):       0.025% * 250000 = 62.5
    Stamp duty on exit (buy):  0.003% * 240000 = 7.2
    """
    result = calc.calculate(
        qty=100,
        entry_price=Decimal("2500"),
        exit_price=Decimal("2400"),
        direction="SHORT",
    )

    assert result.stt == Decimal("62.5")
    assert result.stamp_duty == Decimal("7.2")

    # total equals sum
    expected_total = (
        result.brokerage
        + result.stt
        + result.exchange_txn
        + result.sebi
        + result.stamp_duty
        + result.gst
    )
    assert result.total == expected_total


# ---------------------------------------------------------------------------
# test_brokerage_capped_at_20_per_leg
# ---------------------------------------------------------------------------

def test_brokerage_capped_at_20_per_leg(calc: ChargeCalculator):
    """
    Brokerage = min(₹20, 0.03% of leg turnover).
    Turnover > ₹66,667 per leg → 0.03% > ₹20 → capped at ₹20.

    qty=100, entry=700, exit=750 (both legs exceed cap)
    Entry: min(20, 0.03% * 70000) = min(20, 21) = 20
    Exit:  min(20, 0.03% * 75000) = min(20, 22.5) = 20
    Total brokerage = 40
    """
    result = calc.calculate(
        qty=100,
        entry_price=Decimal("700"),
        exit_price=Decimal("750"),
        direction="LONG",
    )

    assert result.brokerage == Decimal("40")


def test_brokerage_not_capped_small_trade(calc: ChargeCalculator):
    """
    Small trade: turnover < ₹66,667 per leg → 0.03% is the actual charge.
    qty=10, entry=100, exit=110
    Entry turnover: 1000. 0.03% * 1000 = 0.3 < 20 → brokerage_entry = 0.3
    Exit turnover:  1100. 0.03% * 1100 = 0.33 < 20 → brokerage_exit = 0.33
    Total: 0.63
    """
    result = calc.calculate(
        qty=10,
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        direction="LONG",
    )

    assert result.brokerage == Decimal("0.63")


# ---------------------------------------------------------------------------
# test_gst_on_correct_components
# ---------------------------------------------------------------------------

def test_gst_on_correct_components(calc: ChargeCalculator):
    """
    GST = 18% of (brokerage + exchange_txn + sebi) ONLY.
    NOT applied to STT or stamp duty.
    """
    result = calc.calculate(
        qty=100,
        entry_price=Decimal("2500"),
        exit_price=Decimal("2600"),
        direction="LONG",
    )

    expected_gst_base = result.brokerage + result.exchange_txn + result.sebi
    expected_gst = expected_gst_base * Decimal("0.18")
    assert result.gst == expected_gst


# ---------------------------------------------------------------------------
# test_no_float_in_any_calculation
# ---------------------------------------------------------------------------

def test_no_float_in_any_calculation(calc: ChargeCalculator):
    """
    All fields of ChargeBreakdown must be Decimal instances.
    """
    result = calc.calculate(
        qty=100,
        entry_price=Decimal("2500"),
        exit_price=Decimal("2600"),
        direction="LONG",
    )

    assert isinstance(result.brokerage, Decimal)
    assert isinstance(result.stt, Decimal)
    assert isinstance(result.exchange_txn, Decimal)
    assert isinstance(result.sebi, Decimal)
    assert isinstance(result.stamp_duty, Decimal)
    assert isinstance(result.gst, Decimal)
    assert isinstance(result.total, Decimal)


# ---------------------------------------------------------------------------
# Additional: total == sum of components
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_total_equals_sum_of_components(calc: ChargeCalculator, direction: str):
    """total must equal the exact arithmetic sum of all components."""
    result = calc.calculate(
        qty=50,
        entry_price=Decimal("1500"),
        exit_price=Decimal("1480"),
        direction=direction,
    )

    component_sum = (
        result.brokerage
        + result.stt
        + result.exchange_txn
        + result.sebi
        + result.stamp_duty
        + result.gst
    )
    assert result.total == component_sum
