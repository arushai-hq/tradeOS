"""
Unit tests for risk_manager.pnl_tracker.PnlTracker.

D8 mandatory test catalogue:
  test_on_fill_updates_open_positions
  test_on_close_calculates_gross_pnl_long
  test_on_close_calculates_gross_pnl_short
  test_on_close_subtracts_charges
  test_daily_pnl_accumulates_across_trades
  test_daily_pnl_pct_written_to_shared_state
  test_reset_daily_zeroes_pnl
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from risk_manager.pnl_tracker import PnlTracker, TradeResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CAPITAL = Decimal("500000")


@pytest.fixture
def state() -> dict:
    return {
        "open_positions": {},
        "daily_pnl_pct": 0.0,
        "daily_pnl_rs": 0.0,
    }


@pytest.fixture
def tracker(state: dict) -> PnlTracker:
    return PnlTracker(capital=CAPITAL, shared_state=state)


def _fill(tracker: PnlTracker, symbol: str = "RELIANCE", direction: str = "LONG",
          qty: int = 100, price: Decimal = Decimal("2500")) -> None:
    """Helper to open a position."""
    tracker.on_fill(
        symbol=symbol,
        direction=direction,
        qty=qty,
        fill_price=price,
        order_id="ORDER-1",
        signal_id=1,
    )


def _close(tracker: PnlTracker, symbol: str = "RELIANCE",
           exit_price: Decimal = Decimal("2600"),
           reason: str = "TARGET_HIT") -> TradeResult:
    """Helper to close a position."""
    return tracker.on_close(
        symbol=symbol,
        exit_price=exit_price,
        exit_reason=reason,
        exit_order_id="EXIT-1",
    )


# ---------------------------------------------------------------------------
# test_on_fill_updates_open_positions
# ---------------------------------------------------------------------------

def test_on_fill_updates_open_positions(tracker: PnlTracker, state: dict):
    """on_fill must add the symbol to open_positions and update shared_state."""
    _fill(tracker)

    positions = tracker.get_open_positions()
    assert "RELIANCE" in positions
    assert positions["RELIANCE"]["qty"] == 100
    assert positions["RELIANCE"]["entry_price"] == Decimal("2500")
    assert positions["RELIANCE"]["direction"] == "LONG"

    # shared_state["open_positions"] mirrors internal state
    assert "RELIANCE" in state["open_positions"]


def test_on_fill_get_open_positions_returns_copy(tracker: PnlTracker):
    """get_open_positions() must return a copy, not the internal reference."""
    _fill(tracker)
    copy = tracker.get_open_positions()
    copy["RELIANCE"]["qty"] = 9999  # mutate the copy

    # internal state is unchanged
    assert tracker.get_open_positions()["RELIANCE"]["qty"] == 100


# ---------------------------------------------------------------------------
# test_on_close_calculates_gross_pnl_long
# ---------------------------------------------------------------------------

def test_on_close_calculates_gross_pnl_long(tracker: PnlTracker):
    """
    entry=2500, exit=2600, qty=100, LONG
    gross = (2600 - 2500) * 100 = 10000
    """
    _fill(tracker, price=Decimal("2500"))
    result = _close(tracker, exit_price=Decimal("2600"))

    assert result.gross_pnl == Decimal("10000")
    assert result.direction == "LONG"


def test_on_close_calculates_gross_pnl_short(tracker: PnlTracker, state: dict):
    """
    entry=2500, exit=2400, qty=100, SHORT
    gross = (2500 - 2400) * 100 = 10000
    """
    _fill(tracker, direction="SHORT", price=Decimal("2500"))
    result = tracker.on_close(
        symbol="RELIANCE",
        exit_price=Decimal("2400"),
        exit_reason="TARGET_HIT",
        exit_order_id="EXIT-1",
    )

    assert result.gross_pnl == Decimal("10000")
    assert result.direction == "SHORT"


def test_on_close_loss_long(tracker: PnlTracker):
    """LONG trade that closes at a loss: exit < entry → negative gross."""
    _fill(tracker, price=Decimal("2500"))
    result = _close(tracker, exit_price=Decimal("2450"))

    assert result.gross_pnl == Decimal("-5000")


# ---------------------------------------------------------------------------
# test_on_close_subtracts_charges
# ---------------------------------------------------------------------------

def test_on_close_subtracts_charges(tracker: PnlTracker):
    """net_pnl = gross_pnl - charges. Charges must be > 0."""
    _fill(tracker, price=Decimal("2500"))
    result = _close(tracker, exit_price=Decimal("2600"))

    assert result.charges > Decimal("0")
    assert result.net_pnl == result.gross_pnl - result.charges


def test_on_close_removes_from_open_positions(tracker: PnlTracker, state: dict):
    """After close, symbol must be removed from open_positions and shared_state."""
    _fill(tracker)
    _close(tracker)

    assert "RELIANCE" not in tracker.get_open_positions()
    assert "RELIANCE" not in state["open_positions"]


# ---------------------------------------------------------------------------
# test_daily_pnl_accumulates_across_trades
# ---------------------------------------------------------------------------

def test_daily_pnl_accumulates_across_trades(tracker: PnlTracker, state: dict):
    """
    Two trades:
      Trade 1: RELIANCE LONG, entry=2500, exit=2600, qty=100 → gross=10000
      Trade 2: INFY    LONG, entry=1500, exit=1400, qty=50 → gross=-5000
    net_pnl of each differs from gross by charges, but cumulative direction is positive.
    """
    # Trade 1: win
    _fill(tracker, symbol="RELIANCE", price=Decimal("2500"))
    r1 = tracker.on_close("RELIANCE", Decimal("2600"), "TARGET_HIT", "E1")

    # Trade 2: loss
    _fill(tracker, symbol="INFY", price=Decimal("1500"))
    r2 = tracker.on_close("INFY", Decimal("1400"), "STOP_HIT", "E2")

    expected_pnl = r1.net_pnl + r2.net_pnl
    assert tracker.get_daily_pnl_pct() == expected_pnl / CAPITAL


# ---------------------------------------------------------------------------
# test_daily_pnl_pct_written_to_shared_state
# ---------------------------------------------------------------------------

def test_daily_pnl_pct_written_to_shared_state(tracker: PnlTracker, state: dict):
    """shared_state["daily_pnl_pct"] must be updated after every close."""
    _fill(tracker, price=Decimal("2500"))
    _close(tracker, exit_price=Decimal("2600"))

    # Must be a float (shared_state uses float for JSON serialisation compat)
    assert isinstance(state["daily_pnl_pct"], float)
    assert state["daily_pnl_pct"] > 0.0  # winning trade


def test_daily_pnl_rs_written_to_shared_state(tracker: PnlTracker, state: dict):
    """shared_state["daily_pnl_rs"] must also be updated."""
    _fill(tracker, price=Decimal("2500"))
    result = _close(tracker, exit_price=Decimal("2600"))

    assert state["daily_pnl_rs"] == float(result.net_pnl)


# ---------------------------------------------------------------------------
# test_reset_daily_zeroes_pnl
# ---------------------------------------------------------------------------

def test_reset_daily_zeroes_pnl(tracker: PnlTracker, state: dict):
    """reset_daily() must clear accumulated P&L and update shared_state to 0."""
    # Accumulate some P&L
    _fill(tracker, price=Decimal("2500"))
    _close(tracker, exit_price=Decimal("2600"))

    assert tracker.get_daily_pnl_pct() != Decimal("0")

    # Reset
    tracker.reset_daily()

    assert tracker.get_daily_pnl_pct() == Decimal("0")
    assert state["daily_pnl_pct"] == 0.0
    assert state["daily_pnl_rs"] == 0.0


# ---------------------------------------------------------------------------
# Additional: TradeResult dataclass
# ---------------------------------------------------------------------------

def test_trade_result_pnl_pct_calculation(tracker: PnlTracker):
    """pnl_pct = net_pnl / (qty * entry_price)."""
    _fill(tracker, qty=100, price=Decimal("2500"))
    result = _close(tracker, exit_price=Decimal("2600"))

    position_value = Decimal("100") * Decimal("2500")
    expected_pct = result.net_pnl / position_value
    assert result.pnl_pct == expected_pct
