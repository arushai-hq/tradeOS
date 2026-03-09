"""
TradeOS — Unit tests for B4: unrealized P&L in heartbeat.

Root cause fixed: daily_pnl_pct was stuck at 0.0 because:
  1. PnlTracker.on_fill() does not update daily_pnl_pct (only on_close does)
  2. heartbeat() read shared_state["daily_pnl_pct"] without computing unrealized P&L
  3. shared_state["last_tick_prices"] was never populated from the tick pipeline

Fix:
  - DataEngine.run() writes last_tick_prices[symbol] on every validated tick
  - _compute_unrealized_pnl() computes open-position P&L from last tick prices
  - heartbeat() calls it every 30s and updates daily_pnl_pct = (realized + unrealized) / capital

Tests:
  (a) test_b4_unrealized_pnl_long_profit
  (b) test_b4_unrealized_pnl_short_profit
  (c) test_b4_no_tick_price_yields_zero_unrealized
  (d) test_b4_combined_realized_and_unrealized_updates_pnl_pct
"""
from __future__ import annotations

import pytest

from main import _compute_unrealized_pnl


# ---------------------------------------------------------------------------
# (a) LONG position: price moved up → positive unrealized P&L
# ---------------------------------------------------------------------------

def test_b4_unrealized_pnl_long_profit():
    """
    (a) LONG RELIANCE: entry=2450, current=2480, qty=5
    unrealized = (2480 - 2450) * 5 = 150.0
    """
    open_positions = {
        "RELIANCE": {
            "direction": "LONG",
            "qty": 5,
            "entry_price": 2450.0,
        }
    }
    tick_prices = {"RELIANCE": 2480.0}

    result = _compute_unrealized_pnl(open_positions, tick_prices)

    assert result == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# (b) SHORT position: price moved down → positive unrealized P&L
# ---------------------------------------------------------------------------

def test_b4_unrealized_pnl_short_profit():
    """
    (b) SHORT INFY: entry=1500, current=1450, qty=4
    unrealized = (1500 - 1450) * 4 = 200.0 (profitable short)
    """
    open_positions = {
        "INFY": {
            "direction": "SHORT",
            "qty": 4,
            "entry_price": 1500.0,
        }
    }
    tick_prices = {"INFY": 1450.0}

    result = _compute_unrealized_pnl(open_positions, tick_prices)

    assert result == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# (c) Position open but no tick price → unrealized = 0.0 (safe default)
# ---------------------------------------------------------------------------

def test_b4_no_tick_price_yields_zero_unrealized():
    """
    (c) If last_tick_prices has no entry for the symbol, skip that position.
    No crash, no fabricated P&L.
    """
    open_positions = {
        "RELIANCE": {
            "direction": "LONG",
            "qty": 5,
            "entry_price": 2450.0,
        }
    }
    tick_prices = {}  # no prices yet

    result = _compute_unrealized_pnl(open_positions, tick_prices)

    assert result == 0.0


# ---------------------------------------------------------------------------
# (d) Combined realized + unrealized → daily_pnl_pct correct
# ---------------------------------------------------------------------------

def test_b4_combined_realized_and_unrealized_updates_pnl_pct():
    """
    (d) Simulate heartbeat P&L update:
      - realized_pnl_rs = 500.0 (from a previous closed trade in daily_pnl_rs)
      - 1 open LONG position: entry=2450, current=2480, qty=5 → unrealized=150
      - capital = 500000
      - expected daily_pnl_pct = (500 + 150) / 500000 = 0.0013
    """
    open_positions = {
        "RELIANCE": {
            "direction": "LONG",
            "qty": 5,
            "entry_price": 2450.0,
        }
    }
    tick_prices = {"RELIANCE": 2480.0}
    realized_pnl_rs = 500.0
    capital = 500_000.0

    unrealized_rs = _compute_unrealized_pnl(open_positions, tick_prices)
    total_rs = realized_pnl_rs + unrealized_rs
    daily_pnl_pct = total_rs / capital

    assert unrealized_rs == pytest.approx(150.0)
    assert total_rs == pytest.approx(650.0)
    assert daily_pnl_pct == pytest.approx(0.0013)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_b4_unrealized_pnl_multiple_positions():
    """Multiple open positions: unrealized P&L is the sum across all symbols."""
    open_positions = {
        "RELIANCE": {"direction": "LONG", "qty": 5, "entry_price": 2450.0},
        "INFY":     {"direction": "SHORT", "qty": 3, "entry_price": 1500.0},
    }
    tick_prices = {
        "RELIANCE": 2480.0,   # +30 * 5 = +150
        "INFY":     1460.0,   # (1500 - 1460) * 3 = +120
    }

    result = _compute_unrealized_pnl(open_positions, tick_prices)

    assert result == pytest.approx(270.0)


def test_b4_unrealized_pnl_long_loss():
    """LONG position at a loss: price fell below entry → negative unrealized."""
    open_positions = {
        "TCS": {"direction": "LONG", "qty": 2, "entry_price": 3000.0},
    }
    tick_prices = {"TCS": 2950.0}  # -50 * 2 = -100

    result = _compute_unrealized_pnl(open_positions, tick_prices)

    assert result == pytest.approx(-100.0)
