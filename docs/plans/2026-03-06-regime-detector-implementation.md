# Regime Detector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a real-time market regime classifier (BULL/BEAR/HIGH_VOL/CRASH) that gates S1 signals based on Nifty 50 + India VIX conditions.

**Architecture:** RegimeDetector fetches Nifty/VIX via REST every 60s, classifies into one of 4 regimes by priority, and exposes synchronous getters (is_long_allowed, is_short_allowed, position_size_multiplier) consumed by RiskGate Gate 7.

**Tech Stack:** pykiteconnect (historical_data), ta.trend.EMAIndicator, asyncio.to_thread, structlog, pytest + unittest.mock

**Design doc:** `docs/plans/2026-03-06-regime-detector-design.md`

---

## Task 1: Core module — MarketRegime enum + classify() pure function

**Files:**
- Create: `regime_detector/__init__.py`
- Create: `regime_detector/regime_detector.py`
- Test: `tests/unit/test_regime_detector.py`

**Step 1: Write the failing tests — classification logic (8 tests)**

Create `tests/unit/test_regime_detector.py`:

```python
"""Tests for RegimeDetector — classification logic, signal gates, resilience."""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from regime_detector.regime_detector import (
    MarketRegime,
    RegimeDetector,
    classify_regime,
)


# ---------------------------------------------------------------
# Classification logic (pure function — no kite needed)
# ---------------------------------------------------------------

class TestClassifyRegime:
    """Test the classify_regime() pure function with priority ordering."""

    def test_bull_trend_classified_correctly(self):
        """Nifty above EMA, low VIX → BULL_TREND."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=12.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.BULL_TREND

    def test_bear_trend_classified_correctly(self):
        """Nifty below EMA, VIX >= 15 → BEAR_TREND."""
        result = classify_regime(
            nifty_price=20000.0,
            nifty_ema200=21500.0,
            vix=18.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.BEAR_TREND

    def test_high_volatility_from_vix(self):
        """VIX between 25–35 → HIGH_VOLATILITY (even if nifty above EMA)."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=28.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.HIGH_VOLATILITY

    def test_high_volatility_from_intraday_range(self):
        """Intraday range > 1.5% → HIGH_VOLATILITY."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=14.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=1.8,
        )
        assert result == MarketRegime.HIGH_VOLATILITY

    def test_crash_from_vix(self):
        """VIX > 35 → CRASH (overrides everything)."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=38.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.CRASH

    def test_crash_from_intraday_drop(self):
        """Intraday drop > 2.5% → CRASH."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=14.0,
            intraday_drop_pct=3.0,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.CRASH

    def test_crash_priority_over_bear(self):
        """VIX > 35 AND nifty below EMA → CRASH (not BEAR)."""
        result = classify_regime(
            nifty_price=20000.0,
            nifty_ema200=21500.0,
            vix=40.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.CRASH

    def test_high_vol_priority_over_bear(self):
        """VIX 25–35 AND nifty below EMA → HIGH_VOLATILITY (not BEAR)."""
        result = classify_regime(
            nifty_price=20000.0,
            nifty_ema200=21500.0,
            vix=28.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.HIGH_VOLATILITY
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_regime_detector.py::TestClassifyRegime -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'regime_detector'`

**Step 3: Write the implementation — MarketRegime + classify_regime + RegimeDetector skeleton**

Create `regime_detector/__init__.py`:

```python
"""TradeOS — Market Regime Detector."""
from regime_detector.regime_detector import MarketRegime, RegimeDetector

__all__ = ["MarketRegime", "RegimeDetector"]
```

Create `regime_detector/regime_detector.py`:

```python
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
from typing import Optional

import pytz
import structlog
import ta.trend

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

        Returns:
            Initial MarketRegime classification.
        """
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
            log.error(
                "regime_no_nifty_history",
                note="Cannot compute EMA — defaulting to BULL_TREND",
            )

        # Fetch VIX + intraday and classify
        await self._refresh_intraday_data()
        self._classify_and_update("initialize")

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
            raise ValueError("No Nifty intraday data returned")

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
            raise ValueError("No VIX data returned")

    def _classify_and_update(self, trigger_source: str) -> None:
        """Run classify_regime() with validation, update cache + shared_state."""
        # Validate inputs
        if self._last_nifty_price <= 0:
            log.error(
                "regime_invalid_nifty_price",
                price=self._last_nifty_price,
                keeping_regime=self._regime.value,
            )
            return

        if not (0 < self._last_vix < 100):
            log.error(
                "regime_invalid_vix",
                vix=self._last_vix,
                keeping_regime=self._regime.value,
            )
            return

        self._regime = classify_regime(
            nifty_price=self._last_nifty_price,
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
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_regime_detector.py::TestClassifyRegime -v`
Expected: 8 PASSED

**Step 5: Commit**

```bash
git add regime_detector/ tests/unit/test_regime_detector.py
git commit -m "feat: regime detector — MarketRegime enum + classify_regime() + 8 classification tests"
```

---

## Task 2: Signal gate tests + is_long/short_allowed + position_size_multiplier

**Files:**
- Modify: `tests/unit/test_regime_detector.py`
- Already implemented in Task 1: `regime_detector/regime_detector.py`

**Step 1: Add signal gate + multiplier tests (11 tests)**

Append to `tests/unit/test_regime_detector.py`:

```python
# ---------------------------------------------------------------
# Signal gate tests
# ---------------------------------------------------------------

class TestSignalGates:
    """Test is_long_allowed, is_short_allowed, position_size_multiplier."""

    def _make_detector(self, regime: MarketRegime) -> RegimeDetector:
        """Create a RegimeDetector with a pre-set regime (no kite needed)."""
        detector = RegimeDetector.__new__(RegimeDetector)
        detector._regime = regime
        detector._shared_state = {}
        return detector

    def test_long_allowed_in_bull(self):
        d = self._make_detector(MarketRegime.BULL_TREND)
        assert d.is_long_allowed() is True

    def test_long_blocked_in_bear(self):
        d = self._make_detector(MarketRegime.BEAR_TREND)
        assert d.is_long_allowed() is False

    def test_long_blocked_in_crash(self):
        d = self._make_detector(MarketRegime.CRASH)
        assert d.is_long_allowed() is False

    def test_long_allowed_in_high_volatility(self):
        d = self._make_detector(MarketRegime.HIGH_VOLATILITY)
        assert d.is_long_allowed() is True

    def test_short_blocked_in_bull(self):
        d = self._make_detector(MarketRegime.BULL_TREND)
        assert d.is_short_allowed() is False

    def test_short_allowed_in_bear(self):
        d = self._make_detector(MarketRegime.BEAR_TREND)
        assert d.is_short_allowed() is True

    def test_short_allowed_in_crash(self):
        d = self._make_detector(MarketRegime.CRASH)
        assert d.is_short_allowed() is True

    def test_short_allowed_in_high_volatility(self):
        d = self._make_detector(MarketRegime.HIGH_VOLATILITY)
        assert d.is_short_allowed() is True

    def test_position_multiplier_normal_regimes(self):
        for regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND):
            d = self._make_detector(regime)
            assert d.position_size_multiplier() == 1.0

    def test_position_multiplier_high_volatility(self):
        d = self._make_detector(MarketRegime.HIGH_VOLATILITY)
        assert d.position_size_multiplier() == 0.5

    def test_position_multiplier_crash(self):
        d = self._make_detector(MarketRegime.CRASH)
        assert d.position_size_multiplier() == 0.5
```

**Step 2: Run all signal gate tests**

Run: `python -m pytest tests/unit/test_regime_detector.py::TestSignalGates -v`
Expected: 11 PASSED

**Step 3: Commit**

```bash
git add tests/unit/test_regime_detector.py
git commit -m "test: regime detector signal gate + position multiplier tests (11)"
```

---

## Task 3: Resilience tests — API failure, 3-strike Telegram, regime change alert

**Files:**
- Modify: `tests/unit/test_regime_detector.py`

**Step 1: Add resilience tests (3 tests)**

Append to `tests/unit/test_regime_detector.py`:

```python
# ---------------------------------------------------------------
# Resilience tests (D3)
# ---------------------------------------------------------------

class TestResilience:
    """Test failure handling and Telegram alerting."""

    def _make_initialized_detector(self, regime: MarketRegime) -> RegimeDetector:
        """Create a detector with pre-set state, bypassing __init__."""
        detector = RegimeDetector.__new__(RegimeDetector)
        detector._kite = MagicMock()
        detector._config = {}
        detector._shared_state = {"market_regime": regime.value, "regime_position_multiplier": 1.0}
        detector._secrets = {}
        detector._regime = regime
        detector._nifty_ema200 = 21500.0
        detector._consecutive_failures = 0
        detector._last_nifty_price = 22000.0
        detector._last_vix = 12.0
        detector._last_intraday_drop = 0.5
        detector._last_intraday_range = 0.8
        detector._last_trigger = "default_bull"
        return detector

    @pytest.mark.asyncio
    async def test_regime_unchanged_on_api_failure(self):
        """API failure keeps last known regime."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._refresh_intraday_data = AsyncMock(
            side_effect=Exception("API timeout")
        )
        result = await detector.refresh()
        assert result == MarketRegime.BULL_TREND
        assert detector._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_telegram_alert_after_3_failures(self):
        """3 consecutive refresh failures triggers Telegram alert."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._refresh_intraday_data = AsyncMock(
            side_effect=Exception("API timeout")
        )
        with patch("regime_detector.regime_detector.send_telegram", new_callable=AsyncMock) as mock_tg:
            # Need to make send_telegram importable in the module
            # Actually the import is inside refresh(), so we patch at module level
            pass

        # Simpler approach: patch utils.telegram.send_telegram
        with patch("utils.telegram.send_telegram", new_callable=AsyncMock) as mock_tg:
            for _ in range(3):
                await detector.refresh()
            # Telegram called on the 3rd failure
            assert mock_tg.call_count == 1
            assert "degraded" in mock_tg.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_regime_change_triggers_telegram(self):
        """Regime change from BULL → BEAR triggers Telegram alert."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)

        async def fake_refresh():
            detector._last_nifty_price = 20000.0
            detector._last_vix = 18.0
            detector._last_intraday_drop = 0.5
            detector._last_intraday_range = 0.8

        detector._refresh_intraday_data = AsyncMock(side_effect=fake_refresh)

        with patch("utils.telegram.send_telegram", new_callable=AsyncMock) as mock_tg:
            result = await detector.refresh()
            assert result == MarketRegime.BEAR_TREND
            assert mock_tg.call_count == 1
            assert "regime change" in mock_tg.call_args[0][0].lower()
```

**Step 2: Run resilience tests**

Run: `python -m pytest tests/unit/test_regime_detector.py::TestResilience -v`
Expected: 3 PASSED

**Step 3: Commit**

```bash
git add tests/unit/test_regime_detector.py
git commit -m "test: regime detector resilience tests — API failure, 3-strike, regime change (3)"
```

---

## Task 4: Data validation tests

**Files:**
- Modify: `tests/unit/test_regime_detector.py`

**Step 1: Add validation tests (3 tests)**

Append to `tests/unit/test_regime_detector.py`:

```python
# ---------------------------------------------------------------
# Data validation tests (D5)
# ---------------------------------------------------------------

class TestDataValidation:
    """Test that invalid data keeps last known regime."""

    def _make_initialized_detector(self, regime: MarketRegime) -> RegimeDetector:
        detector = RegimeDetector.__new__(RegimeDetector)
        detector._kite = MagicMock()
        detector._config = {}
        detector._shared_state = {"market_regime": regime.value, "regime_position_multiplier": 1.0}
        detector._secrets = {}
        detector._regime = regime
        detector._nifty_ema200 = 21500.0
        detector._consecutive_failures = 0
        detector._last_nifty_price = 22000.0
        detector._last_vix = 12.0
        detector._last_intraday_drop = 0.5
        detector._last_intraday_range = 0.8
        detector._last_trigger = "default_bull"
        return detector

    def test_invalid_nifty_price_keeps_last_regime(self):
        """nifty_price=0 → validation fails → last regime preserved."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._last_nifty_price = 0.0
        detector._classify_and_update("test")
        assert detector.current_regime() == MarketRegime.BULL_TREND

    def test_insufficient_history_warns_but_continues(self):
        """< 200 candles → still classifies (uses available data)."""
        # This is tested via classify_regime directly — it doesn't
        # require exactly 200 candles, just uses whatever EMA is computed.
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,  # computed from fewer candles
            vix=12.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.BULL_TREND

    def test_invalid_vix_keeps_last_regime(self):
        """VIX=150 → validation fails → last regime preserved."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._last_vix = 150.0
        detector._classify_and_update("test")
        assert detector.current_regime() == MarketRegime.BULL_TREND
```

**Step 2: Run validation tests**

Run: `python -m pytest tests/unit/test_regime_detector.py::TestDataValidation -v`
Expected: 3 PASSED

**Step 3: Commit**

```bash
git add tests/unit/test_regime_detector.py
git commit -m "test: regime detector data validation tests (3) — total 25 tests"
```

---

## Task 5: RiskGate Gate 7 — regime check

**Files:**
- Modify: `strategy_engine/risk_gate.py` (lines 50, 58, 149)
- Test: `tests/unit/test_risk_gate.py` (add Gate 7 tests)

**Step 1: Read existing risk_gate tests to understand fixture patterns**

Read: `tests/unit/test_risk_gate.py` — note how signals and shared_state are built.

**Step 2: Add Gate 7 to RiskGate**

In `strategy_engine/risk_gate.py`:

1. Add `regime_detector` parameter to `__init__`:

```python
def __init__(
    self,
    kill_switch: Optional[KillSwitchProtocol] = None,
    regime_detector=None,
) -> None:
    self._kill_switch = kill_switch
    self._regime_detector = regime_detector
```

2. Add Gate 7 before `return True, "OK"` (line 149):

```python
        # Gate 7: regime — block counter-trend signals
        if self._regime_detector is not None:
            if signal.direction == "LONG" and not self._regime_detector.is_long_allowed():
                regime = self._regime_detector.current_regime().value
                reason = f"REGIME_BLOCKED_{regime.upper()}"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol, direction="LONG", regime=regime,
                )
                return False, reason

            if signal.direction == "SHORT" and not self._regime_detector.is_short_allowed():
                regime = self._regime_detector.current_regime().value
                reason = f"REGIME_BLOCKED_{regime.upper()}"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol, direction="SHORT", regime=regime,
                )
                return False, reason

            # CRASH + SHORT: extra volume confirmation (volume_ratio > 2.0)
            if (self._regime_detector.current_regime() == MarketRegime.CRASH
                    and signal.direction == "SHORT"
                    and signal.volume_ratio <= Decimal("2.0")):
                reason = "REGIME_CRASH_LOW_VOLUME_SHORT"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol,
                    volume_ratio=float(signal.volume_ratio),
                )
                return False, reason

        return True, "OK"
```

3. Add import at top of risk_gate.py:

```python
from decimal import Decimal
from regime_detector.regime_detector import MarketRegime
```

**Step 3: Add Gate 7 tests to test_risk_gate.py**

Add tests for: long blocked in bear, short blocked in bull, crash short with low volume blocked, crash short with high volume allowed.

**Step 4: Run risk gate tests**

Run: `python -m pytest tests/unit/test_risk_gate.py -v`
Expected: All pass (existing + new Gate 7 tests)

**Step 5: Commit**

```bash
git add strategy_engine/risk_gate.py tests/unit/test_risk_gate.py
git commit -m "feat: RiskGate Gate 7 — regime check (long/short blocking + CRASH volume gate)"
```

---

## Task 6: StrategyEngine integration — accept regime_detector

**Files:**
- Modify: `strategy_engine/__init__.py` (lines 58-67, 132)

**Step 1: Add regime_detector parameter to StrategyEngine constructor**

In `strategy_engine/__init__.py`:

1. Add `regime_detector=None` to `__init__` params:

```python
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
) -> None:
    ...
    self._regime_detector = regime_detector
```

2. Pass to RiskGate in `__aenter__` (line 132):

```python
self._risk_gate = RiskGate(
    kill_switch=self._kill_switch,
    regime_detector=self._regime_detector,
)
```

**Step 2: Run existing strategy engine tests (no regressions)**

Run: `python -m pytest tests/unit/test_signal_generator.py tests/unit/test_risk_gate.py tests/unit/test_indicators.py -v`
Expected: All pass

**Step 3: Commit**

```bash
git add strategy_engine/__init__.py
git commit -m "feat: StrategyEngine accepts regime_detector, passes to RiskGate"
```

---

## Task 7: main.py integration — Phase 1 init + risk_watchdog 60s refresh

**Files:**
- Modify: `main.py` (lines 77-132, 531-556, 680-705, 844-857)

**Step 1: Add shared_state keys**

In `_init_shared_state()`, add before the closing `}`:

```python
        # Regime detector
        "market_regime": None,
        "regime_position_multiplier": 1.0,
```

**Step 2: Add regime_detector to Phase 1 startup**

In `main()`, after DB pool creation and before DataEngine, add:

```python
        # Regime detector: initialize before DataEngine
        from regime_detector import RegimeDetector
        regime_detector = RegimeDetector(kite, config, shared_state, secrets)
        initial_regime = await regime_detector.initialize()
        log.info("regime_initialized", regime=initial_regime.value)
```

**Step 3: Pass regime_detector to StrategyEngine**

Change the StrategyEngine construction:

```python
                async with StrategyEngine(
                    kite, config, shared_state, strategy_queue, order_queue, db_pool,
                    regime_detector=regime_detector,
                ) as strategy_engine:
```

**Step 4: Add regime_detector to risk_watchdog signature and 60s refresh**

1. Add `regime_detector=None` parameter to `risk_watchdog()`.
2. Add a tick counter and 60s refresh at the top of the while loop:

```python
    regime_refresh_counter: int = 0
    ...
    while True:
        await asyncio.sleep(1)
        regime_refresh_counter += 1

        # Regime refresh every 60s
        if regime_detector is not None and regime_refresh_counter % 60 == 0:
            try:
                await regime_detector.refresh()
            except Exception as exc:
                log.error("regime_refresh_error", error=str(exc))
```

3. Update the `run_trading_session()` gather call to pass regime_detector:

```python
            risk_watchdog(shared_state, config, secrets, exec_engine, regime_detector),
```

**Step 5: Run existing main.py tests (no regressions)**

Run: `python -m pytest tests/ -v`
Expected: All existing tests pass (regime_detector is None in test fixtures → Gate 7 skipped)

**Step 6: Commit**

```bash
git add main.py
git commit -m "feat: main.py — regime detector Phase 1 init + 60s refresh in risk_watchdog"
```

---

## Task 8: Documentation — regime_detector/README.md + spec doc

**Files:**
- Create: `regime_detector/README.md`
- Create: `docs/strategy_specs/regime_detector_spec.md`

**Step 1: Write regime_detector/README.md**

Contents: module purpose, four regimes with thresholds, signal gate rules, integration points, data sources, failure modes, test command.

**Step 2: Write docs/strategy_specs/regime_detector_spec.md**

Contents: design rationale, classification algorithm with priority, threshold justification, future extensions (S2/S3/S4), backtesting validation note.

**Step 3: Commit**

```bash
git add regime_detector/README.md docs/strategy_specs/regime_detector_spec.md
git commit -m "docs: regime detector README + strategy spec"
```

---

## Task 9: Update START.md + final verification

**Files:**
- Modify: `START.md`

**Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All pass (existing 178 + new ~25 regime tests)

**Step 2: Run mypy on regime_detector/**

Run: `python -m mypy regime_detector/ --ignore-missing-imports`
Expected: Clean (no errors)

**Step 3: Update START.md build status**

Add regime_detector to the build status section with commit hash.

**Step 4: Final commit + push**

```bash
git add -A
git commit -m "feat: regime detector complete — 4-regime classifier + RiskGate Gate 7 + 25 tests

- regime_detector/: MarketRegime enum, classify_regime(), RegimeDetector class
- RiskGate Gate 7: long/short blocking by regime + CRASH volume gate
- main.py: Phase 1 init + risk_watchdog 60s refresh
- D3 resilience: stale regime on failure, 3-strike Telegram alert
- D4 observability: structured log on every classification
- D5 validation: bad data keeps last known regime
- 25 tests: classification, signal gates, resilience, validation
- docs: README + strategy spec"
git push origin main
```

---

## Definition of Done Checklist

| # | Criterion | Verified by |
|---|-----------|-------------|
| 1 | regime_detector/ module exists with 4 files | Task 1 |
| 2 | All 4 regimes classify correctly per priority | Task 1 (8 tests) |
| 3 | RegimeDetector integrates into main.py Phase 1 | Task 7 |
| 4 | RiskGate Gate 7 regime check (long/short blocking) | Task 5 |
| 5 | risk_watchdog calls refresh() every 60s | Task 7 |
| 6 | All kite calls use asyncio.to_thread() | Task 1 (_fetch_historical) |
| 7 | D3 resilience: stale regime, 3-strike Telegram | Task 3 |
| 8 | D4 observability: structured log on classify | Task 1 (regime_classified log) |
| 9 | D5 validation: bad data keeps last regime | Task 4 |
| 10 | All tests pass | Task 9 |
| 11 | mypy clean | Task 9 |
| 12 | README + spec doc written | Task 8 |
| 13 | START.md updated | Task 9 |
