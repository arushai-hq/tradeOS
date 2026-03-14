"""
TradeOS — Order Monitor (Execution Engine / D6 Task 3)

Polls order state and drives the post-fill accounting pipeline:
  ENTRY fill  → risk_manager.on_fill() + exit_manager.register_position()
  EXIT fill   → risk_manager.on_close()
  REJECTION   → log WARNING (consecutive_losses already incremented by OSM)

Paper mode: iterates OSM registry every 5s for fills (no kite API calls).
Live mode:  polls kite.orders() every 5s, maps Zerodha status → OrderState,
            drives OSM transitions, then processes fills via OSM.

D2 Zerodha status mapping:
  "OPEN"           → ACKNOWLEDGED
  "COMPLETE"       → FILLED
  "CANCELLED"      → CANCELLED
  "REJECTED"       → REJECTED
  "TRIGGER PENDING"→ ACKNOWLEDGED
  Unknown status   → log WARNING, skip (do not transition)

PARTIALLY_FILLED detection: status == "OPEN" AND filled_qty > 0 AND pending_qty > 0.
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import pytz

from utils.position_helpers import resolve_position_fields
from execution_engine.state_machine import (
    Order,
    OrderState,
    OrderStateMachine,
    TERMINAL_STATES,
    map_zerodha_status,
)

if TYPE_CHECKING:
    from execution_engine.exit_manager import ExitManager
    from risk_manager import RiskManager

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Maps exit_type → exit_reason for RiskManager.on_close()
_EXIT_REASON_MAP: dict[str, str] = {
    "TARGET":      "TARGET_HIT",
    "STOP":        "STOP_HIT",
    "HARD_EXIT":   "HARD_EXIT_1500",
    "KILL_SWITCH": "KILL_SWITCH",
}


class OrderMonitor:
    """
    Monitors order state and drives the post-fill accounting pipeline.

    Runs as a perpetual asyncio task (D6 Task 3 — order_monitor).
    Polls every 5 seconds per D6 spec.

    Paper mode: OSM processes fills inline — no kite.orders() call needed.
    Live mode:  polls kite.orders(), updates OSM, then processes fills.

    Tracks processed order IDs to avoid duplicate fill callbacks.

    Args:
        kite:          Authenticated KiteConnect instance.
        osm:           Shared OrderStateMachine registry.
        shared_state:  D6 shared state dict.
        risk_manager:  RiskManager for on_fill() / on_close() callbacks.
        exit_manager:  ExitManager for register_position() after ENTRY fill.
        config:        Loaded settings.yaml dict.
    """

    def __init__(
        self,
        kite,
        osm: OrderStateMachine,
        shared_state: dict,
        risk_manager: "RiskManager",
        exit_manager: "ExitManager",
        config: dict,
        notifier=None,
        db_pool=None,
    ) -> None:
        self._kite = kite
        self._osm = osm
        self._shared_state = shared_state
        self._risk_manager = risk_manager
        self._exit_manager = exit_manager
        self._mode: str = config.get("system", {}).get("mode", "paper")
        self._is_paper: bool = self._mode == "paper"
        self._notifier = notifier
        self._db_pool = db_pool
        self._session_date = datetime.now(IST).date()

        # Set of order_ids already sent to accounting callbacks
        self._processed_order_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Main run loop (D6 Task 3)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Perpetual polling loop. Runs until cancelled.

        CancelledError is never suppressed per D6 rule.
        API errors are logged and polling continues — never stop monitoring.
        """
        log.info("order_monitor_started", mode=self._mode)
        while True:
            try:
                if not self._is_paper:
                    await self._poll_zerodha()
                await self._process_osm_fills()
                self._update_shared_state()
            except asyncio.CancelledError:
                raise  # D6 rule — never suppress
            except Exception as exc:
                log.error("order_monitor_error", error=str(exc), exc_info=True)
            await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Zerodha polling (live mode)
    # ------------------------------------------------------------------

    async def _poll_zerodha(self) -> None:
        """Fetch kite.orders() and sync OSM states."""
        try:
            broker_orders: list[dict] = await asyncio.to_thread(self._kite.orders)
        except Exception as exc:
            log.error("order_monitor_zerodha_fetch_failed", error=str(exc))
            return

        for broker_order in broker_orders:
            await self._sync_order_from_zerodha(broker_order)

    async def _sync_order_from_zerodha(self, broker_order: dict) -> None:
        """Sync a single Zerodha order dict into the OSM."""
        order_id: str = broker_order.get("order_id", "")
        symbol: str = broker_order.get("tradingsymbol", "")
        zerodha_status: str = broker_order.get("status", "")
        filled_qty: int = broker_order.get("filled_quantity", 0)
        pending_qty: int = broker_order.get("pending_quantity", 0)

        order = self._osm.get_order(order_id)
        if order is None:
            # Order on Zerodha but not in local OSM → skip (startup reconciliation handles this)
            return

        if order.state in TERMINAL_STATES:
            return  # Already terminal — no action needed

        # Detect partial fill: status OPEN but some qty filled and some pending
        target_state: Optional[OrderState]
        if zerodha_status == "OPEN" and filled_qty > 0 and pending_qty > 0:
            target_state = OrderState.PARTIALLY_FILLED
        else:
            target_state = map_zerodha_status(zerodha_status, order_id, symbol)
            if target_state is None:
                return  # Unknown status — do not transition per D2 spec

        if order.state == target_state:
            return  # No change

        # Drive the state machine to the new state
        fill_price: Optional[Decimal] = None
        if target_state == OrderState.FILLED:
            avg_price = broker_order.get("average_price")
            if avg_price:
                fill_price = Decimal(str(avg_price))

        reject_reason: Optional[str] = None
        if target_state == OrderState.REJECTED:
            reject_reason = broker_order.get("status_message", "")

        try:
            self._osm.transition(
                order_id,
                target_state,
                fill_price=fill_price,
                reject_reason=reject_reason,
            )
            log.info(
                "order_state_synced_from_zerodha",
                order_id=order_id,
                symbol=symbol,
                new_state=target_state.value,
            )
        except Exception as exc:
            log.error(
                "order_monitor_transition_error",
                order_id=order_id,
                symbol=symbol,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Fill processing (paper + live)
    # ------------------------------------------------------------------

    async def _process_osm_fills(self) -> None:
        """
        Iterate all OSM orders and process any newly-filled or rejected orders.

        Idempotent: uses _processed_order_ids to avoid double-calling accounting.
        """
        for order in self._osm.get_all_orders():
            if order.order_id in self._processed_order_ids:
                continue

            if order.state == OrderState.FILLED:
                self._processed_order_ids.add(order.order_id)
                if order.order_type == "ENTRY":
                    await self._on_entry_fill(order)
                elif order.order_type == "EXIT":
                    await self._on_exit_fill(order)

            elif order.state == OrderState.REJECTED:
                self._processed_order_ids.add(order.order_id)
                await self._on_rejected(order)

    async def _on_entry_fill(self, order: Order) -> None:
        """Handle ENTRY fill: notify RiskManager + register with ExitManager."""
        fill_price = order.fill_price or order.price

        log.info(
            "order_filled",
            symbol=order.symbol,
            direction=order.direction,
            fill_price=float(fill_price),
            qty=order.qty,
            position_id=order.order_id,
            mode=self._mode,
        )
        if getattr(self, "_notifier", None) is not None:
            self._notifier.notify_position_opened(
                symbol=order.symbol,
                direction=order.direction,
                fill_price=float(fill_price),
                qty=order.qty,
                stop_loss=float(order.stop_loss or fill_price),
                target=float(order.target or fill_price),
            )
        log.info(
            "entry_filled",
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            qty=order.qty,
            fill_price=float(fill_price),
        )

        # D1: Update signal status to FILLED in DB
        await self._update_signal_status(
            order.symbol, "FILLED", order_id=order.order_id,
        )

        # Notify RiskManager
        await self._risk_manager.on_fill(
            symbol=order.symbol,
            direction=order.direction,
            qty=order.qty,
            fill_price=fill_price,
            order_id=order.order_id,
            signal_id=order.signal_id,
        )

        # Register with ExitManager (stop_loss and target from Signal via Order)
        stop_loss = order.stop_loss or fill_price
        target = order.target or fill_price

        await self._exit_manager.register_position(
            symbol=order.symbol,
            direction=order.direction,
            entry_price=fill_price,
            stop_loss=stop_loss,
            target=target,
            qty=order.qty,
            signal_id=order.signal_id,
        )

        # Update shared_state
        self._shared_state["fills_today"] = (
            self._shared_state.get("fills_today", 0) + 1
        )

    async def _on_exit_fill(self, order: Order) -> None:
        """Handle EXIT fill: notify RiskManager.on_close().

        B8 fix: snapshot position data BEFORE on_close() deletes it from
        shared_state.  PnlTracker.on_close() already logs 'position_closed'
        with the authoritative P&L breakdown, so this method only logs
        'exit_filled' and forwards to the Telegram notifier.
        """
        fill_price = order.fill_price or order.price
        exit_type = order.exit_type or "MANUAL"
        exit_reason = _EXIT_REASON_MAP.get(exit_type, "MANUAL")

        log.info(
            "exit_filled",
            order_id=order.order_id,
            symbol=order.symbol,
            exit_type=exit_type,
            fill_price=float(fill_price),
        )

        # Snapshot position data BEFORE on_close() removes it from shared_state
        positions: dict = self._shared_state.get("open_positions", {})
        pos_info: dict = dict(positions.get(order.symbol, {}))

        await self._risk_manager.on_close(
            symbol=order.symbol,
            exit_price=fill_price,
            exit_reason=exit_reason,
            exit_order_id=order.order_id,
        )

        # Notify Telegram using snapshotted data (not deleted shared_state)
        if pos_info and getattr(self, "_notifier", None) is not None:
            entry_price_float, direction, _qty = resolve_position_fields(pos_info)
            fill_price_float: float = float(fill_price)
            pnl_points = (
                fill_price_float - entry_price_float
                if direction == "LONG"
                else entry_price_float - fill_price_float
            )
            pnl_pct = round(pnl_points / entry_price_float * 100, 4) if entry_price_float else 0.0
            hold_duration_minutes = 0.0
            entry_time = pos_info.get("entry_time")
            if entry_time is not None:
                delta = datetime.now(IST) - entry_time
                hold_duration_minutes = round(delta.total_seconds() / 60, 1)

            self._notifier.notify_position_closed(
                symbol=order.symbol,
                direction=direction,
                entry_price=entry_price_float,
                exit_price=fill_price_float,
                exit_reason=exit_reason,
                pnl_points=round(pnl_points, 2),
                pnl_pct=pnl_pct,
                hold_duration_minutes=hold_duration_minutes,
            )

    async def _on_rejected(self, order: Order) -> None:
        """Handle order REJECTION. consecutive_losses already incremented by OSM."""
        log.warning(
            "order_rejected",
            order_id=order.order_id,
            symbol=order.symbol,
            order_type=order.order_type,
            reject_reason=order.reject_reason,
            consecutive_losses=self._shared_state.get("consecutive_losses", 0),
        )

    # ------------------------------------------------------------------
    # Shared state sync
    # ------------------------------------------------------------------

    def _update_shared_state(self) -> None:
        """Sync open_orders and open_positions in shared_state from OSM."""
        # open_orders: all non-terminal orders
        active = self._osm.get_active_orders()
        self._shared_state["open_orders"] = {
            o.order_id: {
                "symbol": o.symbol,
                "direction": o.direction,
                "order_type": o.order_type,
                "qty": o.qty,
                "state": o.state.value,
                "placed_at": o.placed_at.isoformat(),
            }
            for o in active
        }

    # ------------------------------------------------------------------
    # D1: Signal status DB updates
    # ------------------------------------------------------------------

    async def _update_signal_status(
        self,
        symbol: str,
        status: str,
        *,
        order_id: str | None = None,
    ) -> None:
        """Update the most recent PENDING signal for this symbol today."""
        if getattr(self, "_db_pool", None) is None:
            return
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE signals SET status = $1, order_id = $2
                    WHERE id = (
                        SELECT id FROM signals
                        WHERE session_date = $3 AND symbol = $4 AND status = 'PENDING'
                        ORDER BY signal_time DESC LIMIT 1
                    )
                    """,
                    status,
                    order_id,
                    self._session_date,
                    symbol,
                )
            log.debug(
                "signal_status_updated",
                symbol=symbol,
                status=status,
                order_id=order_id,
            )
        except Exception as exc:
            log.error(
                "signal_status_update_failed",
                symbol=symbol,
                status=status,
                error=str(exc),
            )
