"""
Integration tests for execution_engine.

Skipped when DB_DSN env var is absent (requires live TimescaleDB).
When DB_DSN is set, tests use a real asyncpg pool.

Test catalogue (4 cases):
  test_signal_to_fill_paper_mode
  test_target_exit_triggered_on_candle_close
  test_emergency_exit_all_on_kill_switch
  test_startup_reconciliation_locks_unknown_orders

In paper mode, all fills are immediate — no kite API calls.
risk_manager.on_fill() / on_close() are verified via mocks.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun import freeze_time

import pytest
import pytz

from execution_engine import ExecutionEngine
from execution_engine.exit_manager import ExitManager
from execution_engine.order_monitor import OrderMonitor
from execution_engine.order_placer import OrderPlacer
from execution_engine.state_machine import OrderState, OrderStateMachine
from strategy_engine.signal_generator import Signal

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

DB_DSN = os.getenv("DB_DSN")
needs_db = pytest.mark.skipif(not DB_DSN, reason="DB_DSN not set")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _paper_config() -> dict:
    return {"system": {"mode": "paper"}, "risk": {"max_open_positions": 3}}


def _shared_state() -> dict:
    return {
        "kill_switch_level": 0,
        "locked_instruments": set(),
        "open_positions": {},
        "open_orders": {},
        "fills_today": 0,
        "consecutive_losses": 0,
        "last_tick_prices": {},
    }


def _make_signal(
    symbol: str = "RELIANCE",
    direction: str = "LONG",
    entry: str = "2500",
    stop: str = "2450",
    target: str = "2600",
) -> Signal:
    return Signal(
        symbol=symbol,
        instrument_token=738561,
        direction=direction,
        signal_time=datetime.now(IST),
        candle_time=datetime.now(IST),
        theoretical_entry=Decimal(entry),
        stop_loss=Decimal(stop),
        target=Decimal(target),
        ema9=Decimal("9.5"),
        ema21=Decimal("8.5"),
        rsi=Decimal("62"),
        vwap=Decimal("2480"),
        volume_ratio=Decimal("2.1"),
    )


def _make_mock_risk_manager() -> MagicMock:
    rm = MagicMock()
    rm.on_fill = AsyncMock()
    rm.on_close = AsyncMock()
    rm.size_position = MagicMock(return_value=10)
    return rm


# ---------------------------------------------------------------------------
# Test 1: Signal → entry fill → risk_manager notified (no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signal_to_fill_paper_mode():
    """
    Paper mode: put Signal on order_queue → order FILLED → risk_manager.on_fill() called.

    No DB required. risk_manager is mocked.
    """
    shared = _shared_state()
    config = _paper_config()
    order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    mock_rm = _make_mock_risk_manager()
    mock_kite = MagicMock()
    mock_db = MagicMock()

    signal = _make_signal()
    await order_queue.put(signal)

    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(kite=mock_kite, config=config, osm=osm, shared_state=shared)
    exit_manager = ExitManager(order_placer=placer, shared_state=shared, config=config)
    monitor = OrderMonitor(
        kite=mock_kite,
        osm=osm,
        shared_state=shared,
        risk_manager=mock_rm,
        exit_manager=exit_manager,
        config=config,
    )

    # Place entry (simulates what _consume_signals does)
    qty = mock_rm.size_position(signal)
    order = await placer.place_entry(signal, qty)

    assert order is not None
    assert order.state == OrderState.FILLED

    # Process fills (what OrderMonitor does every 5s)
    await monitor._process_osm_fills()

    # Verify risk_manager.on_fill() was called
    mock_rm.on_fill.assert_awaited_once()
    call_kwargs = mock_rm.on_fill.call_args.kwargs
    assert call_kwargs["symbol"] == "RELIANCE"
    assert call_kwargs["direction"] == "LONG"
    assert call_kwargs["qty"] == 10
    assert call_kwargs["fill_price"] == Decimal("2500")


# ---------------------------------------------------------------------------
# Test 2: Target exit triggered on candle close
# ---------------------------------------------------------------------------

@freeze_time("2026-03-05 04:00:00")  # IST 09:30 — within market hours
@pytest.mark.asyncio
async def test_target_exit_triggered_on_candle_close():
    """
    Register a position → call check_exits with price >= target → exit order placed
    → OrderMonitor detects FILLED exit → risk_manager.on_close() called.
    """
    shared = _shared_state()
    config = _paper_config()
    mock_kite = MagicMock()
    mock_rm = _make_mock_risk_manager()

    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(kite=mock_kite, config=config, osm=osm, shared_state=shared)
    exit_manager = ExitManager(order_placer=placer, shared_state=shared, config=config)
    monitor = OrderMonitor(
        kite=mock_kite,
        osm=osm,
        shared_state=shared,
        risk_manager=mock_rm,
        exit_manager=exit_manager,
        config=config,
    )

    # Register an open position (normally done by OrderMonitor after entry fill)
    await exit_manager.register_position(
        symbol="RELIANCE",
        direction="LONG",
        entry_price=Decimal("2500"),
        stop_loss=Decimal("2450"),
        target=Decimal("2600"),
        qty=10,
        signal_id=0,
    )

    assert "RELIANCE" in exit_manager.get_open_positions()

    # Candle close price >= target → trigger TARGET exit
    await exit_manager.check_exits("RELIANCE", current_price=Decimal("2605"))

    # Position should be removed from ExitManager registry
    assert "RELIANCE" not in exit_manager.get_open_positions()

    # Process OSM fills (what order_monitor does every 5s)
    await monitor._process_osm_fills()

    # risk_manager.on_close() should be called with TARGET_HIT reason
    mock_rm.on_close.assert_awaited_once()
    close_kwargs = mock_rm.on_close.call_args.kwargs
    assert close_kwargs["symbol"] == "RELIANCE"
    assert close_kwargs["exit_reason"] == "TARGET_HIT"
    assert close_kwargs["exit_price"] == Decimal("2600")


# ---------------------------------------------------------------------------
# Test 3: Emergency exit all on kill switch Level 2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emergency_exit_all_on_kill_switch():
    """
    Register 2 positions → emergency_exit_all() → both positions closed
    → risk_manager.on_close() called twice.
    """
    shared = _shared_state()
    config = _paper_config()
    mock_kite = MagicMock()
    mock_rm = _make_mock_risk_manager()

    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(kite=mock_kite, config=config, osm=osm, shared_state=shared)
    exit_manager = ExitManager(order_placer=placer, shared_state=shared, config=config)
    monitor = OrderMonitor(
        kite=mock_kite,
        osm=osm,
        shared_state=shared,
        risk_manager=mock_rm,
        exit_manager=exit_manager,
        config=config,
    )

    # Register 2 open positions
    await exit_manager.register_position(
        symbol="RELIANCE",
        direction="LONG",
        entry_price=Decimal("2500"),
        stop_loss=Decimal("2450"),
        target=Decimal("2600"),
        qty=10,
        signal_id=0,
    )
    await exit_manager.register_position(
        symbol="INFY",
        direction="SHORT",
        entry_price=Decimal("1700"),
        stop_loss=Decimal("1750"),
        target=Decimal("1600"),
        qty=5,
        signal_id=0,
    )

    assert len(exit_manager.get_open_positions()) == 2

    # Trigger emergency exit all (Level 2 kill switch)
    await exit_manager.emergency_exit_all("kill_switch_level2")

    # All positions should be gone
    assert len(exit_manager.get_open_positions()) == 0

    # Process OSM fills → both on_close() calls
    await monitor._process_osm_fills()

    assert mock_rm.on_close.await_count == 2
    closed_symbols = {
        call.kwargs["symbol"] for call in mock_rm.on_close.call_args_list
    }
    assert closed_symbols == {"RELIANCE", "INFY"}

    # All exits should be KILL_SWITCH reason
    for call in mock_rm.on_close.call_args_list:
        assert call.kwargs["exit_reason"] == "KILL_SWITCH"


# ---------------------------------------------------------------------------
# Test 4: Startup reconciliation locks unknown orders (no DB required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_reconciliation_locks_unknown_orders():
    """
    Mock kite.orders() returning an unknown order_id (not in local OSM).
    Verify: instrument locked in shared_state, new signal for that instrument blocked.
    """
    shared = _shared_state()
    config = _paper_config()
    mock_kite = MagicMock()

    # Zerodha returns an open order not in our local OSM
    mock_kite.orders.return_value = [
        {
            "order_id": "ZERODHA-MYSTERY-001",
            "tradingsymbol": "HDFCBANK",
            "status": "OPEN",
            "product": "MIS",
            "quantity": 5,
        }
    ]
    mock_kite.positions.return_value = {"day": [], "net": []}

    osm = OrderStateMachine(shared_state=shared)

    # Run startup reconciliation logic manually
    # (ExecutionEngine.__aenter__ does this in live mode)
    zerodha_orders = mock_kite.orders()
    unknown_count = 0
    for broker_order in zerodha_orders:
        order_id = broker_order["order_id"]
        symbol = broker_order["tradingsymbol"]
        status = broker_order["status"]

        if status in ("COMPLETE", "CANCELLED", "REJECTED"):
            continue

        if osm.get_order(order_id) is None:
            osm.mark_unknown(order_id, symbol)
            unknown_count += 1

    # Verify HDFCBANK is locked
    assert "HDFCBANK" in shared["locked_instruments"]
    assert unknown_count == 1

    # Verify new entry for locked instrument is blocked
    placer = OrderPlacer(
        kite=mock_kite,
        config=config,  # paper mode
        osm=osm,
        shared_state=shared,
    )
    signal = _make_signal(symbol="HDFCBANK")
    blocked_order = await placer.place_entry(signal, qty=5)

    assert blocked_order is None  # Blocked by GATE 3 (locked_instruments)

    # Signal for unlocked instrument should still work
    reliance_signal = _make_signal(symbol="RELIANCE")
    allowed_order = await placer.place_entry(reliance_signal, qty=10)
    assert allowed_order is not None
    assert allowed_order.state == OrderState.FILLED


# ---------------------------------------------------------------------------
# DB-required integration tests (skipped without DB_DSN)
# ---------------------------------------------------------------------------

@needs_db
@pytest.mark.asyncio
async def test_execution_engine_full_lifecycle_with_db():
    """
    Full integration with DB: ExecutionEngine context manager → signal → fill
    → risk_manager.on_fill() → trade written to DB.
    """
    import asyncpg

    pool = await asyncpg.create_pool(DB_DSN)
    try:
        shared = _shared_state()
        order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        mock_rm = _make_mock_risk_manager()
        mock_kite = MagicMock()

        async with ExecutionEngine(
            kite=mock_kite,
            config=_paper_config(),
            shared_state=shared,
            order_queue=order_queue,
            risk_manager=mock_rm,
            db_pool=pool,
        ) as ee:
            signal = _make_signal()
            await order_queue.put(signal)

            # Run one cycle manually (avoid blocking forever)
            await asyncio.wait_for(
                ee._handle_signal(signal),
                timeout=2.0,
            )
            await asyncio.wait_for(
                ee._order_monitor._process_osm_fills(),
                timeout=2.0,
            )

        mock_rm.on_fill.assert_awaited_once()
    finally:
        await pool.close()
