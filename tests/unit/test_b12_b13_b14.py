"""
Tests for B12 (gross_pnl=0.0), B13 (Telegram wrong fields), B14 (exit_reason).

(a) resolve_position_fields with avg_price/side → correct values
(b) resolve_position_fields with entry_price/direction → correct values
(c) resolve_position_fields with mixed fields → correct fallback
(d) SHORT position close: gross_pnl computed correctly (positive when profitable)
(e) LONG position close: gross_pnl computed correctly
(f) Hard exit at 15:00: exit_reason=HARD_EXIT_1500, not KILL_SWITCH
(g) Actual kill switch: exit_reason=KILL_SWITCH (unchanged)
(h) Telegram heartbeat shows correct direction and entry for SHORT position
(i) Telegram hard exit summary shows correct P&L
(j) No remaining instances of raw .get("entry_price") without fallback (grep test)
"""
import subprocess
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.position_helpers import resolve_position_fields


# -----------------------------------------------------------------------
# (a) resolve_position_fields with ExitManager schema (avg_price / side)
# -----------------------------------------------------------------------

def test_resolve_avg_price_side():
    pos = {"avg_price": 1825.50, "side": "SELL", "qty": -71}
    entry, direction, qty = resolve_position_fields(pos)
    assert entry == 1825.50
    assert direction == "SHORT"
    assert qty == 71  # abs applied


def test_resolve_avg_price_buy():
    pos = {"avg_price": 500.0, "side": "BUY", "qty": 100}
    entry, direction, qty = resolve_position_fields(pos)
    assert entry == 500.0
    assert direction == "LONG"
    assert qty == 100


# -----------------------------------------------------------------------
# (b) resolve_position_fields with PnlTracker schema (entry_price / direction)
# -----------------------------------------------------------------------

def test_resolve_entry_price_direction():
    pos = {"entry_price": Decimal("1825.50"), "direction": "SHORT", "qty": 71}
    entry, direction, qty = resolve_position_fields(pos)
    assert entry == 1825.50
    assert direction == "SHORT"
    assert qty == 71


def test_resolve_entry_price_long():
    pos = {"entry_price": 500.0, "direction": "LONG", "qty": 100}
    entry, direction, qty = resolve_position_fields(pos)
    assert entry == 500.0
    assert direction == "LONG"
    assert qty == 100


# -----------------------------------------------------------------------
# (c) resolve_position_fields with mixed / missing fields
# -----------------------------------------------------------------------

def test_resolve_mixed_fields():
    """entry_price takes priority over avg_price when both present."""
    pos = {"entry_price": 1800.0, "avg_price": 1825.50, "direction": "SHORT", "qty": 71}
    entry, direction, qty = resolve_position_fields(pos)
    assert entry == 1800.0  # entry_price wins
    assert direction == "SHORT"
    assert qty == 71


def test_resolve_empty_dict():
    entry, direction, qty = resolve_position_fields({})
    assert entry == 0.0
    assert direction == "LONG"  # default: side="BUY" → LONG
    assert qty == 0


# -----------------------------------------------------------------------
# (d) SHORT position close: gross_pnl positive when profitable
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b12_short_gross_pnl_positive():
    """
    SUNPHARMA SHORT: entry=1825.50, exit=1805.00, qty=71.
    gross_pnl = (1825.50 - 1805.00) × 71 = +₹1,455.50
    """
    from risk_manager.pnl_tracker import PnlTracker

    shared_state = {"open_positions": {}, "daily_pnl_pct": 0.0, "daily_pnl_rs": 0.0}
    tracker = PnlTracker(
        capital=Decimal("1000000"),
        shared_state=shared_state,
    )

    # Mock charge calculator
    mock_breakdown = MagicMock()
    mock_breakdown.total = Decimal("94.35")
    tracker._charge_calc.calculate = MagicMock(return_value=mock_breakdown)

    # Register position
    tracker.on_fill(
        symbol="SUNPHARMA",
        direction="SHORT",
        qty=71,
        fill_price=Decimal("1825.50"),
        order_id="TEST-001",
        signal_id=1,
    )

    # Close position
    result = tracker.on_close(
        symbol="SUNPHARMA",
        exit_price=Decimal("1805.00"),
        exit_reason="HARD_EXIT_1500",
        exit_order_id="TEST-EXIT-001",
    )

    expected_gross = Decimal("1825.50") - Decimal("1805.00")
    expected_gross *= Decimal("71")
    assert result.gross_pnl == expected_gross  # +1455.50
    assert result.gross_pnl > 0
    assert result.net_pnl == expected_gross - Decimal("94.35")
    assert result.exit_reason == "HARD_EXIT_1500"


# -----------------------------------------------------------------------
# (e) LONG position close: gross_pnl positive when profitable
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b12_long_gross_pnl_positive():
    """
    HCLTECH LONG: entry=1370.60, exit=1421.40, qty=127.
    gross_pnl = (1421.40 - 1370.60) × 127 = +₹6,451.80
    """
    from risk_manager.pnl_tracker import PnlTracker

    shared_state = {"open_positions": {}, "daily_pnl_pct": 0.0, "daily_pnl_rs": 0.0}
    tracker = PnlTracker(
        capital=Decimal("1000000"),
        shared_state=shared_state,
    )

    mock_breakdown = MagicMock()
    mock_breakdown.total = Decimal("100.00")
    tracker._charge_calc.calculate = MagicMock(return_value=mock_breakdown)

    tracker.on_fill(
        symbol="HCLTECH",
        direction="LONG",
        qty=127,
        fill_price=Decimal("1370.60"),
        order_id="TEST-002",
        signal_id=2,
    )

    result = tracker.on_close(
        symbol="HCLTECH",
        exit_price=Decimal("1421.40"),
        exit_reason="TARGET_HIT",
        exit_order_id="TEST-EXIT-002",
    )

    expected_gross = (Decimal("1421.40") - Decimal("1370.60")) * Decimal("127")
    assert result.gross_pnl == expected_gross  # +6451.80
    assert result.gross_pnl > 0


# -----------------------------------------------------------------------
# (f) Hard exit at 15:00: exit_reason=HARD_EXIT_1500
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b14_hard_exit_uses_hard_exit_type():
    """emergency_exit_all called with exit_type='HARD_EXIT' uses HARD_EXIT, not KILL_SWITCH."""
    from execution_engine.exit_manager import ExitManager

    mock_placer = AsyncMock()
    mock_order = MagicMock()
    mock_placer.place_exit.return_value = mock_order

    shared_state = {
        "open_positions": {},
        "last_tick_prices": {"SUNPHARMA": 1805.00},
    }

    em = ExitManager(
        order_placer=mock_placer,
        shared_state=shared_state,
        config={},
    )

    # Register a position
    await em.register_position(
        symbol="SUNPHARMA",
        direction="SHORT",
        entry_price=Decimal("1825.50"),
        stop_loss=Decimal("1850.00"),
        target=Decimal("1780.00"),
        qty=71,
        signal_id=1,
    )

    # Call emergency_exit_all with HARD_EXIT type (as main.py 15:00 path does)
    await em.emergency_exit_all("hard_exit_1500", exit_type="HARD_EXIT")

    # Verify place_exit was called with exit_type="HARD_EXIT", not "KILL_SWITCH"
    mock_placer.place_exit.assert_called_once()
    call_kwargs = mock_placer.place_exit.call_args
    assert call_kwargs[1]["exit_type"] == "HARD_EXIT"
    # Verify exit price is tick price (1805.00), not entry price (1825.50)
    assert float(call_kwargs[1]["exit_price"]) == pytest.approx(1805.00)


# -----------------------------------------------------------------------
# (g) Actual kill switch: exit_reason=KILL_SWITCH (unchanged)
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b14_kill_switch_keeps_kill_switch_type():
    """emergency_exit_all with default exit_type keeps KILL_SWITCH."""
    from execution_engine.exit_manager import ExitManager

    mock_placer = AsyncMock()
    mock_order = MagicMock()
    mock_placer.place_exit.return_value = mock_order

    shared_state = {
        "open_positions": {},
        "last_tick_prices": {"RELIANCE": 2500.00},
    }

    em = ExitManager(
        order_placer=mock_placer,
        shared_state=shared_state,
        config={},
    )

    await em.register_position(
        symbol="RELIANCE",
        direction="LONG",
        entry_price=Decimal("2500.00"),
        stop_loss=Decimal("2450.00"),
        target=Decimal("2600.00"),
        qty=70,
        signal_id=2,
    )

    # Default call (kill switch path) — no exit_type argument
    await em.emergency_exit_all("daily_loss_3pct")

    call_kwargs = mock_placer.place_exit.call_args
    assert call_kwargs[1]["exit_type"] == "KILL_SWITCH"


# -----------------------------------------------------------------------
# (h) Telegram heartbeat shows correct direction and entry for SHORT
# -----------------------------------------------------------------------

def test_b13_heartbeat_short_position():
    """Heartbeat must show SHORT and correct entry for avg_price/side schema."""
    from utils.telegram_notifier import TelegramNotifier

    shared_state = {
        "open_positions": {
            "SUNPHARMA": {
                "avg_price": 1825.50,
                "side": "SELL",
                "qty": -71,
                "entry_time": None,
            },
        },
        "last_tick_prices": {"SUNPHARMA": 1805.00},
        "daily_pnl_rs": 0.0,
        "market_regime": "bear_trend",
        "signals_generated_today": 3,
        "signals_rejected_today": 1,
    }

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier._shared_state = shared_state
    notifier._config_cache = {}
    notifier._config_path = "config/telegram_alerts.yaml"
    notifier._config_mtime = 0

    msg = notifier._fmt_heartbeat()

    # Must show SHORT, not LONG
    assert "SHORT" in msg
    assert "LONG" not in msg
    # Must show correct entry price
    assert "1825.50" in msg
    # Must show correct unrealized P&L direction (profitable SHORT)
    assert "+" in msg  # positive P&L for profitable SHORT


# -----------------------------------------------------------------------
# (i) Telegram hard exit summary shows correct P&L
# -----------------------------------------------------------------------

def test_b13_hard_exit_short_pnl():
    """Hard exit summary must show correct direction and positive P&L for profitable SHORT."""
    from utils.telegram_notifier import TelegramNotifier

    notifier = TelegramNotifier.__new__(TelegramNotifier)
    notifier._shared_state = {}

    positions_snapshot = {
        "SUNPHARMA": {
            "avg_price": 1825.50,
            "side": "SELL",
            "qty": -71,
        },
    }
    tick_prices = {"SUNPHARMA": 1805.00}

    msg = notifier._fmt_hard_exit(positions_snapshot, tick_prices, 0.0)

    # Must show SHORT
    assert "SHORT" in msg
    # Must show correct entry
    assert "1825" in msg
    # Must show positive P&L (profitable short: 1825.50 - 1805.00 = +20.50 × 71)
    expected_pnl = (1825.50 - 1805.00) * 71  # +1455.50
    assert "+₹1456" in msg or "+₹1455" in msg


# -----------------------------------------------------------------------
# (j) No remaining raw .get("entry_price") outside helpers and tests
# -----------------------------------------------------------------------

def test_no_raw_entry_price_access():
    """
    grep the codebase: .get("entry_price") must only appear in:
    - utils/position_helpers.py (the resolver itself)
    - tests/
    - risk_manager/pnl_tracker.py (reads from its OWN registry, not shared_state)
    - execution_engine/exit_manager.py (reads from its OWN _positions registry)
    """
    result = subprocess.run(
        [
            "grep", "-rn", '.get("entry_price"',
            "--include=*.py",
            ".",
        ],
        capture_output=True,
        text=True,
        cwd=sys.path[0] or ".",
    )

    lines = [
        line for line in result.stdout.strip().split("\n")
        if line  # skip empty
        and "test_" not in line  # tests are allowed
        and "position_helpers.py" not in line  # the resolver itself
        and "pnl_tracker.py" not in line  # PnlTracker's own registry
        and "exit_manager.py" not in line  # ExitManager's own registry
        and "session_report.py" not in line  # log field parsing
        and ".claude/" not in line  # skill workspaces
    ]

    assert lines == [], (
        f"Found raw .get('entry_price') outside allowed files:\n"
        + "\n".join(lines)
    )


def test_no_raw_direction_access_in_shared_state_readers():
    """
    Files that read from shared_state["open_positions"] must not use
    raw .get("direction") — they should use resolve_position_fields().

    Allowed files: position_helpers.py, tests, pnl_tracker.py (own registry),
    exit_manager.py (own registry), session_report.py (log parsing),
    hawk_engine/ (HAWK pick dicts, not position data).
    """
    result = subprocess.run(
        [
            "grep", "-rn", '.get("direction"',
            "--include=*.py",
            ".",
        ],
        capture_output=True,
        text=True,
        cwd=sys.path[0] or ".",
    )

    lines = [
        line for line in result.stdout.strip().split("\n")
        if line
        and "test_" not in line
        and "position_helpers.py" not in line
        and "pnl_tracker.py" not in line
        and "exit_manager.py" not in line
        and "session_report.py" not in line  # log field parsing
        and "hawk_engine/" not in line  # HAWK pick dicts
        and ".claude/" not in line  # skill workspaces
    ]

    assert lines == [], (
        f"Found raw .get('direction') in shared_state readers:\n"
        + "\n".join(lines)
    )
