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
