"""
TradeOS — Previous Close Cache (D5 Gate 2 reference data)

Loads previous day's closing prices for all watchlist instruments at startup.
Required by TickValidator Gate 2 (NSE ±20% circuit breaker filter).

Rules:
  - Load ONCE at startup. Never refresh during market hours.
  - On partial failure: store None, log WARNING — never block startup.
  - Gate 2 rule: None → PASS (never block ticks on missing reference data).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import pytz
import structlog

from utils.time_utils import is_market_hours

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


def _prev_trading_day() -> tuple[datetime, datetime]:
    """
    Return (from_date, to_date) covering the most recent weekday before today.

    Scans back up to 10 calendar days to skip weekends. NSE holidays are not
    checked here — a cache miss results in None which Gate 2 passes (D5 rule).
    """
    today = datetime.now(IST).date()
    for delta in range(1, 11):
        candidate = today - timedelta(days=delta)
        if candidate.weekday() < 5:   # Monday=0 … Friday=4
            break
    # kite.historical_data expects naive datetime; exchange timezone is implicit
    from_dt = datetime(candidate.year, candidate.month, candidate.day, 0, 0, 0)
    to_dt   = datetime(candidate.year, candidate.month, candidate.day, 23, 59, 59)
    return from_dt, to_dt


class PrevCloseCache:
    """
    Loads and caches previous day's closing prices for NSE circuit-breaker reference.

    Must be fully loaded (is_loaded() == True) before TickValidator is instantiated.
    After load() completes the internal dict is read-only — thread-safe for concurrent
    reads during the trading session.
    """

    def __init__(self, kite, instruments: list[dict]) -> None:
        """
        Args:
            kite: Authenticated KiteConnect instance.
            instruments: List of instrument dicts; each must have 'instrument_token'
                         and 'tradingsymbol' keys.
        """
        self._kite = kite
        self._instruments = instruments
        self._cache: dict[int, Optional[float]] = {}
        self._loaded: bool = False

    async def load(self) -> None:
        """
        Fetch previous day's close for every instrument via kite.historical_data().

        Each REST call is blocking — wrapped in asyncio.to_thread() (D6 rule).
        On partial failure: stores None for that instrument and logs WARNING.
        Startup is never blocked by a single instrument failure.
        """
        from_dt, to_dt = _prev_trading_day()
        log.info(
            "prev_close_cache_loading",
            instruments=len(self._instruments),
            reference_date=from_dt.date().isoformat(),
        )

        for instrument in self._instruments:
            token: int = instrument["instrument_token"]
            symbol: str = instrument.get("tradingsymbol", str(token))
            try:
                data: list[dict] = await asyncio.to_thread(
                    self._kite.historical_data,
                    token,
                    from_dt,
                    to_dt,
                    "day",
                    False,   # continuous=False
                )
                if data:
                    self._cache[token] = float(data[-1]["close"])
                else:
                    log.warning(
                        "prev_close_no_data",
                        symbol=symbol,
                        token=token,
                        note="No OHLC returned — Gate 2 will PASS for this instrument",
                    )
                    self._cache[token] = None
            except Exception as exc:
                # B10: DEBUG before market hours, WARNING during market hours
                _log = log.warning if is_market_hours() else log.debug
                _log(
                    "prev_close_load_failed",
                    symbol=symbol,
                    token=token,
                    error=str(exc),
                    note="Gate 2 will PASS for this instrument",
                )
                self._cache[token] = None

        self._loaded = True
        n_ok = sum(1 for v in self._cache.values() if v is not None)
        log.info(
            "prev_close_cache_loaded",
            total=len(self._instruments),
            loaded=n_ok,
            failed=len(self._instruments) - n_ok,
        )

    def get(self, instrument_token: int) -> Optional[float]:
        """
        Return previous close price for an instrument, or None if unavailable.

        Caller (TickValidator Gate 2) must treat None as: pass the gate.
        This is the D5 design rule — never block ticks on missing reference data.
        """
        return self._cache.get(instrument_token)

    def is_loaded(self) -> bool:
        """True if load() has been called and completed (including partial failures)."""
        return self._loaded
