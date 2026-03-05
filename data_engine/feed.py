"""
TradeOS — Data Feed (D3 WebSocket resilience + KiteConnect thread bridge)

KiteTicker runs in its own OS thread. All five callbacks fire in that thread.
Ticks are forwarded to the asyncio event loop via loop.call_soon_threadsafe().
Callbacks must never block the KiteTicker thread.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import pytz
import structlog
from kiteconnect import KiteConnect, KiteTicker

from data_engine.prev_close_cache import PrevCloseCache

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

WS_CONNECT_TIMEOUT: int = 30   # seconds (D3 spec)


class DataFeedConnectionError(Exception):
    """Raised when WebSocket does not confirm CONNECTED within the timeout."""


class DataFeed:
    """
    KiteTicker → asyncio bridge.

    KiteTicker.connect(threaded=True) starts an OS thread that manages the
    WebSocket connection. Tick callbacks in that thread push data into the
    asyncio tick_queue via call_soon_threadsafe() so the event loop is never
    blocked.
    """

    def __init__(
        self,
        kite: KiteConnect,
        instruments: list[dict],
        tick_queue: asyncio.Queue,
        shared_state: dict,
        prev_close_cache: PrevCloseCache,
    ) -> None:
        """
        Args:
            kite: Authenticated KiteConnect instance.
            instruments: Instrument dicts; each must have 'instrument_token'.
            tick_queue: asyncio.Queue (maxsize=1000, D6 contract).
            shared_state: D6 shared state dict. ws_listener owns ws_connected etc.
            prev_close_cache: Loaded cache (held for reference; not used inside feed).
        """
        self._kite             = kite
        self._instruments      = instruments
        self._tokens: list[int] = [i["instrument_token"] for i in instruments]
        self._tick_queue        = tick_queue
        self._shared_state      = shared_state
        self._prev_close_cache  = prev_close_cache

        self._kws: Optional[KiteTicker]                    = None
        self._loop: Optional[asyncio.AbstractEventLoop]    = None
        self._connected_event: Optional[asyncio.Event]     = None

        self._api_key      = kite.api_key
        self._access_token = kite.access_token

    async def connect(self) -> None:
        """
        Initialize KiteTicker, register callbacks, and start in a background thread.

        Waits up to WS_CONNECT_TIMEOUT seconds for the on_connect callback.
        Raises DataFeedConnectionError on timeout (startup blocks until connected).
        """
        self._loop            = asyncio.get_running_loop()
        self._connected_event = asyncio.Event()

        self._kws             = KiteTicker(self._api_key, self._access_token)
        self._kws.on_ticks    = self._on_ticks
        self._kws.on_connect  = self._on_connect
        self._kws.on_close    = self._on_disconnect
        self._kws.on_error    = self._on_error

        log.info("data_feed_connecting", instrument_count=len(self._tokens))
        self._kws.connect(threaded=True)   # non-blocking — KiteTicker runs in own thread

        try:
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=WS_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.critical("data_feed_connect_timeout",
                         timeout_seconds=WS_CONNECT_TIMEOUT)
            raise DataFeedConnectionError(
                f"WebSocket did not connect within {WS_CONNECT_TIMEOUT}s"
            )

    async def disconnect(self) -> None:
        """Gracefully stop KiteTicker and update shared state."""
        if self._kws is not None:
            self._kws.close()
        self._shared_state["ws_connected"] = False
        log.info("data_feed_disconnected")

    # ------------------------------------------------------------------
    # KiteTicker callbacks — all run in the KiteTicker OS thread
    # Rule: never block; use call_soon_threadsafe for asyncio interaction
    # ------------------------------------------------------------------

    def _on_ticks(self, ws: KiteTicker, ticks: list[dict]) -> None:
        """
        Called from KiteTicker thread when tick data arrives.

        Forwards each tick to the asyncio tick_queue via call_soon_threadsafe.
        Also updates shared_state["last_tick_timestamp"] for D3 heartbeat checks.
        Returns immediately — never blocks.
        """
        assert self._loop is not None   # set by connect() before KiteTicker starts
        now_ist = datetime.now(IST)
        self._loop.call_soon_threadsafe(
            self._shared_state.__setitem__,
            "last_tick_timestamp",
            now_ist,
        )
        for tick in ticks:
            self._loop.call_soon_threadsafe(self._tick_queue.put_nowait, tick)

    def _on_connect(self, ws: KiteTicker, response: dict) -> None:
        """
        Called from KiteTicker thread after successful connect or reconnect.

        Subscribes all instruments in MODE_FULL and unblocks DataFeed.connect().
        """
        assert self._loop is not None   # set by connect() before KiteTicker starts
        log.info(
            "kiteticker_connected",
            instruments=len(self._tokens),
            reconnect_attempt=self._shared_state.get("reconnect_attempt", 0),
        )
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)

        self._loop.call_soon_threadsafe(
            self._shared_state.update,
            {
                "ws_connected":       True,
                "reconnect_attempt":  0,
                "disconnect_timestamp": None,
            },
        )
        # Signal the waiting coroutine in connect()
        if self._connected_event is not None:
            self._loop.call_soon_threadsafe(self._connected_event.set)

    def _on_disconnect(self, ws: KiteTicker, code: int, reason: str) -> None:
        """
        Called from KiteTicker thread on graceful or unexpected disconnect.

        Records the disconnection in shared state.
        D3 reconnect logic is handled by the ws_listener task, not here.
        """
        assert self._loop is not None   # set by connect() before KiteTicker starts
        log.warning("kiteticker_disconnected", code=code, reason=reason)
        self._loop.call_soon_threadsafe(
            self._shared_state.update,
            {
                "ws_connected":       False,
                "disconnect_timestamp": datetime.now(IST),
            },
        )

    def _on_error(self, ws: KiteTicker, code: int, reason: str) -> None:
        """Called from KiteTicker thread on WebSocket errors."""
        assert self._loop is not None   # set by connect() before KiteTicker starts
        log.error("kiteticker_error", code=code, reason=reason)
        if not self._shared_state.get("disconnect_timestamp"):
            self._loop.call_soon_threadsafe(
                self._shared_state.__setitem__,
                "disconnect_timestamp",
                datetime.now(IST),
            )
