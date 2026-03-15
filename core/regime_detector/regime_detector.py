"""
TradeOS — Market Regime Detector

Classifies the market into one of four regimes based on Nifty 50 and India VIX:
  CRASH > HIGH_VOLATILITY > BEAR_TREND > BULL_TREND (priority order)

Two call points:
  1. Session start (Phase 1): initialize() — loads 200-day EMA + VIX + intraday
  2. Every 60s (Phase 2): refresh() — re-fetches intraday + VIX, re-classifies

All kite API calls use asyncio.to_thread() (D6 rule).
On failure: keep last known regime, alert after 3 consecutive failures (D3 rule).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from enum import Enum

import pytz
import structlog
import ta.trend

from utils.time_utils import is_market_hours

import pandas as pd

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

NIFTY_50_TOKEN: int = 256265
INDIA_VIX_TOKEN: int = 264969

# Classification thresholds
VIX_CRASH_THRESHOLD: float = 35.0
VIX_HIGH_VOL_MIN: float = 25.0
VIX_HIGH_VOL_MAX: float = 35.0
VIX_BEAR_MIN: float = 15.0
INTRADAY_DROP_CRASH_PCT: float = 2.5
INTRADAY_RANGE_HIGH_VOL_PCT: float = 1.5
EMA_PERIOD: int = 200

# Resilience
MAX_CONSECUTIVE_FAILURES: int = 3


class MarketRegime(Enum):
    """Four market regimes, evaluated in priority order."""
    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    HIGH_VOLATILITY = "high_volatility"
    CRASH = "crash"


def classify_regime(
    nifty_price: float,
    nifty_ema200: float,
    vix: float,
    intraday_drop_pct: float,
    intraday_range_pct: float,
) -> MarketRegime:
    """
    Pure classification function. Evaluate in strict priority order.

    Args:
        nifty_price: Nifty 50 last price.
        nifty_ema200: Nifty 50 200-period EMA (daily candles).
        vix: India VIX current level.
        intraday_drop_pct: Nifty intraday drop from day open (positive = drop).
        intraday_range_pct: Nifty intraday (high - low) / open * 100.

    Returns:
        MarketRegime classification.
    """
    # Priority 1: CRASH
    if vix > VIX_CRASH_THRESHOLD or intraday_drop_pct > INTRADAY_DROP_CRASH_PCT:
        return MarketRegime.CRASH

    # Priority 2: HIGH_VOLATILITY
    if (VIX_HIGH_VOL_MIN <= vix <= VIX_HIGH_VOL_MAX
            or intraday_range_pct > INTRADAY_RANGE_HIGH_VOL_PCT):
        return MarketRegime.HIGH_VOLATILITY

    # Priority 3: BEAR_TREND
    if nifty_price < nifty_ema200 and vix >= VIX_BEAR_MIN:
        return MarketRegime.BEAR_TREND

    # Priority 4: BULL_TREND (default)
    return MarketRegime.BULL_TREND


class RegimeDetector:
    """
    Market regime classifier for TradeOS.

    Fetches Nifty 50 and India VIX data via kite.historical_data() REST API.
    Caches the current regime for synchronous access from signal_processor.
    """

    def __init__(self, kite, config: dict, shared_state: dict, secrets: dict) -> None:
        self._kite = kite
        self._config = config
        self._shared_state = shared_state
        self._secrets = secrets

        self._regime: MarketRegime = MarketRegime.BULL_TREND
        self._nifty_ema200: float = 0.0
        self._consecutive_failures: int = 0
        self._initialized: bool = False

        # Cached data for logging
        self._last_nifty_price: float = 0.0
        self._last_vix: float = 0.0
        self._last_intraday_drop: float = 0.0
        self._last_intraday_range: float = 0.0
        self._last_trigger: str = ""

    async def initialize(self) -> MarketRegime:
        """
        Phase 1 startup: fetch 200-day Nifty data, compute EMA, fetch VIX,
        fetch intraday data, classify regime.

        B11 guard: if already initialized, skip and return cached regime.

        Returns:
            Initial MarketRegime classification.
        """
        if self._initialized:
            log.debug("regime_already_initialized", regime=self._regime.value)
            return self._regime

        # Fetch 200 daily candles for EMA
        today = datetime.now(IST).date()
        from_date = today - timedelta(days=365)  # ~200 trading days in a year
        nifty_daily = await self._fetch_historical(
            NIFTY_50_TOKEN, from_date, today, "day"
        )

        if nifty_daily and len(nifty_daily) >= 50:
            closes = pd.Series([float(c["close"]) for c in nifty_daily])
            ema_series = ta.trend.EMAIndicator(
                close=closes, window=min(EMA_PERIOD, len(closes)), fillna=False
            ).ema_indicator()
            ema_val = ema_series.iloc[-1]
            if not pd.isna(ema_val):
                self._nifty_ema200 = float(ema_val)

            if len(nifty_daily) < EMA_PERIOD:
                log.warning(
                    "regime_insufficient_history",
                    available=len(nifty_daily),
                    required=EMA_PERIOD,
                    note="Using available data for EMA calculation",
                )
        else:
            # Daily data empty or insufficient — pre-market or data unavailable.
            # EMA stays 0.0; _classify_and_update uses VIX-only classification.
            log.warning(
                "nifty_ema_data_unavailable",
                note="market not open yet or insufficient history — EMA set to 0.0",
            )

        # Fetch VIX + intraday and classify
        await self._refresh_intraday_data()
        self._classify_and_update("initialize")

        self._initialized = True

        log.info(
            "regime_initialized",
            regime=self._regime.value,
            nifty_ema200=round(self._nifty_ema200, 2),
            nifty_price=round(self._last_nifty_price, 2),
            vix=round(self._last_vix, 2),
        )

        return self._regime

    async def refresh(self) -> MarketRegime:
        """
        Phase 2 runtime: re-fetch intraday Nifty + VIX, re-classify.

        On API failure: keep last regime, increment failure counter.
        After 3 consecutive failures: Telegram alert.
        On success: reset failure counter.

        Returns:
            Current MarketRegime (may be stale on failure).
        """
        try:
            await self._refresh_intraday_data()
            self._consecutive_failures = 0
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning(
                "regime_refresh_failed",
                error=str(exc),
                consecutive_failures=self._consecutive_failures,
                keeping_regime=self._regime.value,
            )
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                from utils.telegram import send_telegram
                await send_telegram(
                    f"⚠️ Regime detector degraded — {self._consecutive_failures} "
                    f"consecutive failures. Using stale regime: {self._regime.value}",
                    self._shared_state,
                    self._secrets,
                )
            return self._regime

        old_regime = self._regime
        self._classify_and_update("refresh")

        if self._regime != old_regime:
            log.warning(
                "regime_changed",
                old_regime=old_regime.value,
                new_regime=self._regime.value,
                nifty_price=round(self._last_nifty_price, 2),
                vix=round(self._last_vix, 2),
            )
            from utils.telegram import send_telegram
            await send_telegram(
                f"📊 Regime change: {old_regime.value} → {self._regime.value}\n"
                f"Nifty: {self._last_nifty_price:.0f} | VIX: {self._last_vix:.1f}",
                self._shared_state,
                self._secrets,
            )

        return self._regime

    def current_regime(self) -> MarketRegime:
        """Synchronous getter — returns cached regime. Never fetches data."""
        return self._regime

    def is_long_allowed(self) -> bool:
        """True if current regime allows long signals."""
        return self._regime in (MarketRegime.BULL_TREND, MarketRegime.HIGH_VOLATILITY)

    def is_short_allowed(self) -> bool:
        """True if current regime allows short signals."""
        return self._regime in (
            MarketRegime.BEAR_TREND,
            MarketRegime.HIGH_VOLATILITY,
            MarketRegime.CRASH,
        )

    def position_size_multiplier(self) -> float:
        """1.0 for normal regimes, 0.5 for HIGH_VOLATILITY and CRASH."""
        if self._regime in (MarketRegime.HIGH_VOLATILITY, MarketRegime.CRASH):
            return 0.5
        return 1.0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _refresh_intraday_data(self) -> None:
        """Fetch today's intraday Nifty + VIX data."""
        today = datetime.now(IST).date()

        # Nifty intraday
        nifty_intraday = await self._fetch_historical(
            NIFTY_50_TOKEN, today, today, "minute"
        )

        if nifty_intraday:
            day_open = float(nifty_intraday[0]["open"])
            day_high = max(float(c["high"]) for c in nifty_intraday)
            day_low = min(float(c["low"]) for c in nifty_intraday)
            last_close = float(nifty_intraday[-1]["close"])

            if day_open > 0:
                self._last_nifty_price = last_close
                drop = (day_open - last_close) / day_open * 100
                self._last_intraday_drop = max(0.0, drop)
                self._last_intraday_range = (day_high - day_low) / day_open * 100
            else:
                raise ValueError("Nifty day_open is zero")
        else:
            # Market not open yet — Zerodha returns empty list before 09:15 IST.
            # Intraday fields default to 0.0; classification continues via VIX + EMA only.
            self._last_intraday_drop = 0.0
            self._last_intraday_range = 0.0
            # B10: DEBUG before market hours, WARNING during market hours
            _log = log.warning if is_market_hours() else log.debug
            _log(
                "nifty_intraday_unavailable",
                note="market not open yet — using 0.0 for intraday fields",
            )

        # VIX
        vix_data = await self._fetch_historical(
            INDIA_VIX_TOKEN, today, today, "day"
        )

        if vix_data:
            vix_val = float(vix_data[-1]["close"])
            if 0 < vix_val < 100:
                self._last_vix = vix_val
            else:
                raise ValueError(f"VIX out of range: {vix_val}")
        else:
            # Market not open yet — Zerodha returns empty VIX list before 09:15 IST.
            # Use neutral default; classification continues via EMA + intraday only.
            self._last_vix = 15.0
            # B10: DEBUG before market hours, WARNING during market hours
            _log = log.warning if is_market_hours() else log.debug
            _log(
                "vix_data_unavailable",
                note="market not open yet — using neutral default 15.0",
            )

    def _classify_and_update(self, trigger_source: str) -> None:
        """Run classify_regime() with validation, update cache + shared_state."""
        # Validate inputs
        if not (0 < self._last_vix < 100):
            log.error(
                "regime_invalid_vix",
                vix=self._last_vix,
                keeping_regime=self._regime.value,
            )
            return

        # Pre-market: nifty_price may be 0 (no intraday data yet).
        # Use EMA as price so BEAR check (price < EMA) evaluates to False,
        # and classification falls back to VIX-only (BULL/HIGH_VOL/CRASH).
        effective_price = (
            self._last_nifty_price
            if self._last_nifty_price > 0
            else self._nifty_ema200
        )

        self._regime = classify_regime(
            nifty_price=effective_price,
            nifty_ema200=self._nifty_ema200,
            vix=self._last_vix,
            intraday_drop_pct=self._last_intraday_drop,
            intraday_range_pct=self._last_intraday_range,
        )

        # Determine trigger reason for logging
        if self._regime == MarketRegime.CRASH:
            if self._last_vix > VIX_CRASH_THRESHOLD:
                self._last_trigger = "vix_above_35"
            else:
                self._last_trigger = "intraday_drop_above_2.5pct"
        elif self._regime == MarketRegime.HIGH_VOLATILITY:
            if VIX_HIGH_VOL_MIN <= self._last_vix <= VIX_HIGH_VOL_MAX:
                self._last_trigger = "vix_25_35"
            else:
                self._last_trigger = "intraday_range_above_1.5pct"
        elif self._regime == MarketRegime.BEAR_TREND:
            self._last_trigger = "nifty_below_ema200_vix_above_15"
        else:
            self._last_trigger = "default_bull"

        # Update shared_state
        self._shared_state["market_regime"] = self._regime.value
        self._shared_state["regime_position_multiplier"] = self.position_size_multiplier()

        log.info(
            "regime_classified",
            regime=self._regime.value,
            nifty_price=round(self._last_nifty_price, 2),
            nifty_ema200=round(self._nifty_ema200, 2),
            vix=round(self._last_vix, 2),
            intraday_drop_pct=round(self._last_intraday_drop, 2),
            intraday_range_pct=round(self._last_intraday_range, 2),
            trigger=self._last_trigger,
            source=trigger_source,
        )

    async def _fetch_historical(
        self,
        token: int,
        from_date: date,
        to_date: date,
        interval: str,
    ) -> list[dict]:
        """Wrapper around kite.historical_data() using asyncio.to_thread (D6)."""
        from_dt = datetime(from_date.year, from_date.month, from_date.day, 0, 0, 0)
        to_dt = datetime(to_date.year, to_date.month, to_date.day, 23, 59, 59)

        return await asyncio.to_thread(
            self._kite.historical_data,
            token,
            from_dt,
            to_dt,
            interval,
            False,  # continuous=False
        )
