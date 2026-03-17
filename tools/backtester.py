#!/usr/bin/env python3
"""
TradeOS — Backtester Engine

Replays historical candles through the exact S1 pipeline (IndicatorEngine,
SignalGenerator, PositionSizer, ChargeCalculator, classify_regime) to simulate
trades and measure performance.

Three exit modes:
  - fixed:    Current S1 behaviour — stop or target, hard exit at 15:00
  - trailing: ATR-based trailing stop with regime gating
  - partial:  50% exit at 1R profit, trail remainder

Usage:
    python tools/backtester.py run --strategy s1 --from 2025-09-01 --to 2026-03-16
    python tools/backtester.py run --exit-mode trailing --atr-mult 1.5
    python tools/backtester.py optimize --param atr_multiplier --range 1.0:0.25:3.0
    python tools/backtester.py compare --modes fixed,trailing,partial
    python tools/backtester.py show --last-run
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import math
import os
import sys
from collections import defaultdict
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_DOWN, Decimal
from typing import Optional

# Add project root to path so imports work standalone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytz
import structlog
import yaml

from core.regime_detector.regime_detector import (
    MarketRegime,
    classify_regime,
)
from core.risk_manager.charge_calculator import ChargeCalculator
from core.risk_manager.position_sizer import PositionSizer
from core.strategy_engine.candle_builder import Candle
from core.strategy_engine.indicators import IndicatorEngine
from core.strategy_engine.signal_generator import Signal, SignalGenerator
from utils.progress import spinner, step_done, step_fail, step_info

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")
HARD_EXIT_TIME: time = time(15, 0)

# Minimum trail distance as a fraction of current price
MIN_TRAIL_PCT = Decimal("0.005")  # 0.5%


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BacktestPosition:
    """An open simulated position."""

    symbol: str
    instrument_token: int
    direction: str  # 'LONG' or 'SHORT'
    entry_price: Decimal
    entry_time: datetime
    qty: int
    stop_loss: Decimal
    target: Decimal
    original_stop: Decimal
    regime: str
    partial_exited: bool = False
    partial_qty: int = 0


@dataclass
class BacktestTrade:
    """A closed trade record."""

    symbol: str
    instrument_token: int
    direction: str
    entry_price: Decimal
    entry_time: datetime
    exit_price: Decimal
    exit_time: datetime
    exit_reason: str
    qty: int
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    regime: str


@dataclass
class DailyResult:
    """Summary of one trading day."""

    session_date: date
    trades_closed: int
    gross_pnl: Decimal
    net_pnl: Decimal
    regime: str


@dataclass
class BacktestResult:
    """Complete backtest results."""

    trades: list[BacktestTrade]
    daily_results: list[DailyResult]
    params: dict
    # Summary metrics
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    expectancy: Decimal = Decimal("0")
    gross_pnl: Decimal = Decimal("0")
    total_charges: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# S1v2 — Trend Pullback state machine
# ---------------------------------------------------------------------------

class S1v2Phase:
    """State machine phases for S1v2 Trend Pullback per-instrument tracking."""

    WAITING_FOR_TREND = "waiting_for_trend"
    WATCHING_FOR_PULLBACK = "watching_for_pullback"
    IN_PULLBACK = "in_pullback"
    SIGNAL_FIRED = "signal_fired"


@dataclass
class S1v2State:
    """Per-instrument state for S1v2 Trend Pullback strategy."""

    phase: str = S1v2Phase.WAITING_FOR_TREND
    direction: Optional[str] = None       # LONG or SHORT bias from 15min filter
    pullback_count: int = 0
    pullback_extreme: Decimal = Decimal("0")  # lowest low (LONG) or highest high (SHORT) during pullback
    adx_was_above: bool = False           # tracks whether ADX crossed above threshold


# ---------------------------------------------------------------------------
# BacktestRegimeAdapter — wraps MarketRegime for RiskGate compatibility
# ---------------------------------------------------------------------------

class BacktestRegimeAdapter:
    """Provides the same interface as live RegimeDetector for a static regime."""

    def __init__(self, regime: MarketRegime) -> None:
        self._regime = regime

    def current_regime(self) -> MarketRegime:
        return self._regime

    def is_long_allowed(self) -> bool:
        return self._regime in (MarketRegime.BULL_TREND, MarketRegime.HIGH_VOLATILITY)

    def is_short_allowed(self) -> bool:
        return self._regime in (
            MarketRegime.BEAR_TREND,
            MarketRegime.HIGH_VOLATILITY,
            MarketRegime.CRASH,
        )

    def position_size_multiplier(self) -> float:
        if self._regime in (MarketRegime.HIGH_VOLATILITY, MarketRegime.CRASH):
            return 0.5
        return 1.0


# ---------------------------------------------------------------------------
# BacktestRiskGate — replicate Gates 4-7 with candle_time instead of now()
# ---------------------------------------------------------------------------

class BacktestRiskGate:
    """
    Backtest-specific risk gate that replicates live Gates 4-7.

    Gates 0-3 (mode assertion, kill switch, recon, instrument lock) are
    not applicable to backtesting and are skipped.

    Key difference from live: Gate 5 uses candle_time parameter instead
    of datetime.now(IST).
    """

    def check(
        self,
        signal: Signal,
        shared_state: dict,
        config: dict,
        candle_time: datetime,
        regime_adapter: Optional[BacktestRegimeAdapter] = None,
    ) -> tuple[bool, str]:
        """Run Gates 4-7 with explicit candle_time for time-based checks."""

        # Gate 4: max open positions (includes pending_signals counter)
        max_positions = config.get("risk", {}).get("max_open_positions", 6)
        open_count = len(shared_state.get("open_positions", {}))
        pending_count = shared_state.get("pending_signals", 0)
        if open_count + pending_count >= max_positions:
            return False, "MAX_POSITIONS_REACHED"

        # Gate 5: time-based entry restrictions using candle_time
        no_entry_str = config.get("trading_hours", {}).get("no_entry_after", "14:45")
        h, m = map(int, no_entry_str.split(":"))
        no_entry_time = time(h, m)

        ct = candle_time.time() if hasattr(candle_time, "time") else candle_time
        if ct >= HARD_EXIT_TIME:
            return False, "HARD_EXIT_TIME_REACHED"
        if ct >= no_entry_time:
            return False, "NO_ENTRY_WINDOW"

        # Gate 6: duplicate signal — same symbol+direction already open
        open_positions = shared_state.get("open_positions", {})
        if signal.symbol in open_positions:
            pos = open_positions[signal.symbol]
            pos_dir = pos["direction"] if isinstance(pos, dict) else getattr(pos, "direction", "")
            if signal.direction == pos_dir:
                return False, "DUPLICATE_SIGNAL"

        # Gate 7: regime gating
        if regime_adapter is not None:
            if signal.direction == "LONG" and not regime_adapter.is_long_allowed():
                return False, f"REGIME_BLOCKED_{regime_adapter.current_regime().value.upper()}"
            if signal.direction == "SHORT" and not regime_adapter.is_short_allowed():
                return False, f"REGIME_BLOCKED_{regime_adapter.current_regime().value.upper()}"
            # CRASH + SHORT: extra volume confirmation
            if (regime_adapter.current_regime() == MarketRegime.CRASH
                    and signal.direction == "SHORT"
                    and signal.volume_ratio <= Decimal("2.0")):
                return False, "REGIME_CRASH_LOW_VOLUME_SHORT"

        return True, "OK"


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------

def compute_atr(candles: list[Candle], period: int = 14) -> Decimal:
    """Compute Average True Range from a list of candles.

    Uses standard ATR: mean of true ranges over `period` candles.
    True range = max(high-low, |high-prev_close|, |low-prev_close|).
    """
    if len(candles) < 2:
        return Decimal("0")

    true_ranges: list[Decimal] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    # Use the last `period` true ranges
    relevant = true_ranges[-period:] if len(true_ranges) >= period else true_ranges
    if not relevant:
        return Decimal("0")

    return sum(relevant) / Decimal(str(len(relevant)))


# ---------------------------------------------------------------------------
# S1v2 indicator functions (using ta library)
# ---------------------------------------------------------------------------

def compute_ema(candles: list[Candle], period: int) -> Optional[Decimal]:
    """Compute EMA of close prices using ta library.

    Returns None if insufficient data.
    """
    if len(candles) < period:
        return None
    import pandas as pd
    import ta.trend
    closes = pd.Series([float(c.close) for c in candles])
    ema = ta.trend.EMAIndicator(close=closes, window=period, fillna=False).ema_indicator()
    val = ema.iloc[-1]
    if pd.isna(val):
        return None
    return Decimal(str(round(val, 4)))


def compute_adx(candles: list[Candle], period: int = 14) -> Optional[Decimal]:
    """Compute ADX (Average Directional Index) using ta library.

    ADX needs ~2×period bars to produce a valid value.
    Returns None if insufficient data.
    """
    if len(candles) < period * 2:
        return None
    import pandas as pd
    import ta.trend
    highs = pd.Series([float(c.high) for c in candles])
    lows = pd.Series([float(c.low) for c in candles])
    closes = pd.Series([float(c.close) for c in candles])
    adx_ind = ta.trend.ADXIndicator(high=highs, low=lows, close=closes, window=period, fillna=False)
    val = adx_ind.adx().iloc[-1]
    if pd.isna(val):
        return None
    return Decimal(str(round(val, 4)))


def compute_volume_sma(candles: list[Candle], period: int = 20) -> Optional[Decimal]:
    """Compute simple moving average of volume.

    Returns None if insufficient data.
    """
    if len(candles) < period:
        return None
    volumes = [c.volume for c in candles[-period:]]
    avg = sum(volumes) / len(volumes)
    return Decimal(str(round(avg, 2)))


# ---------------------------------------------------------------------------
# Config helpers (reuse data_downloader pattern)
# ---------------------------------------------------------------------------

def _get_nested(d: dict, dotted_key: str) -> object:
    val: object = d
    for part in dotted_key.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _load_config() -> dict:
    settings_path = os.path.join(ROOT, "config", "settings.yaml")
    with open(settings_path) as f:
        return yaml.safe_load(f) or {}


def _load_secrets() -> dict:
    secrets_path = os.path.join(ROOT, "config", "secrets.yaml")
    try:
        with open(secrets_path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _load_dsn() -> str:
    config = _load_config()
    secrets = _load_secrets()
    return str(
        _get_nested(config, "database.dsn")
        or _get_nested(config, "db.dsn")
        or _get_nested(secrets, "database.dsn")
        or ""
    )


def _load_instruments(config: dict) -> list[dict]:
    instruments = config.get("trading", {}).get("instruments", [])
    return [{"symbol": i["symbol"], "token": i["token"]} for i in instruments]


# ---------------------------------------------------------------------------
# Formatting helpers (same as session_report.py)
# ---------------------------------------------------------------------------

def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _bold(text: str) -> str:
    return _color(text, "1")


def _dim(text: str) -> str:
    return _color(text, "2")


def _indian_format(n: float, decimals: int = 0) -> str:
    if decimals > 0:
        int_part, dec_part = f"{abs(n):.{decimals}f}".split(".")
    else:
        int_part = str(int(abs(n)))
        dec_part = ""

    if len(int_part) <= 3:
        formatted = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        groups = []
        while rest:
            groups.append(rest[-2:])
            rest = rest[:-2]
        groups.reverse()
        formatted = ",".join(groups) + "," + last3

    result = formatted
    if dec_part:
        result += "." + dec_part
    if n < 0:
        result = "-" + result
    return result


def _inr(n: float, decimals: int = 0) -> str:
    return "\u20b9" + _indian_format(n, decimals)


def _pnl_color(val: float) -> str:
    """Color a P&L value green/red based on sign."""
    text = _inr(val, 2)
    return _green(text) if val >= 0 else _red(text)


def _pct_color(val: float) -> str:
    """Color a percentage green/red."""
    text = f"{val:.2f}%"
    return _green(text) if val >= 0 else _red(text)


def _pnl_col(val: float, width: int = 14) -> str:
    """Right-align a ₹ P&L value to `width` visible chars, then apply color."""
    text = _inr(val, 2)
    padded = text.rjust(width)
    return _green(padded) if val >= 0 else _red(padded)


# ---------------------------------------------------------------------------
# S1v2SignalEvaluator — 7-step signal flow + 4-state machine
# ---------------------------------------------------------------------------

class S1v2SignalEvaluator:
    """Evaluates S1v2 Trend Pullback signals with per-instrument state machine.

    Signal flow (all must pass):
      1. Directional Filter (15min EMA10)
      2. Trend Confirmation (15min ADX > threshold)
      3. Pullback Detection + State Machine (5min EMA20)
      4. Entry Trigger (5min EMA20 reclaim)
      5. Volume Confirmation (5min volume / SMA20)
      6. Risk:Reward Gate (≥ 3:1)
      7. Execute

    State machine per instrument:
      WAITING_FOR_TREND → WATCHING_FOR_PULLBACK → IN_PULLBACK → SIGNAL_FIRED
    """

    def __init__(self, config: dict) -> None:
        s1v2_cfg = config.get("strategy", {}).get("s1v2", {})
        self._ema_trend_period = s1v2_cfg.get("ema_trend_period", 10)
        self._ema_pullback_period = s1v2_cfg.get("ema_pullback_period", 20)
        self._adx_period = s1v2_cfg.get("adx_period", 14)
        self._adx_threshold = Decimal(str(s1v2_cfg.get("adx_threshold", 25)))
        self._atr_period = s1v2_cfg.get("atr_period", 14)
        self._atr_target_mult = Decimal(str(s1v2_cfg.get("atr_target_mult", 2.5)))
        self._volume_ratio_min = Decimal(str(s1v2_cfg.get("volume_ratio_min", 1.5)))
        self._volume_sma_period = s1v2_cfg.get("volume_sma_period", 20)
        self._rr_min = Decimal(str(s1v2_cfg.get("rr_min", 3.0)))
        self._time_stop_bars = s1v2_cfg.get("time_stop_bars", 30)

        # Per-instrument state machines (persist across days — ADX trend continues)
        self._states: dict[str, S1v2State] = defaultdict(S1v2State)
        # Candle buffers for indicator computation
        self._candles_15min: dict[str, list[Candle]] = defaultdict(list)
        self._candles_5min: dict[str, list[Candle]] = defaultdict(list)
        # Session dedup: one signal per (symbol, direction) per day
        self._session_signals: set[tuple[str, str]] = set()

    def reset_session(self) -> None:
        """Reset per-session dedup set. State machines persist across days."""
        self._session_signals.clear()

    def feed_15min_candle(self, candle: Candle) -> None:
        """Feed a completed 15min candle into the buffer for a symbol."""
        buf = self._candles_15min[candle.symbol]
        buf.append(candle)
        if len(buf) > 200:
            self._candles_15min[candle.symbol] = buf[-200:]

    def feed_warmup_15min(self, symbol: str, candles: list[Candle]) -> None:
        """Bulk-load warmup 15min candles (chronological order)."""
        self._candles_15min[symbol] = list(candles)

    def feed_warmup_5min(self, symbol: str, candles: list[Candle]) -> None:
        """Bulk-load warmup 5min candles (chronological order)."""
        self._candles_5min[symbol] = list(candles)

    def _compute_15min_indicators(self, symbol: str) -> dict:
        """Compute EMA10 and ADX from 15min buffer."""
        buf = self._candles_15min.get(symbol, [])
        return {
            "ema10": compute_ema(buf, self._ema_trend_period),
            "adx": compute_adx(buf, self._adx_period),
            "close": buf[-1].close if buf else None,
        }

    def _compute_5min_indicators(self, symbol: str) -> dict:
        """Compute EMA20, ATR, volume SMA from 5min buffer."""
        buf = self._candles_5min.get(symbol, [])
        return {
            "ema20": compute_ema(buf, self._ema_pullback_period),
            "atr": compute_atr(buf, self._atr_period) if len(buf) >= 2 else Decimal("0"),
            "volume_sma": compute_volume_sma(buf, self._volume_sma_period),
        }

    def evaluate(self, candle_5min: Candle) -> Optional[Signal]:
        """Evaluate S1v2 signal flow on a 5min candle.

        Returns Signal if all 7 steps pass, None otherwise.
        """
        symbol = candle_5min.symbol

        # Add candle to 5min buffer
        buf = self._candles_5min[symbol]
        buf.append(candle_5min)
        if len(buf) > 200:
            self._candles_5min[symbol] = buf[-200:]

        # Get state
        state = self._states[symbol]

        # Compute indicators
        ind_15m = self._compute_15min_indicators(symbol)
        ind_5m = self._compute_5min_indicators(symbol)

        ema10_15m = ind_15m["ema10"]
        adx = ind_15m["adx"]
        close_15m = ind_15m["close"]
        ema20_5m = ind_5m["ema20"]
        atr = ind_5m["atr"]
        vol_sma = ind_5m["volume_sma"]

        # Insufficient indicator data — skip
        if any(v is None for v in (ema10_15m, adx, close_15m, ema20_5m)):
            return None
        if atr <= 0 or vol_sma is None or vol_sma <= 0:
            return None

        # --- STEP 1: Directional Filter (15min) ---
        if close_15m > ema10_15m:
            bias = "LONG"
        elif close_15m < ema10_15m:
            bias = "SHORT"
        else:
            return None  # Exactly at EMA — no bias

        # --- STEP 2: Trend Confirmation (15min ADX) ---
        adx_above = adx > self._adx_threshold

        # --- STATE MACHINE ---
        if not adx_above:
            # ADX below threshold — reset to WAITING
            if state.phase != S1v2Phase.WAITING_FOR_TREND:
                state.phase = S1v2Phase.WAITING_FOR_TREND
                state.adx_was_above = False
                state.pullback_count = 0
                state.direction = None
            return None

        # ADX is above threshold
        if state.phase == S1v2Phase.WAITING_FOR_TREND:
            # ADX just crossed above threshold — start watching
            state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
            state.direction = bias
            state.pullback_count = 0
            state.adx_was_above = True
            return None

        if state.phase == S1v2Phase.SIGNAL_FIRED:
            # Already fired a signal, waiting for trade to close
            return None

        # Update direction if bias changed (trend reversal while ADX stays high)
        if bias != state.direction:
            state.direction = bias
            state.pullback_count = 0
            state.phase = S1v2Phase.WATCHING_FOR_PULLBACK

        close_5m = candle_5min.close

        # --- STEP 3: Pullback Detection (5min) ---
        if state.phase == S1v2Phase.WATCHING_FOR_PULLBACK:
            # Check if price crosses wrong side of EMA20
            in_pullback = False
            if bias == "LONG" and close_5m < ema20_5m:
                in_pullback = True
            elif bias == "SHORT" and close_5m > ema20_5m:
                in_pullback = True

            if in_pullback:
                state.phase = S1v2Phase.IN_PULLBACK
                state.pullback_count += 1
                # Initialize pullback extreme
                if bias == "LONG":
                    state.pullback_extreme = candle_5min.low
                else:
                    state.pullback_extreme = candle_5min.high
            return None

        if state.phase == S1v2Phase.IN_PULLBACK:
            # Update pullback extreme
            if bias == "LONG":
                state.pullback_extreme = min(state.pullback_extreme, candle_5min.low)
            else:
                state.pullback_extreme = max(state.pullback_extreme, candle_5min.high)

            # --- STEP 4: Entry Trigger — reclaim EMA20 ---
            reclaimed = False
            if bias == "LONG" and close_5m > ema20_5m:
                reclaimed = True
            elif bias == "SHORT" and close_5m < ema20_5m:
                reclaimed = True

            if not reclaimed:
                return None  # Still in pullback

            # First pullback only
            if state.pullback_count > 1:
                state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
                return None

            # --- STEP 5: Volume Confirmation ---
            vol_ratio = Decimal(str(candle_5min.volume)) / vol_sma
            if vol_ratio < self._volume_ratio_min:
                # Volume too low — back to watching (pullback consumed)
                state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
                return None

            # --- STEP 6: Risk:Reward Gate ---
            entry_price = close_5m
            stop_loss = state.pullback_extreme
            if bias == "LONG":
                target = entry_price + self._atr_target_mult * atr
                risk = entry_price - stop_loss
                reward = target - entry_price
            else:
                target = entry_price - self._atr_target_mult * atr
                risk = stop_loss - entry_price
                reward = entry_price - target

            if risk <= 0:
                state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
                return None

            rr = reward / risk
            if rr < self._rr_min:
                state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
                return None

            # --- STEP 7: Dedup check ---
            dedup_key = (symbol, bias)
            if dedup_key in self._session_signals:
                state.phase = S1v2Phase.WATCHING_FOR_PULLBACK
                return None
            self._session_signals.add(dedup_key)

            # --- SIGNAL! ---
            state.phase = S1v2Phase.SIGNAL_FIRED

            return Signal(
                symbol=symbol,
                instrument_token=candle_5min.instrument_token,
                direction=bias,
                signal_time=candle_5min.candle_time,
                candle_time=candle_5min.candle_time,
                theoretical_entry=entry_price,
                stop_loss=stop_loss,
                target=target,
                ema9=ema10_15m,     # Repurpose: EMA10(15min)
                ema21=ema20_5m,     # Repurpose: EMA20(5min)
                rsi=adx,            # Repurpose: ADX
                vwap=candle_5min.vwap,
                volume_ratio=vol_ratio,
            )

        return None

    def on_trade_closed(self, symbol: str) -> None:
        """Called when a trade is closed — transition state back to WATCHING."""
        state = self._states.get(symbol)
        if state and state.phase == S1v2Phase.SIGNAL_FIRED:
            state.phase = S1v2Phase.WATCHING_FOR_PULLBACK


# ---------------------------------------------------------------------------
# BacktestEngine — the core simulation engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Replays historical candles through strategy pipelines (S1 or S1v2)."""

    def __init__(
        self,
        pool,
        config: dict,
        exit_mode: str = "fixed",
        atr_mult: float = 1.5,
        atr_period: int = 14,
        partial_pct: float = 0.5,
        slippage: float = 0.001,
        interval: str = "15min",
    ) -> None:
        self._pool = pool
        self._config = config
        self._exit_mode = exit_mode
        self._atr_mult = Decimal(str(atr_mult))
        self._atr_period = atr_period
        self._partial_pct = Decimal(str(partial_pct))
        self._slippage = Decimal(str(slippage))

        # Strategy dispatch
        self._strategy_name = config.get("_strategy_override", "s1")

        # Shared components
        self._position_sizer = PositionSizer()
        self._charge_calc = ChargeCalculator()
        self._risk_gate = BacktestRiskGate()

        # Capital from config
        capital_cfg = config.get("capital", {})
        total_capital = Decimal(str(capital_cfg.get("total", 1000000)))
        s1_alloc = Decimal(str(capital_cfg.get("allocation", {}).get("s1_intraday", 0.9)))
        max_positions = config.get("risk", {}).get("max_open_positions", 6)
        self._total_capital = total_capital * s1_alloc
        self._slot_capital = self._total_capital / Decimal(str(max_positions))
        self._risk_pct = Decimal(str(config.get("risk", {}).get("max_loss_per_trade_pct", 0.015)))

        # Pending partial exit trades (collected during day processing)
        self._pending_partial_trades: list[BacktestTrade] = []

        if self._strategy_name == "s1v2":
            # S1v2: multi-timeframe, uses 5min for execution
            self._interval = "5min"
            self._s1v2_evaluator = S1v2SignalEvaluator(config)
            self._s1v2_warmed_up: set[str] = set()
            # S1 components not needed for s1v2 but initialize for safety
            self._signal_gen = None  # type: ignore[assignment]
            self._indicator_engines: dict[str, IndicatorEngine] = {}
            self._candle_buffers: dict[str, list[Candle]] = defaultdict(list)
        else:
            # S1: original pipeline (unchanged)
            self._interval = interval
            s1_cfg = config.get("strategy", {}).get("s1", {})
            self._signal_gen = SignalGenerator(s1_config=s1_cfg)

            # IndicatorEngine config
            self._ema_fast = s1_cfg.get("ema_fast", 9)
            self._ema_slow = s1_cfg.get("ema_slow", 21)
            self._rsi_period = s1_cfg.get("rsi_period", 14)
            self._swing_lookback = s1_cfg.get("swing_lookback", 5)

            # Persistent indicator engines across days (EMA continuity)
            self._indicator_engines = {}
            # Recent candle buffer per symbol for ATR computation
            self._candle_buffers = defaultdict(list)

    async def run(self, date_from: date, date_to: date) -> BacktestResult:
        """Run the full backtest simulation."""
        trading_days = await self._load_trading_days(date_from, date_to)
        if not trading_days:
            step_fail("No trading days found in the given date range")
            return BacktestResult(
                trades=[], daily_results=[], params=self._build_params(),
                date_from=date_from, date_to=date_to,
            )

        instruments = _load_instruments(self._config)
        symbols = [i["symbol"] for i in instruments]

        all_trades: list[BacktestTrade] = []
        daily_results: list[DailyResult] = []

        step_info(
            f"Backtesting {len(symbols)} stocks, {len(trading_days)} days "
            f"({trading_days[0]} \u2192 {trading_days[-1]}), "
            f"strategy={self._strategy_name}, exit={self._exit_mode}"
        )

        for day_idx, day in enumerate(trading_days):
            # Compute regime for the day
            regime = await self._compute_regime(day)
            regime_adapter = BacktestRegimeAdapter(regime)

            # Process the day — dispatch by strategy
            if self._strategy_name == "s1v2":
                day_trades = await self._process_day_s1v2(
                    day, regime_adapter, symbols,
                )
            else:
                day_trades = await self._process_day(
                    day, regime_adapter, symbols,
                )

            day_gross = sum(t.gross_pnl for t in day_trades)
            day_net = sum(t.net_pnl for t in day_trades)

            daily_results.append(DailyResult(
                session_date=day,
                trades_closed=len(day_trades),
                gross_pnl=day_gross,
                net_pnl=day_net,
                regime=regime.value,
            ))
            all_trades.extend(day_trades)

            # Progress every 10 days
            if (day_idx + 1) % 10 == 0 or day_idx == len(trading_days) - 1:
                pct = (day_idx + 1) / len(trading_days) * 100
                step_info(
                    f"Day {day_idx + 1}/{len(trading_days)} ({pct:.0f}%) — "
                    f"{len(all_trades)} trades so far"
                )

        result = self._compute_metrics(all_trades, daily_results, date_from, date_to)
        return result

    async def _load_trading_days(
        self, date_from: date, date_to: date
    ) -> list[date]:
        """Get distinct session dates from backtest_candles table."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT session_date
                FROM backtest_candles
                WHERE session_date >= $1 AND session_date <= $2
                  AND interval = $3
                ORDER BY session_date
                """,
                date_from, date_to, self._interval,
            )
        return [r["session_date"] for r in rows]

    @staticmethod
    def _compute_vwap_for_day(candles: list[Candle]) -> list[Candle]:
        """Compute running intraday VWAP for a single stock's day candles.

        VWAP = cumulative(typical_price × volume) / cumulative(volume)
        typical_price = (high + low + close) / 3

        Resets each trading day (called per-stock per-day).
        """
        cum_tp_vol = Decimal("0")
        cum_vol = Decimal("0")
        result: list[Candle] = []
        for c in candles:
            tp = (c.high + c.low + c.close) / Decimal("3")
            vol = Decimal(str(c.volume))
            cum_tp_vol += tp * vol
            cum_vol += vol
            vwap = cum_tp_vol / cum_vol if cum_vol > 0 else c.close
            result.append(dataclasses.replace(c, vwap=vwap))
        return result

    async def _load_day_candles(
        self, day: date, symbols: list[str]
    ) -> dict[str, list[Candle]]:
        """Load one day's candles for all symbols, grouped by symbol."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE session_date = $1 AND interval = $2 AND symbol = ANY($3)
                ORDER BY candle_time
                """,
                day, self._interval, symbols,
            )

        result: dict[str, list[Candle]] = defaultdict(list)
        for r in rows:
            candle = Candle(
                instrument_token=r["instrument_token"],
                symbol=r["symbol"],
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
                vwap=Decimal(str(r["close"])),  # Placeholder — overwritten by _compute_vwap_for_day()
                candle_time=r["candle_time"] if r["candle_time"].tzinfo else IST.localize(r["candle_time"]),
                session_date=r["session_date"],
                tick_count=0,
            )
            result[r["symbol"]].append(candle)
        return dict(result)

    async def _load_warmup_candles(
        self, day: date, symbol: str, count: int = 50
    ) -> list[Candle]:
        """Load prior candles for IndicatorEngine warmup."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE symbol = $1 AND interval = $2 AND session_date < $3
                ORDER BY candle_time DESC
                LIMIT $4
                """,
                symbol, self._interval, day, count,
            )

        candles = []
        for r in reversed(rows):  # Reverse to chronological order
            candles.append(Candle(
                instrument_token=r["instrument_token"],
                symbol=r["symbol"],
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
                vwap=Decimal(str(r["close"])),  # Warmup only — EMA/RSI init, not signal gen
                candle_time=r["candle_time"] if r["candle_time"].tzinfo else IST.localize(r["candle_time"]),
                session_date=r["session_date"],
                tick_count=0,
            ))
        return candles

    # ------------------------------------------------------------------
    # S1v2: multi-timeframe data loaders
    # ------------------------------------------------------------------

    async def _load_day_candles_multi(
        self, day: date, symbols: list[str]
    ) -> tuple[dict[str, list[Candle]], dict[str, list[Candle]]]:
        """Load one day's 5min and 15min candles, grouped by symbol.

        Returns (candles_5min_by_symbol, candles_15min_by_symbol).
        """
        async with self._pool.acquire() as conn:
            rows_5 = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE session_date = $1 AND interval = '5min' AND symbol = ANY($2)
                ORDER BY candle_time
                """,
                day, symbols,
            )
            rows_15 = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE session_date = $1 AND interval = '15min' AND symbol = ANY($2)
                ORDER BY candle_time
                """,
                day, symbols,
            )

        def _rows_to_candles(rows) -> dict[str, list[Candle]]:
            result: dict[str, list[Candle]] = defaultdict(list)
            for r in rows:
                candle = Candle(
                    instrument_token=r["instrument_token"],
                    symbol=r["symbol"],
                    open=Decimal(str(r["open"])),
                    high=Decimal(str(r["high"])),
                    low=Decimal(str(r["low"])),
                    close=Decimal(str(r["close"])),
                    volume=int(r["volume"]),
                    vwap=Decimal(str(r["close"])),
                    candle_time=r["candle_time"] if r["candle_time"].tzinfo else IST.localize(r["candle_time"]),
                    session_date=r["session_date"],
                    tick_count=0,
                )
                result[r["symbol"]].append(candle)
            return dict(result)

        return _rows_to_candles(rows_5), _rows_to_candles(rows_15)

    async def _load_warmup_candles_multi(
        self, day: date, symbol: str, days_back: int = 5
    ) -> tuple[list[Candle], list[Candle]]:
        """Load prior days of 5min and 15min candles for S1v2 warmup.

        Returns (warmup_5min, warmup_15min) in chronological order.
        """
        async with self._pool.acquire() as conn:
            rows_5 = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE symbol = $1 AND interval = '5min' AND session_date < $2
                ORDER BY candle_time DESC
                LIMIT $3
                """,
                symbol, day, days_back * 75,  # ~75 5min bars per day
            )
            rows_15 = await conn.fetch(
                """
                SELECT instrument_token, symbol, open, high, low, close,
                       volume, candle_time, session_date
                FROM backtest_candles
                WHERE symbol = $1 AND interval = '15min' AND session_date < $2
                ORDER BY candle_time DESC
                LIMIT $3
                """,
                symbol, day, days_back * 25,  # ~25 15min bars per day
            )

        def _rows_to_list(rows) -> list[Candle]:
            candles = []
            for r in reversed(rows):
                candles.append(Candle(
                    instrument_token=r["instrument_token"],
                    symbol=r["symbol"],
                    open=Decimal(str(r["open"])),
                    high=Decimal(str(r["high"])),
                    low=Decimal(str(r["low"])),
                    close=Decimal(str(r["close"])),
                    volume=int(r["volume"]),
                    vwap=Decimal(str(r["close"])),
                    candle_time=r["candle_time"] if r["candle_time"].tzinfo else IST.localize(r["candle_time"]),
                    session_date=r["session_date"],
                    tick_count=0,
                ))
            return candles

        return _rows_to_list(rows_5), _rows_to_list(rows_15)

    async def _compute_regime(self, day: date) -> MarketRegime:
        """Compute market regime for a given day from historical data."""
        import ta.trend
        import pandas as pd

        async with self._pool.acquire() as conn:
            # Load 200+ prior NIFTY daily candles for EMA200
            nifty_daily = await conn.fetch(
                """
                SELECT close FROM backtest_candles
                WHERE instrument_token = 256265 AND interval = 'day'
                  AND session_date <= $1
                ORDER BY session_date DESC
                LIMIT 250
                """,
                day,
            )

            # Load INDIA VIX daily close
            vix_row = await conn.fetchrow(
                """
                SELECT close FROM backtest_candles
                WHERE instrument_token = 264969 AND interval = 'day'
                  AND session_date = $1
                """,
                day,
            )

            # Load NIFTY intraday candles for the day
            nifty_intraday = await conn.fetch(
                """
                SELECT open, high, low, close FROM backtest_candles
                WHERE instrument_token = 256265 AND interval = $1
                  AND session_date = $2
                ORDER BY candle_time
                """,
                self._interval, day,
            )

        # NIFTY EMA200
        nifty_ema200 = 0.0
        nifty_price = 0.0
        if nifty_daily and len(nifty_daily) >= 50:
            closes = pd.Series(
                [float(r["close"]) for r in reversed(nifty_daily)]
            )
            ema_period = min(200, len(closes))
            ema_series = ta.trend.EMAIndicator(
                close=closes, window=ema_period, fillna=False
            ).ema_indicator()
            ema_val = ema_series.iloc[-1]
            if not pd.isna(ema_val):
                nifty_ema200 = float(ema_val)
            nifty_price = float(closes.iloc[-1])
        elif nifty_daily:
            nifty_price = float(nifty_daily[0]["close"])
            nifty_ema200 = nifty_price  # Fallback: EMA = price → BULL_TREND

        # VIX
        vix = 15.0  # Neutral default
        if vix_row:
            vix_val = float(vix_row["close"])
            if 0 < vix_val < 100:
                vix = vix_val

        # Intraday metrics
        intraday_drop_pct = 0.0
        intraday_range_pct = 0.0
        if nifty_intraday:
            day_open = float(nifty_intraday[0]["open"])
            day_high = max(float(r["high"]) for r in nifty_intraday)
            day_low = min(float(r["low"]) for r in nifty_intraday)
            last_close = float(nifty_intraday[-1]["close"])
            if day_open > 0:
                if nifty_price == 0.0:
                    nifty_price = last_close
                drop = (day_open - last_close) / day_open * 100
                intraday_drop_pct = max(0.0, drop)
                intraday_range_pct = (day_high - day_low) / day_open * 100

        return classify_regime(
            nifty_price=nifty_price or nifty_ema200,
            nifty_ema200=nifty_ema200,
            vix=vix,
            intraday_drop_pct=intraday_drop_pct,
            intraday_range_pct=intraday_range_pct,
        )

    async def _process_day(
        self,
        day: date,
        regime_adapter: BacktestRegimeAdapter,
        symbols: list[str],
    ) -> list[BacktestTrade]:
        """Simulate one trading day."""
        # Reset signal generator for new session
        self._signal_gen.reset_session()

        # Load day candles
        candles_by_symbol = await self._load_day_candles(day, symbols)
        if not candles_by_symbol:
            return []

        # Compute running VWAP per stock (resets each day — intraday indicator)
        for symbol in candles_by_symbol:
            candles_by_symbol[symbol] = self._compute_vwap_for_day(
                candles_by_symbol[symbol]
            )

        # Ensure indicator engines exist (persist across days for EMA continuity)
        for symbol in candles_by_symbol:
            if symbol not in self._indicator_engines:
                warmup = await self._load_warmup_candles(day, symbol)
                self._indicator_engines[symbol] = IndicatorEngine(
                    warmup_candles=warmup,
                    ema_fast=self._ema_fast,
                    ema_slow=self._ema_slow,
                    rsi_period=self._rsi_period,
                    swing_lookback=self._swing_lookback,
                )

        # Merge all candles chronologically
        all_candles: list[Candle] = []
        for candle_list in candles_by_symbol.values():
            all_candles.extend(candle_list)
        all_candles.sort(key=lambda c: c.candle_time)

        # Simulated shared state for this day
        open_positions: dict[str, BacktestPosition] = {}
        shared_state: dict = {
            "open_positions": {},  # For RiskGate compatibility
            "pending_signals": 0,
            "kill_switch_level": 0,
        }

        config = self._config
        day_trades: list[BacktestTrade] = []
        self._pending_partial_trades = []  # Reset per day

        for candle in all_candles:
            symbol = candle.symbol

            # Store candle in buffer for ATR computation
            self._candle_buffers[symbol].append(candle)
            # Keep last 50 candles per symbol
            if len(self._candle_buffers[symbol]) > 50:
                self._candle_buffers[symbol] = self._candle_buffers[symbol][-50:]

            # --- Check exits for open positions on this candle ---
            if symbol in open_positions:
                pos = open_positions[symbol]
                trade = self._check_exits(pos, candle, regime_adapter)
                if trade is not None:
                    day_trades.append(trade)
                    del open_positions[symbol]
                    # Update shared state
                    shared_state["open_positions"] = {
                        s: {"direction": p.direction}
                        for s, p in open_positions.items()
                    }

            # --- Check for hard exit at 15:00 ---
            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time
            if ct >= HARD_EXIT_TIME and symbol in open_positions:
                pos = open_positions[symbol]
                trade = self._close_position(pos, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                del open_positions[symbol]
                shared_state["open_positions"] = {
                    s: {"direction": p.direction}
                    for s, p in open_positions.items()
                }
                continue

            # --- Signal generation ---
            if ct >= HARD_EXIT_TIME:
                continue  # No new signals after 15:00

            engine = self._indicator_engines.get(symbol)
            if engine is None:
                continue

            indicators = engine.update(candle)
            if indicators is None:
                continue

            signal = self._signal_gen.evaluate(candle, indicators)
            if signal is None:
                continue

            # Risk gate check
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True
            )
            qty = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                slot_capital=self._slot_capital,
                risk_pct=self._risk_pct,
            )
            if qty is None or qty == 0:
                continue

            # Open position
            pos = BacktestPosition(
                symbol=signal.symbol,
                instrument_token=signal.instrument_token,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            open_positions[signal.symbol] = pos
            shared_state["open_positions"] = {
                s: {"direction": p.direction}
                for s, p in open_positions.items()
            }

        # End of day: hard exit remaining positions at last candle close
        for symbol, pos in list(open_positions.items()):
            # Find last candle for this symbol
            last_candle = None
            for c in reversed(all_candles):
                if c.symbol == symbol:
                    last_candle = c
                    break
            if last_candle:
                trade = self._close_position(
                    pos, last_candle.close, last_candle.candle_time, "HARD_EXIT"
                )
                day_trades.append(trade)

        # Collect any partial exit trades generated during the day
        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []

        return day_trades

    # ------------------------------------------------------------------
    # S1v2: multi-timeframe simulation loop
    # ------------------------------------------------------------------

    async def _process_day_s1v2(
        self,
        day: date,
        regime_adapter: BacktestRegimeAdapter,
        symbols: list[str],
    ) -> list[BacktestTrade]:
        """Simulate one trading day using S1v2 Trend Pullback strategy.

        Multi-timeframe: 15min candles for trend/ADX, 5min candles for entries.
        Anti-leakage: only feeds 15min candles completed at or before each 5min time.
        """
        evaluator = self._s1v2_evaluator
        evaluator.reset_session()

        # Load both timeframes
        candles_5m_by_sym, candles_15m_by_sym = await self._load_day_candles_multi(day, symbols)
        if not candles_5m_by_sym:
            return []

        # Compute VWAP on 5min candles (for signal target display)
        for symbol in candles_5m_by_sym:
            candles_5m_by_sym[symbol] = self._compute_vwap_for_day(candles_5m_by_sym[symbol])

        # Warmup: load prior days' candles for indicator computation
        for symbol in candles_5m_by_sym:
            if symbol not in self._s1v2_warmed_up:
                warmup_5, warmup_15 = await self._load_warmup_candles_multi(day, symbol)
                evaluator.feed_warmup_5min(symbol, warmup_5)
                evaluator.feed_warmup_15min(symbol, warmup_15)
                self._s1v2_warmed_up.add(symbol)

        # Merge all 5min candles chronologically
        all_5min: list[Candle] = []
        for candle_list in candles_5m_by_sym.values():
            all_5min.extend(candle_list)
        all_5min.sort(key=lambda c: c.candle_time)

        # Per-symbol cursor for 15min candle feeding (anti-leakage)
        fed_15m_idx: dict[str, int] = defaultdict(int)

        # Simulated state
        open_positions: dict[str, BacktestPosition] = {}
        bar_counts: dict[str, int] = defaultdict(int)  # bars since entry for time stop
        shared_state: dict = {
            "open_positions": {},
            "pending_signals": 0,
            "kill_switch_level": 0,
        }

        config = self._config
        day_trades: list[BacktestTrade] = []

        for candle in all_5min:
            symbol = candle.symbol

            # --- Feed completed 15min candles (anti-leakage gate) ---
            sym_15m = candles_15m_by_sym.get(symbol, [])
            while fed_15m_idx[symbol] < len(sym_15m):
                c15 = sym_15m[fed_15m_idx[symbol]]
                if c15.candle_time <= candle.candle_time:
                    evaluator.feed_15min_candle(c15)
                    fed_15m_idx[symbol] += 1
                else:
                    break

            # --- Check exits for open positions ---
            if symbol in open_positions:
                pos = open_positions[symbol]
                bar_counts[symbol] += 1

                # Time stop: 30 bars of 5min = 150 minutes
                if bar_counts[symbol] >= self._s1v2_evaluator._time_stop_bars:
                    trade = self._close_position(pos, candle.close, candle.candle_time, "TIME_STOP")
                    day_trades.append(trade)
                    del open_positions[symbol]
                    del bar_counts[symbol]
                    evaluator.on_trade_closed(symbol)
                    shared_state["open_positions"] = {
                        s: {"direction": p.direction}
                        for s, p in open_positions.items()
                    }
                    continue

                # Fixed exit (stop/target)
                trade = self._check_fixed_exit(pos, candle)
                if trade is not None:
                    day_trades.append(trade)
                    del open_positions[symbol]
                    if symbol in bar_counts:
                        del bar_counts[symbol]
                    evaluator.on_trade_closed(symbol)
                    shared_state["open_positions"] = {
                        s: {"direction": p.direction}
                        for s, p in open_positions.items()
                    }

            # --- Hard exit at 15:00 ---
            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time
            if ct >= HARD_EXIT_TIME and symbol in open_positions:
                pos = open_positions[symbol]
                trade = self._close_position(pos, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                del open_positions[symbol]
                if symbol in bar_counts:
                    del bar_counts[symbol]
                evaluator.on_trade_closed(symbol)
                shared_state["open_positions"] = {
                    s: {"direction": p.direction}
                    for s, p in open_positions.items()
                }
                continue

            # --- Signal generation (S1v2) ---
            if ct >= HARD_EXIT_TIME:
                continue

            signal = evaluator.evaluate(candle)
            if signal is None:
                continue

            # Risk gate check
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True
            )
            qty = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                slot_capital=self._slot_capital,
                risk_pct=self._risk_pct,
            )
            if qty is None or qty == 0:
                continue

            # Open position
            pos = BacktestPosition(
                symbol=signal.symbol,
                instrument_token=signal.instrument_token,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            open_positions[signal.symbol] = pos
            bar_counts[signal.symbol] = 0
            shared_state["open_positions"] = {
                s: {"direction": p.direction}
                for s, p in open_positions.items()
            }

        # End of day: force-close remaining positions
        for symbol, pos in list(open_positions.items()):
            last_candle = None
            for c in reversed(all_5min):
                if c.symbol == symbol:
                    last_candle = c
                    break
            if last_candle:
                trade = self._close_position(
                    pos, last_candle.close, last_candle.candle_time, "HARD_EXIT"
                )
                day_trades.append(trade)
                evaluator.on_trade_closed(symbol)

        return day_trades

    def _check_exits(
        self,
        pos: BacktestPosition,
        candle: Candle,
        regime_adapter: BacktestRegimeAdapter,
    ) -> Optional[BacktestTrade]:
        """Check if a position should be exited on this candle.

        Returns a BacktestTrade if exited, None if still open.
        """
        if self._exit_mode == "fixed":
            return self._check_fixed_exit(pos, candle)
        elif self._exit_mode == "trailing":
            return self._check_trailing_exit(pos, candle, regime_adapter)
        elif self._exit_mode == "partial":
            return self._check_partial_exit(pos, candle, regime_adapter)
        return self._check_fixed_exit(pos, candle)

    def _check_fixed_exit(
        self, pos: BacktestPosition, candle: Candle
    ) -> Optional[BacktestTrade]:
        """Fixed exit: stop or target, pessimistic on same-candle conflict."""
        stop_hit = False
        target_hit = False

        if pos.direction == "LONG":
            stop_hit = candle.low <= pos.stop_loss
            target_hit = candle.high >= pos.target
        else:  # SHORT
            stop_hit = candle.high >= pos.stop_loss
            target_hit = candle.low <= pos.target

        if stop_hit and target_hit:
            # Pessimistic: assume stop hit first
            return self._close_position(pos, pos.stop_loss, candle.candle_time, "STOP_HIT")
        if stop_hit:
            return self._close_position(pos, pos.stop_loss, candle.candle_time, "STOP_HIT")
        if target_hit:
            return self._close_position(pos, pos.target, candle.candle_time, "TARGET_HIT")

        return None

    def _check_trailing_exit(
        self,
        pos: BacktestPosition,
        candle: Candle,
        regime_adapter: BacktestRegimeAdapter,
    ) -> Optional[BacktestTrade]:
        """Trailing stop exit with ATR-based trail and regime gating."""
        # First check stop/target like fixed mode
        fixed_result = self._check_fixed_exit(pos, candle)
        if fixed_result is not None:
            return fixed_result

        # Update trailing stop based on ATR
        candle_buffer = self._candle_buffers.get(candle.symbol, [])
        if len(candle_buffer) >= 2:
            atr = compute_atr(candle_buffer, self._atr_period)
            if atr > Decimal("0"):
                # Regime gating: trail only in favorable regimes
                trail_active = False
                regime = regime_adapter.current_regime()
                if pos.direction == "LONG" and regime == MarketRegime.BULL_TREND:
                    trail_active = True
                elif pos.direction == "SHORT" and regime == MarketRegime.BEAR_TREND:
                    trail_active = True

                if trail_active:
                    trail_distance = atr * self._atr_mult
                    # Floor: minimum 0.5% of current price
                    min_distance = candle.close * MIN_TRAIL_PCT
                    trail_distance = max(trail_distance, min_distance)

                    if pos.direction == "LONG":
                        new_stop = candle.close - trail_distance
                        if new_stop > pos.stop_loss:
                            pos.stop_loss = new_stop
                    else:  # SHORT
                        new_stop = candle.close + trail_distance
                        if new_stop < pos.stop_loss:
                            pos.stop_loss = new_stop

        return None

    def _check_partial_exit(
        self,
        pos: BacktestPosition,
        candle: Candle,
        regime_adapter: BacktestRegimeAdapter,
    ) -> Optional[BacktestTrade]:
        """Partial exit: 50% at 1R profit, trail remainder."""
        # Check for 1R profit and partial exit
        if not pos.partial_exited:
            risk_distance = abs(pos.entry_price - pos.original_stop)
            if pos.direction == "LONG":
                at_1r = pos.entry_price + risk_distance
                if candle.high >= at_1r:
                    # Exit partial_pct of the position
                    partial_qty = int(
                        (Decimal(str(pos.qty)) * self._partial_pct)
                        .to_integral_value(rounding=ROUND_DOWN)
                    )
                    if partial_qty > 0:
                        # Record the partial exit as a trade
                        partial_trade = self._close_position_partial(
                            pos, at_1r, candle.candle_time, "PARTIAL_1R", partial_qty
                        )
                        pos.qty -= partial_qty
                        pos.partial_exited = True
                        pos.partial_qty = partial_qty
                        # If all qty exited, return the trade
                        if pos.qty <= 0:
                            return partial_trade
                        # Store partial trade (will be collected later)
                        # Continue to trail the remainder
                        self._pending_partial_trades.append(partial_trade)
            else:  # SHORT
                at_1r = pos.entry_price - risk_distance
                if candle.low <= at_1r:
                    partial_qty = int(
                        (Decimal(str(pos.qty)) * self._partial_pct)
                        .to_integral_value(rounding=ROUND_DOWN)
                    )
                    if partial_qty > 0:
                        partial_trade = self._close_position_partial(
                            pos, at_1r, candle.candle_time, "PARTIAL_1R", partial_qty
                        )
                        pos.qty -= partial_qty
                        pos.partial_exited = True
                        pos.partial_qty = partial_qty
                        if pos.qty <= 0:
                            return partial_trade
                        self._pending_partial_trades.append(partial_trade)

        # Trail the remainder using trailing logic
        return self._check_trailing_exit(pos, candle, regime_adapter)

    def _close_position(
        self,
        pos: BacktestPosition,
        exit_price: Decimal,
        exit_time: datetime,
        reason: str,
    ) -> BacktestTrade:
        """Close a position and compute P&L with charges."""
        exit_price = self._apply_slippage(exit_price, pos.direction, is_entry=False)

        if pos.direction == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * Decimal(str(pos.qty))
        else:
            gross_pnl = (pos.entry_price - exit_price) * Decimal(str(pos.qty))

        charges_breakdown = self._charge_calc.calculate(
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
        )
        net_pnl = gross_pnl - charges_breakdown.total

        return BacktestTrade(
            symbol=pos.symbol,
            instrument_token=pos.instrument_token,
            direction=pos.direction,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            qty=pos.qty,
            gross_pnl=gross_pnl,
            charges=charges_breakdown.total,
            net_pnl=net_pnl,
            regime=pos.regime,
        )

    def _close_position_partial(
        self,
        pos: BacktestPosition,
        exit_price: Decimal,
        exit_time: datetime,
        reason: str,
        qty: int,
    ) -> BacktestTrade:
        """Close a partial quantity of a position."""
        exit_price = self._apply_slippage(exit_price, pos.direction, is_entry=False)

        if pos.direction == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * Decimal(str(qty))
        else:
            gross_pnl = (pos.entry_price - exit_price) * Decimal(str(qty))

        charges_breakdown = self._charge_calc.calculate(
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
        )
        net_pnl = gross_pnl - charges_breakdown.total

        return BacktestTrade(
            symbol=pos.symbol,
            instrument_token=pos.instrument_token,
            direction=pos.direction,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            qty=qty,
            gross_pnl=gross_pnl,
            charges=charges_breakdown.total,
            net_pnl=net_pnl,
            regime=pos.regime,
        )

    def _apply_slippage(
        self, price: Decimal, direction: str, is_entry: bool
    ) -> Decimal:
        """Apply slippage to price.

        Entry: adverse direction (LONG higher, SHORT lower).
        Exit: adverse direction (LONG lower, SHORT higher).
        """
        if is_entry:
            if direction == "LONG":
                return price * (Decimal("1") + self._slippage)
            else:
                return price * (Decimal("1") - self._slippage)
        else:
            if direction == "LONG":
                return price * (Decimal("1") - self._slippage)
            else:
                return price * (Decimal("1") + self._slippage)

    def _compute_metrics(
        self,
        trades: list[BacktestTrade],
        daily_results: list[DailyResult],
        date_from: date,
        date_to: date,
    ) -> BacktestResult:
        """Compute all summary metrics from trade list."""
        params = self._build_params()

        if not trades:
            return BacktestResult(
                trades=trades, daily_results=daily_results, params=params,
                date_from=date_from, date_to=date_to,
            )

        total = len(trades)
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total * 100 if total > 0 else 0.0

        avg_win = (
            sum(t.net_pnl for t in wins) / Decimal(str(win_count))
            if win_count > 0 else Decimal("0")
        )
        avg_loss = (
            sum(t.net_pnl for t in losses) / Decimal(str(loss_count))
            if loss_count > 0 else Decimal("0")
        )

        # Expectancy
        if total > 0:
            win_pct = Decimal(str(win_count)) / Decimal(str(total))
            loss_pct = Decimal(str(loss_count)) / Decimal(str(total))
            expectancy = win_pct * avg_win + loss_pct * avg_loss
        else:
            expectancy = Decimal("0")

        gross_pnl = sum(t.gross_pnl for t in trades)
        total_charges = sum(t.charges for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)

        # Max drawdown
        cumulative = Decimal("0")
        peak = Decimal("0")
        max_dd = Decimal("0")
        for dr in daily_results:
            cumulative += dr.net_pnl
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        max_dd_pct = float(max_dd / self._total_capital * 100) if self._total_capital > 0 else 0.0

        # Sharpe ratio
        daily_returns = []
        for dr in daily_results:
            if self._total_capital > 0:
                daily_returns.append(float(dr.net_pnl / self._total_capital))

        sharpe = 0.0
        if len(daily_returns) > 1:
            mean_ret = sum(daily_returns) / len(daily_returns)
            std_ret = (sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)) ** 0.5
            if std_ret > 0:
                sharpe = (mean_ret / std_ret) * math.sqrt(252)

        # Profit factor
        gross_wins = sum(float(t.gross_pnl) for t in wins) if wins else 0.0
        gross_losses = abs(sum(float(t.gross_pnl) for t in losses)) if losses else 0.0
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0.0

        # Max consecutive wins/losses
        max_con_w, max_con_l = 0, 0
        cur_w, cur_l = 0, 0
        for t in trades:
            if t.net_pnl > 0:
                cur_w += 1
                cur_l = 0
            else:
                cur_l += 1
                cur_w = 0
            max_con_w = max(max_con_w, cur_w)
            max_con_l = max(max_con_l, cur_l)

        return BacktestResult(
            trades=trades,
            daily_results=daily_results,
            params=params,
            total_trades=total,
            wins=win_count,
            losses=loss_count,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            expectancy=expectancy,
            gross_pnl=gross_pnl,
            total_charges=total_charges,
            net_pnl=net_pnl,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
            max_consecutive_wins=max_con_w,
            max_consecutive_losses=max_con_l,
            date_from=date_from,
            date_to=date_to,
        )

    def _build_params(self) -> dict:
        """Build params dict for storage."""
        return {
            "strategy": self._strategy_name,
            "exit_mode": self._exit_mode,
            "interval": self._interval,
            "atr_mult": float(self._atr_mult),
            "atr_period": self._atr_period,
            "partial_pct": float(self._partial_pct),
            "slippage": float(self._slippage),
            "capital": float(self._total_capital),
        }


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

    # Params passed as BacktestEngine constructor kwargs
_ENGINE_KWARG_PARAMS: dict[str, str] = {
    "atr_multiplier": "atr_mult",
    "atr_mult": "atr_mult",
    "atr_period": "atr_period",
    "partial_pct": "partial_pct",
    "slippage": "slippage",
}

# Params injected into config dict (dot-separated path)
_CONFIG_PATH_PARAMS: dict[str, list[str]] = {
    # S1 params
    "volume_ratio_min": ["strategy", "s1", "volume_ratio_min"],
    "rsi_long_min": ["strategy", "s1", "rsi_long_min"],
    "rsi_long_max": ["strategy", "s1", "rsi_long_max"],
    "rsi_short_min": ["strategy", "s1", "rsi_short_min"],
    "min_stop_pct": ["strategy", "s1", "min_stop_pct"],
    "rr_ratio": ["strategy", "s1", "rr_ratio"],
    "ema_fast": ["strategy", "s1", "ema_fast"],
    "ema_slow": ["strategy", "s1", "ema_slow"],
    "rsi_period": ["strategy", "s1", "rsi_period"],
    "swing_lookback": ["strategy", "s1", "swing_lookback"],
    # S1v2 params
    "adx_threshold": ["strategy", "s1v2", "adx_threshold"],
    "atr_target_mult": ["strategy", "s1v2", "atr_target_mult"],
    "s1v2_volume_ratio_min": ["strategy", "s1v2", "volume_ratio_min"],
    "rr_min": ["strategy", "s1v2", "rr_min"],
    "time_stop_bars": ["strategy", "s1v2", "time_stop_bars"],
    "ema_trend_period": ["strategy", "s1v2", "ema_trend_period"],
    "ema_pullback_period": ["strategy", "s1v2", "ema_pullback_period"],
    # Shared params
    "no_entry_after": ["trading_hours", "no_entry_after"],
    "max_open_positions": ["risk", "max_open_positions"],
    "max_loss_per_trade_pct": ["risk", "max_loss_per_trade_pct"],
}


def _set_config_value(cfg: dict, path: list[str], value) -> None:
    """Set a nested config value by path (e.g. ["strategy", "s1", "volume_ratio_min"])."""
    for key in path[:-1]:
        cfg = cfg.setdefault(key, {})
    cfg[path[-1]] = value


async def run_optimize(
    pool,
    config: dict,
    param_name: str,
    range_str: str,
    date_from: date,
    date_to: date,
    **engine_kwargs,
) -> list[tuple[float, BacktestResult]]:
    """Sweep a parameter and collect results."""
    start, step, end = map(float, range_str.split(":"))
    values = []
    v = start
    while v <= end + 1e-9:
        values.append(round(v, 4))
        v += step

    results: list[tuple[float, BacktestResult]] = []
    step_info(f"Optimizer: sweeping {param_name} over {len(values)} values")

    for i, val in enumerate(values):
        kwargs = dict(engine_kwargs)
        iter_config = config

        if param_name in _ENGINE_KWARG_PARAMS:
            # Constructor kwarg (atr_mult, atr_period, etc.)
            kwarg_name = _ENGINE_KWARG_PARAMS[param_name]
            kwargs[kwarg_name] = int(val) if kwarg_name == "atr_period" else val
        elif param_name in _CONFIG_PATH_PARAMS:
            # Config override — deep-copy and inject
            iter_config = copy.deepcopy(config)
            path = _CONFIG_PATH_PARAMS[param_name]
            cast_val: object = val
            if param_name in ("ema_fast", "ema_slow", "rsi_period",
                              "swing_lookback", "max_open_positions"):
                cast_val = int(val)
            elif param_name == "no_entry_after":
                # Convert float like 14.75 → "14:45"
                hours = int(val)
                minutes = int((val - hours) * 60)
                cast_val = f"{hours:02d}:{minutes:02d}"
            _set_config_value(iter_config, path, cast_val)
        else:
            raise ValueError(
                f"Unknown optimizer param: {param_name}. "
                f"Valid: {sorted(set(_ENGINE_KWARG_PARAMS) | set(_CONFIG_PATH_PARAMS))}"
            )

        with spinner(f"[{i + 1}/{len(values)}] {param_name}={val}"):
            engine = BacktestEngine(pool, iter_config, **kwargs)
            result = await engine.run(date_from, date_to)
        results.append((val, result))
        step_done(f"{param_name}={val} \u2192 {result.total_trades} trades, net={_inr(float(result.net_pnl), 2)}")

    # Sort by net P&L descending
    results.sort(key=lambda x: x[1].net_pnl, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

async def run_compare(
    pool,
    config: dict,
    exit_modes: list[str],
    date_from: date,
    date_to: date,
    **engine_kwargs,
) -> dict[str, BacktestResult]:
    """Run backtest for each exit mode and return results."""
    results: dict[str, BacktestResult] = {}

    for mode in exit_modes:
        kwargs = dict(engine_kwargs)
        kwargs["exit_mode"] = mode
        with spinner(f"Running {mode} mode"):
            engine = BacktestEngine(pool, config, **kwargs)
            result = await engine.run(date_from, date_to)
        results[mode] = result
        step_done(f"{mode} \u2192 {result.total_trades} trades, net={_inr(float(result.net_pnl), 2)}")

    return results


# ---------------------------------------------------------------------------
# DB Storage
# ---------------------------------------------------------------------------

async def _save_run(pool, result: BacktestResult) -> int:
    """Save backtest run to DB, return run_id."""
    import json

    async with pool.acquire() as conn:
        run_id = await conn.fetchval(
            """
            INSERT INTO backtest_runs (
                strategy, params, exit_mode, date_from, date_to,
                total_trades, win_rate, gross_pnl, total_charges, net_pnl,
                max_drawdown, max_drawdown_pct, sharpe_ratio, profit_factor,
                avg_win, avg_loss, expectancy
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12, $13, $14,
                $15, $16, $17
            )
            RETURNING id
            """,
            result.params.get("strategy", "s1"),
            json.dumps(result.params),
            result.params.get("exit_mode", "fixed"),
            result.date_from,
            result.date_to,
            result.total_trades,
            round(result.win_rate, 2),
            float(result.gross_pnl),
            float(result.total_charges),
            float(result.net_pnl),
            float(result.max_drawdown),
            round(result.max_drawdown_pct, 4),
            round(result.sharpe_ratio, 4),
            round(result.profit_factor, 4),
            float(result.avg_win),
            float(result.avg_loss),
            float(result.expectancy),
        )
    return run_id


async def _save_trades(pool, run_id: int, trades: list[BacktestTrade]) -> None:
    """Save all trades to backtest_trades table."""
    if not trades:
        return

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO backtest_trades (
                run_id, symbol, direction, entry_time, exit_time,
                entry_price, exit_price, exit_reason, qty,
                gross_pnl, charges, net_pnl, regime
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11, $12, $13
            )
            """,
            [
                (
                    run_id, t.symbol, t.direction, t.entry_time, t.exit_time,
                    float(t.entry_price), float(t.exit_price), t.exit_reason, t.qty,
                    float(t.gross_pnl), float(t.charges), float(t.net_pnl), t.regime,
                )
                for t in trades
            ],
        )


async def _load_run(pool, run_id: int) -> Optional[BacktestResult]:
    """Load a backtest run from DB."""
    import json

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM backtest_runs WHERE id = $1", run_id
        )
        if not row:
            return None

        trades = await conn.fetch(
            "SELECT * FROM backtest_trades WHERE run_id = $1 ORDER BY entry_time",
            run_id,
        )

    trade_list = [
        BacktestTrade(
            symbol=t["symbol"],
            instrument_token=0,
            direction=t["direction"],
            entry_price=Decimal(str(t["entry_price"])),
            entry_time=t["entry_time"],
            exit_price=Decimal(str(t["exit_price"])),
            exit_time=t["exit_time"],
            exit_reason=t["exit_reason"],
            qty=t["qty"],
            gross_pnl=Decimal(str(t["gross_pnl"])),
            charges=Decimal(str(t["charges"])),
            net_pnl=Decimal(str(t["net_pnl"])),
            regime=t["regime"] or "",
        )
        for t in trades
    ]

    params = json.loads(row["params"]) if row["params"] else {}

    return BacktestResult(
        trades=trade_list,
        daily_results=[],
        params=params,
        total_trades=row["total_trades"] or 0,
        wins=0,
        losses=0,
        win_rate=float(row["win_rate"] or 0),
        avg_win=Decimal(str(row["avg_win"] or 0)),
        avg_loss=Decimal(str(row["avg_loss"] or 0)),
        expectancy=Decimal(str(row["expectancy"] or 0)),
        gross_pnl=Decimal(str(row["gross_pnl"] or 0)),
        total_charges=Decimal(str(row["total_charges"] or 0)),
        net_pnl=Decimal(str(row["net_pnl"] or 0)),
        max_drawdown=Decimal(str(row["max_drawdown"] or 0)),
        max_drawdown_pct=float(row["max_drawdown_pct"] or 0),
        sharpe_ratio=float(row["sharpe_ratio"] or 0),
        profit_factor=float(row["profit_factor"] or 0),
        date_from=row["date_from"],
        date_to=row["date_to"],
        run_id=run_id,
    )


async def _load_last_run(pool) -> Optional[BacktestResult]:
    """Load the most recent backtest run."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM backtest_runs ORDER BY created_at DESC LIMIT 1"
        )
    if not row:
        return None
    return await _load_run(pool, row["id"])


# ---------------------------------------------------------------------------
# Report output
# ---------------------------------------------------------------------------

def print_report(result: BacktestResult) -> None:
    """Print formatted backtest report to terminal."""
    print()
    print(_bold("=" * 70))
    print(_bold("  BACKTEST RESULTS"))
    print(_bold("=" * 70))
    print()

    # Summary
    p = result.params
    print(f"  Strategy:    {p.get('strategy', 's1').upper()}")
    print(f"  Exit Mode:   {p.get('exit_mode', 'fixed')}")
    print(f"  Period:      {result.date_from} \u2192 {result.date_to}")
    print(f"  Interval:    {p.get('interval', '15min')}")
    print(f"  Capital:     {_inr(float(p.get('capital', 0)))}")
    if p.get("exit_mode") in ("trailing", "partial"):
        print(f"  ATR Mult:    {p.get('atr_mult', 1.5)}")
        print(f"  ATR Period:  {p.get('atr_period', 14)}")
    if p.get("exit_mode") == "partial":
        print(f"  Partial %:   {p.get('partial_pct', 0.5)}")
    print(f"  Slippage:    {p.get('slippage', 0.001) * 100:.2f}%")
    print()

    # Key metrics
    print(_bold("  PERFORMANCE"))
    print(f"  {'Trades:':<25}{result.total_trades}")
    print(f"  {'Wins/Losses:':<25}{result.wins}/{result.losses}")
    print(f"  {'Win Rate:':<25}{result.win_rate:.1f}%")
    print(f"  {'Avg Win:':<25}{_pnl_color(float(result.avg_win))}")
    print(f"  {'Avg Loss:':<25}{_pnl_color(float(result.avg_loss))}")
    print(f"  {'Expectancy:':<25}{_pnl_color(float(result.expectancy))}")
    print()
    print(f"  {'Gross P&L:':<25}{_pnl_color(float(result.gross_pnl))}")
    print(f"  {'Total Charges:':<25}{_inr(float(result.total_charges), 2)}")
    print(f"  {'Net P&L:':<25}{_pnl_color(float(result.net_pnl))}")
    print()
    print(f"  {'Max Drawdown:':<25}{_red(_inr(float(result.max_drawdown), 2))} ({result.max_drawdown_pct:.2f}%)")
    print(f"  {'Sharpe Ratio:':<25}{result.sharpe_ratio:.2f}")
    print(f"  {'Profit Factor:':<25}{result.profit_factor:.2f}")
    print(f"  {'Max Con. Wins:':<25}{result.max_consecutive_wins}")
    print(f"  {'Max Con. Losses:':<25}{result.max_consecutive_losses}")

    # S1v2-specific: exit reason breakdown
    if result.params.get("strategy") == "s1v2" and result.trades:
        time_stops = sum(1 for t in result.trades if t.exit_reason == "TIME_STOP")
        hard_exits = sum(1 for t in result.trades if t.exit_reason == "HARD_EXIT")
        stop_hits = sum(1 for t in result.trades if t.exit_reason == "STOP_HIT")
        target_hits = sum(1 for t in result.trades if t.exit_reason == "TARGET_HIT")
        total = max(result.total_trades, 1)
        print()
        print(_bold("  EXIT REASONS"))
        print(f"  {'Target Hit:':<25}{target_hits} ({target_hits/total*100:.1f}%)")
        print(f"  {'Stop Hit:':<25}{stop_hits} ({stop_hits/total*100:.1f}%)")
        print(f"  {'Time Stop:':<25}{time_stops} ({time_stops/total*100:.1f}%)")
        print(f"  {'Hard Exit:':<25}{hard_exits} ({hard_exits/total*100:.1f}%)")

    if result.run_id:
        print(f"\n  Run ID: {result.run_id}")

    # Monthly breakdown
    if result.daily_results:
        print()
        print(_bold("  MONTHLY BREAKDOWN"))
        print(f"  {'Month':<12}  {'Trades':>6}  {'Win%':>5}  {'Net P&L':>14}  {'Regime'}")
        print("  " + "-" * 58)

        monthly: dict[str, dict] = {}
        for dr in result.daily_results:
            key = dr.session_date.strftime("%Y-%m")
            if key not in monthly:
                monthly[key] = {"trades": 0, "wins": 0, "net_pnl": Decimal("0"), "regimes": []}
            monthly[key]["trades"] += dr.trades_closed
            monthly[key]["net_pnl"] += dr.net_pnl
            monthly[key]["regimes"].append(dr.regime)

        # Count wins from trades for each month
        for t in result.trades:
            key = t.exit_time.strftime("%Y-%m") if hasattr(t.exit_time, "strftime") else ""
            if key in monthly and t.net_pnl > 0:
                monthly[key]["wins"] += 1

        for month, data in sorted(monthly.items()):
            wr = data["wins"] / data["trades"] * 100 if data["trades"] > 0 else 0
            # Dominant regime
            from collections import Counter
            regime_counts = Counter(data["regimes"])
            dominant = regime_counts.most_common(1)[0][0] if regime_counts else ""
            print(f"  {month:<12}  {data['trades']:>6}  {wr:>4.0f}%  {_pnl_col(float(data['net_pnl']), 14)}  {dominant}")

    # Per-stock performance
    if result.trades:
        print()
        print(_bold("  PER-STOCK PERFORMANCE"))
        print(f"  {'Symbol':<14}  {'Trades':>6}  {'Win%':>5}  {'Net P&L':>14}  {'Best':>12}  {'Worst':>12}")
        print("  " + "-" * 73)

        by_stock: dict[str, list[BacktestTrade]] = defaultdict(list)
        for t in result.trades:
            by_stock[t.symbol].append(t)

        stock_summary = []
        for symbol, stock_trades in by_stock.items():
            total = len(stock_trades)
            wins = sum(1 for t in stock_trades if t.net_pnl > 0)
            wr = wins / total * 100 if total > 0 else 0
            net = sum(t.net_pnl for t in stock_trades)
            best = max(t.net_pnl for t in stock_trades)
            worst = min(t.net_pnl for t in stock_trades)
            stock_summary.append((symbol, total, wr, net, best, worst))

        # Sort alphabetically by symbol
        stock_summary.sort(key=lambda x: x[0])
        for symbol, total, wr, net, best, worst in stock_summary:
            print(
                f"  {symbol:<14}  {total:>6}  {wr:>4.0f}%"
                f"  {_pnl_col(float(net), 14)}"
                f"  {_pnl_col(float(best), 12)}"
                f"  {_pnl_col(float(worst), 12)}"
            )

    # Regime performance
    if result.trades:
        print()
        print(_bold("  REGIME PERFORMANCE"))
        print(f"  {'Regime':<20}  {'Trades':>6}  {'Win%':>5}  {'Avg P&L':>14}")
        print("  " + "-" * 53)

        by_regime: dict[str, list[BacktestTrade]] = defaultdict(list)
        for t in result.trades:
            by_regime[t.regime].append(t)

        for regime, regime_trades in sorted(by_regime.items()):
            total = len(regime_trades)
            wins = sum(1 for t in regime_trades if t.net_pnl > 0)
            wr = wins / total * 100 if total > 0 else 0
            avg_pnl = sum(t.net_pnl for t in regime_trades) / Decimal(str(total))
            print(f"  {regime:<20}  {total:>6}  {wr:>4.0f}%  {_pnl_col(float(avg_pnl), 14)}")

    print()
    print(_bold("=" * 70))
    print()


def print_optimize_report(results: list[tuple[float, BacktestResult]], param_name: str) -> None:
    """Print optimizer sensitivity table."""
    print()
    print(_bold(f"  OPTIMIZER: {param_name}"))
    print(f"  {'Value':>8}  {'Trades':>6}  {'Win%':>5}  {'Net P&L':>14}  {'Sharpe':>7}  {'PF':>6}  {'MaxDD%':>7}")
    print("  " + "-" * 65)

    for val, r in results:
        print(
            f"  {val:>8.2f}  {r.total_trades:>6}  {r.win_rate:>4.1f}%"
            f"  {_pnl_col(float(r.net_pnl), 14)}  {r.sharpe_ratio:>7.2f}"
            f"  {r.profit_factor:>6.2f}  {r.max_drawdown_pct:>6.2f}%"
        )
    print()


def print_compare_report(results: dict[str, BacktestResult]) -> None:
    """Print side-by-side comparison of exit modes."""
    print()
    print(_bold("  EXIT MODE COMPARISON"))
    print(f"  {'Mode':<12}  {'Trades':>6}  {'Win%':>5}  {'Net P&L':>14}  {'Sharpe':>7}  {'PF':>6}  {'MaxDD%':>7}")
    print("  " + "-" * 67)

    for mode, r in results.items():
        print(
            f"  {mode:<12}  {r.total_trades:>6}  {r.win_rate:>4.1f}%"
            f"  {_pnl_col(float(r.net_pnl), 14)}  {r.sharpe_ratio:>7.2f}"
            f"  {r.profit_factor:>6.2f}  {r.max_drawdown_pct:>6.2f}%"
        )
    print()


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------

async def _run_backtest(args) -> None:
    """Run a single backtest."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn()
    if not dsn:
        print(_red("ERROR: No database DSN configured"))
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        # Inject strategy override for engine dispatch
        config["_strategy_override"] = args.strategy

        engine = BacktestEngine(
            pool=pool,
            config=config,
            exit_mode=args.exit_mode,
            atr_mult=args.atr_mult,
            atr_period=args.atr_period,
            partial_pct=args.partial_pct,
            slippage=args.slippage,
            interval=args.interval,
        )

        date_from = date.fromisoformat(getattr(args, "from"))
        date_to = date.fromisoformat(args.to)

        result = await engine.run(date_from, date_to)

        # Save to DB
        run_id = await _save_run(pool, result)
        await _save_trades(pool, run_id, result.trades)
        result.run_id = run_id

        print_report(result)
        step_done(f"Saved as run #{run_id}")
    finally:
        await pool.close()


async def _run_optimize(args) -> None:
    """Run parameter optimization."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn()
    if not dsn:
        print(_red("ERROR: No database DSN configured"))
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        date_from = date.fromisoformat(getattr(args, "from"))
        date_to = date.fromisoformat(args.to)

        results = await run_optimize(
            pool=pool,
            config=config,
            param_name=args.param,
            range_str=args.range,
            date_from=date_from,
            date_to=date_to,
            exit_mode=args.exit_mode,
            atr_mult=args.atr_mult,
            atr_period=args.atr_period,
            partial_pct=args.partial_pct,
            slippage=args.slippage,
            interval=args.interval,
        )

        print_optimize_report(results, args.param)
    finally:
        await pool.close()


async def _run_compare(args) -> None:
    """Run exit mode comparison."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn()
    if not dsn:
        print(_red("ERROR: No database DSN configured"))
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        date_from = date.fromisoformat(getattr(args, "from"))
        date_to = date.fromisoformat(args.to)
        modes = [m.strip() for m in args.modes.split(",")]

        results = await run_compare(
            pool=pool,
            config=config,
            exit_modes=modes,
            date_from=date_from,
            date_to=date_to,
            atr_mult=args.atr_mult,
            atr_period=args.atr_period,
            partial_pct=args.partial_pct,
            slippage=args.slippage,
            interval=args.interval,
        )

        print_compare_report(results)
    finally:
        await pool.close()


async def _run_show(args) -> None:
    """Show a previous backtest run."""
    import asyncpg

    dsn = _load_dsn()
    if not dsn:
        print(_red("ERROR: No database DSN configured"))
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        if args.last_run:
            result = await _load_last_run(pool)
        elif args.run_id:
            result = await _load_run(pool, args.run_id)
        else:
            print(_red("ERROR: Specify --last-run or --run-id N"))
            sys.exit(1)

        if result is None:
            print(_yellow("No backtest runs found"))
            return

        print_report(result)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradeOS Backtester — S1 strategy simulation engine"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Common arguments
    def add_common_args(p):
        p.add_argument("--from", required=True, help="Start date (YYYY-MM-DD)")
        p.add_argument("--to", required=True, help="End date (YYYY-MM-DD)")
        p.add_argument("--strategy", default="s1", help="Strategy name (default: s1)")
        p.add_argument("--interval", default="15min", help="Candle interval (default: 15min)")
        p.add_argument("--slippage", type=float, default=0.001, help="Slippage fraction (default: 0.001)")

    def add_exit_args(p):
        p.add_argument("--exit-mode", default="fixed", choices=["fixed", "trailing", "partial"],
                        help="Exit mode (default: fixed)")
        p.add_argument("--atr-mult", type=float, default=1.5, help="ATR multiplier for trailing (default: 1.5)")
        p.add_argument("--atr-period", type=int, default=14, help="ATR period in candles (default: 14)")
        p.add_argument("--partial-pct", type=float, default=0.5, help="Partial exit fraction (default: 0.5)")

    # run
    run_parser = subparsers.add_parser("run", help="Run a single backtest")
    add_common_args(run_parser)
    add_exit_args(run_parser)
    run_parser.add_argument("--verbose", action="store_true", help="Show per-trade detail")

    # optimize
    opt_parser = subparsers.add_parser("optimize", help="Sweep a parameter")
    add_common_args(opt_parser)
    add_exit_args(opt_parser)
    opt_parser.add_argument("--param", required=True, help="Parameter to sweep")
    opt_parser.add_argument("--range", required=True, help="start:step:end")

    # compare
    cmp_parser = subparsers.add_parser("compare", help="Compare exit modes")
    add_common_args(cmp_parser)
    add_exit_args(cmp_parser)
    cmp_parser.add_argument("--modes", required=True, help="Comma-separated exit modes")

    # show
    show_parser = subparsers.add_parser("show", help="Show a previous run")
    show_parser.add_argument("--last-run", action="store_true", help="Show most recent run")
    show_parser.add_argument("--run-id", type=int, help="Show specific run by ID")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(_run_backtest(args))
    elif args.command == "optimize":
        asyncio.run(_run_optimize(args))
    elif args.command == "compare":
        asyncio.run(_run_compare(args))
    elif args.command == "show":
        asyncio.run(_run_show(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
