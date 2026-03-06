"""
TradeOS — Risk Manager

Accounting and enforcement-feed layer between Strategy Engine and Execution Engine.

Two responsibilities only:
  1. ACCOUNTING  — position sizing, P&L, charges, consecutive loss tracking.
  2. ENFORCEMENT — write accurate numbers into shared_state so D1 KillSwitch
                   always has correct data to act on.

Does NOT generate signals.
Does NOT place orders.
Does NOT make kill switch decisions.

Startup sequence (__aenter__):
  1. Initialise PositionSizer with config
  2. Initialise ChargeCalculator
  3. Initialise PnlTracker with capital + shared_state
  4. Initialise LossTracker with shared_state
  5. Call on_session_start() on trackers

Shared state keys written (D6 contract):
  shared_state["daily_pnl_pct"]      ← PnlTracker
  shared_state["daily_pnl_rs"]       ← PnlTracker
  shared_state["consecutive_losses"] ← LossTracker
  shared_state["open_positions"]     ← PnlTracker
"""
from __future__ import annotations

import structlog
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import asyncpg
import pytz

from risk_manager.loss_tracker import LossTracker
from risk_manager.pnl_tracker import PnlTracker, TradeResult
from risk_manager.position_sizer import PositionSizer

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Early warning threshold (before the -3% D1 kill switch trigger)
_DAILY_LOSS_WARNING_PCT: float = -0.02   # -2%


class RiskManager:
    """
    Single interface to the risk accounting layer for the Execution Engine.

    Must be used as an async context manager:
        async with RiskManager(config, shared_state, db_pool) as rm:
            qty = rm.size_position(signal)
            await rm.on_fill(...)
            await rm.on_close(...)
    """

    def __init__(
        self,
        config: dict,
        shared_state: dict,
        db_pool: asyncpg.Pool,
    ) -> None:
        """
        Args:
            config:       Loaded settings.yaml dict.
            shared_state: D6 shared state dict.
            db_pool:      asyncpg connection pool (trades + system_events tables).
        """
        self._config = config
        self._shared_state = shared_state
        self._db_pool = db_pool

        # Extracted at init — avoids repeated dict lookups in hot path
        self._capital: Decimal = Decimal(str(config["capital"]["total"]))
        self._risk_pct: Decimal = Decimal(str(config["risk"]["max_loss_per_trade_pct"]))

        self._session_date: Optional[date] = None
        self._trades_closed: int = 0

        # Components initialised in __aenter__
        self._sizer: Optional[PositionSizer] = None
        self._pnl_tracker: Optional[PnlTracker] = None
        self._loss_tracker: Optional[LossTracker] = None
        self._pnl_warning_sent: bool = False

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "RiskManager":
        """Initialise all Risk Manager components and reset session state."""
        self._session_date = datetime.now(IST).date()
        log.info(
            "risk_manager_starting",
            session_date=str(self._session_date),
            capital=float(self._capital),
        )

        # Step 1: stateless position sizer
        self._sizer = PositionSizer()

        # Step 3: P&L tracker (holds its own ChargeCalculator)
        self._pnl_tracker = PnlTracker(self._capital, self._shared_state)

        # Step 4: loss counter
        self._loss_tracker = LossTracker(self._shared_state)

        # Step 5: reset both for the new session
        self._pnl_tracker.reset_daily()
        self._loss_tracker.on_session_start()
        self._pnl_warning_sent = False

        log.info("risk_manager_ready", session_date=str(self._session_date))
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Log session summary on shutdown."""
        daily_pnl_pct = (
            self._pnl_tracker.get_daily_pnl_pct()
            if self._pnl_tracker
            else Decimal("0")
        )
        log.info(
            "risk_manager_stopped",
            trades_closed=self._trades_closed,
            daily_pnl_pct=float(daily_pnl_pct),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def size_position(self, signal: object) -> int | None:
        """
        Calculate position size for a signal.

        Args:
            signal: Signal object with theoretical_entry and stop_loss attributes.

        Returns:
            Integer quantity, or None if signal should be rejected.
        """
        assert self._sizer is not None, "RiskManager not initialised"
        entry: Decimal = getattr(signal, "theoretical_entry")
        stop: Decimal = getattr(signal, "stop_loss")
        return self._sizer.calculate(entry, stop, self._capital, self._risk_pct)

    async def on_fill(
        self,
        symbol: str,
        direction: str,
        qty: int,
        fill_price: Decimal,
        order_id: str,
        signal_id: int,
    ) -> None:
        """
        Record a filled order (position entry). Delegates to PnlTracker.

        Args:
            symbol:     Trading symbol.
            direction:  'LONG' or 'SHORT'.
            qty:        Filled quantity.
            fill_price: Actual fill price.
            order_id:   Broker order ID.
            signal_id:  DB row ID of the originating signal.
        """
        assert self._pnl_tracker is not None, "RiskManager not initialised"
        self._pnl_tracker.on_fill(symbol, direction, qty, fill_price, order_id, signal_id)

    async def on_close(
        self,
        symbol: str,
        exit_price: Decimal,
        exit_reason: str,
        exit_order_id: str,
    ) -> None:
        """
        Process a position close: P&L, charges, DB write, loss tracking.

        Writes to trades table. Writes system_event WARNING if daily P&L crosses -2%.

        Args:
            symbol:         Trading symbol.
            exit_price:     Actual exit fill price.
            exit_reason:    Reason code (e.g. 'TARGET_HIT', 'STOP_HIT').
            exit_order_id:  Broker exit order ID.
        """
        assert self._pnl_tracker is not None, "RiskManager not initialised"
        assert self._loss_tracker is not None, "RiskManager not initialised"

        result: TradeResult = self._pnl_tracker.on_close(
            symbol, exit_price, exit_reason, exit_order_id
        )
        self._loss_tracker.on_trade_close(result.net_pnl)
        self._trades_closed += 1

        log.info(
            "trade_closed",
            symbol=result.symbol,
            direction=result.direction,
            qty=result.qty,
            exit_reason=result.exit_reason,
            gross_pnl=float(result.gross_pnl),
            charges=float(result.charges),
            net_pnl=float(result.net_pnl),
            pnl_pct=float(result.pnl_pct),
        )

        # DB write — trades table
        await self._write_trade(result)

        # Early warning if daily P&L crosses -2%
        daily_pnl_pct = float(self._pnl_tracker.get_daily_pnl_pct())
        if daily_pnl_pct <= _DAILY_LOSS_WARNING_PCT and not self._pnl_warning_sent:
            self._pnl_warning_sent = True
            await self._write_pnl_warning_event(daily_pnl_pct)

    def on_kill_switch_reset(self) -> None:
        """
        Called after manual kill switch reset. Resets consecutive loss counter.

        Critical gap fix: prevents immediate re-trigger after manual reset.
        """
        assert self._loss_tracker is not None, "RiskManager not initialised"
        self._loss_tracker.on_kill_switch_reset()

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    async def _write_trade(self, result: TradeResult) -> None:
        """INSERT trade into trades table (idempotent on duplicate — no conflict key)."""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades (
                        session_date, symbol, direction, signal_id,
                        entry_order_id, entry_time,
                        actual_entry, theoretical_entry, entry_slippage,
                        qty,
                        exit_order_id, exit_time, actual_exit, exit_reason,
                        gross_pnl, charges, net_pnl, pnl_pct
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                        $11,$12,$13,$14,$15,$16,$17,$18
                    )
                    """,
                    self._session_date,
                    result.symbol,
                    result.direction,
                    result.signal_id if result.signal_id > 0 else None,
                    result.entry_order_id,
                    result.entry_time,
                    float(result.entry_price),   # actual_entry
                    float(result.entry_price),   # theoretical_entry (=actual in paper mode)
                    None,                        # entry_slippage (None in paper mode)
                    result.qty,
                    result.exit_order_id,
                    datetime.now(IST),           # exit_time
                    float(result.exit_price),
                    result.exit_reason,
                    float(result.gross_pnl),
                    float(result.charges),
                    float(result.net_pnl),
                    float(result.pnl_pct),
                )
        except Exception as exc:
            log.error("trade_write_failed", symbol=result.symbol, error=str(exc))

    async def _write_pnl_warning_event(self, daily_pnl_pct: float) -> None:
        """Write WARNING system_event when daily P&L crosses -2%."""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO system_events
                        (session_date, event_time, event_type, level, detail, kill_switch_level)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                    """,
                    self._session_date,
                    datetime.now(IST),
                    "DAILY_LOSS_WARNING",
                    "WARNING",
                    f'{{"daily_pnl_pct": {daily_pnl_pct:.4f}, "threshold": -0.02}}',
                    self._shared_state.get("kill_switch_level", 0),
                )
            log.warning(
                "daily_loss_warning",
                daily_pnl_pct=daily_pnl_pct,
                threshold_pct=-0.02,
            )
        except Exception as exc:
            log.error("pnl_warning_event_write_failed", error=str(exc))
