"""
TradeOS — Slot-Based Position Sizer (Risk Manager)

3-layer calculation:
  Layer 1 (Risk sizing):   shares = floor(risk_amount / stop_distance)
  Layer 2 (Capital cap):   if shares × entry > slot_capital → scale down
  Layer 3 (Viability):     reject if actual_risk < min_risk_floor
                           reject if position_value < min_position_value

FORMULA:
    slot_capital   = (total_capital × strategy_allocation) ÷ max_positions
    risk_amount    = slot_capital × max_loss_per_trade_pct
    stop_distance  = abs(entry_price - stop_loss)
    shares         = floor(risk_amount / stop_distance)

    Scale-down:    if shares × entry > slot_capital → shares = floor(slot_capital / entry)
    Viability:     reject if shares × stop_distance < ₹1,000 (min_risk_floor)
                   reject if shares × entry < ₹15,000 (min_position_value)

All arithmetic uses Decimal; never float.
"""
from __future__ import annotations

import structlog
from decimal import ROUND_DOWN, Decimal

log = structlog.get_logger()

DEFAULT_MIN_RISK_FLOOR: Decimal = Decimal("1000")
DEFAULT_MIN_POSITION_VALUE: Decimal = Decimal("15000")


class PositionSizer:
    """
    Slot-based position sizer using 3-layer calculation.

    Pure calculator — accepts slot_capital and risk_pct as parameters so tests
    can inject arbitrary values without depending on config structure.
    """

    def calculate(
        self,
        entry_price: Decimal,
        stop_loss: Decimal,
        slot_capital: Decimal,
        risk_pct: Decimal,
        min_risk_floor: Decimal = DEFAULT_MIN_RISK_FLOOR,
        min_position_value: Decimal = DEFAULT_MIN_POSITION_VALUE,
    ) -> int | None:
        """
        Calculate share quantity for a signal using 3-layer slot-based sizing.

        Args:
            entry_price:        Theoretical entry price (Decimal).
            stop_loss:          Stop loss price (Decimal).
            slot_capital:       Pre-reserved capital per slot in ₹ (Decimal).
                                Computed as: (total × s1_allocation) ÷ max_positions.
            risk_pct:           Fraction of slot_capital to risk, e.g. Decimal("0.015").
            min_risk_floor:     Minimum actual risk per trade in ₹ (default ₹1,000).
            min_position_value: Minimum position value in ₹ (default ₹15,000).

        Returns:
            Integer quantity, or None if the signal should be rejected.
        """
        # --- Layer 1: Risk-based sizing ---
        risk_amount: Decimal = slot_capital * risk_pct
        stop_distance: Decimal = abs(entry_price - stop_loss)

        if stop_distance == Decimal("0"):
            log.warning(
                "position_sizer_zero_stop_distance",
                entry=float(entry_price),
                stop=float(stop_loss),
            )
            return None

        shares: int = int(
            (risk_amount / stop_distance).to_integral_value(rounding=ROUND_DOWN)
        )

        if shares < 1:
            log.debug(
                "position_sizer_reject_stop_too_wide",
                entry=float(entry_price),
                stop=float(stop_loss),
                stop_distance=float(stop_distance),
                risk_amount=float(risk_amount),
            )
            return None

        # --- Layer 2: Capital cap — scale down if needed ---
        capital_needed: Decimal = Decimal(str(shares)) * entry_price
        scaled_down: bool = False

        if capital_needed > slot_capital:
            shares = int(
                (slot_capital / entry_price).to_integral_value(rounding=ROUND_DOWN)
            )
            capital_needed = Decimal(str(shares)) * entry_price
            scaled_down = True

            if shares < 1:
                log.debug(
                    "position_sizer_reject_slot_too_small",
                    entry=float(entry_price),
                    slot_capital=float(slot_capital),
                )
                return None

        # --- Layer 3: Viability checks ---
        actual_risk: Decimal = Decimal(str(shares)) * stop_distance

        if actual_risk < min_risk_floor:
            log.debug(
                "position_sizer_reject_risk_floor",
                entry=float(entry_price),
                stop=float(stop_loss),
                shares=shares,
                actual_risk=float(actual_risk),
                min_risk_floor=float(min_risk_floor),
                scaled_down=scaled_down,
            )
            return None

        position_value: Decimal = capital_needed

        if position_value < min_position_value:
            log.debug(
                "position_sizer_reject_position_value",
                entry=float(entry_price),
                shares=shares,
                position_value=float(position_value),
                min_position_value=float(min_position_value),
            )
            return None

        log.debug(
            "position_sizer_calc",
            entry=float(entry_price),
            stop=float(stop_loss),
            slot_capital=float(slot_capital),
            risk_amount=float(risk_amount),
            stop_distance=float(stop_distance),
            shares=shares,
            capital_needed=float(capital_needed),
            actual_risk=float(actual_risk),
            scaled_down=scaled_down,
        )
        return shares
