"""
TradeOS — Indicator Engine (EMA9/21, RSI14, Volume Ratio, Swing High/Low)

Uses the `ta` library for all indicator calculations (EMA, RSI).
pandas-ta is not used because it is incompatible with pandas 3.x.
The `ta` library provides identical calculations via pandas Series.

Requires minimum 21 candles for valid output (EMA21 lookback period).
Recommended: 60 candles for stable RSI and volume ratio.

Swing high/low uses the last 5 completed candles (including the current one).
Volume ratio = current candle volume / 20-period rolling mean.
"""
from __future__ import annotations

import structlog
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import pandas as pd
import ta.trend
import ta.momentum

from core.strategy_engine.candle_builder import Candle

log = structlog.get_logger()

MIN_CANDLES: int = 21    # EMA21 requires at least 21 data points
DEQUE_MAXLEN: int = 200  # rolling window cap


@dataclass
class Indicators:
    """Snapshot of all indicator values at a given candle time."""

    ema9: Decimal
    ema21: Decimal
    rsi: Decimal           # RSI(14) on close prices
    volume_ratio: Decimal  # current candle volume / 20-period rolling mean
    swing_high: Decimal    # highest high in last 5 candles
    swing_low: Decimal     # lowest low in last 5 candles
    vwap: Decimal          # KiteConnect session VWAP (passed through from candle)
    candle_time: datetime
    symbol: str


class IndicatorEngine:
    """
    Computes EMA9/21, RSI14, Volume Ratio, and Swing High/Low from 15-min candles.

    Initialised with historical warmup candles to prime the lookback.
    After each new live candle, call update() to get a fresh Indicators snapshot.
    Returns None until at least MIN_CANDLES (21) are available.
    """

    def __init__(
        self,
        warmup_candles: list[Candle],
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        swing_lookback: int = 5,
    ) -> None:
        """
        Args:
            warmup_candles: Historical candles in chronological order.
                            Minimum ema_slow required for non-None output; 60+ recommended.
            ema_fast:       Fast EMA period (default 9).
            ema_slow:       Slow EMA period (default 21).
            rsi_period:     RSI calculation period (default 14).
            swing_lookback: Number of candles for swing high/low (default 5).
        """
        self._candles: deque[Candle] = deque(
            warmup_candles[-DEQUE_MAXLEN:], maxlen=DEQUE_MAXLEN
        )
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow
        self._rsi_period = rsi_period
        self._swing_lookback = swing_lookback
        self._min_candles = ema_slow  # derived from slowest EMA
        log.debug(
            "indicator_engine_initialised",
            warmup_count=len(warmup_candles),
            loaded=len(self._candles),
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_period=rsi_period,
            swing_lookback=swing_lookback,
        )

    def update(self, candle: Candle) -> Optional[Indicators]:
        """
        Append the latest completed candle and recompute all indicators.

        Returns None during warm-up (fewer than MIN_CANDLES available).
        The caller must skip signal evaluation when None is returned.

        Args:
            candle: Freshly completed 15-minute candle.

        Returns:
            Indicators snapshot, or None if not enough history yet.
        """
        self._candles.append(candle)

        if len(self._candles) < self._min_candles:
            log.debug(
                "indicator_engine_warming_up",
                symbol=candle.symbol,
                candle_count=len(self._candles),
                required=self._min_candles,
            )
            return None

        return self._compute(candle)

    # ------------------------------------------------------------------
    # Internal computation
    # ------------------------------------------------------------------

    def _compute(self, candle: Candle) -> Optional[Indicators]:
        """Compute all indicators from the current deque of candles."""
        candle_list = list(self._candles)
        n = len(candle_list)

        close_series = pd.Series(
            [float(c.close) for c in candle_list], dtype=float
        )
        volume_series = pd.Series(
            [float(c.volume) for c in candle_list], dtype=float
        )

        # EMA fast and EMA slow (via ta.trend.EMAIndicator)
        ema9_series = ta.trend.EMAIndicator(
            close=close_series, window=self._ema_fast, fillna=False
        ).ema_indicator()
        ema21_series = ta.trend.EMAIndicator(
            close=close_series, window=self._ema_slow, fillna=False
        ).ema_indicator()

        ema9_val = ema9_series.iloc[-1]
        ema21_val = ema21_series.iloc[-1]

        if pd.isna(ema9_val) or pd.isna(ema21_val):
            log.warning("indicator_engine_ema_nan", symbol=candle.symbol, n=n)
            return None

        # RSI via ta.momentum.RSIIndicator
        rsi_series = ta.momentum.RSIIndicator(
            close=close_series, window=self._rsi_period, fillna=False
        ).rsi()
        rsi_val = rsi_series.iloc[-1]

        if pd.isna(rsi_val):
            log.debug("indicator_engine_rsi_nan", symbol=candle.symbol, n=n)
            return None

        # Volume ratio: current / 20-period rolling mean
        vol_mean = volume_series.rolling(window=20).mean().iloc[-1]
        current_vol = float(candle_list[-1].volume)
        if pd.isna(vol_mean) or vol_mean <= 0:
            vol_ratio = Decimal("1.0")
        else:
            vol_ratio = Decimal(str(round(current_vol / float(vol_mean), 4)))

        # Swing high/low: last N candles (inclusive of current)
        last5 = candle_list[-self._swing_lookback:]
        swing_high = max(c.high for c in last5)
        swing_low = min(c.low for c in last5)

        return Indicators(
            ema9=Decimal(str(round(ema9_val, 4))),
            ema21=Decimal(str(round(ema21_val, 4))),
            rsi=Decimal(str(round(rsi_val, 2))),
            volume_ratio=vol_ratio,
            swing_high=swing_high,
            swing_low=swing_low,
            vwap=candle.vwap,
            candle_time=candle.candle_time,
            symbol=candle.symbol,
        )
