"""
TradeOS — Position Sizer (Risk Manager)

Calculates share quantity for each signal based on 1.5% capital-at-risk rule.

FORMULA:
    risk_amount    = capital * risk_per_trade_pct
    risk_per_share = abs(entry_price - stop_loss)
    raw_qty        = floor(risk_amount / risk_per_share)

    Reject if raw_qty < 1 (stop too wide for available capital).
    Cap to floor(capital * 0.40 / entry_price) — single position ≤ 40% capital.

All arithmetic uses Decimal; never float.
"""
from __future__ import annotations

import structlog
from decimal import ROUND_DOWN, Decimal

log = structlog.get_logger()

# 40% single-position capital limit
MAX_POSITION_FRACTION: Decimal = Decimal("0.40")


class PositionSizer:
    """
    Calculates share quantity per signal using percentage-of-capital risk.

    Pure calculator — accepts capital and risk_pct as parameters so tests
    can inject arbitrary values without depending on config structure.
    """

    def calculate(
        self,
        entry_price: Decimal,
        stop_loss: Decimal,
        capital: Decimal,
        risk_pct: Decimal,
    ) -> int | None:
        """
        Calculate share quantity for a signal.

        Args:
            entry_price: Theoretical entry price (Decimal).
            stop_loss:   Stop loss price (Decimal).
            capital:     Total trading capital in ₹ (Decimal).
            risk_pct:    Fraction of capital to risk per trade, e.g. Decimal("0.015").

        Returns:
            Integer quantity, or None if the signal should be rejected
            (stop too wide → raw_qty < 1, or capital constraint → final_qty < 1).
        """
        risk_amount: Decimal = capital * risk_pct
        risk_per_share: Decimal = abs(entry_price - stop_loss)

        if risk_per_share == Decimal("0"):
            log.warning(
                "position_sizer_zero_rps",
                entry=float(entry_price),
                stop=float(stop_loss),
            )
            return None

        raw_qty: int = int(
            (risk_amount / risk_per_share).to_integral_value(rounding=ROUND_DOWN)
        )

        if raw_qty < 1:
            log.debug(
                "position_sizer_reject_stop_too_wide",
                entry=float(entry_price),
                stop=float(stop_loss),
                risk_per_share=float(risk_per_share),
                raw_qty=raw_qty,
            )
            return None

        # 40% capital cap — single position never exceeds 40% of capital
        max_qty: int = int(
            (capital * MAX_POSITION_FRACTION / entry_price).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        final_qty: int = min(raw_qty, max_qty)

        if final_qty < 1:
            log.debug(
                "position_sizer_reject_cap_too_small",
                entry=float(entry_price),
                capital=float(capital),
                max_qty=max_qty,
            )
            return None

        log.debug(
            "position_sizer_calc",
            entry=float(entry_price),
            stop=float(stop_loss),
            risk_amt=float(risk_amount),
            risk_per_share=float(risk_per_share),
            raw_qty=raw_qty,
            final_qty=final_qty,
        )
        return final_qty
