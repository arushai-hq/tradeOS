"""
TradeOS — S1 Signal Generator

Evaluates S1 (Intraday Momentum) entry conditions on completed 15-min candles.
One SignalGenerator instance is shared across all instruments.

S1 CONDITIONS — ALL must be true simultaneously:

  LONG:
    1. ema9 > ema21                    (trend: bullish)
    2. candle.close > vwap             (price above session VWAP)
    3. 55 <= rsi <= 70                 (momentum: not overbought)
    4. volume_ratio >= 1.5             (conviction: above-avg volume)

  SHORT:
    1. ema9 < ema21                    (trend: bearish)
    2. candle.close < vwap             (price below session VWAP)
    3. rsi >= 45                       (momentum: above oversold zone — not exhausted)
    4. volume_ratio >= 1.5             (conviction: above-avg volume)

  STOP LOSS:
    LONG:  stop = indicators.swing_low  (lowest low, last 5 candles)
    SHORT: stop = indicators.swing_high (highest high, last 5 candles)

  TARGET (1:2 risk-reward):
    LONG:  target = entry + 2 * (entry - stop)
    SHORT: target = entry - 2 * (stop - entry)

  ENTRY: candle.close (theoretical; actual fill will differ)

DEDUPLICATION: one signal per (symbol, direction) per session.
Prevents re-entry after a stop-out on the same instrument.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import pytz

from strategy_engine.candle_builder import Candle
from strategy_engine.indicators import Indicators

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")

# --- S1 parameter constants ---
LONG_RSI_MIN: Decimal = Decimal("55")
LONG_RSI_MAX: Decimal = Decimal("70")
SHORT_RSI_MIN: Decimal = Decimal("30")
SHORT_RSI_MAX: Decimal = Decimal("45")
MIN_VOLUME_RATIO: Decimal = Decimal("1.5")
RR_RATIO: Decimal = Decimal("2")  # 1:2 risk-reward
DEFAULT_MIN_STOP_PCT: Decimal = Decimal("0.02")  # 2.0% minimum stop distance


@dataclass
class Signal:
    """Complete S1 signal with full indicator snapshot and risk levels."""

    symbol: str
    instrument_token: int
    direction: str            # 'LONG' or 'SHORT'
    signal_time: datetime
    candle_time: datetime
    theoretical_entry: Decimal
    stop_loss: Decimal
    target: Decimal
    ema9: Decimal
    ema21: Decimal
    rsi: Decimal
    vwap: Decimal
    volume_ratio: Decimal


class SignalGenerator:
    """
    Evaluates S1 entry conditions on completed 15-minute candles.

    Maintains per-session deduplication — one signal per (symbol, direction)
    to prevent re-entry on the same instrument after a stop-out.

    Call reset_session() at the start of each trading day.
    """

    def __init__(self, s1_config: dict | None = None) -> None:
        # (symbol, direction) pairs already signalled in this session
        self._session_signals: set[tuple[str, str]] = set()

        cfg = s1_config or {}
        self._rsi_long_min = Decimal(str(cfg.get("rsi_long_min", LONG_RSI_MIN)))
        self._rsi_long_max = Decimal(str(cfg.get("rsi_long_max", LONG_RSI_MAX)))
        self._rsi_short_min = Decimal(str(cfg.get("rsi_short_min", SHORT_RSI_MAX)))
        self._volume_ratio_min = Decimal(str(cfg.get("volume_ratio_min", MIN_VOLUME_RATIO)))
        self._rr_ratio = Decimal(str(cfg.get("rr_ratio", RR_RATIO)))
        self._min_stop_pct = Decimal(str(cfg.get("min_stop_pct", DEFAULT_MIN_STOP_PCT)))

        log.info(
            "s1_config_loaded",
            ema_fast=cfg.get("ema_fast", 9),
            ema_slow=cfg.get("ema_slow", 21),
            rsi_period=cfg.get("rsi_period", 14),
            rsi_long_min=float(self._rsi_long_min),
            rsi_long_max=float(self._rsi_long_max),
            rsi_short_min=float(self._rsi_short_min),
            volume_ratio_min=float(self._volume_ratio_min),
            rr_ratio=float(self._rr_ratio),
            swing_lookback=cfg.get("swing_lookback", 5),
            min_stop_pct=float(self._min_stop_pct),
        )

    def reset_session(self) -> None:
        """Clear deduplication state for a new trading session."""
        self._session_signals.clear()
        log.info("signal_generator_session_reset")

    def evaluate(self, candle: Candle, indicators: Indicators) -> Optional[Signal]:
        """
        Evaluate S1 entry conditions for LONG and SHORT on the completed candle.

        Returns a Signal if all S1 conditions are met and this is a fresh signal
        (not yet signalled for this symbol/direction in the current session).
        Returns None otherwise.

        Args:
            candle: Completed 15-minute OHLCV candle.
            indicators: Indicator snapshot computed for this candle.

        Returns:
            Signal if all conditions pass; None otherwise.
        """
        symbol = candle.symbol
        token = candle.instrument_token
        close = candle.close
        vwap = indicators.vwap

        # --- LONG signal evaluation ---
        if (
            indicators.ema9 > indicators.ema21
            and close > vwap
            and self._rsi_long_min <= indicators.rsi <= self._rsi_long_max
            and indicators.volume_ratio >= self._volume_ratio_min
        ):
            key = (symbol, "LONG")
            if key in self._session_signals:
                log.debug("signal_dedup_skipped", symbol=symbol, direction="LONG")
                return None

            swing_stop = indicators.swing_low
            min_stop = close * (Decimal("1") - self._min_stop_pct)
            stop = min(swing_stop, min_stop)  # wider stop (lower for LONG)
            if stop != swing_stop:
                log.info(
                    "stop_floor_applied",
                    symbol=symbol,
                    direction="LONG",
                    swing_stop=float(swing_stop),
                    enforced_stop=float(stop),
                    min_pct=f"{float(self._min_stop_pct) * 100:.1f}%",
                )
            risk = close - stop
            target = close + (self._rr_ratio * risk)

            signal = Signal(
                symbol=symbol,
                instrument_token=token,
                direction="LONG",
                signal_time=datetime.now(IST),
                candle_time=candle.candle_time,
                theoretical_entry=close,
                stop_loss=stop,
                target=target,
                ema9=indicators.ema9,
                ema21=indicators.ema21,
                rsi=indicators.rsi,
                vwap=vwap,
                volume_ratio=indicators.volume_ratio,
            )
            self._session_signals.add(key)
            log.info(
                "s1_signal_generated",
                symbol=symbol,
                direction="LONG",
                entry=float(close),
                stop=float(stop),
                target=float(target),
                rsi=float(indicators.rsi),
                volume_ratio=float(indicators.volume_ratio),
            )
            log.debug(
                "signal_evaluated",
                symbol=symbol,
                direction="LONG",
                candle_time=candle.candle_time.isoformat(),
                ema9=float(indicators.ema9),
                ema21=float(indicators.ema21),
                rsi=float(indicators.rsi),
                vwap=float(vwap),
                price=float(close),
                volume_ratio=float(indicators.volume_ratio),
                ema_cross=True,
                price_above_vwap=True,
                rsi_in_range=True,
                volume_ok=True,
                result="signal_generated",
            )
            return signal

        # --- SHORT RSI rejection log — bearish setup met but RSI already oversold ---
        if (
            indicators.ema9 < indicators.ema21
            and close < vwap
            and indicators.rsi < self._rsi_short_min
            and indicators.volume_ratio >= self._volume_ratio_min
        ):
            log.debug(
                "rsi_filter_rejected",
                symbol=symbol,
                direction="SHORT",
                rsi=float(indicators.rsi),
                threshold=float(self._rsi_short_min),
            )

        # --- SHORT signal evaluation ---
        if (
            indicators.ema9 < indicators.ema21
            and close < vwap
            and indicators.rsi >= self._rsi_short_min   # B3 fix: RSI above oversold zone
            and indicators.volume_ratio >= self._volume_ratio_min
        ):
            key = (symbol, "SHORT")
            if key in self._session_signals:
                log.debug("signal_dedup_skipped", symbol=symbol, direction="SHORT")
                return None

            swing_stop = indicators.swing_high
            min_stop = close * (Decimal("1") + self._min_stop_pct)
            stop = max(swing_stop, min_stop)  # wider stop (higher for SHORT)
            if stop != swing_stop:
                log.info(
                    "stop_floor_applied",
                    symbol=symbol,
                    direction="SHORT",
                    swing_stop=float(swing_stop),
                    enforced_stop=float(stop),
                    min_pct=f"{float(self._min_stop_pct) * 100:.1f}%",
                )
            risk = stop - close
            target = close - (self._rr_ratio * risk)

            signal = Signal(
                symbol=symbol,
                instrument_token=token,
                direction="SHORT",
                signal_time=datetime.now(IST),
                candle_time=candle.candle_time,
                theoretical_entry=close,
                stop_loss=stop,
                target=target,
                ema9=indicators.ema9,
                ema21=indicators.ema21,
                rsi=indicators.rsi,
                vwap=vwap,
                volume_ratio=indicators.volume_ratio,
            )
            self._session_signals.add(key)
            log.info(
                "s1_signal_generated",
                symbol=symbol,
                direction="SHORT",
                entry=float(close),
                stop=float(stop),
                target=float(target),
                rsi=float(indicators.rsi),
                volume_ratio=float(indicators.volume_ratio),
            )
            log.debug(
                "signal_evaluated",
                symbol=symbol,
                direction="SHORT",
                candle_time=candle.candle_time.isoformat(),
                ema9=float(indicators.ema9),
                ema21=float(indicators.ema21),
                rsi=float(indicators.rsi),
                vwap=float(vwap),
                price=float(close),
                volume_ratio=float(indicators.volume_ratio),
                ema_cross=True,
                price_above_vwap=True,
                rsi_in_range=True,
                volume_ok=True,
                result="signal_generated",
            )
            return signal

        log.debug(
            "signal_evaluated",
            symbol=symbol,
            candle_time=candle.candle_time.isoformat(),
            ema9=float(indicators.ema9),
            ema21=float(indicators.ema21),
            rsi=float(indicators.rsi),
            vwap=float(vwap),
            price=float(close),
            volume_ratio=float(indicators.volume_ratio),
            ema_cross=indicators.ema9 > indicators.ema21,
            price_above_vwap=close > vwap,
            rsi_in_range=(
                (self._rsi_long_min <= indicators.rsi <= self._rsi_long_max)
                or (indicators.rsi >= self._rsi_short_min)
            ),
            volume_ok=indicators.volume_ratio >= self._volume_ratio_min,
            result="no_signal",
        )
        return None
