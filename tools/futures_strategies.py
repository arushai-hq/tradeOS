"""
TradeOS — Index Futures Strategy Classes

Three strategies purpose-built for single-instrument index futures (NIFTY/BANKNIFTY):

  1. ORBStrategy        — Opening Range Breakout
  2. VWAPMeanReversionStrategy — VWAP SD Mean Reversion
  3. MACDSupertrendStrategy    — MACD + Supertrend (multi-timeframe)

Self-contained indicator functions — ZERO imports from tools/backtester.py.
All parameters sourced from config/settings.yaml under futures.strategies.{name}.

TRADEOS-04-CC010
"""
from __future__ import annotations

import math
import structlog
from dataclasses import dataclass
from datetime import datetime, time as dt_time, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import pytz

from core.strategy_engine.candle_builder import Candle
from core.strategy_engine.signal_generator import Signal

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

# ═══════════════════════════════════════════════════════════════════════════
# INDICATOR FUNCTIONS — pure, self-contained, Decimal-based
# ═══════════════════════════════════════════════════════════════════════════


def compute_atr(candles: list[Candle], period: int = 14) -> Decimal:
    """Average True Range using SMA of true ranges.

    Returns Decimal("0") if fewer than 2 candles.
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

    if not true_ranges:
        return Decimal("0")

    # Use last `period` TRs (or all if fewer)
    window = true_ranges[-period:]
    return sum(window) / Decimal(str(len(window)))


def compute_ema(values: list[Decimal], period: int) -> Optional[Decimal]:
    """Exponential Moving Average on a list of Decimal values.

    Returns None if fewer than `period` values.
    """
    if len(values) < period:
        return None

    # SMA seed
    sma = sum(values[:period]) / Decimal(str(period))
    multiplier = Decimal("2") / Decimal(str(period + 1))

    ema = sma
    for val in values[period:]:
        ema = (val - ema) * multiplier + ema
    return ema


def compute_rsi(candles: list[Candle], period: int = 14) -> Optional[Decimal]:
    """Wilder's smoothed RSI on close prices.

    Returns None if fewer than period + 1 candles.
    """
    if len(candles) < period + 1:
        return None

    closes = [c.close for c in candles]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial average gain/loss (SMA of first `period` deltas)
    gains = [d if d > 0 else Decimal("0") for d in deltas[:period]]
    losses = [abs(d) if d < 0 else Decimal("0") for d in deltas[:period]]
    avg_gain = sum(gains) / Decimal(str(period))
    avg_loss = sum(losses) / Decimal(str(period))

    # Wilder's smoothing for remaining deltas
    for d in deltas[period:]:
        if d > 0:
            avg_gain = (avg_gain * Decimal(str(period - 1)) + d) / Decimal(str(period))
            avg_loss = (avg_loss * Decimal(str(period - 1))) / Decimal(str(period))
        else:
            avg_gain = (avg_gain * Decimal(str(period - 1))) / Decimal(str(period))
            avg_loss = (avg_loss * Decimal(str(period - 1)) + abs(d)) / Decimal(str(period))

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss
    rsi = Decimal("100") - Decimal("100") / (Decimal("1") + rs)
    return rsi


def compute_adx(candles: list[Candle], period: int = 14) -> Optional[Decimal]:
    """Average Directional Index (ADX).

    Computes +DI, -DI, DX, then smoothed ADX.
    Returns None if fewer than period * 2 candles.
    """
    if len(candles) < period * 2:
        return None

    plus_dm_list: list[Decimal] = []
    minus_dm_list: list[Decimal] = []
    tr_list: list[Decimal] = []

    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_high = candles[i - 1].high
        prev_low = candles[i - 1].low
        prev_close = candles[i - 1].close

        # True Range
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

        # Directional Movement
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else Decimal("0")
        minus_dm = down_move if (down_move > up_move and down_move > 0) else Decimal("0")
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    # Wilder's smoothing for first period
    atr = sum(tr_list[:period])
    plus_di_sum = sum(plus_dm_list[:period])
    minus_di_sum = sum(minus_dm_list[:period])

    dx_values: list[Decimal] = []
    p = Decimal(str(period))

    for i in range(period, len(tr_list)):
        atr = atr - atr / p + tr_list[i]
        plus_di_sum = plus_di_sum - plus_di_sum / p + plus_dm_list[i]
        minus_di_sum = minus_di_sum - minus_di_sum / p + minus_dm_list[i]

        if atr == 0:
            dx_values.append(Decimal("0"))
            continue

        plus_di = Decimal("100") * plus_di_sum / atr
        minus_di = Decimal("100") * minus_di_sum / atr
        di_sum = plus_di + minus_di

        if di_sum == 0:
            dx_values.append(Decimal("0"))
        else:
            dx = Decimal("100") * abs(plus_di - minus_di) / di_sum
            dx_values.append(dx)

    if len(dx_values) < period:
        return None

    # ADX = smoothed average of DX
    adx = sum(dx_values[:period]) / p
    for dx_val in dx_values[period:]:
        adx = (adx * (p - Decimal("1")) + dx_val) / p

    return adx


def compute_volume_sma(candles: list[Candle], period: int = 20) -> Optional[Decimal]:
    """Simple Moving Average of volume.

    Returns None if fewer than `period` candles.
    """
    if len(candles) < period:
        return None
    volumes = [Decimal(str(c.volume)) for c in candles[-period:]]
    return sum(volumes) / Decimal(str(period))


def compute_vwap_with_bands(
    candles: list[Candle], band_mult: Decimal = Decimal("2"),
) -> tuple[Decimal, Decimal, Decimal]:
    """Running VWAP + standard deviation bands from intraday candles.

    Returns (vwap, upper_band, lower_band).
    Falls back to (close, close, close) if no volume data.
    """
    cum_tp_vol = Decimal("0")
    cum_vol = Decimal("0")
    tp_values: list[Decimal] = []

    for c in candles:
        tp = (c.high + c.low + c.close) / Decimal("3")
        vol = Decimal(str(c.volume)) if c.volume else Decimal("1")
        cum_tp_vol += tp * vol
        cum_vol += vol
        tp_values.append(tp)

    if cum_vol == 0:
        last_close = candles[-1].close if candles else Decimal("0")
        return last_close, last_close, last_close

    vwap = cum_tp_vol / cum_vol

    # Standard deviation of typical price from VWAP
    if len(tp_values) < 2:
        return vwap, vwap, vwap

    sq_diff_sum = sum((tp - vwap) ** 2 for tp in tp_values)
    variance = sq_diff_sum / Decimal(str(len(tp_values)))
    # Decimal sqrt via float conversion
    std_dev = Decimal(str(math.sqrt(float(variance))))

    upper = vwap + band_mult * std_dev
    lower = vwap - band_mult * std_dev
    return vwap, upper, lower


def compute_macd(
    candles: list[Candle],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> Optional[tuple[Decimal, Decimal, Decimal]]:
    """MACD line, signal line, and histogram.

    Returns None if fewer than slow + signal_period candles.
    Returns (macd_line, signal_line, histogram).
    """
    min_len = slow + signal_period
    if len(candles) < min_len:
        return None

    closes = [c.close for c in candles]

    fast_ema = compute_ema(closes, fast)
    slow_ema = compute_ema(closes, slow)

    if fast_ema is None or slow_ema is None:
        return None

    # Compute MACD line series for signal line EMA
    # We need enough MACD values to compute the signal EMA
    macd_values: list[Decimal] = []
    fast_mult = Decimal("2") / Decimal(str(fast + 1))
    slow_mult = Decimal("2") / Decimal(str(slow + 1))

    # Seed fast and slow EMAs
    f_ema = sum(closes[:fast]) / Decimal(str(fast))
    s_ema = sum(closes[:slow]) / Decimal(str(slow))

    # Advance fast EMA to slow start point
    for val in closes[fast:slow]:
        f_ema = (val - f_ema) * fast_mult + f_ema

    # Now iterate from slow period onward, computing both EMAs and MACD
    macd_values.append(f_ema - s_ema)
    for val in closes[slow:]:
        f_ema = (val - f_ema) * fast_mult + f_ema
        s_ema = (val - s_ema) * slow_mult + s_ema
        macd_values.append(f_ema - s_ema)

    if len(macd_values) < signal_period:
        return None

    # Signal line = EMA of MACD values
    sig_ema = sum(macd_values[:signal_period]) / Decimal(str(signal_period))
    sig_mult = Decimal("2") / Decimal(str(signal_period + 1))
    for mv in macd_values[signal_period:]:
        sig_ema = (mv - sig_ema) * sig_mult + sig_ema

    macd_line = macd_values[-1]
    signal_line = sig_ema
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def compute_supertrend(
    candles: list[Candle], period: int = 10, multiplier: Decimal = Decimal("3"),
) -> Optional[tuple[Decimal, int]]:
    """Supertrend indicator.

    Returns (supertrend_value, direction) where direction is:
      1  = bullish (price above supertrend)
     -1  = bearish (price below supertrend)

    Returns None if fewer than period + 1 candles.
    """
    if len(candles) < period + 1:
        return None

    # Compute ATR series using SMA method
    true_ranges: list[Decimal] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # We need at least `period` TRs to start
    if len(true_ranges) < period:
        return None

    # Start computing from index `period` in candles (index `period-1` in TRs)
    atr = sum(true_ranges[:period]) / Decimal(str(period))

    # Initial bands
    hl2 = (candles[period].high + candles[period].low) / Decimal("2")
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    # Initial direction: bullish if close > upper_band seed
    in_uptrend = candles[period].close > hl2

    final_upper = upper_band
    final_lower = lower_band

    for i in range(period + 1, len(candles)):
        # Smoothed ATR (Wilder's)
        atr = (atr * Decimal(str(period - 1)) + true_ranges[i - 1]) / Decimal(str(period))

        hl2 = (candles[i].high + candles[i].low) / Decimal("2")
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        # Final upper band: use previous final_upper if it's lower (tighter)
        if basic_upper < final_upper or candles[i - 1].close > final_upper:
            final_upper = basic_upper
        # else keep previous final_upper

        # Final lower band: use previous final_lower if it's higher (tighter)
        if basic_lower > final_lower or candles[i - 1].close < final_lower:
            final_lower = basic_lower
        # else keep previous final_lower

        # Direction change logic
        if in_uptrend:
            if candles[i].close < final_lower:
                in_uptrend = False
        else:
            if candles[i].close > final_upper:
                in_uptrend = True

    direction = 1 if in_uptrend else -1
    st_value = final_lower if in_uptrend else final_upper
    return st_value, direction


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY CLASSES
# ═══════════════════════════════════════════════════════════════════════════


class ORBStrategy:
    """Opening Range Breakout — proven on index futures globally.

    Collects the opening range (09:15–09:30 or 09:45), then trades the first
    breakout above/below the range with volume confirmation.
    Max 1 trade per day.
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("strategy", {}).get("orb", {})
        self._range_minutes = int(cfg.get("range_minutes", 15))
        self._min_range_pct = Decimal(str(cfg.get("min_range_pct", 0.0015)))
        self._max_range_pct = Decimal(str(cfg.get("max_range_pct", 0.006)))
        self._volume_ratio_min = Decimal(str(cfg.get("volume_ratio_min", 1.2)))
        self._stop_mode = cfg.get("stop_mode", "range_end")
        self._target_multiplier = Decimal(str(cfg.get("target_multiplier", 1.5)))
        self._max_trades_per_day = int(cfg.get("max_trades_per_day", 1))

        no_entry_str = cfg.get("no_entry_after", "14:00")
        h, m = map(int, no_entry_str.split(":"))
        self._no_entry_after = dt_time(h, m)

        # Per-day state
        self._range_high: Optional[Decimal] = None
        self._range_low: Optional[Decimal] = None
        self._range_formed = False
        self._range_invalid = False  # True if range pct out of bounds
        self._trades_today = 0

        # Computed range end time: 09:15 + range_minutes
        end_min = 15 + self._range_minutes  # minutes past 09:00
        self._range_end_time = dt_time(9, end_min)

    def reset_day(self) -> None:
        """Reset per-day state."""
        self._range_high = None
        self._range_low = None
        self._range_formed = False
        self._range_invalid = False
        self._trades_today = 0

    def evaluate(
        self, candle: Candle, candle_buffer: list[Candle],
    ) -> Optional[Signal]:
        """Evaluate ORB signal on current candle."""
        ct = candle.candle_time.time()

        # Before market open
        if ct < dt_time(9, 15):
            return None

        # Range formation phase
        if ct < self._range_end_time:
            if self._range_high is None:
                self._range_high = candle.high
                self._range_low = candle.low
            else:
                self._range_high = max(self._range_high, candle.high)
                self._range_low = min(self._range_low, candle.low)
            return None

        # First candle after range — finalize
        if not self._range_formed and not self._range_invalid:
            if self._range_high is None or self._range_low is None:
                self._range_invalid = True
                return None
            self._range_formed = True
            midpoint = (self._range_high + self._range_low) / Decimal("2")
            if midpoint == 0:
                self._range_invalid = True
                return None
            range_pct = (self._range_high - self._range_low) / midpoint
            if range_pct < self._min_range_pct or range_pct > self._max_range_pct:
                self._range_invalid = True
                log.debug(
                    "orb_range_filtered",
                    range_pct=float(range_pct),
                    min_pct=float(self._min_range_pct),
                    max_pct=float(self._max_range_pct),
                )
                return None

        # Range invalid — skip rest of day
        if self._range_invalid:
            return None

        # Max trades gate
        if self._trades_today >= self._max_trades_per_day:
            return None

        # Time cutoff
        if ct >= self._no_entry_after:
            return None

        # Breakout detection (close-based, not wick)
        direction: Optional[str] = None
        if candle.close > self._range_high:
            direction = "LONG"
        elif candle.close < self._range_low:
            direction = "SHORT"

        if direction is None:
            return None

        # Volume confirmation
        vol_sma = compute_volume_sma(candle_buffer, 20)
        if vol_sma is not None and vol_sma > 0:
            vol_ratio = Decimal(str(candle.volume)) / vol_sma
            if vol_ratio < self._volume_ratio_min:
                return None
        else:
            vol_ratio = Decimal("0")

        # Entry, stop, target
        entry = candle.close
        range_width = self._range_high - self._range_low

        if direction == "LONG":
            if self._stop_mode == "midpoint":
                stop = (self._range_high + self._range_low) / Decimal("2")
            else:  # range_end
                stop = self._range_low
            target = entry + range_width * self._target_multiplier
        else:  # SHORT
            if self._stop_mode == "midpoint":
                stop = (self._range_high + self._range_low) / Decimal("2")
            else:  # range_end
                stop = self._range_high
            target = entry - range_width * self._target_multiplier

        self._trades_today += 1

        return Signal(
            symbol=candle.symbol,
            instrument_token=candle.instrument_token,
            direction=direction,
            signal_time=candle.candle_time,
            candle_time=candle.candle_time,
            theoretical_entry=entry,
            stop_loss=stop,
            target=target,
            ema9=Decimal("0"),
            ema21=Decimal("0"),
            rsi=Decimal("0"),
            vwap=Decimal("0"),
            volume_ratio=vol_ratio,
        )


class VWAPMeanReversionStrategy:
    """VWAP SD Mean Reversion — institutional standard for index futures.

    Trades reversions from VWAP ± N*SD bands in low-ADX (ranging) markets.
    Filtered by RSI and minimum distance from VWAP.
    Max 3 trades per day.
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("strategy", {}).get("vwap_mr", {})
        self._band_mult = Decimal(str(cfg.get("band_mult", 2.0)))
        self._rsi_period = int(cfg.get("rsi_period", 14))
        self._rsi_overbought = Decimal(str(cfg.get("rsi_overbought", 65)))
        self._rsi_oversold = Decimal(str(cfg.get("rsi_oversold", 35)))
        self._adx_period = int(cfg.get("adx_period", 14))
        self._adx_max_threshold = Decimal(str(cfg.get("adx_max_threshold", 25)))
        self._atr_period = int(cfg.get("atr_period", 14))
        self._atr_stop_mult = Decimal(str(cfg.get("atr_stop_mult", 0.5)))
        self._min_distance_pct = Decimal(str(cfg.get("min_distance_pct", 0.003)))
        self._max_trades_per_day = int(cfg.get("max_trades_per_day", 3))

        no_entry_str = cfg.get("no_entry_after", "14:30")
        h, m = map(int, no_entry_str.split(":"))
        self._no_entry_after = dt_time(h, m)

        # Per-day state
        self._trades_today = 0

    def reset_day(self) -> None:
        """Reset per-day state."""
        self._trades_today = 0

    def evaluate(
        self,
        candle: Candle,
        candle_buffer: list[Candle],
        day_candles_so_far: list[Candle],
    ) -> Optional[Signal]:
        """Evaluate VWAP MR signal on current candle."""
        ct = candle.candle_time.time()

        # Time gates
        if ct < dt_time(9, 30) or ct >= self._no_entry_after:
            return None

        # Max trades gate
        if self._trades_today >= self._max_trades_per_day:
            return None

        # Need enough candles for indicators
        if len(day_candles_so_far) < 3:
            return None

        # VWAP + SD bands from day's candles
        vwap, upper, lower = compute_vwap_with_bands(day_candles_so_far, self._band_mult)

        # ADX regime filter — suppress in trending markets
        adx = compute_adx(candle_buffer, self._adx_period)
        if adx is not None and adx >= self._adx_max_threshold:
            return None

        # RSI filter
        rsi = compute_rsi(candle_buffer, self._rsi_period)
        if rsi is None:
            return None

        # Signal detection
        direction: Optional[str] = None
        if candle.close <= lower and rsi < self._rsi_oversold:
            direction = "LONG"
        elif candle.close >= upper and rsi > self._rsi_overbought:
            direction = "SHORT"

        if direction is None:
            return None

        # Minimum distance check
        if vwap == 0:
            return None
        distance_pct = abs(candle.close - vwap) / vwap
        if distance_pct < self._min_distance_pct:
            return None

        # ATR for stop
        atr = compute_atr(candle_buffer, self._atr_period)
        if atr == 0:
            return None

        entry = candle.close
        if direction == "LONG":
            stop = entry - self._atr_stop_mult * atr
            target = vwap  # Mean reversion target
        else:  # SHORT
            stop = entry + self._atr_stop_mult * atr
            target = vwap

        self._trades_today += 1

        return Signal(
            symbol=candle.symbol,
            instrument_token=candle.instrument_token,
            direction=direction,
            signal_time=candle.candle_time,
            candle_time=candle.candle_time,
            theoretical_entry=entry,
            stop_loss=stop,
            target=target,
            ema9=Decimal("0"),
            ema21=Decimal("0"),
            rsi=rsi,
            vwap=vwap,
            volume_ratio=Decimal("0"),
        )


class MACDSupertrendStrategy:
    """MACD + Supertrend — multi-timeframe trend strategy for index futures.

    Daily Supertrend sets directional bias (LONG/SHORT only).
    Intraday MACD crossover + 50 EMA filter for entries.
    Max 2 trades per day.
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("strategy", {}).get("macd_st", {})
        self._st_daily_period = int(cfg.get("st_daily_period", 10))
        self._st_daily_multiplier = Decimal(str(cfg.get("st_daily_multiplier", 3.0)))
        self._macd_fast = int(cfg.get("macd_fast", 12))
        self._macd_slow = int(cfg.get("macd_slow", 26))
        self._macd_signal = int(cfg.get("macd_signal", 9))
        self._ema_trend_period = int(cfg.get("ema_trend_period", 50))
        self._st_intraday_period = int(cfg.get("st_intraday_period", 10))
        self._st_intraday_mult = Decimal(str(cfg.get("st_intraday_multiplier", 2.0)))
        self._atr_period = int(cfg.get("atr_period", 14))
        self._atr_target_mult = Decimal(str(cfg.get("atr_target_mult", 2.5)))
        self._exit_mode = cfg.get("exit_mode", "supertrend_trail")
        self._max_trades_per_day = int(cfg.get("max_trades_per_day", 2))

        no_entry_str = cfg.get("no_entry_after", "14:00")
        h, m = map(int, no_entry_str.split(":"))
        self._no_entry_after = dt_time(h, m)

        # Per-day state
        self._daily_bias: Optional[str] = None
        self._trades_today = 0
        self._prev_histogram: Optional[Decimal] = None

    def reset_day(self) -> None:
        """Reset per-day state."""
        self._daily_bias = None
        self._trades_today = 0
        self._prev_histogram = None

    def set_daily_bias(self, daily_candles: list[Candle]) -> Optional[str]:
        """Set daily directional bias from daily Supertrend.

        Returns "LONG", "SHORT", or None (skip day).
        """
        result = compute_supertrend(
            daily_candles, self._st_daily_period, self._st_daily_multiplier,
        )
        if result is None:
            self._daily_bias = None
            return None

        _value, direction = result
        if direction == 1:
            self._daily_bias = "LONG"
        else:
            self._daily_bias = "SHORT"

        log.debug(
            "macd_st_daily_bias",
            bias=self._daily_bias,
            daily_candles=len(daily_candles),
        )
        return self._daily_bias

    def evaluate(
        self, candle: Candle, candle_buffer: list[Candle],
    ) -> Optional[Signal]:
        """Evaluate MACD + Supertrend signal on current candle."""
        ct = candle.candle_time.time()

        # Time gates
        if ct < dt_time(9, 30) or ct >= self._no_entry_after:
            return None

        # Daily bias required
        if self._daily_bias is None:
            return None

        # Max trades gate
        if self._trades_today >= self._max_trades_per_day:
            return None

        # 50 EMA trend filter
        closes = [c.close for c in candle_buffer]
        ema50 = compute_ema(closes, self._ema_trend_period)
        if ema50 is None:
            return None

        if self._daily_bias == "LONG" and candle.close <= ema50:
            return None
        if self._daily_bias == "SHORT" and candle.close >= ema50:
            return None

        # MACD crossover detection
        macd_result = compute_macd(
            candle_buffer, self._macd_fast, self._macd_slow, self._macd_signal,
        )
        if macd_result is None:
            return None

        _macd_line, _signal_line, histogram = macd_result

        # Need previous histogram for crossover detection
        if self._prev_histogram is None:
            self._prev_histogram = histogram
            return None

        # Detect crossover
        direction: Optional[str] = None
        if self._prev_histogram < 0 and histogram >= 0:
            direction = "LONG"
        elif self._prev_histogram > 0 and histogram <= 0:
            direction = "SHORT"

        self._prev_histogram = histogram

        if direction is None:
            return None

        # Direction must agree with daily bias
        if direction != self._daily_bias:
            return None

        # Entry, stop, target
        entry = candle.close

        # Stop from intraday Supertrend
        st_result = compute_supertrend(
            candle_buffer, self._st_intraday_period, self._st_intraday_mult,
        )
        if st_result is not None:
            st_value, _ = st_result
            stop = st_value
        else:
            # Fallback: ATR-based stop
            atr = compute_atr(candle_buffer, self._atr_period)
            if direction == "LONG":
                stop = entry - atr * Decimal("1.5")
            else:
                stop = entry + atr * Decimal("1.5")

        # Target: ATR-based
        atr = compute_atr(candle_buffer, self._atr_period)
        if atr == 0:
            return None

        if direction == "LONG":
            target = entry + self._atr_target_mult * atr
        else:
            target = entry - self._atr_target_mult * atr

        self._trades_today += 1

        return Signal(
            symbol=candle.symbol,
            instrument_token=candle.instrument_token,
            direction=direction,
            signal_time=candle.candle_time,
            candle_time=candle.candle_time,
            theoretical_entry=entry,
            stop_loss=stop,
            target=target,
            ema9=Decimal("0"),
            ema21=ema50,
            rsi=Decimal("0"),
            vwap=Decimal("0"),
            volume_ratio=Decimal("0"),
        )
