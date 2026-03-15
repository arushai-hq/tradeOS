"""Tests for RegimeDetector — classification logic, signal gates, resilience."""
from __future__ import annotations

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from core.regime_detector.regime_detector import (
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
        with patch("utils.telegram.send_telegram", new_callable=AsyncMock) as mock_tg:
            for _ in range(3):
                await detector.refresh()
            assert mock_tg.call_count == 1
            assert "degraded" in mock_tg.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_regime_change_triggers_telegram(self):
        """Regime change from BULL -> BEAR triggers Telegram alert."""
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
        """nifty_price=0 -> validation fails -> last regime preserved."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._last_nifty_price = 0.0
        detector._classify_and_update("test")
        assert detector.current_regime() == MarketRegime.BULL_TREND

    def test_insufficient_history_warns_but_continues(self):
        """< 200 candles -> still classifies (uses available data)."""
        result = classify_regime(
            nifty_price=22000.0,
            nifty_ema200=21500.0,
            vix=12.0,
            intraday_drop_pct=0.5,
            intraday_range_pct=0.8,
        )
        assert result == MarketRegime.BULL_TREND

    def test_invalid_vix_keeps_last_regime(self):
        """VIX=150 -> validation fails -> last regime preserved."""
        detector = self._make_initialized_detector(MarketRegime.BULL_TREND)
        detector._last_vix = 150.0
        detector._classify_and_update("test")
        assert detector.current_regime() == MarketRegime.BULL_TREND

    @pytest.mark.asyncio
    async def test_initialize_before_market_open(self):
        """Empty intraday (pre-market) -> no raise, valid regime returned."""
        import structlog.testing

        # Build daily candles: 60 simple close values for EMA
        daily_candles = [
            {"open": 22000.0, "high": 22100.0, "low": 21900.0, "close": 22000.0}
            for _ in range(60)
        ]
        vix_candles = [
            {"open": 14.0, "high": 15.0, "low": 13.5, "close": 14.0}
        ]

        def fake_historical(token, _from_dt, _to_dt, interval, _continuous):
            if interval == "minute":
                return []          # pre-market: no intraday data
            if token == 264969:    # INDIA_VIX_TOKEN
                return vix_candles
            return daily_candles   # daily Nifty

        mock_kite = MagicMock()
        mock_kite.historical_data.side_effect = fake_historical

        detector = RegimeDetector(
            kite=mock_kite,
            config={},
            shared_state={},
            secrets={},
        )

        with structlog.testing.capture_logs() as cap_logs:
            result = await detector.initialize()

        assert isinstance(result, MarketRegime), "initialize() must return a MarketRegime"
        assert result is not None

        events = [entry.get("event") for entry in cap_logs]
        assert "nifty_intraday_unavailable" in events, (
            f"Expected 'nifty_intraday_unavailable' in log events, got: {events}"
        )

    @pytest.mark.asyncio
    async def test_vix_unavailable_pre_market(self):
        """Empty VIX data (pre-market) -> no raise, valid regime returned."""
        import structlog.testing

        daily_candles = [
            {"open": 22000.0, "high": 22100.0, "low": 21900.0, "close": 22000.0}
            for _ in range(60)
        ]

        def fake_historical(token, _from_dt, _to_dt, interval, _continuous):
            if token == 264969:  # INDIA_VIX_TOKEN — empty pre-market
                return []
            if interval == "minute":
                return []
            return daily_candles

        mock_kite = MagicMock()
        mock_kite.historical_data.side_effect = fake_historical

        detector = RegimeDetector(
            kite=mock_kite, config={}, shared_state={}, secrets={},
        )

        with structlog.testing.capture_logs() as cap_logs:
            result = await detector.initialize()

        assert isinstance(result, MarketRegime)
        events = [entry.get("event") for entry in cap_logs]
        assert "vix_data_unavailable" in events, (
            f"Expected 'vix_data_unavailable' in log events, got: {events}"
        )

    @pytest.mark.asyncio
    async def test_all_data_unavailable_pre_market(self):
        """All three fetches empty (pre-market) -> BULL_TREND, three warnings."""
        import structlog.testing

        mock_kite = MagicMock()
        mock_kite.historical_data.return_value = []  # all fetches return empty

        detector = RegimeDetector(
            kite=mock_kite, config={}, shared_state={}, secrets={},
        )

        with structlog.testing.capture_logs() as cap_logs:
            result = await detector.initialize()

        assert result == MarketRegime.BULL_TREND, (
            f"Expected BULL_TREND with all-empty data, got {result}"
        )
        events = [entry.get("event") for entry in cap_logs]
        assert "nifty_ema_data_unavailable" in events, f"Missing nifty_ema_data_unavailable in {events}"
        assert "nifty_intraday_unavailable" in events, f"Missing nifty_intraday_unavailable in {events}"
        assert "vix_data_unavailable" in events, f"Missing vix_data_unavailable in {events}"
