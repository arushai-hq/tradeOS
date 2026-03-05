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
import json
from datetime import date, datetime
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

    # ------------------------------------------------------------------
    # Low-frequency direct writes (not batched)
    # ------------------------------------------------------------------

    async def write_signal(self, signal: dict) -> None:
        """Write a signal to the signals table (immediate INSERT, not batched)."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signals (
                        session_date, symbol, instrument_token, direction,
                        signal_time, candle_time,
                        ema9, ema21, rsi, vwap, volume_ratio,
                        theoretical_entry, stop_loss, target,
                        order_id, status
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    """,
                    self._session_date,
                    signal["symbol"], signal["instrument_token"], signal["direction"],
                    signal["signal_time"], signal["candle_time"],
                    signal["ema9"], signal["ema21"], signal["rsi"],
                    signal["vwap"], signal["volume_ratio"],
                    signal["theoretical_entry"], signal["stop_loss"], signal["target"],
                    signal.get("order_id"), signal.get("status", "PENDING"),
                )
            log.info("signal_written",
                     symbol=signal["symbol"], direction=signal["direction"])
        except Exception as exc:
            log.error("signal_write_failed",
                      symbol=signal.get("symbol"), error=str(exc))

    async def write_trade(self, trade: dict) -> None:
        """Write a completed trade to the trades table (immediate INSERT)."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades (
                        session_date, symbol, direction, signal_id,
                        entry_order_id, entry_time, actual_entry, theoretical_entry,
                        entry_slippage, qty,
                        exit_order_id, exit_time, actual_exit, exit_reason,
                        gross_pnl, charges, net_pnl, pnl_pct
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    """,
                    self._session_date,
                    trade["symbol"], trade["direction"], trade.get("signal_id"),
                    trade["entry_order_id"], trade["entry_time"],
                    trade["actual_entry"], trade["theoretical_entry"],
                    trade.get("entry_slippage"), trade["qty"],
                    trade.get("exit_order_id"), trade.get("exit_time"),
                    trade.get("actual_exit"), trade.get("exit_reason"),
                    trade.get("gross_pnl"), trade.get("charges"),
                    trade.get("net_pnl"), trade.get("pnl_pct"),
                )
            log.info("trade_written",
                     symbol=trade["symbol"], direction=trade["direction"],
                     net_pnl=trade.get("net_pnl"))
        except Exception as exc:
            log.error("trade_write_failed",
                      symbol=trade.get("symbol"), error=str(exc))

    async def write_system_event(self, event: dict) -> None:
        """Write a system event to the system_events table (immediate INSERT)."""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO system_events (
                        session_date, event_time, event_type, level, detail, kill_switch_level
                    ) VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                    """,
                    self._session_date,
                    event.get("event_time", datetime.now(IST)),
                    event["event_type"],
                    event.get("level", "INFO"),
                    json.dumps(event["detail"]) if event.get("detail") else None,
                    event.get("kill_switch_level"),
                )
        except Exception as exc:
            log.error("system_event_write_failed",
                      event_type=event.get("event_type"), error=str(exc))
