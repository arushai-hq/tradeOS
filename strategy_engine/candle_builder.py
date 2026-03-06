"""
TradeOS — 15-minute Candle Builder (S1 Strategy Engine)

Converts validated KiteConnect ticks into 15-minute OHLCV + VWAP candles.
One CandleBuilder instance per instrument.

Candle boundaries (IST, fixed): 09:15, 09:30, ..., 14:45, 15:00
26 candles per session.  Ticks outside [09:15, 15:30) are ignored.

Boundary rule: tick exactly at 09:30:00.000 belongs to the NEW candle (09:30),
not the closing candle (09:15).

Gap candles are NOT synthesised — logged as WARNING, skipped.
Volume is computed as a delta (candle window only, not cumulative session volume).
VWAP = tick["average_traded_price"] at candle close (KiteConnect session VWAP).
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

import pytz

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN: time = time(9, 15)
MARKET_CLOSE: time = time(15, 30)
CANDLE_MINUTES: int = 15


@dataclass
class Candle:
    """One completed 15-minute OHLCV candle."""

    instrument_token: int
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int          # delta volume for this 15-min window
    vwap: Decimal        # KiteConnect session VWAP (average_traded_price) at candle close
    candle_time: datetime  # candle open time (IST, timezone-aware)
    session_date: date
    tick_count: int      # number of ticks included in this candle


class CandleBuilder:
    """
    Builds 15-minute OHLCV candles from validated KiteConnect tick dicts.

    One instance per instrument.  Thread-safe for single asyncio task use.
    """

    def __init__(self, instrument_token: int, symbol: str) -> None:
        """
        Args:
            instrument_token: Zerodha instrument token.
            symbol: Trading symbol (e.g. "RELIANCE").
        """
        self._token: int = instrument_token
        self._symbol: str = symbol

        # Current in-progress candle state
        self._current_candle_time: Optional[datetime] = None
        self._open: Optional[Decimal] = None
        self._high: Optional[Decimal] = None
        self._low: Optional[Decimal] = None
        self._close: Optional[Decimal] = None
        self._volume_at_candle_start: int = 0  # cumulative volume at candle start
        self._volume_last: int = 0             # latest cumulative volume seen
        self._vwap: Decimal = Decimal("0")
        self._tick_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_tick(self, tick: dict) -> Optional[Candle]:
        """
        Process a validated tick.

        Returns a completed Candle when a 15-minute boundary is crossed.
        Returns None while the current candle is still building.

        Gap candles (periods with no ticks) are not synthesised — a WARNING
        is logged for each missed boundary and the gap is skipped.

        Args:
            tick: Validated KiteConnect tick dict (exchange_timestamp required).

        Returns:
            Completed Candle on boundary crossing, None otherwise.
        """
        exchange_ts: Optional[datetime] = tick.get("exchange_timestamp")
        if exchange_ts is None:
            return None

        # Ensure timezone-aware IST
        if exchange_ts.tzinfo is None:
            exchange_ts = IST.localize(exchange_ts)
        ts_ist: datetime = exchange_ts.astimezone(IST)

        # Ignore ticks outside [09:15, 15:30)
        t: time = ts_ist.time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return None

        candle_open_time: datetime = self._candle_boundary(ts_ist)
        price_raw = tick.get("last_price", 0)
        if price_raw is None or float(price_raw) <= 0:
            return None

        price = Decimal(str(price_raw))
        volume = int(tick.get("volume_traded", 0) or 0)
        avg_raw = tick.get("average_traded_price") or tick.get("last_price", price_raw)
        avg_price = Decimal(str(avg_raw))

        # First tick ever — start the first candle
        if self._current_candle_time is None:
            self._start_new_candle(candle_open_time, price, volume, avg_price)
            return None

        # Same candle boundary — update in place
        if candle_open_time == self._current_candle_time:
            self._update_candle(price, volume, avg_price)
            return None

        # Boundary crossed — detect any gap candles, then finalise
        expected_next = self._current_candle_time + timedelta(minutes=CANDLE_MINUTES)
        while expected_next < candle_open_time:
            log.warning(
                "gap_candle_detected",
                symbol=self._symbol,
                candle_time=expected_next.isoformat(),
            )
            expected_next += timedelta(minutes=CANDLE_MINUTES)

        completed = self._finalise_candle()
        self._start_new_candle(candle_open_time, price, volume, avg_price)
        return completed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _candle_boundary(self, ts: datetime) -> datetime:
        """
        Return the candle open time for an IST timestamp.

        Examples:
            09:22:30 IST → 09:15
            09:30:00 IST → 09:30   (on boundary → new candle)
            09:31:00 IST → 09:30

        Args:
            ts: Timezone-aware IST datetime.

        Returns:
            Timezone-aware IST datetime at the candle's open time.
        """
        t = ts.time()
        minutes_from_open = (t.hour * 60 + t.minute) - (9 * 60 + 15)
        if minutes_from_open < 0:
            minutes_from_open = 0
        slot = (minutes_from_open // CANDLE_MINUTES) * CANDLE_MINUTES
        total_minutes = 9 * 60 + 15 + slot
        hour = total_minutes // 60
        minute = total_minutes % 60
        return ts.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _start_new_candle(
        self,
        candle_time: datetime,
        price: Decimal,
        volume: int,
        avg_price: Decimal,
    ) -> None:
        """Initialise internal state for a new candle."""
        self._current_candle_time = candle_time
        self._open = price
        self._high = price
        self._low = price
        self._close = price
        self._volume_at_candle_start = volume
        self._volume_last = volume
        self._vwap = avg_price
        self._tick_count = 1

    def _update_candle(self, price: Decimal, volume: int, avg_price: Decimal) -> None:
        """Update OHLCV with the latest tick (same candle period)."""
        assert self._high is not None and self._low is not None
        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._close = price
        self._volume_last = volume         # cumulative — delta computed at finalise
        self._vwap = avg_price             # session VWAP; updated each tick
        self._tick_count += 1

    def _finalise_candle(self) -> Candle:
        """
        Construct and return the completed Candle dataclass.

        Volume = delta between cumulative volume_traded at the last tick
        of this candle and the first tick of this candle.
        """
        assert self._current_candle_time is not None
        assert self._open is not None
        assert self._high is not None
        assert self._low is not None
        assert self._close is not None

        delta_volume = max(0, self._volume_last - self._volume_at_candle_start)

        return Candle(
            instrument_token=self._token,
            symbol=self._symbol,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=delta_volume,
            vwap=self._vwap,
            candle_time=self._current_candle_time,
            session_date=self._current_candle_time.date(),
            tick_count=self._tick_count,
        )
