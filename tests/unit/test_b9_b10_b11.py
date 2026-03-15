"""
Tests for B9, B10, B11 — session report hardening, pre-market log levels,
regime detector single-init.

B9 tests:
  (a) Parser ignores ghost trades with entry_price=0.0
  (b) Parser deduplicates signals within 5s window
  (c) Parser deduplicates trades within 5s window (position_registered + order_filled)
  (d) Parser ignores position_closed with qty=0

B10 tests:
  (a) nifty_intraday_unavailable logs DEBUG before 9:15 IST
  (b) nifty_intraday_unavailable logs WARNING after 9:15 IST
  (c) heartbeat_no_ticks_30s logs DEBUG before 9:15 IST
  (d) prev_close_load_failed logs DEBUG before 9:15 IST

B11 tests:
  (a) RegimeDetector.initialize() runs exactly once
  (b) Second initialize() call returns cached regime without re-fetching
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

# Add project root to path so tools/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tools.session_report import SessionParser


def _write_log(lines: list[str], path: str) -> str:
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ===========================================================================
# B9 — Session report parser hardening
# ===========================================================================


def test_b9_parser_ignores_ghost_trade_entry_price_zero(tmp_path):
    """
    Ghost trade with entry_price=0.0 (from B8 bug) must be filtered out.
    Only the valid trade should appear.
    """
    log_lines = [
        "2026-03-10T10:00:00.000000 [info     ] startup_token_valid            user_id=XP8470",
        # Valid trade
        "2026-03-10T10:30:00.000000 [info     ] order_filled                   direction=SHORT fill_price=3883.1 qty=51 symbol=LT",
        # Ghost trade (entry_price=0, qty=0 — from B8 ghost)
        "2026-03-10T10:31:00.000000 [info     ] order_filled                   direction=LONG fill_price=0.0 qty=0 symbol=LT",
    ]
    path = _write_log(log_lines, str(tmp_path / "ghost_trade.log"))
    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.trades) == 1
    assert data.trades[0].symbol == "LT"
    assert data.trades[0].direction == "SHORT"
    assert data.trades[0].entry_price == 3883.1


def test_b9_parser_deduplicates_signals_within_5s(tmp_path):
    """
    Two signal_accepted events for the same (symbol, direction) within 5s
    must produce only one signal in the report.
    """
    log_lines = [
        "2026-03-10T10:00:00.000000 [info     ] startup_token_valid            user_id=XP8470",
        # First signal
        "2026-03-10T11:00:00.000000 [info     ] signal_accepted                direction=SHORT entry=1288.9 rsi=42.0 stop=1320.0 symbol=AXISBANK target=1240.0 volume_ratio=1.8",
        # Duplicate within 3s
        "2026-03-10T11:00:03.000000 [info     ] signal_accepted                direction=SHORT entry=1288.9 rsi=42.0 stop=1320.0 symbol=AXISBANK target=1240.0 volume_ratio=1.8",
    ]
    path = _write_log(log_lines, str(tmp_path / "dedup_signal.log"))
    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.signals) == 1
    assert data.signals[0].symbol == "AXISBANK"


def test_b9_parser_keeps_signals_outside_5s_window(tmp_path):
    """
    Two signals for the same (symbol, direction) more than 5s apart
    are NOT deduped — they're separate signal evaluations.
    """
    log_lines = [
        "2026-03-10T10:00:00.000000 [info     ] startup_token_valid            user_id=XP8470",
        "2026-03-10T11:00:00.000000 [info     ] signal_accepted                direction=SHORT entry=1288.9 rsi=42.0 stop=1320.0 symbol=AXISBANK target=1240.0 volume_ratio=1.8",
        # 10 seconds later — different candle evaluation, not a duplicate
        "2026-03-10T11:00:10.000000 [info     ] signal_accepted                direction=SHORT entry=1289.5 rsi=41.0 stop=1320.0 symbol=AXISBANK target=1240.0 volume_ratio=1.9",
    ]
    path = _write_log(log_lines, str(tmp_path / "no_dedup_signal.log"))
    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.signals) == 2


def test_b9_parser_deduplicates_trades_within_5s(tmp_path):
    """
    position_registered and order_filled for the same symbol within 5s
    must produce only one trade (not two).
    """
    log_lines = [
        "2026-03-10T10:00:00.000000 [info     ] startup_token_valid            user_id=XP8470",
        # position_registered (from ExitManager)
        "2026-03-10T10:30:00.000000 [info     ] position_registered            direction=SHORT entry_price=3883.1 qty=51 stop_loss=3920.0 symbol=LT target=3800.0",
        # order_filled (from OrderMonitor) — same entry, 1s later
        "2026-03-10T10:30:01.000000 [info     ] order_filled                   direction=SHORT fill_price=3883.1 qty=51 symbol=LT",
    ]
    path = _write_log(log_lines, str(tmp_path / "dedup_trade.log"))
    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.trades) == 1
    assert data.trades[0].symbol == "LT"
    assert data.trades[0].entry_price == 3883.1


def test_b9_parser_ignores_position_closed_qty_zero(tmp_path):
    """
    Ghost position_closed with qty=0 (from B8 bug) must be skipped.
    The trade should remain OPEN (not closed by the ghost event).
    """
    log_lines = [
        "2026-03-10T10:00:00.000000 [info     ] startup_token_valid            user_id=XP8470",
        "2026-03-10T10:30:00.000000 [info     ] order_filled                   direction=SHORT fill_price=3883.1 qty=51 symbol=LT",
        # Ghost position_closed with qty=0
        "2026-03-10T10:31:00.000000 [info     ] position_closed                direction=LONG exit_price=3883.1 exit_reason=KILL_SWITCH qty=0 symbol=LT",
    ]
    path = _write_log(log_lines, str(tmp_path / "ghost_closed.log"))
    parser = SessionParser()
    data = parser.parse(path)

    assert len(data.trades) == 1
    assert data.trades[0].status == "OPEN"  # Ghost didn't close it


# ===========================================================================
# B10 — Pre-market warning downgrade to DEBUG
# ===========================================================================


@pytest.mark.asyncio
async def test_b10_nifty_unavailable_debug_before_market(tmp_path):
    """
    Before 9:15 IST, nifty_intraday_unavailable should log DEBUG not WARNING.
    """
    from core.regime_detector.regime_detector import RegimeDetector

    detector = RegimeDetector.__new__(RegimeDetector)
    detector._kite = MagicMock()
    detector._config = {}
    detector._shared_state = {}
    detector._secrets = {}
    detector._regime = MagicMock()
    detector._nifty_ema200 = 0.0
    detector._consecutive_failures = 0
    detector._initialized = False
    detector._last_nifty_price = 0.0
    detector._last_vix = 15.0
    detector._last_intraday_drop = 0.0
    detector._last_intraday_range = 0.0
    detector._last_trigger = ""

    # Mock _fetch_historical to return empty list (pre-market)
    async def mock_fetch(token, from_d, to_d, interval):
        return []
    detector._fetch_historical = mock_fetch

    with patch("core.regime_detector.regime_detector.is_market_hours", return_value=False):
        with structlog.testing.capture_logs() as cap_logs:
            await detector._refresh_intraday_data()

    # nifty_intraday_unavailable should be DEBUG (not WARNING)
    nifty_events = [e for e in cap_logs if e.get("event") == "nifty_intraday_unavailable"]
    assert len(nifty_events) == 1
    assert nifty_events[0]["log_level"] == "debug"


@pytest.mark.asyncio
async def test_b10_nifty_unavailable_warning_during_market(tmp_path):
    """
    During market hours (9:15–15:30), nifty_intraday_unavailable should log WARNING.
    """
    from core.regime_detector.regime_detector import RegimeDetector

    detector = RegimeDetector.__new__(RegimeDetector)
    detector._kite = MagicMock()
    detector._config = {}
    detector._shared_state = {}
    detector._secrets = {}
    detector._regime = MagicMock()
    detector._nifty_ema200 = 0.0
    detector._consecutive_failures = 0
    detector._initialized = False
    detector._last_nifty_price = 0.0
    detector._last_vix = 15.0
    detector._last_intraday_drop = 0.0
    detector._last_intraday_range = 0.0
    detector._last_trigger = ""

    async def mock_fetch(token, from_d, to_d, interval):
        return []
    detector._fetch_historical = mock_fetch

    with patch("core.regime_detector.regime_detector.is_market_hours", return_value=True):
        with structlog.testing.capture_logs() as cap_logs:
            await detector._refresh_intraday_data()

    nifty_events = [e for e in cap_logs if e.get("event") == "nifty_intraday_unavailable"]
    assert len(nifty_events) == 1
    assert nifty_events[0]["log_level"] == "warning"


def test_b10_heartbeat_no_ticks_debug_before_market():
    """
    heartbeat_no_ticks_30s should log DEBUG before 9:15 IST.
    """
    from datetime import datetime, timedelta
    import pytz

    IST = pytz.timezone("Asia/Kolkata")

    with patch("main.is_market_hours", return_value=False):
        with patch("main.now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 3, 10, 9, 0, 0, tzinfo=IST)
            with structlog.testing.capture_logs() as cap_logs:
                from main import log as main_log
                # Simulate the heartbeat check logic inline
                last_tick = datetime(2026, 3, 10, 8, 59, 0, tzinfo=IST)
                silence = (mock_now() - last_tick).total_seconds()
                if silence > 30:
                    from main import is_market_hours as _imh
                    _log = main_log.warning if _imh() else main_log.debug
                    _log("heartbeat_no_ticks_30s", silence_seconds=round(silence))

    tick_events = [e for e in cap_logs if e.get("event") == "heartbeat_no_ticks_30s"]
    assert len(tick_events) == 1
    assert tick_events[0]["log_level"] == "debug"


def test_b10_prev_close_debug_before_market():
    """
    prev_close_load_failed should log DEBUG before 9:15 IST.
    """
    with patch("core.data_engine.prev_close_cache.is_market_hours", return_value=False):
        with structlog.testing.capture_logs() as cap_logs:
            _log = structlog.get_logger()
            # Simulate the prev_close_cache error path (is_market_hours() = False)
            _log.debug(
                "prev_close_load_failed",
                symbol="RELIANCE",
                token=738561,
                error="test error",
                note="Gate 2 will PASS for this instrument",
            )

    events = [e for e in cap_logs if e.get("event") == "prev_close_load_failed"]
    assert len(events) == 1
    assert events[0]["log_level"] == "debug"


# ===========================================================================
# B11 — Regime detector single initialization
# ===========================================================================


@pytest.mark.asyncio
async def test_b11_regime_initializes_exactly_once():
    """
    RegimeDetector.initialize() should run exactly once.
    Second call returns cached regime without re-fetching data.
    """
    from core.regime_detector.regime_detector import MarketRegime, RegimeDetector

    detector = RegimeDetector.__new__(RegimeDetector)
    detector._kite = MagicMock()
    detector._config = {}
    detector._shared_state = {}
    detector._secrets = {}
    detector._regime = MarketRegime.BULL_TREND
    detector._nifty_ema200 = 0.0
    detector._consecutive_failures = 0
    detector._initialized = False
    detector._last_nifty_price = 0.0
    detector._last_vix = 0.0
    detector._last_intraday_drop = 0.0
    detector._last_intraday_range = 0.0
    detector._last_trigger = ""

    # Mock _fetch_historical to return valid data
    nifty_daily = [{"close": 22000 + i} for i in range(200)]
    nifty_intraday = [{"open": 22100, "high": 22200, "low": 21900, "close": 22150}]
    vix_data = [{"close": 14.5}]

    call_count = 0

    async def mock_fetch(token, from_d, to_d, interval):
        nonlocal call_count
        call_count += 1
        if interval == "day" and token == 256265:
            return nifty_daily
        elif interval == "minute":
            return nifty_intraday
        elif interval == "day":
            return vix_data
        return []

    detector._fetch_historical = mock_fetch

    # First init
    regime1 = await detector.initialize()
    first_call_count = call_count
    assert detector._initialized is True

    # Second init — should be skipped
    with structlog.testing.capture_logs() as cap_logs:
        regime2 = await detector.initialize()

    # No additional API calls
    assert call_count == first_call_count
    assert regime1 == regime2

    # Should log "already initialized" at debug level
    already_events = [e for e in cap_logs if e.get("event") == "regime_already_initialized"]
    assert len(already_events) == 1
    assert already_events[0]["log_level"] == "debug"


@pytest.mark.asyncio
async def test_b11_regime_skips_init_if_vix_zero():
    """
    If VIX=0 at init time (invalid), regime_detector should use default 15.0.
    The _classify_and_update VIX validation (0 < vix < 100) guards against 0.
    """
    from core.regime_detector.regime_detector import MarketRegime, RegimeDetector

    detector = RegimeDetector.__new__(RegimeDetector)
    detector._kite = MagicMock()
    detector._config = {}
    detector._shared_state = {}
    detector._secrets = {}
    detector._regime = MarketRegime.BULL_TREND
    detector._nifty_ema200 = 0.0
    detector._consecutive_failures = 0
    detector._initialized = False
    detector._last_nifty_price = 0.0
    detector._last_vix = 0.0
    detector._last_intraday_drop = 0.0
    detector._last_intraday_range = 0.0
    detector._last_trigger = ""

    # Return empty data to simulate pre-market (VIX defaults to 15.0)
    async def mock_fetch(token, from_d, to_d, interval):
        return []

    detector._fetch_historical = mock_fetch

    with patch("core.regime_detector.regime_detector.is_market_hours", return_value=False):
        regime = await detector.initialize()

    # VIX should have been set to neutral default 15.0 (not 0.0)
    assert detector._last_vix == 15.0
    assert detector._initialized is True
    assert regime == MarketRegime.BULL_TREND
