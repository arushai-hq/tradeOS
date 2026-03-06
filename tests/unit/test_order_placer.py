"""
Unit tests for execution_engine.order_placer.OrderPlacer.

Test catalogue (8 cases):
  test_paper_mode_entry_simulates_fill
  test_paper_mode_fill_price_equals_theoretical
  test_paper_mode_exit_target_uses_signal_target
  test_paper_mode_exit_stop_uses_signal_stop
  test_live_mode_calls_kite_place_order
  test_mode_assertion_blocks_wrong_mode
  test_duplicate_order_blocked_before_kite_call
  test_locked_instrument_blocked_before_kite_call
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution_engine.order_placer import OrderPlacer
from execution_engine.state_machine import OrderState, OrderStateMachine
from strategy_engine.signal_generator import Signal
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _paper_config() -> dict:
    return {"system": {"mode": "paper"}}


def _live_config() -> dict:
    return {"system": {"mode": "live"}}


def _bad_config() -> dict:
    return {"system": {"mode": "staging"}}


def _shared_state() -> dict:
    return {
        "kill_switch_level": 0,
        "locked_instruments": set(),
        "open_positions": {},
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


def _make_placer(
    config: dict | None = None,
    shared_state: dict | None = None,
    kite=None,
    kill_switch=None,
) -> tuple[OrderPlacer, OrderStateMachine]:
    osm = OrderStateMachine(shared_state=shared_state or _shared_state())
    placer = OrderPlacer(
        kite=kite or MagicMock(),
        config=config or _paper_config(),
        osm=osm,
        shared_state=shared_state or _shared_state(),
        kill_switch=kill_switch,
    )
    return placer, osm


# ---------------------------------------------------------------------------
# 1. Paper mode entry simulates fill → FILLED order returned
# ---------------------------------------------------------------------------

def test_paper_mode_entry_simulates_fill():
    """place_entry() in paper mode returns an Order in FILLED state."""
    placer, osm = _make_placer()
    signal = _make_signal()

    order = asyncio.run(placer.place_entry(signal, qty=10))

    assert order is not None
    assert order.state == OrderState.FILLED
    assert order.order_type == "ENTRY"
    assert order.symbol == "RELIANCE"
    assert order.qty == 10
    assert order.order_id.startswith("PAPER-")


# ---------------------------------------------------------------------------
# 2. Paper mode fill price equals theoretical entry
# ---------------------------------------------------------------------------

def test_paper_mode_fill_price_equals_theoretical():
    """Paper mode entry fill_price must equal signal.theoretical_entry."""
    placer, _ = _make_placer()
    signal = _make_signal(entry="2487.50")

    order = asyncio.run(placer.place_entry(signal, qty=5))

    assert order is not None
    assert order.fill_price == Decimal("2487.50")


# ---------------------------------------------------------------------------
# 3. Paper mode exit TARGET uses signal target price
# ---------------------------------------------------------------------------

def test_paper_mode_exit_target_uses_signal_target():
    """place_exit() TARGET exit uses the supplied target price."""
    placer, osm = _make_placer()
    signal = _make_signal(entry="2500", stop="2450", target="2600")

    # Place an entry first
    asyncio.run(placer.place_entry(signal, qty=10))

    # Place TARGET exit with explicit exit_price = target
    exit_order = asyncio.run(
        placer.place_exit("RELIANCE", "TARGET", qty=10, exit_price=Decimal("2600"))
    )

    assert exit_order is not None
    assert exit_order.state == OrderState.FILLED
    assert exit_order.fill_price == Decimal("2600")
    assert exit_order.exit_type == "TARGET"


# ---------------------------------------------------------------------------
# 4. Paper mode exit STOP uses signal stop_loss price
# ---------------------------------------------------------------------------

def test_paper_mode_exit_stop_uses_signal_stop():
    """place_exit() STOP exit uses the supplied stop_loss price."""
    placer, osm = _make_placer()
    signal = _make_signal(entry="2500", stop="2450", target="2600")

    asyncio.run(placer.place_entry(signal, qty=10))

    exit_order = asyncio.run(
        placer.place_exit("RELIANCE", "STOP", qty=10, exit_price=Decimal("2450"))
    )

    assert exit_order is not None
    assert exit_order.fill_price == Decimal("2450")
    assert exit_order.exit_type == "STOP"


# ---------------------------------------------------------------------------
# 5. Live mode calls kite.place_order() with correct params
# ---------------------------------------------------------------------------

def test_live_mode_calls_kite_place_order():
    """
    _execute_live_entry() calls kite.place_order() with correct MIS params.

    We test the internal live-mode method directly since the public
    place_entry() would also exercise paper/live routing.
    """
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "LIVE-ORDER-001"

    shared = _shared_state()
    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(
        kite=mock_kite,
        config=_live_config(),
        osm=osm,
        shared_state=shared,
    )

    signal = _make_signal(symbol="TCS", direction="LONG", entry="3500")

    order = asyncio.run(placer._execute_live_entry(signal, qty=5))

    assert order is not None
    mock_kite.place_order.assert_called_once()
    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert call_kwargs["tradingsymbol"] == "TCS"
    assert call_kwargs["transaction_type"] == "BUY"
    assert call_kwargs["quantity"] == 5
    assert call_kwargs["product"] == "MIS"
    assert call_kwargs["order_type"] == "MARKET"
    assert call_kwargs["exchange"] == "NSE"


def test_live_mode_short_uses_sell_transaction():
    """SHORT entries must use SELL transaction type in live mode."""
    mock_kite = MagicMock()
    mock_kite.place_order.return_value = "LIVE-SHORT-001"

    shared = _shared_state()
    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(
        kite=mock_kite,
        config=_live_config(),
        osm=osm,
        shared_state=shared,
    )

    signal = _make_signal(symbol="INFY", direction="SHORT", entry="1700")

    asyncio.run(placer._execute_live_entry(signal, qty=3))

    call_kwargs = mock_kite.place_order.call_args.kwargs
    assert call_kwargs["transaction_type"] == "SELL"


# ---------------------------------------------------------------------------
# 6. Unknown/invalid mode raises AssertionError
# ---------------------------------------------------------------------------

def test_mode_assertion_blocks_wrong_mode():
    """Mode other than 'paper' or 'live' raises AssertionError."""
    placer, _ = _make_placer(config=_bad_config())
    signal = _make_signal()

    with pytest.raises(AssertionError) as exc_info:
        asyncio.run(placer.place_entry(signal, qty=5))

    assert "staging" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Duplicate order blocked before kite.place_order() is called
# ---------------------------------------------------------------------------

def test_duplicate_order_blocked_before_kite_call():
    """Second ENTRY for same symbol returns None without calling kite."""
    mock_kite = MagicMock()
    shared = _shared_state()
    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(
        kite=mock_kite,
        config=_paper_config(),
        osm=osm,
        shared_state=shared,
    )

    signal = _make_signal(symbol="HDFCBANK")

    # First entry — succeeds
    first_order = asyncio.run(placer.place_entry(signal, qty=10))
    assert first_order is not None

    # FILLED orders are terminal → duplicate check passes but we need to
    # manually set an ACTIVE order to test the duplicate block.
    # Create a second OSM entry that is in SUBMITTED (active) state.
    osm.create_order(
        order_id="ACTIVE-DUPLICATE",
        symbol="HDFCBANK",
        instrument_token=341249,
        direction="LONG",
        order_type="ENTRY",
        qty=5,
        price=Decimal("1600"),
    )
    osm.transition("ACTIVE-DUPLICATE", OrderState.SUBMITTED)
    # Now SUBMITTED is active — next entry for HDFCBANK should be blocked

    second_order = asyncio.run(placer.place_entry(signal, qty=5))
    assert second_order is None
    # kite.place_order should never have been called (paper mode — but gate applies)
    mock_kite.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Locked instrument blocked before kite.place_order() is called
# ---------------------------------------------------------------------------

def test_locked_instrument_blocked_before_kite_call():
    """Instrument in locked_instruments returns None without calling kite."""
    mock_kite = MagicMock()
    shared = _shared_state()
    shared["locked_instruments"].add("KOTAKBANK")

    osm = OrderStateMachine(shared_state=shared)
    placer = OrderPlacer(
        kite=mock_kite,
        config=_paper_config(),
        osm=osm,
        shared_state=shared,
    )

    signal = _make_signal(symbol="KOTAKBANK")
    order = asyncio.run(placer.place_entry(signal, qty=5))

    assert order is None
    mock_kite.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Extra: kill switch blocks entry
# ---------------------------------------------------------------------------

def test_kill_switch_blocks_entry():
    """Active kill switch (level > 0) blocks order placement."""
    shared = _shared_state()
    shared["kill_switch_level"] = 1  # Level 1 active
    placer, _ = _make_placer(shared_state=shared)

    signal = _make_signal(symbol="WIPRO")
    order = asyncio.run(placer.place_entry(signal, qty=10))

    assert order is None
