"""
TradeOS — Order Placer (Execution Engine)

Places entry and exit orders via Zerodha KiteConnect (live mode) or
simulates them immediately (paper mode). All orders are MIS intraday product.

Gate sequence (strictly enforced before every order placement):
  GATE 0: Mode check — routes to paper simulation or live API call
  GATE 1: Kill switch — shared_state["kill_switch_level"] == 0
  GATE 2: Duplicate order check — no active ENTRY order for symbol
  GATE 3: Instrument not locked — symbol not in shared_state["locked_instruments"]

Paper mode simulation rules:
  Entry fill price  = signal.theoretical_entry   (no slippage)
  Exit TARGET       = caller-supplied exit_price (signal.target)
  Exit STOP         = caller-supplied exit_price (signal.stop_loss)
  Exit HARD_EXIT    = caller-supplied exit_price (candle close or theoretical)
  Simulated fills are IMMEDIATE. DB writes are identical to live trades.

Live mode:
  Calls kite.place_order() via asyncio.to_thread() (D6 — never block event loop).
  Currently not deployed (Phase 1 = paper only), but fully implemented for Phase 2.
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

import pytz

from execution_engine.state_machine import (
    DuplicateOrderError,
    Order,
    OrderState,
    OrderStateMachine,
)
from strategy_engine.signal_generator import Signal

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# Zerodha MIS product — all intraday orders
_PRODUCT_MIS = "MIS"
_ORDER_TYPE_MARKET = "MARKET"
_EXCHANGE_NSE = "NSE"
_VARIETY_REGULAR = "regular"


class OrderPlacer:
    """
    Places ENTRY and EXIT orders.

    In paper mode: simulates fills immediately (no kite API calls).
    In live mode: places market orders via kite.place_order().

    Args:
        kite:         Authenticated KiteConnect instance.
        config:       Loaded settings.yaml dict.
        osm:          Shared OrderStateMachine registry.
        shared_state: D6 shared state dict (for kill switch + instrument locks).
        kill_switch:  Optional kill switch object. If provided, calls
                      kill_switch.is_trading_allowed(). Falls back to
                      shared_state["kill_switch_level"] if None.
    """

    def __init__(
        self,
        kite,
        config: dict,
        osm: OrderStateMachine,
        shared_state: dict,
        kill_switch=None,
    ) -> None:
        self._kite = kite
        self._config = config
        self._osm = osm
        self._shared_state = shared_state
        self._kill_switch = kill_switch
        self._mode: str = config.get("system", {}).get("mode", "paper")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def place_entry(self, signal: Signal, qty: int) -> Optional[Order]:
        """
        Place an entry order for a signal.

        Paper mode: simulate immediate FILLED order.
        Live mode: call kite.place_order() → track in OSM.

        Args:
            signal: Approved Signal from order_queue.
            qty:    Position size from RiskManager.size_position().

        Returns:
            Order in FILLED state (paper) or SUBMITTED state (live),
            or None if any gate blocks the order.
        """
        # GATE 0: mode routing
        if self._mode == "paper":
            return await self._simulate_entry(signal, qty)
        elif self._mode == "live":
            return await self._execute_live_entry(signal, qty)
        else:
            raise AssertionError(
                f"Unknown system mode '{self._mode}'. "
                f"Must be 'paper' or 'live' (config/settings.yaml: system.mode)"
            )

    async def place_exit(
        self,
        symbol: str,
        exit_type: str,
        qty: int,
        exit_price: Optional[Decimal] = None,
    ) -> Optional[Order]:
        """
        Place an exit order for an open position.

        Args:
            symbol:     Symbol to exit.
            exit_type:  'TARGET', 'STOP', 'HARD_EXIT', or 'KILL_SWITCH'.
            qty:        Quantity to exit (matches entry qty).
            exit_price: Fill price for paper simulation. If None, falls back
                        to last tick price or entry price from shared_state.

        Returns:
            Order in FILLED state (paper) or SUBMITTED state (live), or None.
        """
        if self._mode == "paper":
            return await self._simulate_exit(symbol, exit_type, qty, exit_price)
        elif self._mode == "live":
            return await self._execute_live_exit(symbol, exit_type, qty)
        else:
            raise AssertionError(
                f"Unknown system mode '{self._mode}'. "
                f"Must be 'paper' or 'live' (config/settings.yaml: system.mode)"
            )

    # ------------------------------------------------------------------
    # Paper mode simulation
    # ------------------------------------------------------------------

    async def _simulate_entry(self, signal: Signal, qty: int) -> Optional[Order]:
        """Simulate entry fill in paper mode. Returns FILLED order immediately."""
        # GATE 1: kill switch
        if not self._is_trading_allowed():
            log.info("entry_blocked_kill_switch", symbol=signal.symbol)
            return None

        # GATE 2: duplicate order check
        order_id = f"PAPER-{uuid4().hex[:12].upper()}"
        try:
            order = self._osm.create_order(
                order_id=order_id,
                symbol=signal.symbol,
                instrument_token=signal.instrument_token,
                direction=signal.direction,
                order_type="ENTRY",
                qty=qty,
                price=signal.theoretical_entry,
                signal_id=signal.db_id or 0,
                stop_loss=signal.stop_loss,
                target=signal.target,
            )
        except DuplicateOrderError:
            log.warning("entry_blocked_duplicate", symbol=signal.symbol)
            return None

        # GATE 3: instrument lock
        if signal.symbol in self._shared_state.get("locked_instruments", set()):
            log.warning("entry_blocked_instrument_locked", symbol=signal.symbol)
            # Remove the just-created order since we can't proceed
            del self._osm._orders[order_id]
            return None

        # Simulate broker acknowledgement chain → immediate fill
        self._osm.transition(order_id, OrderState.SUBMITTED)
        self._osm.transition(order_id, OrderState.ACKNOWLEDGED)
        self._osm.transition(
            order_id,
            OrderState.FILLED,
            fill_price=signal.theoretical_entry,
        )

        log.info(
            "order_placed",
            symbol=signal.symbol,
            direction=signal.direction,
            order_type="MARKET",
            price=float(signal.theoretical_entry),
            qty=qty,
            mode="paper",
        )
        log.info(
            "paper_entry_simulated",
            symbol=signal.symbol,
            direction=signal.direction,
            qty=qty,
            fill_price=float(signal.theoretical_entry),
            order_id=order_id,
        )
        return order

    async def _simulate_exit(
        self,
        symbol: str,
        exit_type: str,
        qty: int,
        exit_price: Optional[Decimal],
    ) -> Optional[Order]:
        """Simulate exit fill in paper mode. Returns FILLED order immediately."""
        # GATE 1: kill switch (allow KILL_SWITCH exits even when kill switch active)
        if exit_type != "KILL_SWITCH" and not self._is_trading_allowed():
            log.info("exit_blocked_kill_switch", symbol=symbol, exit_type=exit_type)
            return None

        # Determine direction (opposite of entry)
        direction = self._get_exit_direction(symbol)

        # Resolve exit fill price
        resolved_price = self._resolve_exit_price(symbol, exit_type, exit_price)

        order_id = f"PAPER-EXIT-{uuid4().hex[:10].upper()}"

        # Get instrument_token from open positions or OSM
        instrument_token = self._get_instrument_token(symbol)

        try:
            order = self._osm.create_order(
                order_id=order_id,
                symbol=symbol,
                instrument_token=instrument_token,
                direction=direction,
                order_type="EXIT",
                qty=qty,
                price=resolved_price,
                signal_id=0,
            )
        except DuplicateOrderError:
            # EXIT orders don't trigger duplicate check (only ENTRY does)
            # DuplicateOrderError shouldn't occur for EXIT, but handle defensively
            log.warning("exit_duplicate_unexpected", symbol=symbol)
            return None

        order.exit_type = exit_type

        # Simulate broker acknowledgement chain → immediate fill
        self._osm.transition(order_id, OrderState.SUBMITTED)
        self._osm.transition(order_id, OrderState.ACKNOWLEDGED)
        self._osm.transition(
            order_id,
            OrderState.FILLED,
            fill_price=resolved_price,
        )

        log.info(
            "paper_exit_simulated",
            symbol=symbol,
            exit_type=exit_type,
            qty=qty,
            fill_price=float(resolved_price),
            order_id=order_id,
        )
        return order

    # ------------------------------------------------------------------
    # Live mode (Phase 2 — implemented but not active in Phase 1)
    # ------------------------------------------------------------------

    async def _execute_live_entry(self, signal: Signal, qty: int) -> Optional[Order]:
        """Call kite.place_order() for a live ENTRY. Returns SUBMITTED order."""
        # GATE 1: kill switch
        if not self._is_trading_allowed():
            log.info("live_entry_blocked_kill_switch", symbol=signal.symbol)
            return None

        # GATE 2: duplicate check (pre-registration)
        existing = self._osm._get_active_entry_for_symbol(signal.symbol)
        if existing:
            log.warning(
                "live_entry_blocked_duplicate",
                symbol=signal.symbol,
                existing=existing.order_id,
            )
            return None

        # GATE 3: instrument lock
        if signal.symbol in self._shared_state.get("locked_instruments", set()):
            log.warning("live_entry_blocked_instrument_locked", symbol=signal.symbol)
            return None

        transaction_type = "BUY" if signal.direction == "LONG" else "SELL"

        try:
            order_id: str = await asyncio.to_thread(
                self._kite.place_order,
                variety=_VARIETY_REGULAR,
                exchange=_EXCHANGE_NSE,
                tradingsymbol=signal.symbol,
                transaction_type=transaction_type,
                quantity=qty,
                product=_PRODUCT_MIS,
                order_type=_ORDER_TYPE_MARKET,
            )
        except Exception as exc:
            log.error(
                "live_entry_placement_failed",
                symbol=signal.symbol,
                direction=signal.direction,
                error=str(exc),
            )
            return None

        order = self._osm.create_order(
            order_id=order_id,
            symbol=signal.symbol,
            instrument_token=signal.instrument_token,
            direction=signal.direction,
            order_type="ENTRY",
            qty=qty,
            price=signal.theoretical_entry,
            signal_id=signal.db_id or 0,
            stop_loss=signal.stop_loss,
            target=signal.target,
        )
        self._osm.transition(order_id, OrderState.SUBMITTED)

        log.info(
            "order_placed",
            symbol=signal.symbol,
            direction=signal.direction,
            order_type="MARKET",
            price=float(signal.theoretical_entry),
            qty=qty,
            mode="live",
        )
        log.info(
            "live_entry_placed",
            symbol=signal.symbol,
            direction=signal.direction,
            qty=qty,
            order_id=order_id,
        )
        return order

    async def _execute_live_exit(
        self, symbol: str, exit_type: str, qty: int
    ) -> Optional[Order]:
        """Call kite.place_order() for a live EXIT. Returns SUBMITTED order."""
        direction = self._get_exit_direction(symbol)
        transaction_type = "SELL" if direction == "SELL" else "BUY"
        instrument_token = self._get_instrument_token(symbol)

        try:
            order_id: str = await asyncio.to_thread(
                self._kite.place_order,
                variety=_VARIETY_REGULAR,
                exchange=_EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=qty,
                product=_PRODUCT_MIS,
                order_type=_ORDER_TYPE_MARKET,
            )
        except Exception as exc:
            log.error(
                "live_exit_placement_failed",
                symbol=symbol,
                exit_type=exit_type,
                error=str(exc),
            )
            return None

        exit_price = Decimal("0")  # Will be updated by OrderMonitor on fill
        order = self._osm.create_order(
            order_id=order_id,
            symbol=symbol,
            instrument_token=instrument_token,
            direction=direction,
            order_type="EXIT",
            qty=qty,
            price=exit_price,
            signal_id=0,
        )
        order.exit_type = exit_type
        self._osm.transition(order_id, OrderState.SUBMITTED)

        log.info(
            "live_exit_placed",
            symbol=symbol,
            exit_type=exit_type,
            qty=qty,
            order_id=order_id,
        )
        return order

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_trading_allowed(self) -> bool:
        """Check kill switch state. Uses kill_switch object if available."""
        if self._kill_switch is not None:
            return self._kill_switch.is_trading_allowed()
        return self._shared_state.get("kill_switch_level", 0) == 0

    def _resolve_exit_price(
        self,
        symbol: str,
        exit_type: str,
        exit_price: Optional[Decimal],
    ) -> Decimal:
        """
        Determine exit fill price for paper simulation.

        Priority:
          1. caller-supplied exit_price
          2. last known tick price from shared_state["last_tick_prices"]
          3. entry price from open positions as fallback
        """
        if exit_price is not None:
            return exit_price

        # Fallback: last tick price (if available in shared_state)
        tick_prices: dict = self._shared_state.get("last_tick_prices", {})
        if symbol in tick_prices:
            return Decimal(str(tick_prices[symbol]))

        # Final fallback: entry price from open positions
        positions: dict = self._shared_state.get("open_positions", {})
        if symbol in positions:
            return Decimal(str(positions[symbol].get("avg_price", "0")))

        log.warning(
            "exit_price_fallback_zero",
            symbol=symbol,
            exit_type=exit_type,
        )
        return Decimal("0")

    def _get_exit_direction(self, symbol: str) -> str:
        """Return exit transaction direction (opposite of entry direction)."""
        positions: dict = self._shared_state.get("open_positions", {})
        if symbol in positions:
            side = positions[symbol].get("side", "BUY")
            return "SELL" if side == "BUY" else "BUY"
        # Fallback
        return "SELL"

    def _get_instrument_token(self, symbol: str) -> int:
        """Get instrument token from open positions or return 0."""
        positions: dict = self._shared_state.get("open_positions", {})
        if symbol in positions:
            return positions[symbol].get("instrument_token", 0)
        return 0
