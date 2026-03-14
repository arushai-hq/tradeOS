"""
TradeOS — P&L Tracker (Risk Manager)

Tracks positions and P&L in real-time as orders fill and close.
Single source of truth for daily_pnl_pct fed to kill switch via shared_state.

State:
  _open_positions: internal position dict (symbol → position dict)
  _daily_pnl:      accumulated net P&L for this session (₹)

shared_state keys written by this module (D6 contract):
  "open_positions"  — mirror of _open_positions (for order_monitor reads)
  "daily_pnl_pct"   — daily_pnl / capital
  "daily_pnl_rs"    — daily_pnl in ₹

Note: per D6 shared-state-contract, 'open_positions' owner is order_monitor.
In paper mode, RiskManager/PnlTracker updates it as a convenience since
order_monitor is the one that calls on_fill/on_close.
"""
from __future__ import annotations

import copy
import structlog
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytz

from risk_manager.charge_calculator import ChargeCalculator

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")


@dataclass
class TradeResult:
    """Complete record of a closed trade, ready for DB write."""

    symbol: str
    direction: str
    qty: int
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    pnl_pct: Decimal          # net_pnl / (qty * entry_price)
    entry_order_id: str
    exit_order_id: str
    signal_id: int
    entry_time: datetime      # IST-aware, stored at on_fill time


class PnlTracker:
    """
    Real-time P&L tracker. Updated on every fill and position close.

    Writes daily_pnl_pct and open_positions to shared_state after every change.
    """

    def __init__(self, capital: Decimal, shared_state: dict) -> None:
        """
        Args:
            capital:      Total trading capital (₹). Used to compute daily_pnl_pct.
            shared_state: D6 shared state dict (written by this module).
        """
        self._capital: Decimal = capital
        self._shared_state: dict = shared_state
        self._daily_pnl: Decimal = Decimal("0")
        self._open_positions: dict[str, dict] = {}
        self._charge_calc = ChargeCalculator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_fill(
        self,
        symbol: str,
        direction: str,
        qty: int,
        fill_price: Decimal,
        order_id: str,
        signal_id: int,
    ) -> None:
        """
        Record an order fill (position entry).

        Args:
            symbol:     Trading symbol.
            direction:  'LONG' or 'SHORT'.
            qty:        Number of shares filled.
            fill_price: Actual fill price.
            order_id:   Entry order ID from broker.
            signal_id:  DB row ID of the originating signal.
        """
        self._open_positions[symbol] = {
            "direction": direction,
            "qty": qty,
            "entry_price": fill_price,
            "order_id": order_id,
            "signal_id": signal_id,
            "entry_time": datetime.now(IST),
        }
        self._shared_state["open_positions"] = dict(self._open_positions)

        log.info(
            "position_opened",
            symbol=symbol,
            direction=direction,
            qty=qty,
            price=float(fill_price),
        )

    def on_close(
        self,
        symbol: str,
        exit_price: Decimal,
        exit_reason: str,
        exit_order_id: str,
    ) -> TradeResult:
        """
        Close an open position and calculate P&L.

        Args:
            symbol:         Trading symbol to close.
            exit_price:     Actual exit fill price.
            exit_reason:    Reason string (e.g. 'TARGET_HIT', 'STOP_HIT').
            exit_order_id:  Exit order ID from broker.

        Returns:
            TradeResult with full P&L breakdown for DB write by caller.

        Raises:
            KeyError: if symbol has no open position.
        """
        pos = self._open_positions[symbol]
        direction: str = pos["direction"]
        qty: int = pos["qty"]
        entry_price: Decimal = pos["entry_price"]
        entry_order_id: str = pos["order_id"]
        signal_id: int = pos["signal_id"]
        entry_time: datetime = pos["entry_time"]

        # Gross P&L
        if direction == "LONG":
            gross_pnl = (exit_price - entry_price) * Decimal(str(qty))
        else:  # SHORT
            gross_pnl = (entry_price - exit_price) * Decimal(str(qty))

        # Charges
        breakdown = self._charge_calc.calculate(qty, entry_price, exit_price, direction)
        charges = breakdown.total

        net_pnl = gross_pnl - charges

        # Position value (cost basis)
        position_value = Decimal(str(qty)) * entry_price
        pnl_pct = net_pnl / position_value if position_value != Decimal("0") else Decimal("0")

        # Accumulate daily P&L
        self._daily_pnl += net_pnl

        # Update shared_state
        daily_pnl_pct = self._daily_pnl / self._capital
        self._shared_state["daily_pnl_pct"] = float(daily_pnl_pct)
        self._shared_state["daily_pnl_rs"] = float(self._daily_pnl)

        # Remove from open positions
        del self._open_positions[symbol]
        self._shared_state["open_positions"] = dict(self._open_positions)

        log.info(
            "position_closed",
            symbol=symbol,
            direction=direction,
            qty=qty,
            exit_price=float(exit_price),
            exit_reason=exit_reason,
            gross_pnl=float(gross_pnl),
            charges=float(charges),
            net_pnl=float(net_pnl),
            daily_pnl_pct=float(daily_pnl_pct),
        )

        return TradeResult(
            symbol=symbol,
            direction=direction,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_pnl=gross_pnl,
            charges=charges,
            net_pnl=net_pnl,
            pnl_pct=pnl_pct,
            entry_order_id=entry_order_id,
            exit_order_id=exit_order_id,
            signal_id=signal_id,
            entry_time=entry_time,
        )

    def get_daily_pnl_pct(self) -> Decimal:
        """Returns current daily P&L as a fraction of capital (e.g. -0.02 = -2%)."""
        if self._capital == Decimal("0"):
            return Decimal("0")
        return self._daily_pnl / self._capital

    def get_open_positions(self) -> dict:
        """Returns a deep copy of the current open positions dict."""
        return copy.deepcopy(self._open_positions)

    def reset_daily(self) -> None:
        """Reset daily P&L state at session start."""
        self._daily_pnl = Decimal("0")
        self._shared_state["daily_pnl_pct"] = 0.0
        self._shared_state["daily_pnl_rs"] = 0.0
        log.info("pnl_tracker_daily_reset")
