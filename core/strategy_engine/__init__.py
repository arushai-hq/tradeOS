"""
TradeOS — Strategy Engine

Async context manager that coordinates:
  WarmupLoader → CandleBuilder (per instrument) → IndicatorEngine (per instrument)
  → SignalGenerator → RiskGate → order_queue

Consumes validated ticks from tick_queue. Produces approved Signal objects on
order_queue.  Does NOT place orders — signals only.

Startup sequence (__aenter__):
  0. Assert paper mode
  1. Resolve instruments from config watchlist + kite.instruments("NSE")
  2. Load warmup candles (WarmupLoader)
  3. Initialise CandleBuilder per instrument
  4. Initialise IndicatorEngine per instrument with warmup candles
  5. Initialise SignalGenerator + RiskGate

Per-tick processing (run()):
  1. CandleBuilder.process_tick() → Candle or None
  2. Persist completed Candle to candles_15m
  3. IndicatorEngine.update() → Indicators or None
  4. SignalGenerator.evaluate() → Signal or None
  5. RiskGate.check() → (allowed, reason)
  6. Write signal to signals table (always, with appropriate status)
  7. If allowed: enqueue Signal to order_queue + update shared_state
"""
from __future__ import annotations

import asyncio
import structlog
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import asyncpg
import pytz

from core.strategy_engine.candle_builder import Candle, CandleBuilder
from core.strategy_engine.indicators import IndicatorEngine
from core.strategy_engine.risk_gate import KillSwitchProtocol, RiskGate
from core.strategy_engine.signal_generator import Signal, SignalGenerator
from core.strategy_engine.warmup import WarmupLoader

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Gate metadata for signal_rejected log events
# ---------------------------------------------------------------------------
_GATE_INFO: dict[str, tuple[int, str]] = {
    "KILL_SWITCH":            (1, "kill_switch"),
    "RECON_IN_PROGRESS":      (2, "recon_in_progress"),
    "INSTRUMENT_LOCKED":      (3, "instrument_locked"),
    "MAX_POSITIONS_REACHED":  (4, "max_positions"),
    "HARD_EXIT_TIME_REACHED": (5, "hard_exit_time"),
    "NO_ENTRY_WINDOW":        (5, "no_entry_window"),
    "DUPLICATE_SIGNAL":       (6, "duplicate_signal"),
    "REGIME_BLOCKED":         (7, "regime_check"),
    "REGIME_CRASH":           (7, "regime_check"),
}


def _parse_gate_info(reason: str) -> tuple[int, str]:
    """Map a rejection reason string to (gate_number, gate_name)."""
    for prefix, info in _GATE_INFO.items():
        if reason.startswith(prefix):
            return info
    return 0, "unknown"


class StrategyEngine:
    """
    Main Strategy Engine: tick → candle → indicators → signal → order_queue.

    Must be used as an async context manager:
        async with StrategyEngine(...) as engine:
            await engine.run()
    """

    def __init__(
        self,
        kite,
        config: dict,
        shared_state: dict,
        tick_queue: asyncio.Queue,
        order_queue: asyncio.Queue,
        db_pool: asyncpg.Pool,
        kill_switch: Optional[KillSwitchProtocol] = None,
        regime_detector=None,
        notifier=None,
    ) -> None:
        """
        Args:
            kite: Authenticated KiteConnect instance.
            config: Loaded settings.yaml dict.
            shared_state: D6 shared state dict.
            tick_queue: Source of validated ticks (from ws_listener).
            order_queue: Destination for approved signals (to execution engine).
            db_pool: asyncpg connection pool (candles_15m + signals tables).
            kill_switch: Optional D1 KillSwitch instance.
                         Falls back to shared_state["kill_switch_level"] if None.
            regime_detector: Optional RegimeDetector instance.
                             If None, Gate 7 in RiskGate is skipped.
            notifier: Optional TelegramNotifier for rich signal notifications.
        """
        self._kite = kite
        self._config = config
        self._shared_state = shared_state
        self._tick_queue = tick_queue
        self._order_queue = order_queue
        self._db_pool = db_pool
        self._kill_switch = kill_switch
        self._regime_detector = regime_detector
        self._notifier = notifier

        self._instruments: list[dict] = []
        self._candle_builders: dict[int, CandleBuilder] = {}
        self._indicator_engines: dict[int, IndicatorEngine] = {}
        self._signal_generator: Optional[SignalGenerator] = None
        self._risk_gate: Optional[RiskGate] = None

        self._session_date: date = datetime.now(IST).date()
        self._signals_generated: int = 0

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "StrategyEngine":
        """Bootstrap all Strategy Engine components in dependency order."""
        mode = self._config.get("system", {}).get("mode", "")
        assert mode == "paper", (
            f"StrategyEngine: system.mode must be 'paper', got '{mode}'."
        )
        log.info(
            "strategy_engine_starting",
            session_date=self._session_date.isoformat(),
        )

        # Step 1: resolve instruments from watchlist
        self._instruments = await self._resolve_instruments()
        log.info(
            "strategy_engine_instruments_resolved",
            count=len(self._instruments),
        )

        # Step 2: load warmup candles
        loader = WarmupLoader()
        warmup_data = await loader.load(self._instruments, self._kite, self._db_pool)

        # Load S1 strategy config
        s1_config = self._config.get("strategy", {}).get("s1", {})

        # Steps 3 & 4: CandleBuilder + IndicatorEngine per instrument
        for instrument in self._instruments:
            token = instrument["instrument_token"]
            symbol = instrument.get("tradingsymbol", str(token))
            self._candle_builders[token] = CandleBuilder(token, symbol)
            candles = warmup_data.get(token, [])
            self._indicator_engines[token] = IndicatorEngine(
                candles,
                ema_fast=int(s1_config.get("ema_fast", 9)),
                ema_slow=int(s1_config.get("ema_slow", 21)),
                rsi_period=int(s1_config.get("rsi_period", 14)),
                swing_lookback=int(s1_config.get("swing_lookback", 5)),
            )

        # Steps 5: SignalGenerator + RiskGate
        self._signal_generator = SignalGenerator(s1_config=s1_config)
        self._risk_gate = RiskGate(
            kill_switch=self._kill_switch,
            regime_detector=self._regime_detector,
        )

        log.info(
            "strategy_engine_ready",
            instruments=len(self._instruments),
            session_date=self._session_date.isoformat(),
        )
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Log session stats on clean shutdown."""
        log.info(
            "strategy_engine_stopped",
            signals_generated=self._signals_generated,
            session_date=self._session_date.isoformat(),
        )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Consume validated ticks from tick_queue and run the strategy pipeline.

        Runs indefinitely until cancelled. CancelledError is never suppressed
        (D6 rule: never suppress CancelledError in any task coroutine).
        """
        if self._signal_generator is None or self._risk_gate is None:
            raise RuntimeError("StrategyEngine.run() called before __aenter__")

        log.info("strategy_engine_run_started")
        while True:
            try:
                tick = await self._tick_queue.get()
                try:
                    await self._process_tick(tick)
                except asyncio.CancelledError:
                    raise  # propagate — never suppress
                except Exception as exc:
                    log.error(
                        "strategy_engine_tick_error",
                        error=str(exc),
                        exc_info=True,
                    )
                finally:
                    self._tick_queue.task_done()
            except asyncio.CancelledError:
                raise  # D6 rule

    # ------------------------------------------------------------------
    # Per-tick pipeline
    # ------------------------------------------------------------------

    async def _process_tick(self, tick: dict) -> None:
        """Run a single tick through the full strategy pipeline."""
        token = tick.get("instrument_token")
        if token is None:
            return

        builder = self._candle_builders.get(token)
        ind_engine = self._indicator_engines.get(token)
        if builder is None or ind_engine is None:
            return

        # Step 1: build candle
        candle = builder.process_tick(tick)
        if candle is None:
            return  # candle still building

        # Step 2: persist candle to DB
        await self._write_candle(candle)

        # Step 3: compute indicators
        indicators = ind_engine.update(candle)
        if indicators is None:
            return  # still warming up

        # B2 fix: halt gate — no new signals after hard_exit at 15:00
        if not self._shared_state.get("accepting_signals", True):
            log.debug(
                "signal_skipped_hard_exit_active",
                symbol=candle.symbol,
                candle_time=candle.candle_time.isoformat(),
            )
            return

        # Step 4: evaluate S1 entry conditions
        assert self._signal_generator is not None
        signal = self._signal_generator.evaluate(candle, indicators)
        if signal is None:
            return  # no entry

        # Step 5: risk gate check
        assert self._risk_gate is not None
        allowed, reason = self._risk_gate.check(
            signal, self._shared_state, self._config
        )

        # Step 6: write signal to DB (always — for the audit trail)
        signal_db_id = await self._write_signal(signal, allowed, reason)
        if signal_db_id is not None:
            signal.db_id = signal_db_id

        # Step 7: enqueue if allowed — notification deferred to execution engine
        # (signal_accepted Telegram fires AFTER position sizer check in EE)
        if allowed:
            await self._order_queue.put(signal)
            self._signals_generated += 1
            self._shared_state["last_signal"] = {
                "symbol": signal.symbol,
                "direction": signal.direction,
                "signal_time": signal.signal_time.isoformat(),
            }
            _regime = (
                self._regime_detector.current_regime().value
                if self._regime_detector is not None else "unknown"
            )
            log.info(
                "signal_accepted",
                symbol=signal.symbol,
                direction=signal.direction,
                entry=float(signal.theoretical_entry),
                stop=float(signal.stop_loss),
                target=float(signal.target),
                rsi=float(signal.rsi),
                volume_ratio=float(signal.volume_ratio),
                regime=_regime,
                gates_passed="all",
            )
            log.info(
                "signal_queued",
                symbol=signal.symbol,
                direction=signal.direction,
                entry=float(signal.theoretical_entry),
                stop=float(signal.stop_loss),
                target=float(signal.target),
            )
        else:
            _gate_number, _gate_name = _parse_gate_info(reason)
            log.info(
                "signal_rejected",
                symbol=signal.symbol,
                direction=signal.direction,
                entry=float(signal.theoretical_entry),
                stop=float(signal.stop_loss),
                target=float(signal.target),
                gate=_gate_number,
                gate_name=_gate_name,
                reason=reason,
                rsi=float(signal.rsi),
                volume_ratio=float(signal.volume_ratio),
            )
            self._shared_state["signals_rejected_today"] = (
                self._shared_state.get("signals_rejected_today", 0) + 1
            )
            if getattr(self, "_notifier", None) is not None:
                self._notifier.notify_signal_rejected(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    gate_name=_gate_name,
                    gate_number=_gate_number,
                    reason=reason,
                    rsi=float(signal.rsi),
                )
            log.info(
                "signal_blocked",
                symbol=signal.symbol,
                direction=signal.direction,
                reason=reason,
            )

    # ------------------------------------------------------------------
    # DB writes
    # ------------------------------------------------------------------

    async def _write_candle(self, candle: Candle) -> None:
        """Persist a completed candle to candles_15m (idempotent via ON CONFLICT)."""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO candles_15m (
                        instrument_token, symbol,
                        open, high, low, close, volume, vwap,
                        candle_time, session_date
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (instrument_token, candle_time) DO NOTHING
                    """,
                    candle.instrument_token, candle.symbol,
                    float(candle.open), float(candle.high),
                    float(candle.low), float(candle.close),
                    candle.volume,
                    float(candle.vwap) if candle.vwap != Decimal("0") else None,
                    candle.candle_time, candle.session_date,
                )
        except Exception as exc:
            log.error("candle_write_failed", symbol=candle.symbol, error=str(exc))

    async def _write_signal(
        self,
        signal: Signal,
        allowed: bool,
        reason: str,
    ) -> Optional[int]:
        """
        Persist signal to signals table. Returns the auto-generated DB id.

        Status mapping (matches signals.status CHECK constraint):
          allowed      → status='PENDING'
          kill switch  → status='KILL_SWITCHED',  reject_reason=reason
          other block  → status='IGNORED',         reject_reason=reason
        """
        if allowed:
            status = "PENDING"
            reject_reason = None
        elif reason.startswith("KILL_SWITCH"):
            status = "KILL_SWITCHED"
            reject_reason = reason
        else:
            status = "IGNORED"
            reject_reason = reason

        try:
            async with self._db_pool.acquire() as conn:
                row_id: int = await conn.fetchval(
                    """
                    INSERT INTO signals (
                        session_date, symbol, instrument_token, direction,
                        signal_time, candle_time,
                        ema9, ema21, rsi, vwap, volume_ratio,
                        theoretical_entry, stop_loss, target,
                        status, reject_reason
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    RETURNING id
                    """,
                    self._session_date,
                    signal.symbol, signal.instrument_token, signal.direction,
                    signal.signal_time, signal.candle_time,
                    float(signal.ema9), float(signal.ema21),
                    float(signal.rsi), float(signal.vwap),
                    float(signal.volume_ratio),
                    float(signal.theoretical_entry),
                    float(signal.stop_loss), float(signal.target),
                    status, reject_reason,
                )
                return row_id
        except Exception as exc:
            log.error("signal_write_failed", symbol=signal.symbol, error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Instrument resolution
    # ------------------------------------------------------------------

    async def _resolve_instruments(self) -> list[dict]:
        """
        Fetch NSE instrument list and filter to config watchlist symbols.

        Uses asyncio.to_thread() for the blocking kite.instruments() call (D6 rule).
        """
        watchlist: list[str] = self._config.get("watchlist", [])
        log.info("strategy_engine_resolving_instruments", watchlist_count=len(watchlist))

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
            log.warning("strategy_engine_instruments_not_found", missing=sorted(missing))
        return filtered
