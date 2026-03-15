"""
TradeOS — WarmupLoader: Historical candle loader at startup

Two-phase strategy (per instrument):
  Phase 1 (fast path): SELECT from candles_15m table (last 60 days).
  Phase 2 (fill):      Fetch from kite.historical_data() if DB has < WARMUP_TARGET.

Rules:
  - kite.historical_data() is WARMUP ONLY — never call during live trading.
  - All Zerodha API calls use asyncio.to_thread() (D6 rule).
  - On partial failure: log WARNING, continue with fewer candles.
  - Fewer than 21 candles → log ERROR, return [] for that instrument.
  - API-fetched candles are stored to candles_15m for next session.
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import asyncpg
import pytz

from core.strategy_engine.candle_builder import Candle

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")

WARMUP_TARGET: int = 60   # target candle count (recommended minimum for stable RSI)
WARMUP_MIN: int = 21      # absolute minimum (EMA21 period)
WARMUP_DAYS: int = 60     # calendar days of history to fetch from API


class WarmupLoader:
    """
    Loads historical 15-minute candles at startup to prime the IndicatorEngine.

    Reads from the candles_15m DB table first (fast), then falls back to
    kite.historical_data() for any instrument with fewer than WARMUP_TARGET candles.
    API-fetched candles are persisted to DB for subsequent sessions.
    """

    async def load(
        self,
        instruments: list[dict],
        kite,
        db_pool: asyncpg.Pool,
    ) -> dict[int, list[Candle]]:
        """
        Load warmup candles for all instruments.

        Args:
            instruments: List of instrument dicts with 'instrument_token'
                         and 'tradingsymbol' keys.
            kite: Authenticated KiteConnect instance.
            db_pool: asyncpg connection pool.

        Returns:
            Dict mapping instrument_token → list[Candle] in chronological order.
            Instruments with fewer than WARMUP_MIN candles are mapped to [].
        """
        result: dict[int, list[Candle]] = {}

        for instrument in instruments:
            token: int = instrument["instrument_token"]
            symbol: str = instrument.get("tradingsymbol", str(token))

            # Phase 1: read from DB
            candles = await self._load_from_db(token, symbol, db_pool)

            # Phase 2: fill from KiteConnect if below target
            if len(candles) < WARMUP_TARGET:
                log.info(
                    "warmup_db_insufficient",
                    symbol=symbol,
                    db_count=len(candles),
                    target=WARMUP_TARGET,
                )
                api_candles = await self._load_from_api(token, symbol, kite, db_pool)
                # API candles are older; merge without duplicates by candle_time
                existing_times = {c.candle_time for c in candles}
                new_candles = [c for c in api_candles if c.candle_time not in existing_times]
                candles = sorted(new_candles + candles, key=lambda c: c.candle_time)

            if len(candles) < WARMUP_MIN:
                log.error(
                    "warmup_insufficient_for_trading",
                    symbol=symbol,
                    candle_count=len(candles),
                    minimum=WARMUP_MIN,
                    note="Instrument will be skipped today — cannot compute EMA21",
                )
                result[token] = []
            else:
                log.info(
                    "warmup_complete",
                    symbol=symbol,
                    candle_count=len(candles),
                )
                result[token] = candles

        return result

    # ------------------------------------------------------------------
    # Phase 1 — DB read
    # ------------------------------------------------------------------

    async def _load_from_db(
        self,
        token: int,
        symbol: str,
        db_pool: asyncpg.Pool,
    ) -> list[Candle]:
        """Load historical candles from the candles_15m table."""
        try:
            async with db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT instrument_token, symbol,
                           open, high, low, close,
                           volume, vwap, candle_time, session_date
                    FROM candles_15m
                    WHERE instrument_token = $1
                      AND session_date >= CURRENT_DATE - INTERVAL '60 days'
                    ORDER BY candle_time ASC
                    """,
                    token,
                )
            return [self._row_to_candle(row) for row in rows]
        except Exception as exc:
            log.warning(
                "warmup_db_load_failed",
                symbol=symbol,
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------
    # Phase 2 — KiteConnect REST API (WARMUP ONLY)
    # ------------------------------------------------------------------

    async def _load_from_api(
        self,
        token: int,
        symbol: str,
        kite,
        db_pool: asyncpg.Pool,
    ) -> list[Candle]:
        """
        Fetch historical 15-min candles via kite.historical_data().

        Uses asyncio.to_thread() because kite.historical_data() is synchronous (D6 rule).
        Stores fetched candles into candles_15m for the next session.
        """
        today = datetime.now(IST).date()
        from_date = today - timedelta(days=WARMUP_DAYS)
        to_date = today - timedelta(days=1)

        # kite.historical_data expects naive datetimes (exchange timezone is implicit)
        from_dt = datetime(from_date.year, from_date.month, from_date.day, 9, 15, 0)
        to_dt = datetime(to_date.year, to_date.month, to_date.day, 15, 30, 0)

        try:
            data: list[dict] = await asyncio.to_thread(
                kite.historical_data,
                token,
                from_dt,
                to_dt,
                "15minute",
                False,  # continuous=False
            )
            candles = [self._api_row_to_candle(row, token, symbol) for row in data]
            log.info(
                "warmup_fetched_from_kiteconnect",
                symbol=symbol,
                count=len(candles),
            )
            if candles:
                await self._store_to_db(candles, db_pool)
            return candles

        except Exception as exc:
            log.warning(
                "warmup_api_load_failed",
                symbol=symbol,
                error=str(exc),
                note="Continuing with available candles — may be below WARMUP_TARGET",
            )
            return []

    async def _store_to_db(self, candles: list[Candle], db_pool: asyncpg.Pool) -> None:
        """Persist API-fetched candles to candles_15m for future sessions."""
        rows = [
            (
                c.instrument_token, c.symbol,
                float(c.open), float(c.high), float(c.low), float(c.close),
                c.volume,
                float(c.vwap) if c.vwap != Decimal("0") else None,
                c.candle_time, c.session_date,
            )
            for c in candles
        ]
        try:
            async with db_pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO candles_15m (
                        instrument_token, symbol,
                        open, high, low, close,
                        volume, vwap, candle_time, session_date
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (instrument_token, candle_time) DO NOTHING
                    """,
                    rows,
                )
        except Exception as exc:
            log.warning("warmup_store_to_db_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_candle(row) -> Candle:
        """Convert asyncpg record from candles_15m to Candle."""
        ct = row["candle_time"]
        if hasattr(ct, "tzinfo") and ct.tzinfo is None:
            ct = IST.localize(ct)
        vwap_val = row["vwap"]
        return Candle(
            instrument_token=row["instrument_token"],
            symbol=row["symbol"],
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["volume"]),
            vwap=Decimal(str(vwap_val)) if vwap_val is not None else Decimal("0"),
            candle_time=ct,
            session_date=row["session_date"],
            tick_count=0,  # historical — tick_count not stored in DB
        )

    @staticmethod
    def _api_row_to_candle(row: dict, token: int, symbol: str) -> Candle:
        """Convert kite.historical_data() row to Candle."""
        ct = row["date"]
        if hasattr(ct, "tzinfo") and ct.tzinfo is None:
            ct = IST.localize(ct)
        avg_raw = row.get("average_price") or row.get("close")
        return Candle(
            instrument_token=token,
            symbol=symbol,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=int(row["volume"]),
            vwap=Decimal(str(avg_raw)) if avg_raw else Decimal("0"),
            candle_time=ct,
            session_date=ct.date(),
            tick_count=0,
        )
