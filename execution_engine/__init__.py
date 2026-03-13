"""
TradeOS — Execution Engine

Owns the full order lifecycle from Signal receipt to position close.
The only module that calls kite.place_order().

Startup sequence (__aenter__):
  1. Set system_ready = False
  2. Run startup reconciliation (D2 restart-safety):
     - Fetch kite.orders() from Zerodha
     - Any order_id in Zerodha but NOT in local OSM → mark_unknown() + lock instrument
     - Any order in OSM (non-terminal) NOT in Zerodha → mark EXPIRED
     - If unknown_count == 0 → system_ready = True
  3. Init OrderStateMachine
  4. Init OrderPlacer
  5. Init ExitManager
  6. Init OrderMonitor

Main run loop (run()):
  Two concurrent async tasks:
    Task A: _consume_signals() — reads order_queue, sizes, places entries
    Task B: order_monitor.run() — polls fills every 5s

Exit handling:
  - StrategyEngine calls notify_candle_close() on each candle
  - ExitManager.check_exits() evaluates TARGET/STOP/HARD_EXIT conditions
  - emergency_exit_all() is called on kill switch Level 2 / system shutdown

Shared state keys written (D2/D6 contract):
  shared_state["open_orders"]    ← OrderMonitor
  shared_state["open_positions"] ← ExitManager (via register_position)
  shared_state["fills_today"]    ← OrderMonitor
  shared_state["locked_instruments"] ← mark_unknown() on startup
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import asyncpg
import pytz

from execution_engine.exit_manager import ExitManager
from execution_engine.order_monitor import OrderMonitor
from execution_engine.order_placer import OrderPlacer
from execution_engine.state_machine import OrderState, OrderStateMachine
from strategy_engine.signal_generator import Signal

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


class ExecutionEngine:
    """
    Main Execution Engine: signal → order → fill → close.

    Must be used as an async context manager:
        async with ExecutionEngine(...) as engine:
            await engine.run()

    Args:
        kite:          Authenticated KiteConnect instance.
        config:        Loaded settings.yaml dict.
        shared_state:  D6 shared state dict.
        order_queue:   Source of approved Signal objects (from StrategyEngine).
        risk_manager:  RiskManager for on_fill() / on_close() accounting.
        db_pool:       asyncpg connection pool (for system_events writes).
        kill_switch:   Optional D1 KillSwitch instance.
    """

    def __init__(
        self,
        kite,
        config: dict,
        shared_state: dict,
        order_queue: asyncio.Queue,
        risk_manager,
        db_pool: asyncpg.Pool,
        kill_switch=None,
        notifier=None,
    ) -> None:
        self._kite = kite
        self._config = config
        self._shared_state = shared_state
        self._order_queue = order_queue
        self._risk_manager = risk_manager
        self._db_pool = db_pool
        self._kill_switch = kill_switch
        self._notifier = notifier

        self._session_date: date = datetime.now(IST).date()
        self._system_ready: bool = False
        self._signals_consumed: int = 0

        # Components initialised in __aenter__
        self._osm: Optional[OrderStateMachine] = None
        self._order_placer: Optional[OrderPlacer] = None
        self._exit_manager: Optional[ExitManager] = None
        self._order_monitor: Optional[OrderMonitor] = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ExecutionEngine":
        """Initialise all EE components and run startup reconciliation."""
        log.info(
            "execution_engine_starting",
            session_date=self._session_date.isoformat(),
        )

        # Step 1: Init OSM (shared_state for instrument locking + consecutive_losses)
        self._osm = OrderStateMachine(shared_state=self._shared_state)

        # Step 2: Startup reconciliation (D2 restart-safety)
        self._system_ready = await self._run_startup_reconciliation()
        if not self._system_ready:
            log.critical(
                "execution_engine_startup_blocked",
                reason="unknown_orders_found",
            )
            # Block proceeds but system_ready=False gates order placement in OrderPlacer
        else:
            log.info("execution_engine_reconciliation_passed")

        # Step 3: Init OrderPlacer
        self._order_placer = OrderPlacer(
            kite=self._kite,
            config=self._config,
            osm=self._osm,
            shared_state=self._shared_state,
            kill_switch=self._kill_switch,
        )

        # Step 4: Init ExitManager
        self._exit_manager = ExitManager(
            order_placer=self._order_placer,
            shared_state=self._shared_state,
            config=self._config,
        )

        # Step 5: Init OrderMonitor
        self._order_monitor = OrderMonitor(
            kite=self._kite,
            osm=self._osm,
            shared_state=self._shared_state,
            risk_manager=self._risk_manager,
            exit_manager=self._exit_manager,
            config=self._config,
            notifier=self._notifier,
            db_pool=self._db_pool,
        )

        log.info(
            "execution_engine_ready",
            session_date=self._session_date.isoformat(),
            system_ready=self._system_ready,
        )
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Emergency exit open positions and log session summary on shutdown."""
        if self._exit_manager is not None:
            open_positions = self._exit_manager.get_open_positions()
            if open_positions:
                log.warning(
                    "execution_engine_shutdown_with_open_positions",
                    symbols=list(open_positions.keys()),
                )
                await self._exit_manager.emergency_exit_all("system_shutdown")

        log.info(
            "execution_engine_stopped",
            session_date=self._session_date.isoformat(),
            signals_consumed=self._signals_consumed,
        )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Run signal consumption and order monitoring concurrently.

        Two concurrent tasks:
          Task A: _consume_signals() — reads order_queue, sizes, places entries
          Task B: order_monitor.run() — polls fills every 5s

        CancelledError is not suppressed per D6 rule.
        """
        assert self._order_monitor is not None, "ExecutionEngine not initialised"

        log.info("execution_engine_run_started")
        task_a = asyncio.create_task(
            self._consume_signals(),
            name="ee_consume_signals",
        )
        task_b = asyncio.create_task(
            self._order_monitor.run(),
            name="ee_order_monitor",
        )
        try:
            await asyncio.gather(task_a, task_b)
        except asyncio.CancelledError:
            task_a.cancel()
            task_b.cancel()
            raise

    # ------------------------------------------------------------------
    # Signal consumption
    # ------------------------------------------------------------------

    async def _consume_signals(self) -> None:
        """
        Read signals from order_queue and place entry orders.

        Runs indefinitely until cancelled. CancelledError not suppressed.
        """
        assert self._order_placer is not None
        assert self._risk_manager is not None

        log.info("execution_engine_signal_consumer_started")
        while True:
            try:
                signal: Signal = await self._order_queue.get()
                try:
                    await self._handle_signal(signal)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(
                        "signal_handling_error",
                        symbol=signal.symbol,
                        error=str(exc),
                        exc_info=True,
                    )
                finally:
                    self._order_queue.task_done()
            except asyncio.CancelledError:
                raise  # D6 rule

    async def _handle_signal(self, signal: Signal) -> None:
        """Size and place entry order for a single signal."""
        assert self._order_placer is not None, "ExecutionEngine not initialised — _order_placer is None"
        qty = self._risk_manager.size_position(signal)
        if qty is None:
            log.warning(
                "signal_rejected_by_sizer",
                symbol=signal.symbol,
                direction=signal.direction,
                entry=float(signal.theoretical_entry),
                stop=float(signal.stop_loss),
            )
            # T2+T3: count sizer rejection in heartbeat + send notification
            self._shared_state["signals_rejected_today"] = (
                self._shared_state.get("signals_rejected_today", 0) + 1
            )
            if self._notifier is not None:
                self._notifier.notify_signal_sizer_rejected(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry=float(signal.theoretical_entry),
                    stop=float(signal.stop_loss),
                )
            # D1: Update signal status in DB
            await self._update_signal_status(
                signal.symbol, "REJECTED",
                reject_reason=f"SIZER_REJECTED:{signal.direction}",
            )
            return

        order = await self._order_placer.place_entry(signal, qty)
        if order:
            self._signals_consumed += 1
            # T2+T3: count accepted signal + send notification AFTER sizer passes
            self._shared_state["signals_generated_today"] = (
                self._shared_state.get("signals_generated_today", 0) + 1
            )
            _regime = str(self._shared_state.get("market_regime") or "unknown")
            if self._notifier is not None:
                self._notifier.notify_signal_accepted(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry=float(signal.theoretical_entry),
                    stop=float(signal.stop_loss),
                    target=float(signal.target),
                    rsi=float(signal.rsi),
                    vol_ratio=float(signal.volume_ratio),
                    regime=_regime,
                )
            log.info(
                "entry_placed",
                symbol=signal.symbol,
                direction=signal.direction,
                qty=qty,
                order_id=order.order_id,
            )

    # ------------------------------------------------------------------
    # Candle close notification (called by StrategyEngine integration layer)
    # ------------------------------------------------------------------

    def notify_candle_close(self, symbol: str, close_price: Decimal) -> None:
        """
        Called by StrategyEngine on each 15-min candle close.

        Schedules exit checks as an asyncio task (not async — does not block).
        Exits are evaluated by ExitManager and placed if conditions are met.

        Args:
            symbol:      Symbol whose candle just closed.
            close_price: 15-min candle close price.
        """
        if self._exit_manager is None:
            return
        asyncio.create_task(
            self._exit_manager.check_exits(symbol, close_price),
            name=f"exit_check_{symbol}",
        )

    # ------------------------------------------------------------------
    # D2 startup reconciliation
    # ------------------------------------------------------------------

    async def _run_startup_reconciliation(self) -> bool:
        """
        D2 restart-safety protocol.

        Fetches kite.orders() and compares against local OSM:
          - Orders on Zerodha but NOT in OSM → mark_unknown() + lock instrument
          - Orders in OSM (non-terminal) NOT on Zerodha → mark EXPIRED

        Returns True if system_ready (zero UNKNOWN orders), False otherwise.
        """
        log.info("startup_reconciliation_starting")
        assert self._osm is not None

        mode = self._config.get("system", {}).get("mode", "paper")

        # In paper mode with empty OSM — skip kite.orders() call (no live orders)
        if mode == "paper":
            log.info(
                "startup_reconciliation_paper_mode",
                result="passed",
                unknown_count=0,
            )
            return True

        # Live mode: fetch from Zerodha
        try:
            zerodha_orders: list[dict] = await asyncio.to_thread(self._kite.orders)
        except Exception as exc:
            log.critical("startup_reconciliation_fetch_failed", error=str(exc))
            raise

        zerodha_order_ids: set[str] = set()
        unknown_count = 0

        for broker_order in zerodha_orders:
            order_id: str = broker_order.get("order_id", "")
            symbol: str = broker_order.get("tradingsymbol", "UNKNOWN")
            status: str = broker_order.get("status", "")
            zerodha_order_ids.add(order_id)

            # Only care about non-terminal orders on Zerodha
            if status in ("COMPLETE", "CANCELLED", "REJECTED"):
                continue

            # Order on Zerodha but NOT in local OSM → UNKNOWN
            if self._osm.get_order(order_id) is None:
                self._osm.mark_unknown(order_id, symbol)
                unknown_count += 1
                log.critical(
                    "unknown_order_found_on_startup",
                    order_id=order_id,
                    symbol=symbol,
                    zerodha_status=status,
                )

        # Orders in OSM (non-terminal) that Zerodha no longer knows → mark EXPIRED
        for order in self._osm.get_active_orders():
            if order.order_id not in zerodha_order_ids:
                log.warning(
                    "local_order_missing_from_zerodha",
                    order_id=order.order_id,
                    symbol=order.symbol,
                    current_state=order.state.value,
                )
                try:
                    self._osm.transition(order.order_id, OrderState.EXPIRED)
                except Exception as exc:
                    log.error(
                        "expire_transition_failed",
                        order_id=order.order_id,
                        error=str(exc),
                    )

        system_ready = unknown_count == 0
        log.info(
            "startup_reconciliation_complete",
            system_ready=system_ready,
            unknown_count=unknown_count,
            zerodha_orders_checked=len(zerodha_orders),
        )
        return system_ready

    # ------------------------------------------------------------------
    # D1: Signal status DB updates
    # ------------------------------------------------------------------

    async def _update_signal_status(
        self,
        symbol: str,
        status: str,
        *,
        reject_reason: str | None = None,
        order_id: str | None = None,
    ) -> None:
        """Update the most recent PENDING signal for this symbol today."""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE signals SET status = $1, reject_reason = $2, order_id = $3
                    WHERE id = (
                        SELECT id FROM signals
                        WHERE session_date = $4 AND symbol = $5 AND status = 'PENDING'
                        ORDER BY signal_time DESC LIMIT 1
                    )
                    """,
                    status,
                    reject_reason,
                    order_id,
                    self._session_date,
                    symbol,
                )
            log.debug(
                "signal_status_updated",
                symbol=symbol,
                status=status,
            )
        except Exception as exc:
            log.error(
                "signal_status_update_failed",
                symbol=symbol,
                status=status,
                error=str(exc),
            )
