"""
Tests for B7: SHORT position unrealized P&L fix.

Root cause: ExitManager writes open_positions with different schema than PnlTracker:
  PnlTracker:  {"direction": "LONG/SHORT", "entry_price": X, "qty": N}
  ExitManager: {"side": "BUY/SELL", "avg_price": X, "qty": ±N}

ExitManager writes AFTER PnlTracker, overwriting with a schema that
_compute_unrealized_pnl() couldn't read correctly → direction defaulted to LONG,
entry_price defaulted to 0.0, qty was negative.

Fix: _compute_unrealized_pnl() now handles both schemas:
  - direction from "direction" key, falls back to inferring from "side"
  - entry_price from "entry_price" key, falls back to "avg_price"
  - qty uses abs() to handle negative qty (SHORT convention in ExitManager)
  - Skips positions with no tick price or price <= 0

Tests:
  (a) SHORT profit: stock drops → positive P&L
  (b) SHORT loss: stock rises → negative P&L
  (c) LONG regression: unchanged behavior
  (d) No-tick-yet: position exists, no tick → unrealized = 0
  (e) Mixed: 1 LONG profit + 1 SHORT loss → correct combined
  (f) Kill switch doesn't trigger on near-zero P&L
"""
from __future__ import annotations

import pytest

from main import _compute_unrealized_pnl


# ExitManager schema — the one that actually persists in shared_state
def _exit_mgr_pos(direction: str, qty: int, entry_price: float) -> dict:
    """Build a position dict in ExitManager's schema."""
    return {
        "qty": qty if direction == "LONG" else -qty,
        "avg_price": entry_price,
        "side": "BUY" if direction == "LONG" else "SELL",
    }


# ---------------------------------------------------------------------------
# (a) SHORT profit: stock drops → positive unrealized P&L
# ---------------------------------------------------------------------------

def test_b7_short_profit_stock_drops():
    """
    AXISBANK SHORT: entry=1288.9, current=1270.0, qty=155
    unrealized = (1288.9 - 1270.0) * 155 = 2929.5
    """
    positions = {"AXISBANK": _exit_mgr_pos("SHORT", 155, 1288.9)}
    ticks = {"AXISBANK": 1270.0}

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == pytest.approx(2929.5)


# ---------------------------------------------------------------------------
# (b) SHORT loss: stock rises → negative unrealized P&L
# ---------------------------------------------------------------------------

def test_b7_short_loss_stock_rises():
    """
    LT SHORT: entry=3883.1, current=3900.0, qty=51
    unrealized = (3883.1 - 3900.0) * 51 = -861.9
    """
    positions = {"LT": _exit_mgr_pos("SHORT", 51, 3883.1)}
    ticks = {"LT": 3900.0}

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == pytest.approx(-861.9)


# ---------------------------------------------------------------------------
# (c) LONG regression: behavior unchanged
# ---------------------------------------------------------------------------

def test_b7_long_profit_regression():
    """
    RELIANCE LONG: entry=2450, current=2480, qty=5
    unrealized = (2480 - 2450) * 5 = 150.0
    Must work with ExitManager schema too.
    """
    positions = {"RELIANCE": _exit_mgr_pos("LONG", 5, 2450.0)}
    ticks = {"RELIANCE": 2480.0}

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# (d) No-tick-yet: position exists, no tick received → unrealized = 0
# ---------------------------------------------------------------------------

def test_b7_no_tick_yet_yields_zero():
    """
    Position exists but last_tick_prices has no entry for the symbol.
    Must contribute ₹0, not crash or fabricate phantom loss.
    """
    positions = {
        "AXISBANK": _exit_mgr_pos("SHORT", 155, 1288.9),
        "LT": _exit_mgr_pos("SHORT", 51, 3883.1),
    }
    ticks = {}  # no ticks received yet

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == 0.0


def test_b7_tick_price_zero_yields_zero():
    """
    Tick price is 0.0 (not None) — must skip, not compute phantom P&L.
    Before fix: (0 - 0) * qty or (entry - 0) * qty → huge phantom number.
    """
    positions = {"AXISBANK": _exit_mgr_pos("SHORT", 155, 1288.9)}
    ticks = {"AXISBANK": 0.0}

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == 0.0


# ---------------------------------------------------------------------------
# (e) Mixed: 1 LONG profit + 1 SHORT loss → correct combined P&L
# ---------------------------------------------------------------------------

def test_b7_mixed_long_profit_short_loss():
    """
    RELIANCE LONG: entry=2450, current=2480, qty=5 → +150
    LT SHORT: entry=3883.1, current=3900.0, qty=51 → -861.9
    Combined: 150 + (-861.9) = -711.9
    """
    positions = {
        "RELIANCE": _exit_mgr_pos("LONG", 5, 2450.0),
        "LT": _exit_mgr_pos("SHORT", 51, 3883.1),
    }
    ticks = {"RELIANCE": 2480.0, "LT": 3900.0}

    result = _compute_unrealized_pnl(positions, ticks)
    assert result == pytest.approx(-711.9)


# ---------------------------------------------------------------------------
# (f) Kill switch does NOT trigger on correctly near-zero P&L
# ---------------------------------------------------------------------------

def test_b7_kill_switch_no_false_trigger():
    """
    Session 04 reproduction: 2 SHORT positions, filled 29 seconds ago,
    price barely moved. Unrealized P&L should be near zero, not -₹199,679.

    Before fix: direction defaulted to LONG, entry_price defaulted to 0,
    qty was negative → phantom -₹199,679 → kill switch at 3%.

    After fix: correct SHORT formula, real entry_price, abs(qty) →
    unrealized near ₹0. daily_pnl_pct < 3% threshold.
    """
    capital = 500_000.0

    # Session 04 exact scenario: LT and AXISBANK SHORT, price barely moved
    positions = {
        "LT": _exit_mgr_pos("SHORT", 51, 3883.1),
        "AXISBANK": _exit_mgr_pos("SHORT", 155, 1288.9),
    }
    # Prices moved slightly against (1-2 points up)
    ticks = {"LT": 3885.0, "AXISBANK": 1290.0}

    unrealized = _compute_unrealized_pnl(positions, ticks)

    # LT: (3883.1 - 3885.0) * 51 = -96.9
    # AXISBANK: (1288.9 - 1290.0) * 155 = -170.5
    # Total: -267.4
    assert unrealized == pytest.approx(-267.4)

    # daily_pnl_pct = -267.4 / 500000 = -0.000535 → well under 3% threshold
    daily_pnl_pct = unrealized / capital
    assert abs(daily_pnl_pct) < 0.03, (
        f"Kill switch would false-trigger: daily_pnl_pct={daily_pnl_pct:.6f}"
    )
