"""
TradeOS — Data Engine

Async context manager that wires together all Data Engine components:
  PrevCloseCache → TickValidator → DataFeed → TickStorage

Usage (from main.py):
    async with DataEngine(kite, config, shared_state) as engine:
        await engine.run()
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import pytz
import structlog

from data_engine.feed import DataFeed
from data_engine.prev_close_cache import PrevCloseCache
from data_engine.storage import TickStorage
from data_engine.validator import TickValidator

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


class DataEngine:
    """
    Bootstraps and runs the full Data Engine pipeline.

    __aenter__ startup order (all steps must complete before run() is called):
      0. Assert paper mode — hard fail if misconfigured (D6 Gate 0)
      1. Resolve instruments from watchlist via kite.instruments("NSE")
      2. Load PrevCloseCache (blocking until complete)
      3. Connect TickStorage (asyncpg pool)
      4. Start flush_loop as background task
      5. Create TickValidator with loaded cache
      6. Connect DataFeed (blocks until WebSocket CONNECTED, 30 s timeout)

    __aexit__ shutdown order:
      1. Disconnect DataFeed (stop new ticks)
      2. Cancel background tasks
      3. Final buffer flush + close DB pool
    """

    def __init__(
        self,
        kite,
        config: dict,
        shared_state: dict,
        tick_queue: Optional[asyncio.Queue] = None,
        strategy_queue: Optional[asyncio.Queue] = None,
    ) -> None:
        """
        Args:
            kite: Authenticated KiteConnect instance.
            config: Loaded settings.yaml dict.
            shared_state: D6 shared state dict (initialised by caller).
            tick_queue: Override queue; defaults to shared_state["tick_queue"].
            strategy_queue: Second queue for StrategyEngine fan-out (fixes dual-consumer bug).
        """
        self._kite         = kite
        self._config       = config
        self._shared_state = shared_state
        self._tick_queue: asyncio.Queue = (
            tick_queue if tick_queue is not None
            else shared_state.get("tick_queue", asyncio.Queue(maxsize=1000))
        )
        self._strategy_queue: Optional[asyncio.Queue] = strategy_queue

        self._instruments: list[dict]              = []
        self._prev_close_cache: Optional[PrevCloseCache] = None
        self._storage: Optional[TickStorage]       = None
        self._validator: Optional[TickValidator]   = None
        self._feed: Optional[DataFeed]             = None
        self._flush_task: Optional[asyncio.Task]   = None
        self._bad_tick_task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "DataEngine":
        """Bootstrap all Data Engine components in dependency order."""
        # Step 0: mode safety gate
        mode = self._config.get("system", {}).get("mode", "")
        assert mode == "paper", (
            f"DataEngine: system.mode must be 'paper', got '{mode}'. "
            "Set system.mode: paper in config/settings.yaml."
        )
        log.info("data_engine_starting", mode=mode)

        # Step 1: resolve instruments
        self._instruments = await self._resolve_instruments()
        log.info("data_engine_instruments_resolved", count=len(self._instruments))

        # Step 2: load prev_close_cache
        self._prev_close_cache = PrevCloseCache(self._kite, self._instruments)
        await self._prev_close_cache.load()

        # Step 3: connect tick storage
        db_dsn       = self._config.get("database", {}).get("dsn", "")
        session_date = datetime.now(IST).date()
        self._storage = TickStorage(dsn=db_dsn, session_date=session_date)
        await self._storage.connect()

        # Step 4: start flush_loop background task
        self._flush_task = asyncio.create_task(
            self._storage.flush_loop(), name="tick_storage_flush",
        )

        # Step 5: create validator (requires loaded cache)
        self._validator = TickValidator(self._prev_close_cache)
        log.info("data_engine_validator_ready")

        # Step 6: connect feed (blocks on WebSocket CONNECTED)
        self._feed = DataFeed(
            kite=self._kite,
            instruments=self._instruments,
            tick_queue=self._tick_queue,
            shared_state=self._shared_state,
            prev_close_cache=self._prev_close_cache,
        )
        await self._feed.connect()

        # Start hourly bad-tick alert monitor
        self._bad_tick_task = asyncio.create_task(
            self._hourly_bad_tick_monitor(), name="bad_tick_monitor",
        )

        log.info(
            "data_engine_ready",
            instruments=len(self._instruments),
            session_date=session_date.isoformat(),
        )
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Gracefully shut down all Data Engine components."""
        log.info("data_engine_stopping")

        if self._feed is not None:
            await self._feed.disconnect()

        for task in (self._flush_task, self._bad_tick_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._storage is not None:
            await self._storage.disconnect()

        log.info("data_engine_stopped")

    async def run(self) -> None:
        """
        Consume raw ticks from tick_queue_storage, validate, write to DB,
        then fan out validated ticks to tick_queue_strategy.

        Fan-out happens here (not in _on_ticks) so that:
          - StrategyEngine always receives only validated ticks.
          - await put() provides back-pressure instead of silent put_nowait drops.

        Main processing loop. Runs until cancelled. CancelledError is never
        suppressed (D6 rule).
        """
        if self._validator is None or self._storage is None:
            raise RuntimeError("DataEngine.run() called before __aenter__")

        log.info("data_engine_run_started")
        while True:
            try:
                tick = await self._tick_queue.get()
                validated = self._validator.validate(tick)
                if validated is not None:
                    await self._storage.write_tick(validated)
                    # Fan-out validated tick to StrategyEngine queue
                    if self._strategy_queue is not None:
                        await self._strategy_queue.put(validated)
                self._tick_queue.task_done()
            except asyncio.CancelledError:
                raise   # D6 rule: never suppress CancelledError
            except Exception as exc:
                log.error("data_engine_run_error", error=str(exc), exc_info=True)

    async def _resolve_instruments(self) -> list[dict]:
        """
        Fetch NSE instrument listing and filter to watchlist symbols.

        Uses asyncio.to_thread() for the blocking kite.instruments() call (D6 rule).
        """
        watchlist: list[str] = self._config.get("watchlist", [])
        log.info("resolving_instruments", watchlist_count=len(watchlist))

        all_instruments: list[dict] = await asyncio.to_thread(
            self._kite.instruments, "NSE"
        )

        watchlist_set = set(watchlist)
        filtered = [
            i for i in all_instruments
            if i.get("tradingsymbol") in watchlist_set
            and i.get("segment") == "NSE"
        ]

        missing = watchlist_set - {i["tradingsymbol"] for i in filtered}
        if missing:
            log.warning("instruments_not_found", missing=sorted(missing))

        return filtered

    async def _hourly_bad_tick_monitor(self) -> None:
        """Background task: invoke bad-tick threshold check every hour."""
        while True:
            await asyncio.sleep(3600)
            if self._validator is not None:
                self._validator.check_hourly_bad_tick_alert()

    @property
    def storage(self) -> TickStorage:
        """Access TickStorage for writing signals, trades, and system events."""
        if self._storage is None:
            raise RuntimeError("DataEngine not initialised — call __aenter__ first")
        return self._storage
