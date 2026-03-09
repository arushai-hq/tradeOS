"""
TradeOS — Unit tests for TelegramNotifier.

Tests:
  (a) Config loading from file
  (b) Config hot-reload after TTL expires
  (c) Message formatting for all six event types
  (d) Config toggle disables notification (_send not called)
  (e) heartbeat_interval_cycles reflects configured value
  (f) notify_position_closed routes HARD_EXIT_1500 silently (no send)
"""
from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch

import pytest
import yaml

from utils.telegram_notifier import TelegramNotifier, _fmt_hold_time, _unrealized_pnl


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_config(path: str, overrides: dict | None = None) -> None:
    cfg = {
        "telegram_alerts": {
            "signal_generated": True,
            "position_opened": True,
            "stop_hit": True,
            "target_hit": True,
            "hard_exit": True,
            "heartbeat_summary": True,
            "heartbeat_interval_min": 30,
        }
    }
    if overrides:
        cfg["telegram_alerts"].update(overrides)
    with open(path, "w") as f:
        yaml.dump(cfg, f)


def _make_notifier(path: str, shared_state: dict | None = None) -> TelegramNotifier:
    return TelegramNotifier(
        config_path=path,
        shared_state=shared_state or {},
        secrets={},
    )


# ---------------------------------------------------------------------------
# (a) Config loading from file
# ---------------------------------------------------------------------------

def test_config_loaded_from_file():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path, {"heartbeat_interval_min": 60})
        n = _make_notifier(path)
        cfg = n._load_alert_config()
        assert cfg["heartbeat_interval_min"] == 60
        assert cfg["signal_generated"] is True
    finally:
        os.unlink(path)


def test_config_returns_empty_dict_on_missing_file():
    n = TelegramNotifier("/nonexistent/path.yaml", {}, {})
    cfg = n._load_alert_config()
    assert cfg == {}


# ---------------------------------------------------------------------------
# (b) Config hot-reload after TTL expires
# ---------------------------------------------------------------------------

def test_config_hot_reload_after_ttl():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path, {"heartbeat_interval_min": 30})
        n = _make_notifier(path)

        # First load: populates cache
        cfg1 = n._load_alert_config()
        assert cfg1["heartbeat_interval_min"] == 30

        # Write updated config
        _write_config(path, {"heartbeat_interval_min": 60})

        # Still within TTL — returns cached value
        cfg2 = n._load_alert_config()
        assert cfg2["heartbeat_interval_min"] == 30

        # Expire the cache manually
        n._cache_loaded_at = time.monotonic() - TelegramNotifier._CACHE_TTL - 1

        # Re-reads file — returns new value
        cfg3 = n._load_alert_config()
        assert cfg3["heartbeat_interval_min"] == 60
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# (c) Message formatting — all event types
# ---------------------------------------------------------------------------

class TestFormatters:
    def setup_method(self):
        self.shared_state = {
            "market_regime": "bear_trend",
            "open_positions": {},
            "last_tick_prices": {},
            "daily_pnl_rs": 0.0,
            "signals_generated_today": 4,
            "signals_rejected_today": 2,
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            self.cfg_path = f.name
        _write_config(self.cfg_path)
        self.n = _make_notifier(self.cfg_path, self.shared_state)

    def teardown_method(self):
        os.unlink(self.cfg_path)

    def test_fmt_signal_accepted_long(self):
        msg = self.n._fmt_signal_accepted(
            "HCLTECH", "LONG", 1370.60, 1345.20, 1421.40, 59.9, 1.96, "bear_trend"
        )
        assert "🟢" in msg
        assert "LONG HCLTECH" in msg
        assert "1370.60" in msg
        assert "1345.20" in msg
        assert "1421.40" in msg
        assert "59.9" in msg
        assert "1.96" in msg
        assert "bear_trend" in msg

    def test_fmt_signal_accepted_short(self):
        msg = self.n._fmt_signal_accepted(
            "NESTLEIND", "SHORT", 2500.0, 2525.0, 2450.0, 32.5, 2.10, "bull_trend"
        )
        assert "🔴" in msg
        assert "SHORT NESTLEIND" in msg

    def test_fmt_signal_rejected(self):
        msg = self.n._fmt_signal_rejected(
            "NESTLEIND", "SHORT", "regime_check", 7, "REGIME_BLOCKED: BULL_TREND", 31.5
        )
        assert "🔴 Signal Rejected" in msg
        assert "SHORT NESTLEIND" in msg
        assert "Gate 7" in msg
        assert "regime_check" in msg
        assert "31.5" in msg

    def test_fmt_position_opened(self):
        msg = self.n._fmt_position_opened("HCLTECH", "LONG", 1370.60, 10, 1345.20, 1421.40)
        assert "📈 Position Opened" in msg
        assert "LONG HCLTECH" in msg
        assert "1370.60" in msg
        assert "Qty: 10" in msg
        # Capital at risk = (1370.60 - 1345.20) * 10 = 254
        assert "254" in msg

    def test_fmt_stop_hit(self):
        msg = self.n._fmt_stop_hit("HCLTECH", "LONG", 1370.60, 1345.20, -254.0, -1.85, 47.0)
        assert "🛑 Stop Hit" in msg
        assert "LONG HCLTECH" in msg
        assert "1370.60" in msg
        assert "1345.20" in msg
        assert "-254.00" in msg
        assert "47 min" in msg

    def test_fmt_target_hit(self):
        msg = self.n._fmt_target_hit("HCLTECH", "LONG", 1370.60, 1421.40, 508.0, 3.71, 83.0)
        assert "🎯 Target Hit" in msg
        assert "LONG HCLTECH" in msg
        assert "508.00" in msg
        assert "+3.71" in msg
        assert "1h 23min" in msg

    def test_fmt_hard_exit_with_positions(self):
        positions = {
            "HCLTECH": {"direction": "LONG", "entry_price": 1370.60, "qty": 10},
            "WIPRO": {"direction": "LONG", "entry_price": 196.71, "qty": 100},
        }
        tick_prices = {"HCLTECH": 1382.30, "WIPRO": 195.40}
        msg = self.n._fmt_hard_exit(positions, tick_prices, 0.0)
        assert "⚠️ Hard Exit" in msg
        assert "15:00 IST" in msg
        assert "HCLTECH" in msg
        assert "WIPRO" in msg
        assert "<pre>" in msg
        assert "</pre>" in msg

    def test_fmt_hard_exit_no_positions(self):
        msg = self.n._fmt_hard_exit({}, {}, 100.0)
        assert "0 position" in msg
        assert "⚠️ Hard Exit" in msg

    def test_fmt_heartbeat_no_positions(self):
        msg = self.n._fmt_heartbeat()
        assert "💓 TradeOS" in msg
        assert "bear_trend" in msg
        assert "No open positions" in msg
        assert "4 accepted" in msg
        assert "2 rejected" in msg

    def test_fmt_heartbeat_with_positions(self):
        self.shared_state["open_positions"] = {
            "HCLTECH": {"direction": "LONG", "entry_price": 1370.60, "qty": 10},
        }
        self.shared_state["last_tick_prices"] = {"HCLTECH": 1378.20}
        msg = self.n._fmt_heartbeat()
        assert "HCLTECH" in msg
        assert "<pre>" in msg
        assert "</pre>" in msg
        # Unrealized = (1378.20 - 1370.60) * 10 = 76
        assert "76" in msg


# ---------------------------------------------------------------------------
# (d) Config toggle disables notification (_send not called)
# ---------------------------------------------------------------------------

class TestConfigToggle:
    def setup_method(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            self.cfg_path = f.name

    def teardown_method(self):
        os.unlink(self.cfg_path)

    def _notifier_with(self, overrides: dict) -> TelegramNotifier:
        _write_config(self.cfg_path, overrides)
        return _make_notifier(self.cfg_path)

    def test_signal_generated_disabled_skips_accepted(self):
        n = self._notifier_with({"signal_generated": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_signal_accepted("X", "LONG", 100, 95, 110, 60, 2.0, "bull_trend")
            mock_send.assert_not_called()

    def test_signal_generated_disabled_skips_rejected(self):
        n = self._notifier_with({"signal_generated": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_signal_rejected("X", "SHORT", "regime_check", 7, "REGIME_BLOCKED", 31.0)
            mock_send.assert_not_called()

    def test_position_opened_disabled_skips_send(self):
        n = self._notifier_with({"position_opened": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_position_opened("X", "LONG", 100, 10, 95, 110)
            mock_send.assert_not_called()

    def test_stop_hit_disabled_skips_send(self):
        n = self._notifier_with({"stop_hit": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed("X", "LONG", 100, 95, "STOP_HIT", -50, -1.5, 30)
            mock_send.assert_not_called()

    def test_target_hit_disabled_skips_send(self):
        n = self._notifier_with({"target_hit": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed("X", "LONG", 100, 110, "TARGET_HIT", 100, 3.0, 60)
            mock_send.assert_not_called()

    def test_hard_exit_disabled_skips_send(self):
        n = self._notifier_with({"hard_exit": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_hard_exit({"X": {"direction": "LONG", "entry_price": 100, "qty": 1}}, {}, 0)
            mock_send.assert_not_called()

    def test_heartbeat_disabled_skips_send(self):
        n = self._notifier_with({"heartbeat_summary": False})
        with patch.object(n, "_send") as mock_send:
            n.notify_heartbeat()
            mock_send.assert_not_called()

    def test_signal_generated_enabled_calls_send(self):
        n = self._notifier_with({"signal_generated": True})
        with patch.object(n, "_send") as mock_send:
            n.notify_signal_accepted("X", "LONG", 100, 95, 110, 60, 2.0, "bull_trend")
            mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# (e) heartbeat_interval_cycles reflects configured value
# ---------------------------------------------------------------------------

def test_heartbeat_interval_cycles_30_min():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path, {"heartbeat_interval_min": 30})
        n = _make_notifier(path)
        assert n.heartbeat_interval_cycles() == 60  # 30 * 2
    finally:
        os.unlink(path)


def test_heartbeat_interval_cycles_60_min():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path, {"heartbeat_interval_min": 60})
        n = _make_notifier(path)
        assert n.heartbeat_interval_cycles() == 120  # 60 * 2
    finally:
        os.unlink(path)


def test_heartbeat_interval_cycles_default_when_missing():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        # Write config without heartbeat_interval_min
        with open(path, "w") as f2:
            yaml.dump({"telegram_alerts": {"signal_generated": True}}, f2)
        n = _make_notifier(path)
        assert n.heartbeat_interval_cycles() == 60  # default 30 min * 2
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# (f) notify_position_closed routes HARD_EXIT_1500 silently
# ---------------------------------------------------------------------------

def test_hard_exit_reason_not_sent_individually():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path)
        n = _make_notifier(path)
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed("X", "LONG", 100, 101, "HARD_EXIT_1500", 10, 0.1, 30)
            mock_send.assert_not_called()
    finally:
        os.unlink(path)


def test_kill_switch_reason_not_sent_individually():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
        path = f.name
    try:
        _write_config(path)
        n = _make_notifier(path)
        with patch.object(n, "_send") as mock_send:
            n.notify_position_closed("X", "LONG", 100, 101, "KILL_SWITCH", 10, 0.1, 30)
            mock_send.assert_not_called()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

def test_fmt_hold_time_minutes():
    assert _fmt_hold_time(47) == "47 min"
    assert _fmt_hold_time(0) == "0 min"


def test_fmt_hold_time_hours():
    assert _fmt_hold_time(83) == "1h 23min"
    assert _fmt_hold_time(60) == "1h"
    assert _fmt_hold_time(120) == "2h"


def test_unrealized_pnl_long_profit():
    pos = {"RELIANCE": {"direction": "LONG", "qty": 5, "entry_price": 2450.0}}
    prices = {"RELIANCE": 2480.0}
    assert _unrealized_pnl(pos, prices) == pytest.approx(150.0)


def test_unrealized_pnl_short_profit():
    pos = {"INFY": {"direction": "SHORT", "qty": 4, "entry_price": 1500.0}}
    prices = {"INFY": 1450.0}
    assert _unrealized_pnl(pos, prices) == pytest.approx(200.0)


def test_unrealized_pnl_missing_price_zero():
    pos = {"RELIANCE": {"direction": "LONG", "qty": 5, "entry_price": 2450.0}}
    assert _unrealized_pnl(pos, {}) == 0.0
