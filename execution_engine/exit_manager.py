"""
TradeOS — Exit Manager (Execution Engine)

Monitors open positions and triggers exits when conditions are met.
Exit checks run on each 15-minute candle close (not every tick).

Exit priority (checked in strict order):
  1. HARD_EXIT: current IST time >= 15:00 (mandatory end-of-day exit)
  2. TARGET_HIT: price crosses target
  3. STOP_HIT: price crosses stop loss

Only one exit per position — first condition wins. Position removed from
registry after exit is placed (regardless of fill confirmation).

The ExitManager does NOT call risk_manager.on_close() directly. That happens
when OrderMonitor detects the FILLED exit order in the OSM (5s poll cycle).

emergency_exit_all() is the Level 2 kill switch handler. It closes all
open positions with exit_type='KILL_SWITCH'.
"""
from __future__ import annotations

import asyncio
import copy
import structlog
from datetime import datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

import pytz

if TYPE_CHECKING:
    from execution_engine.order_placer import OrderPlacer

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Hard exit time per D1 spec and config/settings.yaml
_HARD_EXIT_TIME = time(15, 0)  # 15:00 IST


class ExitManager:
    """
    Monitors open positions and triggers exits on candle closes.

    Maintains a registry of open positions populated by OrderMonitor
    after each ENTRY fill. Checks exit conditions when notify_candle_close()
    is called by ExecutionEngine.

    Args:
        order_placer:  OrderPlacer for placing exit orders.
        shared_state:  D6 shared state dict.
        config:        Loaded settings.yaml dict.
    """

    def __init__(
        self,
        order_placer: "OrderPlacer",
        shared_state: dict,
        config: dict,
    ) -> None:
        self._order_placer = order_placer
        self._shared_state = shared_state
        self._config = config

        # {symbol: {direction, entry_price, stop_loss, target, qty, signal_id}}
        self._positions: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Position registration
    # ------------------------------------------------------------------

    async def register_position(
        self,
        symbol: str,
        direction: str,
        entry_price: Decimal,
        stop_loss: Decimal,
        target: Decimal,
        qty: int,
        signal_id: int,
    ) -> None:
        """
        Register a new open position after ENTRY fill.

        Called by OrderMonitor._on_entry_fill() after risk_manager.on_fill().

        Args:
            symbol:       Trading symbol.
            direction:    'LONG' or 'SHORT'.
            entry_price:  Actual fill price.
            stop_loss:    Stop loss level from originating Signal.
            target:       Target price from originating Signal.
            qty:          Filled quantity.
            signal_id:    Signal DB id (0 = no DB record).
        """
        self._positions[symbol] = {
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "target": target,
            "qty": qty,
            "signal_id": signal_id,
        }

        # Also update shared_state["open_positions"] (owner: order_monitor, but
        # we update here for consistency since ExitManager is called from OrderMonitor)
        self._shared_state.setdefault("open_positions", {})[symbol] = {
            "qty": qty if direction == "LONG" else -qty,
            "avg_price": float(entry_price),
            "side": "BUY" if direction == "LONG" else "SELL",
            "entry_time": datetime.now(IST),
        }

        log.info(
            "position_registered",
            symbol=symbol,
            direction=direction,
            entry_price=float(entry_price),
            stop_loss=float(stop_loss),
            target=float(target),
            qty=qty,
        )

    # ------------------------------------------------------------------
    # Exit checks
    # ------------------------------------------------------------------

    async def check_exits(self, symbol: str, current_price: Decimal) -> None:
        """
        Check exit conditions for a symbol at a candle close price.

        Called by ExecutionEngine.notify_candle_close() on every 15-min candle.
        Checks conditions in strict priority order. Only one exit per position.
        Position is removed from registry after exit is placed.

        Args:
            symbol:        Symbol to check.
            current_price: 15-min candle close price.
        """
        position = self._positions.get(symbol)
        if position is None:
            return  # No open position for this symbol

        direction: str = position["direction"]
        stop_loss: Decimal = position["stop_loss"]
        target: Decimal = position["target"]
        qty: int = position["qty"]

        now_time: time = datetime.now(IST).time()

        # Priority 1: HARD EXIT at 15:00 IST
        if now_time >= _HARD_EXIT_TIME:
            log.info(
                "hard_exit_triggered",
                symbol=symbol,
                current_time=now_time.isoformat(),
                direction=direction,
            )
            await self._place_exit_and_remove(symbol, "HARD_EXIT", qty, current_price)
            return

        # Priority 2: TARGET HIT
        if direction == "LONG" and current_price >= target:
            log.info(
                "target_hit",
                symbol=symbol,
                current_price=float(current_price),
                target=float(target),
            )
            await self._place_exit_and_remove(symbol, "TARGET", qty, target)
            return

        if direction == "SHORT" and current_price <= target:
            log.info(
                "target_hit",
                symbol=symbol,
                current_price=float(current_price),
                target=float(target),
            )
            await self._place_exit_and_remove(symbol, "TARGET", qty, target)
            return

        # Priority 3: STOP HIT
        if direction == "LONG" and current_price <= stop_loss:
            log.info(
                "stop_hit",
                symbol=symbol,
                current_price=float(current_price),
                stop_loss=float(stop_loss),
            )
            await self._place_exit_and_remove(symbol, "STOP", qty, stop_loss)
            return

        if direction == "SHORT" and current_price >= stop_loss:
            log.info(
                "stop_hit",
                symbol=symbol,
                current_price=float(current_price),
                stop_loss=float(stop_loss),
            )
            await self._place_exit_and_remove(symbol, "STOP", qty, stop_loss)
            return

    async def emergency_exit_all(self, reason: str) -> None:
        """
        Emergency exit ALL open positions. Called by Level 2 kill switch.

        Logs CRITICAL. Continues even if individual exits fail.

        Args:
            reason: Kill switch trigger reason (for logging).
        """
        symbols = list(self._positions.keys())

        log.critical(
            "emergency_exit_all",
            reason=reason,
            positions=symbols,
        )

        for symbol in symbols:
            position = self._positions.get(symbol)
            if position is None:
                continue
            qty = position["qty"]
            # Use current open position price as exit price fallback
            entry_price = position.get("entry_price", Decimal("0"))
            try:
                await self._place_exit_and_remove(
                    symbol, "KILL_SWITCH", qty, entry_price
                )
            except Exception as exc:
                log.error(
                    "emergency_exit_failed",
                    symbol=symbol,
                    error=str(exc),
                )

    def get_open_positions(self) -> dict:
        """Return a copy of the open positions registry."""
        return copy.deepcopy(self._positions)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _place_exit_and_remove(
        self,
        symbol: str,
        exit_type: str,
        qty: int,
        exit_price: Decimal,
    ) -> None:
        """Place exit order and remove position from registry."""
        # Remove first to prevent double-exit if check_exits is called again
        # before the order monitor processes the fill
        self._positions.pop(symbol, None)

        order = await self._order_placer.place_exit(
            symbol=symbol,
            exit_type=exit_type,
            qty=qty,
            exit_price=exit_price,
        )

        if order is None:
            log.error(
                "exit_placement_failed",
                symbol=symbol,
                exit_type=exit_type,
                qty=qty,
            )
        else:
            log.info(
                "exit_placed",
                symbol=symbol,
                exit_type=exit_type,
                qty=qty,
                exit_price=float(exit_price),
                order_id=order.order_id,
            )
