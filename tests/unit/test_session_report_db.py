"""
Tests for session_report.py DB mode + verify cross-check.

(a) test_db_report_signals — mock 3 signal rows, verify status mapping
(b) test_db_report_trades — mock 2 trade rows with gross/charges/net
(c) test_db_report_empty_session — empty results → zeroed SessionReport
(d) test_verify_match — identical reports → all match
(e) test_verify_mismatch_pnl — net_pnl differs → mismatch detected
(f) test_verify_mismatch_count — different trade counts → mismatch detected
(g) test_log_report_backward_compatible — log snippet → generate_log_report → correct structure
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

# Add project root to path so tools/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tools.session_report import (
    SessionData,
    SessionParser,
    SessionReport,
    SignalRecord,
    TradeRecord,
    generate_log_report,
    verify_reports,
    _generate_db_report_async,
    _map_signal_status,
)

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_log(lines: list[str], path: str) -> str:
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _mock_signal_row(symbol, direction, status, entry=100.0, stop=95.0,
                     target=110.0, rsi=60.0, vol_ratio=1.5, reject_reason=None):
    """Create a mock asyncpg Record for signals table."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "reject_reason": reject_reason,
        "signal_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST),
        "theoretical_entry": Decimal(str(entry)),
        "stop_loss": Decimal(str(stop)),
        "target": Decimal(str(target)),
        "rsi": Decimal(str(rsi)),
        "volume_ratio": Decimal(str(vol_ratio)),
    }[k]
    return row


def _mock_trade_row(symbol, direction, qty, entry, exit_price, gross, charges, net,
                    exit_reason="HARD_EXIT_1500", signal_stop=95.0, signal_target=110.0):
    """Create a mock asyncpg Record for trades LEFT JOIN signals."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "symbol": symbol,
        "direction": direction,
        "qty": qty,
        "actual_entry": Decimal(str(entry)),
        "actual_exit": Decimal(str(exit_price)) if exit_price is not None else None,
        "entry_time": datetime(2026, 3, 16, 10, 15, 0, tzinfo=IST),
        "exit_time": datetime(2026, 3, 16, 15, 0, 0, tzinfo=IST) if exit_price else None,
        "exit_reason": exit_reason,
        "gross_pnl": Decimal(str(gross)) if gross is not None else None,
        "charges": Decimal(str(charges)) if charges is not None else None,
        "net_pnl": Decimal(str(net)) if net is not None else None,
        "signal_stop": Decimal(str(signal_stop)),
        "signal_target": Decimal(str(signal_target)),
    }[k]
    return row


def _mock_session_row(total_sig=3, accepted=2, rejected=1, total_trades=2,
                      won=1, lost=1, gross=1500.0, charges=100.0, net=1400.0):
    """Create a mock asyncpg Record for sessions table."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "signals_total": total_sig,
        "signals_accepted": accepted,
        "signals_rejected": rejected,
        "trades_total": total_trades,
        "trades_won": won,
        "trades_lost": lost,
        "gross_pnl": Decimal(str(gross)),
        "total_charges": Decimal(str(charges)),
        "net_pnl": Decimal(str(net)),
    }[k]
    return row


# ---------------------------------------------------------------------------
# (a) DB report signals — status mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_report_signals():
    """DB signal rows with FILLED/REJECTED/KILL_SWITCHED map to correct report status."""
    sig_rows = [
        _mock_signal_row("RELIANCE", "LONG", "FILLED"),
        _mock_signal_row("INFY", "SHORT", "REJECTED", reject_reason="SIZER_REJECTED"),
        _mock_signal_row("TCS", "SHORT", "KILL_SWITCHED", reject_reason="KILL_SWITCH_L2"),
    ]
    session_row = _mock_session_row()

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[sig_rows, []])  # signals, trades
    mock_conn.fetchrow = AsyncMock(return_value=session_row)
    mock_conn.close = AsyncMock()

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        report = await _generate_db_report_async("postgresql://test", "2026-03-16")

    assert len(report.signals) == 3
    assert report.signals[0].status == "ACCEPTED"
    assert report.signals[0].symbol == "RELIANCE"
    assert report.signals[1].status == "BLOCKED"
    assert report.signals[1].block_reason == "SIZER_REJECTED"
    assert report.signals[2].status == "BLOCKED"
    assert report.signals[2].block_reason == "KILL_SWITCH_L2"


# ---------------------------------------------------------------------------
# (b) DB report trades — gross/charges/net fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_report_trades():
    """DB trade rows populate TradeRecord with gross, charges, and net_pnl."""
    trade_rows = [
        _mock_trade_row("SUNPHARMA", "SHORT", 71, 1825.50, 1805.10,
                        gross=1448.40, charges=94.25, net=1354.15),
        _mock_trade_row("TITAN", "SHORT", 25, 3400.00, 3395.00,
                        gross=125.00, charges=55.00, net=70.00),
    ]
    session_row = _mock_session_row(total_trades=2, won=2, lost=0,
                                    gross=1573.40, charges=149.25, net=1424.15)

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[[], trade_rows])  # signals, trades
    mock_conn.fetchrow = AsyncMock(return_value=session_row)
    mock_conn.close = AsyncMock()

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        report = await _generate_db_report_async("postgresql://test", "2026-03-16")

    assert len(report.trades) == 2
    t1 = report.trades[0]
    assert t1.symbol == "SUNPHARMA"
    assert t1.pnl_rs == 1448.40
    assert t1.charges == 94.25
    assert t1.net_pnl == 1354.15
    assert t1.qty == 71
    assert t1.exit_reason == "HARD_EXIT_1500"

    assert report.gross_pnl == 1573.40
    assert report.total_charges == 149.25
    assert report.net_pnl == 1424.15


# ---------------------------------------------------------------------------
# (c) DB report empty session — zeroed SessionReport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_report_empty_session():
    """Empty DB results produce zeroed SessionReport."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[[], []])  # empty signals, trades
    mock_conn.fetchrow = AsyncMock(return_value=None)  # no sessions row
    mock_conn.close = AsyncMock()

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        report = await _generate_db_report_async("postgresql://test", "2026-03-16")

    assert report.source == "db"
    assert report.session_date == "2026-03-16"
    assert report.total_signals == 0
    assert report.total_trades == 0
    assert report.gross_pnl == 0.0
    assert report.net_pnl == 0.0
    assert len(report.signals) == 0
    assert len(report.trades) == 0


# ---------------------------------------------------------------------------
# (d) Verify match — identical reports
# ---------------------------------------------------------------------------

def test_verify_match():
    """Identical SessionReport objects produce all-match results."""
    trades = [
        TradeRecord(symbol="RELIANCE", direction="LONG", entry_price=2500.0,
                    qty=50, stop_loss=2450.0, target=2600.0,
                    exit_price=2580.0, exit_reason="TARGET_HIT", pnl_rs=4000.0),
    ]
    log_report = SessionReport(
        source="log", session_date="2026-03-16",
        signals=[], trades=trades,
        total_signals=3, signals_accepted=2, signals_rejected=1,
        total_trades=1, trades_won=1, trades_lost=0,
        gross_pnl=4000.0, total_charges=0.0, net_pnl=4000.0,
    )
    db_report = SessionReport(
        source="db", session_date="2026-03-16",
        signals=[], trades=[
            TradeRecord(symbol="RELIANCE", direction="LONG", entry_price=2500.0,
                        qty=50, stop_loss=2450.0, target=2600.0,
                        exit_price=2580.0, exit_reason="TARGET_HIT",
                        pnl_rs=4000.0, charges=100.0, net_pnl=3900.0),
        ],
        total_signals=3, signals_accepted=2, signals_rejected=1,
        total_trades=1, trades_won=1, trades_lost=0,
        gross_pnl=4000.0, total_charges=100.0, net_pnl=4000.0,
    )

    results = verify_reports(log_report, db_report)
    assert all(r.match for r in results), f"Mismatches: {[(r.field, r.log_value, r.db_value) for r in results if not r.match]}"


# ---------------------------------------------------------------------------
# (e) Verify mismatch P&L — gross_pnl differs beyond tolerance
# ---------------------------------------------------------------------------

def test_verify_mismatch_pnl():
    """gross_pnl differing by >2.0 triggers mismatch (like-for-like comparison)."""
    log_report = SessionReport(
        source="log", session_date="2026-03-16",
        signals=[], trades=[],
        total_signals=0, signals_accepted=0, signals_rejected=0,
        total_trades=0, trades_won=0, trades_lost=0,
        gross_pnl=1500.0, total_charges=0.0, net_pnl=1500.0,
    )
    db_report = SessionReport(
        source="db", session_date="2026-03-16",
        signals=[], trades=[],
        total_signals=0, signals_accepted=0, signals_rejected=0,
        total_trades=0, trades_won=0, trades_lost=0,
        gross_pnl=1495.0, total_charges=100.0, net_pnl=1395.0,
    )

    results = verify_reports(log_report, db_report)
    pnl_results = [r for r in results if r.field == "session_gross_pnl"]
    assert len(pnl_results) == 1
    assert pnl_results[0].match is False


# ---------------------------------------------------------------------------
# (f) Verify mismatch count — different trade counts
# ---------------------------------------------------------------------------

def test_verify_mismatch_count():
    """Different trade counts produce a mismatch."""
    log_report = SessionReport(
        source="log", session_date="2026-03-16",
        signals=[], trades=[
            TradeRecord(symbol="RELIANCE", direction="LONG", entry_price=2500.0,
                        qty=50, stop_loss=2450.0, target=2600.0),
        ],
        total_signals=1, signals_accepted=1, signals_rejected=0,
        total_trades=1, trades_won=0, trades_lost=0,
        gross_pnl=0.0, total_charges=0.0, net_pnl=0.0,
    )
    db_report = SessionReport(
        source="db", session_date="2026-03-16",
        signals=[], trades=[
            TradeRecord(symbol="RELIANCE", direction="LONG", entry_price=2500.0,
                        qty=50, stop_loss=2450.0, target=2600.0),
            TradeRecord(symbol="INFY", direction="SHORT", entry_price=1500.0,
                        qty=100, stop_loss=1550.0, target=1400.0),
        ],
        total_signals=1, signals_accepted=1, signals_rejected=0,
        total_trades=2, trades_won=0, trades_lost=0,
        gross_pnl=0.0, total_charges=0.0, net_pnl=0.0,
    )

    results = verify_reports(log_report, db_report)
    count_results = [r for r in results if r.field == "trade_count"]
    assert len(count_results) == 1
    assert count_results[0].match is False
    assert count_results[0].log_value == 1
    assert count_results[0].db_value == 2


# ---------------------------------------------------------------------------
# (g) Log report backward compatible — log snippet → generate_log_report
# ---------------------------------------------------------------------------

def test_log_report_backward_compatible(tmp_path):
    """Log parsing → generate_log_report produces correct SessionReport structure."""
    log_lines = [
        "2026-03-16T09:15:00.000000 [info     ] startup_token_valid            session_date=2026-03-16 mode=paper",
        "2026-03-16T10:00:00.000000 [info     ] s1_signal_generated            direction=SHORT entry=1825.5 rsi=42.0 stop=1860.0 symbol=SUNPHARMA target=1760.0 volume_ratio=1.8",
        "2026-03-16T10:00:00.100000 [info     ] signal_queued                  direction=SHORT entry=1825.5 stop=1860.0 symbol=SUNPHARMA target=1760.0",
        "2026-03-16T10:15:00.000000 [info     ] s1_signal_generated            direction=SHORT entry=3400.0 rsi=38.0 stop=3500.0 symbol=TITAN target=3200.0 volume_ratio=1.3",
        "2026-03-16T10:15:00.100000 [info     ] risk_gate_blocked              gate=3 reason=max_positions symbol=TITAN",
        "2026-03-16T10:15:00.200000 [info     ] signal_blocked                 direction=SHORT reason=max_positions symbol=TITAN",
        "2026-03-16T10:15:01.000000 [info     ] position_registered            direction=SHORT entry_price=1825.5 qty=71 stop_loss=1860.0 symbol=SUNPHARMA target=1760.0",
        "2026-03-16T15:00:00.000000 [info     ] position_closed                exit_price=1805.1 exit_reason=HARD_EXIT_1500 gross_pnl=1448.4 symbol=SUNPHARMA",
    ]
    path = str(tmp_path / "test_backward.log")
    _write_log(log_lines, path)

    parser = SessionParser()
    data = parser.parse(path)
    report = generate_log_report(data)

    assert report.source == "log"
    assert report.session_date == "2026-03-16"
    assert report.total_signals == 2
    assert report.signals_accepted == 1
    assert report.signals_rejected == 1
    assert report.total_trades == 1
    assert report.trades_won == 1
    assert report.trades_lost == 0
    assert report.gross_pnl == 1448.4
    assert report.total_charges == 0.0
    assert report.net_pnl == 1448.4
