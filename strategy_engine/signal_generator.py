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

    def __init__(self) -> None:
        # (symbol, direction) pairs already signalled in this session
        self._session_signals: set[tuple[str, str]] = set()

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
            and LONG_RSI_MIN <= indicators.rsi <= LONG_RSI_MAX
            and indicators.volume_ratio >= MIN_VOLUME_RATIO
        ):
            key = (symbol, "LONG")
            if key in self._session_signals:
                log.debug("signal_dedup_skipped", symbol=symbol, direction="LONG")
                return None

            stop = indicators.swing_low
            risk = close - stop
            target = close + (RR_RATIO * risk)

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
            and indicators.rsi < SHORT_RSI_MAX
            and indicators.volume_ratio >= MIN_VOLUME_RATIO
        ):
            log.debug(
                "rsi_filter_rejected",
                symbol=symbol,
                direction="SHORT",
                rsi=float(indicators.rsi),
                threshold=float(SHORT_RSI_MAX),
            )

        # --- SHORT signal evaluation ---
        if (
            indicators.ema9 < indicators.ema21
            and close < vwap
            and indicators.rsi >= SHORT_RSI_MAX   # B3 fix: was SHORT_RSI_MIN <= rsi <= SHORT_RSI_MAX
            and indicators.volume_ratio >= MIN_VOLUME_RATIO
        ):
            key = (symbol, "SHORT")
            if key in self._session_signals:
                log.debug("signal_dedup_skipped", symbol=symbol, direction="SHORT")
                return None

            stop = indicators.swing_high
            risk = stop - close
            target = close - (RR_RATIO * risk)

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
                (LONG_RSI_MIN <= indicators.rsi <= LONG_RSI_MAX)
                or (indicators.rsi >= SHORT_RSI_MAX)  # B3 fix: was SHORT_RSI_MIN <= rsi <= SHORT_RSI_MAX
            ),
            volume_ok=indicators.volume_ratio >= MIN_VOLUME_RATIO,
            result="no_signal",
        )
        return None
