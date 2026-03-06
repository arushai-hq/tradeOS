"""
Unit tests for utils/time_utils.py

Tests:
  - now_ist returns timezone-aware datetime in Asia/Kolkata
  - today_ist returns a date object
  - is_market_hours boundary conditions
  - is_accepting_signals boundary (hard exit at 15:00)
"""
from __future__ import annotations

from datetime import datetime, time
from unittest.mock import patch

import pytz
import pytest

from utils.time_utils import (
    IST,
    is_accepting_signals,
    is_market_hours,
    now_ist,
    today_ist,
)


def _make_ist_dt(hour: int, minute: int, second: int = 0) -> datetime:
    """Helper: make a timezone-aware IST datetime for today."""
    return datetime.now(IST).replace(hour=hour, minute=minute, second=second, microsecond=0)


class TestNowIst:
    def test_now_ist_is_timezone_aware(self):
        result = now_ist()
        assert result.tzinfo is not None

    def test_now_ist_is_kolkata_timezone(self):
        result = now_ist()
        # UTC offset for IST is +5:30
        offset_hours = result.utcoffset().total_seconds() / 3600
        assert offset_hours == pytest.approx(5.5)


class TestTodayIst:
    def test_today_ist_returns_date(self):
        import datetime as dt
        result = today_ist()
        assert isinstance(result, dt.date)
        assert not isinstance(result, dt.datetime)  # date not datetime

    def test_today_ist_matches_now_ist_date(self):
        result = today_ist()
        assert result == now_ist().date()


class TestIsMarketHours:
    def test_returns_false_before_915(self):
        fake_now = _make_ist_dt(9, 14)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_market_hours() is False

    def test_returns_true_at_915(self):
        fake_now = _make_ist_dt(9, 15)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_market_hours() is True

    def test_returns_true_during_session(self):
        fake_now = _make_ist_dt(12, 0)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_market_hours() is True

    def test_returns_true_at_1530(self):
        fake_now = _make_ist_dt(15, 30)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_market_hours() is True

    def test_returns_false_after_1530(self):
        fake_now = _make_ist_dt(15, 31)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_market_hours() is False


class TestIsAcceptingSignals:
    def test_returns_false_before_915(self):
        fake_now = _make_ist_dt(9, 0)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_accepting_signals() is False

    def test_returns_true_at_915(self):
        fake_now = _make_ist_dt(9, 15)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_accepting_signals() is True

    def test_returns_true_just_before_1500(self):
        fake_now = _make_ist_dt(14, 59)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_accepting_signals() is True

    def test_returns_false_at_1500(self):
        """Hard exit at exactly 15:00 — no new signals."""
        fake_now = _make_ist_dt(15, 0)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_accepting_signals() is False

    def test_returns_false_after_1500(self):
        fake_now = _make_ist_dt(15, 15)
        with patch("utils.time_utils.now_ist", return_value=fake_now):
            assert is_accepting_signals() is False
