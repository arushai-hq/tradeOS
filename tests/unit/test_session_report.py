"""
TradeOS — Unit tests for session_report.py

Tests:
  (a) parse_line correctly strips ANSI and parses all field types
  (b) pre-B5 signal correlation: s1_signal_generated + signal_queued → ACCEPTED
  (c) pre-B5 signal blocking:   s1_signal_generated + risk_gate_blocked + signal_blocked → BLOCKED
  (d) CSV export rows match SessionData signals and trades
  (e) Indian number formatting: _indian_format and _inr helpers
"""
from __future__ import annotations

import csv
import io
import os
import sys
import tempfile

import pytest

# Add project root to path so tools/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tools.session_report import (
    SessionParser,
    _coerce,
    _indian_format,
    _inr,
    export_csv,
    parse_fields,
    parse_line,
    strip_ansi,
)


# ---------------------------------------------------------------------------
# (a) parse_line — ANSI stripping and field type coercion
# ---------------------------------------------------------------------------

def test_parse_line_bare_values():
    """Bare values (int, float, bool, bare string) are coerced correctly."""
    raw = (
        "\x1b[2m2026-03-09T11:00:00.829569\x1b[0m "
        "[\x1b[32m\x1b[1minfo     \x1b[0m] "
        "\x1b[1ms1_signal_generated\x1b[0m "
        "\x1b[36mdirection\x1b[0m=\x1b[35mSHORT\x1b[0m "
        "\x1b[36mentry\x1b[0m=\x1b[35m1227.0\x1b[0m "
        "\x1b[36mqty\x1b[0m=\x1b[35m10\x1b[0m "
        "\x1b[36mws_connected\x1b[0m=\x1b[35mTrue\x1b[0m"
    )
    result = parse_line(raw)
    assert result is not None
    assert result["ts"] == "2026-03-09T11:00:00.829569"
    assert result["level"] == "info"
    assert result["event"] == "s1_signal_generated"
    assert result["fields"]["direction"] == "SHORT"
    assert result["fields"]["entry"] == 1227.0
    assert result["fields"]["qty"] == 10
    assert result["fields"]["ws_connected"] is True


def test_parse_line_quoted_string():
    """Single-quoted strings are stripped of their quotes."""
    raw = "2026-03-09T15:00:00.485930 [info     ] hard_exit_triggered note='Scheduled EOD — NOT a kill switch event' open_positions=3"
    result = parse_line(raw)
    assert result is not None
    assert result["event"] == "hard_exit_triggered"
    assert result["fields"]["note"] == "Scheduled EOD — NOT a kill switch event"
    assert result["fields"]["open_positions"] == 3


def test_parse_line_returns_none_on_garbage():
    """Lines that don't match the structlog format return None."""
    assert parse_line("") is None
    assert parse_line("some random garbage text") is None
    assert parse_line("---") is None


# ---------------------------------------------------------------------------
# (b) Signal correlation — ACCEPTED path
# ---------------------------------------------------------------------------

def _write_log(lines: list[str], tmp_path: str) -> str:
    with open(tmp_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return tmp_path


def test_signal_accepted_correlation(tmp_path):
    """s1_signal_generated followed by signal_queued → status ACCEPTED."""
    log_lines = [
        "2026-03-09T10:46:31.000000 [info     ] startup_token_valid            user_id=XP8470",
        "2026-03-09T11:00:00.829569 [info     ] s1_signal_generated            direction=SHORT entry=1227.0 rsi=31.5 stop=1258.9 symbol=NESTLEIND target=1163.2 volume_ratio=1.69",
        "2026-03-09T11:00:00.834051 [info     ] signal_queued                  direction=SHORT entry=1227.0 stop=1258.9 symbol=NESTLEIND target=1163.2",
    ]
    path = str(tmp_path / "test_accepted.log")
    _write_log(log_lines, path)

    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.signals) == 1
    sig = data.signals[0]
    assert sig.symbol == "NESTLEIND"
    assert sig.direction == "SHORT"
    assert sig.entry == 1227.0
    assert sig.status == "ACCEPTED"
    assert sig.block_reason == ""


# ---------------------------------------------------------------------------
# (c) Signal correlation — BLOCKED path with gate number
# ---------------------------------------------------------------------------

def test_signal_blocked_correlation(tmp_path):
    """s1_signal_generated + risk_gate_blocked + signal_blocked → BLOCKED with gate and reason."""
    log_lines = [
        "2026-03-09T10:46:31.000000 [info     ] startup_token_valid            user_id=XP8470",
        "2026-03-09T11:30:00.634189 [info     ] s1_signal_generated            direction=LONG entry=1370.6 rsi=59.9 stop=1345.2 symbol=HCLTECH target=1421.4 volume_ratio=1.96",
        "2026-03-09T11:30:00.640733 [debug    ] risk_gate_blocked              direction=LONG gate=7 reason=REGIME_BLOCKED_BEAR_TREND regime=bear_trend symbol=HCLTECH",
        "2026-03-09T11:30:00.656925 [info     ] signal_blocked                 direction=LONG reason=REGIME_BLOCKED_BEAR_TREND symbol=HCLTECH",
    ]
    path = str(tmp_path / "test_blocked.log")
    _write_log(log_lines, path)

    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.signals) == 1
    sig = data.signals[0]
    assert sig.symbol == "HCLTECH"
    assert sig.direction == "LONG"
    assert sig.status == "BLOCKED"
    assert sig.block_reason == "REGIME_BLOCKED_BEAR_TREND"
    assert sig.gate == 7


# ---------------------------------------------------------------------------
# (d) CSV export — rows match session data
# ---------------------------------------------------------------------------

def test_csv_export_signals(tmp_path):
    """export_csv writes correct signal rows to {date}_signals.csv."""
    log_lines = [
        "2026-03-09T10:46:31.000000 [info     ] startup_phase1_begin           mode=paper session_date=2026-03-09",
        "2026-03-09T11:00:00.000000 [info     ] s1_signal_generated            direction=SHORT entry=1227.0 rsi=31.5 stop=1258.9 symbol=NESTLEIND target=1163.2 volume_ratio=1.69",
        "2026-03-09T11:00:01.000000 [info     ] signal_queued                  direction=SHORT entry=1227.0 stop=1258.9 symbol=NESTLEIND target=1163.2",
        "2026-03-09T11:30:00.000000 [info     ] s1_signal_generated            direction=LONG entry=1370.6 rsi=59.9 stop=1345.2 symbol=HCLTECH target=1421.4 volume_ratio=1.96",
        "2026-03-09T11:30:01.000000 [debug    ] risk_gate_blocked              direction=LONG gate=7 reason=REGIME_BLOCKED_BEAR_TREND symbol=HCLTECH",
        "2026-03-09T11:30:02.000000 [info     ] signal_blocked                 direction=LONG reason=REGIME_BLOCKED_BEAR_TREND symbol=HCLTECH",
    ]
    log_path = str(tmp_path / "test_export.log")
    _write_log(log_lines, log_path)

    parser = SessionParser()
    data = parser.parse(log_path)

    out_dir = str(tmp_path / "reports")
    export_csv(data, out_dir)

    csv_path = os.path.join(out_dir, "2026-03-09_signals.csv")
    assert os.path.exists(csv_path), f"CSV not found: {csv_path}"

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2

    nestleind = next(r for r in rows if r["symbol"] == "NESTLEIND")
    assert nestleind["status"] == "ACCEPTED"
    assert nestleind["direction"] == "SHORT"
    assert nestleind["block_reason"] == ""

    hcltech = next(r for r in rows if r["symbol"] == "HCLTECH")
    assert hcltech["status"] == "BLOCKED"
    assert hcltech["block_reason"] == "REGIME_BLOCKED_BEAR_TREND"
    assert hcltech["gate"] == "7"


# ---------------------------------------------------------------------------
# (e) Indian number formatting — _indian_format and _inr
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, decimals, expected", [
    (500, 0, "500"),
    (1000, 0, "1,000"),
    (10000, 0, "10,000"),
    (100000, 0, "1,00,000"),
    (1000000, 0, "10,00,000"),
    (10000000, 0, "1,00,00,000"),
    (1234567, 0, "12,34,567"),
    (50.25, 2, "50.25"),
    (129550.5, 2, "1,29,550.50"),
    (-1500, 0, "-1,500"),
    (-125000, 2, "-1,25,000.00"),
    (0, 0, "0"),
    (99, 0, "99"),
])
def test_indian_format(value, decimals, expected):
    """_indian_format produces correct Indian comma grouping."""
    assert _indian_format(value, decimals) == expected


@pytest.mark.parametrize("value, decimals, expected", [
    (125000, 0, "₹1,25,000"),
    (1448.40, 2, "₹1,448.40"),
    (0, 0, "₹0"),
])
def test_inr_format(value, decimals, expected):
    """_inr wraps _indian_format with ₹ prefix."""
    assert _inr(value, decimals) == expected
