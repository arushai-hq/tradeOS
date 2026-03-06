"""
TradeOS — IST time utilities.

All time operations use Asia/Kolkata timezone.
Never use datetime.now() without IST — always go through these helpers.
"""
from __future__ import annotations

from datetime import date, datetime, time

import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    """Return current datetime in IST (timezone-aware)."""
    return datetime.now(IST)


def today_ist() -> date:
    """Return today's date in IST."""
    return now_ist().date()


def is_market_hours() -> bool:
    """
    Return True if current IST time is within NSE trading hours (09:15–15:30).
    """
    t = now_ist().time()
    return time(9, 15) <= t <= time(15, 30)


def is_accepting_signals() -> bool:
    """
    Return True if current IST time is within signal-acceptance window (09:15–15:00).

    Hard exit at 15:00 — no new entries after that even if market is open.
    """
    t = now_ist().time()
    return time(9, 15) <= t < time(15, 0)
