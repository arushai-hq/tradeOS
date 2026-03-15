"""
TradeOS — Tick Storage (asyncpg → TimescaleDB)

Batched writes: accumulate ticks in memory, flush every 1 second OR when
the buffer reaches 500 ticks — whichever comes first.

Never blocks the asyncio event loop. On DB failure: retries once, then
falls back to a local CSV so no ticks are lost.
"""
from __future__ import annotations

import asyncio
import csv
from datetime import date
from pathlib import Path
from typing import Optional

import asyncpg
import pytz
import structlog

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

FLUSH_INTERVAL_SECS: float = 1.0
FLUSH_MAX_TICKS: int       = 500
FALLBACK_DIR: Path         = Path("data_engine/fallback")


class TickStorage:
    """
    Async PostgreSQL/TimescaleDB writer.

    Ticks are batched in memory and bulk-inserted every second (or at 500 ticks).
    Signals, trades, and system events bypass the buffer and are written immediately
    (low frequency — direct write latency is acceptable).

    Failure handling:
      1. DB error → log ERROR, retry bulk-insert once
      2. Retry fails → log CRITICAL, write to fallback CSV (never lose a tick)
    """

    def __init__(self, dsn: str, session_date: date) -> None:
        """
        Args:
            dsn: asyncpg DSN string. Never logged in full (credentials).
            session_date: Trading session date (IST). Written to every row.
        """
        self._dsn          = dsn
        self._session_date = session_date
        self._pool: Optional[asyncpg.Pool] = None
        self._buffer: list[dict]           = []
        self._fallback_dir                 = FALLBACK_DIR
        self._fallback_dir.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        """Create asyncpg connection pool (min_size=2, max_size=5)."""
        host_hint = self._dsn.split("@")[-1] if "@" in self._dsn else "localhost"
        log.info("tick_storage_connecting", host=host_hint)
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=2, max_size=5,
        )
        log.info("tick_storage_connected")

    async def disconnect(self) -> None:
        """Flush any remaining buffer, then close the connection pool."""
        log.info("tick_storage_disconnecting", buffer_remaining=len(self._buffer))
        if self._buffer:
            await self._do_flush()
        if self._pool is not None:
            await self._pool.close()
        log.info("tick_storage_pool_closed")

    async def write_tick(self, tick: dict) -> None:
        """
        Append a validated tick to the in-memory buffer (non-blocking).

        flush_loop() drains the buffer every second.
        If the buffer reaches FLUSH_MAX_TICKS an immediate flush is triggered.
        """
        self._buffer.append(tick)
        if len(self._buffer) >= FLUSH_MAX_TICKS:
            await self._do_flush()

    async def flush_loop(self) -> None:
        """
        Background asyncio task: flush the tick buffer every FLUSH_INTERVAL_SECS.

        Runs until cancelled by DataEngine.__aexit__.
        On DB error → retry once → fallback CSV (never lose ticks).
        """
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECS)
            if self._buffer:
                await self._do_flush()

    async def _do_flush(self) -> None:
        """Snapshot the buffer and bulk-insert into TimescaleDB."""
        if not self._buffer:
            return
        batch       = self._buffer[:]
        self._buffer.clear()

        try:
            await self._bulk_insert_ticks(batch)
            log.debug("tick_storage_flushed", count=len(batch))
        except Exception as exc:
            log.error("tick_storage_flush_failed_retrying",
                      count=len(batch), error=str(exc))
            try:
                await self._bulk_insert_ticks(batch)
                log.info("tick_storage_flush_retry_success", count=len(batch))
            except Exception as retry_exc:
                log.critical("tick_storage_flush_failed_using_fallback",
                             count=len(batch), error=str(retry_exc))
                self._write_fallback_csv(batch)

    async def _bulk_insert_ticks(self, batch: list[dict]) -> None:
        """Execute a bulk INSERT for the given batch of tick dicts."""
        if not batch or self._pool is None:
            return

        rows = []
        for tick in batch:
            exchange_ts = tick.get("exchange_timestamp")
            if exchange_ts is not None:
                if hasattr(exchange_ts, "tzinfo") and exchange_ts.tzinfo is None:
                    exchange_ts = IST.localize(exchange_ts)
            rows.append((
                tick.get("instrument_token"),
                tick.get("tradingsymbol", ""),
                tick.get("last_price"),
                tick.get("volume_traded"),
                tick.get("oi"),
                tick.get("bid"),
                tick.get("ask"),
                exchange_ts,
                self._session_date,
            ))

        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO ticks (
                    instrument_token, symbol, last_price, volume_traded, oi,
                    bid_price, ask_price, exchange_timestamp, session_date
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                rows,
            )

    def _write_fallback_csv(self, batch: list[dict]) -> None:
        """
        Write a batch of ticks to the fallback CSV when DB is unreachable.

        Path: data_engine/fallback/{session_date}.csv
        Appends if the file already exists. Never raises.
        """
        csv_path   = self._fallback_dir / f"{self._session_date.isoformat()}.csv"
        file_exists = csv_path.exists()
        fieldnames  = [
            "instrument_token", "tradingsymbol", "last_price", "volume_traded",
            "oi", "bid", "ask", "exchange_timestamp", "session_date",
        ]
        try:
            with open(csv_path, "a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                if not file_exists:
                    writer.writeheader()
                for tick in batch:
                    writer.writerow({
                        "instrument_token": tick.get("instrument_token"),
                        "tradingsymbol":    tick.get("tradingsymbol", ""),
                        "last_price":       tick.get("last_price"),
                        "volume_traded":    tick.get("volume_traded"),
                        "oi":               tick.get("oi"),
                        "bid":              tick.get("bid"),
                        "ask":              tick.get("ask"),
                        "exchange_timestamp": str(tick.get("exchange_timestamp", "")),
                        "session_date":     str(self._session_date),
                    })
            log.info("tick_storage_fallback_written",
                     path=str(csv_path), count=len(batch))
        except Exception as exc:
            log.critical("tick_storage_fallback_csv_failed",
                         path=str(csv_path), error=str(exc))

