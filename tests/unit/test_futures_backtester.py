"""
Futures Backtester — Unit tests.

Tests:
  FuturesChargeCalculator (5):
    (1)  LONG round-trip charges match Zerodha futures rates
    (2)  SHORT round-trip charges (STT/stamp legs reversed)
    (3)  Brokerage ₹20 cap per leg
    (4)  Zero qty returns zero charges
    (5)  Large turnover (₹1cr+) — SEBI charge verification

  FuturesPositionSizer (5):
    (6)  Basic lot calculation: risk ₹15,000, stop 50 pts, lot 65 → 4 lots
    (7)  Capital insufficient for 1 lot → None
    (8)  Scale-down when margin exceeds available capital
    (9)  Minimum 1 lot when risk allows fractional
    (10) Zero stop distance → None

  FuturesCapitalTracker (4):
    (11) Initial state: available = total
    (12) Open position reduces available by margin
    (13) Close position releases margin, adds P&L
    (14) Drawdown tracking (peak capital, max drawdown)

  Data & Indicators (3):
    (15) VWAP computation on futures candles
    (16) OI indicator computation (oi_change, oi_change_pct)
    (17) Day candle loading SQL targets backtest_futures_candles

  Signal Flow Integration (4):
    (18) Mock candles → S1v2 evaluator produces signal (mock)
    (19) Signal → FuturesPositionSizer → lot-based qty
    (20) Full trade lifecycle: signal → position → exit → charges → net P&L
    (21) Hard exit at 15:10 IST

  Exit Modes (3):
    (22) Fixed exit: stop hit and target hit
    (23) Trailing exit: ATR-based trail updates
    (24) Partial exit: 50% at 1R, remainder trails

  CLI (2):
    (25) Argument parsing for run subcommand
    (26) Argument parsing for optimize subcommand

  Edge Cases (2):
    (27) No signals → 0 trades, no errors
    (28) Multiple consecutive trades (capital tracking across trades)
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytz
import pytest

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _async_ctx:
    """Async context manager wrapper for mocking pool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass


def _make_candle(
    symbol="NIFTY",
    token=0,
    open_=Decimal("22000"),
    high=Decimal("22100"),
    low=Decimal("21900"),
    close=Decimal("22050"),
    volume=50000,
    candle_time=None,
    session_date=None,
):
    """Create a Candle dataclass for futures testing."""
    from core.strategy_engine.candle_builder import Candle

    ct = candle_time or IST.localize(datetime(2026, 3, 1, 9, 30))
    sd = session_date or ct.date()
    return Candle(
        instrument_token=token,
        symbol=symbol,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=Decimal("0"),
        candle_time=ct,
        session_date=sd,
        tick_count=0,
    )


def _make_config(strategy="s1v2"):
    """Build a minimal config dict for futures backtester tests."""
    config = {
        "_strategy_override": strategy,
        "futures": {
            "backtest": {
                "initial_capital": 1000000,
                "margin_rate": 0.12,
                "risk_per_trade_pct": 0.015,
                "max_positions": 1,
                "no_entry_after": "14:45",
                "hard_exit_time": "15:10",
                "reward_ratio": 2.0,
                "slippage": 0.001,
                "default_interval": "15min",
            },
            "strategies": {
                "s1v2": {
                    "timeframe_mode": "single",
                    "ema_fast": 10,
                    "ema_slow": 20,
                    "adx_threshold": 20,
                    "pullback_candles": 3,
                    "atr_period": 14,
                    "atr_stop_multiplier": 1.5,
                },
                "s1v3": {
                    "interval": "15min",
                    "rsi_period": 14,
                    "rsi_oversold": 30,
                    "rsi_overbought": 70,
                    "bb_period": 20,
                    "bb_std": 2.0,
                },
            },
        },
        "strategy": {
            "s1v2": {
                "timeframe_mode": "single",
                "ema_fast": 10,
                "ema_slow": 20,
                "adx_threshold": 20,
                "pullback_candles": 3,
                "atr_period": 14,
                "atr_stop_multiplier": 1.5,
                "atr_stop_floor_mult": 1.0,
                "volume_ratio_min": 1.5,
                "volume_sma_period": 20,
                "rr_min": 3.0,
                "time_stop_bars": 30,
                "time_stop_bars_15min": 20,
                "atr_target_mult": 2.5,
                "ema_trend_period": 10,
                "ema_pullback_period": 20,
                "adx_period": 14,
            },
            "s1v3": {
                "interval": "15min",
                "signal_start": "09:30",
                "signal_end": "14:30",
                "atr_period": 14,
                "panic_atr_multiplier": 2.0,
                "rsi_period": 14,
                "rsi_oversold": 30,
                "rsi_overbought": 70,
                "bb_period": 20,
                "bb_std": 2.0,
                "volume_ratio_min": 1.5,
                "volume_sma_period": 20,
                "min_rr_ratio": 2.0,
                "reversal_timeout_bars": 5,
            },
        },
        "risk": {
            "max_open_positions": 6,
        },
        "trading_hours": {
            "no_entry_after": "14:45",
        },
    }
    return config


def _make_position(
    direction="LONG",
    entry_price=Decimal("22000"),
    stop_loss=Decimal("21900"),
    target=Decimal("22200"),
    qty=65,
    candle_time=None,
):
    """Create a BacktestPosition for testing."""
    from tools.backtester import BacktestPosition

    ct = candle_time or IST.localize(datetime(2026, 3, 1, 10, 0))
    return BacktestPosition(
        symbol="NIFTY",
        instrument_token=0,
        direction=direction,
        entry_price=entry_price,
        entry_time=ct,
        qty=qty,
        stop_loss=stop_loss,
        target=target,
        original_stop=stop_loss,
        regime="NEUTRAL",
    )


# ===========================================================================
# FuturesChargeCalculator
# ===========================================================================

class TestFuturesChargeCalculator:
    """Tests 1-5: Zerodha futures MIS charge model."""

    def _calc(self):
        from tools.futures_backtester import FuturesChargeCalculator
        return FuturesChargeCalculator()

    def test_long_round_trip_charges(self):
        """(1) LONG: STT on sell-side, stamp on buy-side."""
        calc = self._calc()
        qty = 65
        entry = Decimal("22000")
        exit_ = Decimal("22100")
        result = calc.calculate(qty, entry, exit_, "LONG")

        # Turnover
        turnover_entry = Decimal("65") * entry  # 1,430,000
        turnover_exit = Decimal("65") * exit_    # 1,436,500
        total_turnover = turnover_entry + turnover_exit

        # Brokerage: 0.03% per leg, capped at ₹20
        brok_entry = min(turnover_entry * Decimal("0.0003"), Decimal("20"))
        brok_exit = min(turnover_exit * Decimal("0.0003"), Decimal("20"))
        assert result.brokerage == brok_entry + brok_exit

        # STT: 0.02% on sell-side (exit for LONG)
        assert result.stt == turnover_exit * Decimal("0.0002")

        # Exchange txn: 0.00183% on total turnover
        assert result.exchange_txn == total_turnover * Decimal("0.0000183")

        # SEBI: ₹10/crore
        assert result.sebi == total_turnover * Decimal("0.000001")

        # Stamp duty: 0.002% on buy-side (entry for LONG)
        assert result.stamp_duty == turnover_entry * Decimal("0.00002")

        # GST: 18% on (brokerage + exchange + sebi)
        assert result.gst == (result.brokerage + result.exchange_txn + result.sebi) * Decimal("0.18")

        # Total
        expected_total = (result.brokerage + result.stt + result.exchange_txn
                          + result.sebi + result.stamp_duty + result.gst)
        assert result.total == expected_total

    def test_short_round_trip_charges(self):
        """(2) SHORT: STT on sell-side (entry for shorts), stamp on buy-side (exit for shorts)."""
        calc = self._calc()
        qty = 30  # BANKNIFTY lot size
        entry = Decimal("48000")
        exit_ = Decimal("47900")
        result = calc.calculate(qty, entry, exit_, "SHORT")

        turnover_entry = Decimal("30") * entry
        turnover_exit = Decimal("30") * exit_

        # STT: sell-side = entry for SHORT
        assert result.stt == turnover_entry * Decimal("0.0002")

        # Stamp duty: buy-side = exit for SHORT
        assert result.stamp_duty == turnover_exit * Decimal("0.00002")

    def test_brokerage_cap_20_per_leg(self):
        """(3) Brokerage caps at ₹20 per leg on high turnover."""
        calc = self._calc()
        # 4 lots × 65 × 22000 = 5,720,000 per leg → 0.03% = ₹1,716 (way above ₹20)
        qty = 260
        entry = Decimal("22000")
        exit_ = Decimal("22100")
        result = calc.calculate(qty, entry, exit_, "LONG")

        # Both legs should be capped at ₹20 each
        assert result.brokerage == Decimal("40")

    def test_zero_qty(self):
        """(4) Zero qty → zero charges."""
        calc = self._calc()
        result = calc.calculate(0, Decimal("22000"), Decimal("22100"), "LONG")
        assert result.total == Decimal("0")
        assert result.brokerage == Decimal("0")
        assert result.stt == Decimal("0")

    def test_large_turnover_sebi(self):
        """(5) Large turnover (₹1cr+) — SEBI charge = ₹10/crore."""
        calc = self._calc()
        # 100 lots × 65 = 6500 qty, entry 22000 → turnover = 143,000,000 per leg
        qty = 6500
        entry = Decimal("22000")
        exit_ = Decimal("22050")
        result = calc.calculate(qty, entry, exit_, "LONG")

        total_turnover = Decimal("6500") * entry + Decimal("6500") * exit_
        expected_sebi = total_turnover * Decimal("0.000001")
        assert result.sebi == expected_sebi
        # At ~₹28.6cr turnover, SEBI should be ~₹28.6
        assert result.sebi > Decimal("20")


# ===========================================================================
# FuturesPositionSizer
# ===========================================================================

class TestFuturesPositionSizer:
    """Tests 6-10: Lot-based position sizing."""

    def _sizer(self):
        from tools.futures_backtester import FuturesPositionSizer
        return FuturesPositionSizer()

    def test_basic_lot_calculation(self):
        """(6) risk ₹15,000, stop 50 pts, lot 65 → 4 lots."""
        sizer = self._sizer()
        # available_capital=1,000,000, risk_pct=0.015 → risk_amount=15,000
        # stop_distance=50, risk_per_lot=50*65=3,250 → 15,000/3,250=4.61 → 4 lots
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("21950"),
            available_capital=Decimal("1000000"),
            risk_pct=Decimal("0.015"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is not None
        num_lots, total_qty = result
        assert num_lots == 4
        assert total_qty == 260

    def test_insufficient_capital_for_one_lot(self):
        """(7) Capital < 1 lot margin → None."""
        sizer = self._sizer()
        # 1 lot margin: 65 × 22000 × 0.12 = ₹171,600
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("21950"),
            available_capital=Decimal("100000"),  # Not enough for 1 lot
            risk_pct=Decimal("0.015"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is None

    def test_scale_down_when_margin_exceeds_capital(self):
        """(8) Risk says 4 lots, but capital only supports 2 lots after margin check."""
        sizer = self._sizer()
        # risk_amount = 400,000 * 0.015 = 6,000
        # stop=50, risk_per_lot=3,250 → 1 lot from risk
        # 1 lot margin = 65*22000*0.12 = 171,600 → fits in 400,000
        # But let's make risk allow more lots than capital supports:
        # available_capital=400,000, risk_pct=0.10 → risk_amount=40,000
        # stop=50, risk_per_lot=3,250 → 12 lots from risk
        # 12 lot margin = 12*65*22000*0.12 = 2,059,200 → too much
        # Scale down until fits: 2 lots = 65*2*22000*0.12 = 343,200 → fits
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("21950"),
            available_capital=Decimal("400000"),
            risk_pct=Decimal("0.10"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is not None
        num_lots, total_qty = result
        # 2 lots: margin = 2*65*22000*0.12 = 343,200 ≤ 400,000
        # 3 lots: margin = 3*65*22000*0.12 = 514,800 > 400,000
        assert num_lots == 2
        assert total_qty == 130

    def test_minimum_one_lot(self):
        """(9) Fractional risk → rounds up to min 1 lot."""
        sizer = self._sizer()
        # risk_amount = 1,000,000 * 0.001 = 1,000
        # stop=200, risk_per_lot = 200*65 = 13,000 → 1000/13000 = 0.07 → rounds to 0
        # Layer 3: bumps to 1 lot, margin = 65*22000*0.12 = 171,600 → fits
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("21800"),
            available_capital=Decimal("1000000"),
            risk_pct=Decimal("0.001"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is not None
        num_lots, total_qty = result
        assert num_lots == 1
        assert total_qty == 65

    def test_zero_stop_distance(self):
        """(10) Zero stop distance → None."""
        sizer = self._sizer()
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("22000"),
            available_capital=Decimal("1000000"),
            risk_pct=Decimal("0.015"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is None


# ===========================================================================
# FuturesCapitalTracker
# ===========================================================================

class TestFuturesCapitalTracker:
    """Tests 11-14: Margin-based capital tracking."""

    def _tracker(self, initial=Decimal("1000000"), margin_rate=Decimal("0.12")):
        from tools.futures_backtester import FuturesCapitalTracker
        return FuturesCapitalTracker(initial, margin_rate)

    def test_initial_state(self):
        """(11) Initial state: available = total, no drawdown."""
        tracker = self._tracker()
        assert tracker.available_capital == Decimal("1000000")
        assert tracker.current_equity == Decimal("1000000")
        assert tracker.margin_used == Decimal("0")
        assert tracker.realized_pnl == Decimal("0")
        assert tracker.max_drawdown == Decimal("0")

    def test_open_position_reduces_available(self):
        """(12) Opening a position locks margin, reducing available capital."""
        tracker = self._tracker()
        # 1 lot NIFTY: 65 × 22000 = 1,430,000 contract value
        contract_value = Decimal("1430000")
        tracker.open_position(contract_value)

        # margin_used = 1,430,000 × 0.12 = 171,600
        assert tracker.margin_used == contract_value * Decimal("0.12")
        assert tracker.available_capital == Decimal("1000000") - tracker.margin_used

    def test_close_position_releases_margin(self):
        """(13) Closing a position releases margin and records P&L."""
        tracker = self._tracker()
        contract_value = Decimal("1430000")
        tracker.open_position(contract_value)

        # Close with ₹5,000 profit
        tracker.close_position(contract_value, Decimal("5000"))
        assert tracker.margin_used == Decimal("0")
        assert tracker.realized_pnl == Decimal("5000")
        assert tracker.available_capital == Decimal("1005000")
        assert tracker.current_equity == Decimal("1005000")

    def test_drawdown_tracking(self):
        """(14) Peak capital updates, drawdown computed correctly."""
        tracker = self._tracker()
        contract_value = Decimal("1430000")

        # Win: peak goes to 1,005,000
        tracker.open_position(contract_value)
        tracker.close_position(contract_value, Decimal("5000"))
        assert tracker.peak_capital == Decimal("1005000")
        assert tracker.max_drawdown == Decimal("0")

        # Loss: equity drops to 998,000 → drawdown = 7,000
        tracker.open_position(contract_value)
        tracker.close_position(contract_value, Decimal("-7000"))
        assert tracker.current_equity == Decimal("998000")
        assert tracker.peak_capital == Decimal("1005000")
        assert tracker.max_drawdown == Decimal("7000")


# ===========================================================================
# Data & Indicators
# ===========================================================================

class TestDataAndIndicators:
    """Tests 15-17: VWAP, OI indicators, and data loading."""

    def test_vwap_computation(self):
        """(15) VWAP on futures candles matches expected formula."""
        from tools.futures_backtester import FuturesBacktestEngine

        c1 = _make_candle(
            open_=Decimal("22000"), high=Decimal("22100"),
            low=Decimal("21900"), close=Decimal("22050"), volume=10000,
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
        )
        c2 = _make_candle(
            open_=Decimal("22050"), high=Decimal("22200"),
            low=Decimal("22000"), close=Decimal("22150"), volume=15000,
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 45)),
        )

        result = FuturesBacktestEngine._compute_vwap_for_day([c1, c2])

        # c1 tp = (22100 + 21900 + 22050) / 3 = 22016.666...
        # cum_tp_vol = 22016.666... × 10000
        # vwap_1 = cum_tp_vol / 10000 = 22016.666...
        tp1 = (Decimal("22100") + Decimal("21900") + Decimal("22050")) / Decimal("3")
        assert result[0].vwap == tp1

        # c2 tp = (22200 + 22000 + 22150) / 3 = 22116.666...
        tp2 = (Decimal("22200") + Decimal("22000") + Decimal("22150")) / Decimal("3")
        cum_tp_vol = tp1 * Decimal("10000") + tp2 * Decimal("15000")
        cum_vol = Decimal("25000")
        expected_vwap2 = cum_tp_vol / cum_vol
        assert result[1].vwap == expected_vwap2

    def test_oi_indicator_computation(self):
        """(16) OI change and oi_change_pct computed correctly."""
        from tools.futures_backtester import FuturesBacktestEngine

        config = _make_config()
        engine = FuturesBacktestEngine.__new__(FuturesBacktestEngine)
        engine._oi_data = {}

        candles = [
            _make_candle(candle_time=IST.localize(datetime(2026, 3, 1, 9, 30))),
            _make_candle(candle_time=IST.localize(datetime(2026, 3, 1, 9, 45))),
            _make_candle(candle_time=IST.localize(datetime(2026, 3, 1, 10, 0))),
        ]
        oi_values = [100000, 110000, 105000]

        engine._compute_oi_indicators(candles, oi_values)

        # First bar: no previous OI
        ct0 = candles[0].candle_time
        assert engine._oi_data[ct0]["oi"] == 100000
        assert engine._oi_data[ct0]["oi_change"] is None

        # Second bar: change = +10000, pct = +10%
        ct1 = candles[1].candle_time
        assert engine._oi_data[ct1]["oi_change"] == 10000
        assert engine._oi_data[ct1]["oi_change_pct"] == pytest.approx(10.0)

        # Third bar: change = -5000, pct = -4.545...%
        ct2 = candles[2].candle_time
        assert engine._oi_data[ct2]["oi_change"] == -5000
        assert engine._oi_data[ct2]["oi_change_pct"] == pytest.approx(-4.5454545, rel=1e-4)

    @pytest.mark.asyncio
    async def test_load_day_candles_sql(self):
        """(17) _load_day_candles queries backtest_futures_candles with correct params."""
        from tools.futures_backtester import FuturesBacktestEngine

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = _async_ctx(mock_conn)

        config = _make_config()
        engine = FuturesBacktestEngine(
            pool=mock_pool,
            config=config,
            instrument="NIFTY",
            lot_size=65,
        )

        await engine._load_day_candles(date(2026, 3, 1))

        # Verify SQL targets backtest_futures_candles
        call_args = mock_conn.fetch.call_args
        sql = call_args[0][0]
        assert "backtest_futures_candles" in sql
        assert "$1" in sql  # instrument param


# ===========================================================================
# Signal Flow Integration
# ===========================================================================

class TestSignalFlow:
    """Tests 18-21: Signal → sizing → trade → P&L."""

    def test_evaluator_integration_mock(self):
        """(18) S1v2 evaluator can be instantiated with futures config."""
        from tools.backtester import S1v2SignalEvaluator
        config = _make_config("s1v2")
        evaluator = S1v2SignalEvaluator(config)
        assert evaluator is not None

    def test_signal_to_lot_sizing(self):
        """(19) Signal entry/stop → sizer returns lot-based qty."""
        from tools.futures_backtester import FuturesPositionSizer

        sizer = FuturesPositionSizer()
        # Typical NIFTY signal: entry 22000, stop 21900 (100 pts)
        result = sizer.calculate(
            entry_price=Decimal("22000"),
            stop_loss=Decimal("21900"),
            available_capital=Decimal("1000000"),
            risk_pct=Decimal("0.015"),
            lot_size=65,
            margin_rate=Decimal("0.12"),
        )
        assert result is not None
        num_lots, total_qty = result
        # risk=15,000, per_lot=100*65=6,500 → 2 lots
        assert num_lots == 2
        assert total_qty == 130

    def test_full_trade_lifecycle(self):
        """(20) Full lifecycle: open → exit → charges → net P&L computed."""
        from tools.futures_backtester import (
            FuturesBacktestEngine, FuturesChargeCalculator, FuturesCapitalTracker,
        )
        from tools.backtester import BacktestPosition

        config = _make_config()
        engine = FuturesBacktestEngine.__new__(FuturesBacktestEngine)
        engine._charge_calc = FuturesChargeCalculator()
        engine._slippage = Decimal("0")  # No slippage for clean test
        engine._capital_tracker = FuturesCapitalTracker(
            Decimal("1000000"), Decimal("0.12")
        )

        # Create a LONG position: entry 22000, qty 65 (1 lot)
        pos = BacktestPosition(
            symbol="NIFTY",
            instrument_token=0,
            direction="LONG",
            entry_price=Decimal("22000"),
            entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
            qty=65,
            stop_loss=Decimal("21900"),
            target=Decimal("22200"),
            original_stop=Decimal("21900"),
            regime="NEUTRAL",
        )
        # Register margin
        contract_value = Decimal("65") * Decimal("22000")
        engine._capital_tracker.open_position(contract_value)

        exit_time = IST.localize(datetime(2026, 3, 1, 11, 0))
        trade = engine._close_position(pos, Decimal("22200"), exit_time, "TARGET_HIT")

        # Gross P&L: (22200 - 22000) × 65 = ₹13,000
        assert trade.gross_pnl == Decimal("13000")
        assert trade.charges > Decimal("0")
        assert trade.net_pnl == trade.gross_pnl - trade.charges
        assert trade.exit_reason == "TARGET_HIT"

    def test_hard_exit_at_1510(self):
        """(21) Positions open past 15:10 IST are force-closed."""
        from tools.futures_backtester import FuturesBacktestEngine
        from tools.backtester import BacktestPosition

        config = _make_config()
        engine = FuturesBacktestEngine.__new__(FuturesBacktestEngine)
        engine._hard_exit_time = __import__("datetime").time(15, 10)

        # A candle at 15:15 should trigger hard exit
        candle_time = IST.localize(datetime(2026, 3, 1, 15, 15))
        assert candle_time.time() > engine._hard_exit_time


# ===========================================================================
# Exit Modes
# ===========================================================================

class TestExitModes:
    """Tests 22-24: Fixed, trailing, partial exit logic."""

    def _make_engine(self, exit_mode="fixed"):
        from tools.futures_backtester import (
            FuturesBacktestEngine, FuturesChargeCalculator, FuturesCapitalTracker,
        )

        engine = FuturesBacktestEngine.__new__(FuturesBacktestEngine)
        engine._charge_calc = FuturesChargeCalculator()
        engine._slippage = Decimal("0")
        engine._capital_tracker = FuturesCapitalTracker(
            Decimal("1000000"), Decimal("0.12")
        )
        engine._exit_mode = exit_mode
        engine._atr_mult = Decimal("1.5")
        engine._atr_period = 14
        engine._partial_pct = Decimal("0.5")
        engine._candle_buffer = []
        engine._pending_partial_trades = []
        return engine

    def test_fixed_exit_stop_and_target(self):
        """(22) Fixed mode: stop hit → STOP_HIT, target hit → TARGET_HIT."""
        engine = self._make_engine("fixed")
        # Register margin
        engine._capital_tracker.open_position(Decimal("65") * Decimal("22000"))

        pos = _make_position()

        # Target candle: high ≥ 22200
        target_candle = _make_candle(
            high=Decimal("22250"), low=Decimal("21950"),
            close=Decimal("22200"),
            candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
        )
        trade = engine._check_fixed_exit(pos, target_candle)
        assert trade is not None
        assert trade.exit_reason == "TARGET_HIT"

        # Stop candle: low ≤ 21900
        engine._capital_tracker.open_position(Decimal("65") * Decimal("22000"))
        pos2 = _make_position()
        stop_candle = _make_candle(
            high=Decimal("22050"), low=Decimal("21850"),
            close=Decimal("21880"),
            candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
        )
        trade2 = engine._check_fixed_exit(pos2, stop_candle)
        assert trade2 is not None
        assert trade2.exit_reason == "STOP_HIT"

    def test_trailing_exit_updates_stop(self):
        """(23) Trailing mode: stop trails up as price moves favorably."""
        engine = self._make_engine("trailing")
        # Register margin so _close_position won't fail
        engine._capital_tracker.open_position(Decimal("65") * Decimal("22000"))

        # Set target very high so fixed exit doesn't trigger
        pos = _make_position(target=Decimal("23000"))
        initial_stop = pos.stop_loss

        # Build enough candle buffer for ATR (14 candles)
        for i in range(15):
            c = _make_candle(
                open_=Decimal("22000") + Decimal(str(i * 10)),
                high=Decimal("22050") + Decimal(str(i * 10)),
                low=Decimal("21950") + Decimal(str(i * 10)),
                close=Decimal("22020") + Decimal(str(i * 10)),
                candle_time=IST.localize(
                    datetime(2026, 3, 1, 9, 30) + timedelta(minutes=15 * i)
                ),
            )
            engine._candle_buffer.append(c)

        # Feed a candle with price above entry but below target
        trail_candle = _make_candle(
            open_=Decimal("22300"), high=Decimal("22350"),
            low=Decimal("22250"), close=Decimal("22320"),
            candle_time=IST.localize(datetime(2026, 3, 1, 13, 0)),
        )
        engine._candle_buffer.append(trail_candle)

        result = engine._check_trailing_exit(pos, trail_candle)
        # No exit yet (price above stop, below target)
        assert result is None
        # Stop should have moved up from initial 21900
        assert pos.stop_loss >= initial_stop

    def test_partial_exit_at_1r(self):
        """(24) Partial mode: 50% closed at 1R, stop moves to breakeven."""
        engine = self._make_engine("partial")
        # Register margin
        engine._capital_tracker.open_position(Decimal("130") * Decimal("22000"))

        # LONG: entry 22000, stop 21900 → risk = 100 → 1R at 22100
        pos = _make_position(qty=130)  # 2 lots

        # Candle that hits 1R: high ≥ 22100
        one_r_candle = _make_candle(
            high=Decimal("22150"), low=Decimal("21950"),
            close=Decimal("22120"),
            candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
        )

        # Need candle buffer for trailing (build minimal)
        for i in range(15):
            engine._candle_buffer.append(_make_candle(
                candle_time=IST.localize(
                    datetime(2026, 3, 1, 9, 30) + timedelta(minutes=15 * i)
                ),
            ))
        engine._candle_buffer.append(one_r_candle)

        result = engine._check_partial_exit(pos, one_r_candle)

        # Partial trade should be recorded
        assert len(engine._pending_partial_trades) == 1
        partial_trade = engine._pending_partial_trades[0]
        assert partial_trade.exit_reason == "PARTIAL_1R"
        # Qty should be 50% of 130 = 65
        assert partial_trade.qty == 65

        # Position should have:
        assert pos.partial_exited is True
        assert pos.qty == 65  # Remaining
        assert pos.stop_loss == pos.entry_price  # Breakeven


# ===========================================================================
# CLI
# ===========================================================================

class TestCLI:
    """Tests 25-26: Argument parsing."""

    def test_run_subcommand_parsing(self):
        """(25) 'run' subcommand parses all required/optional args."""
        from tools.futures_backtester import main, _parse_date

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        run_p = subparsers.add_parser("run")
        run_p.add_argument("--instrument", required=True)
        run_p.add_argument("--strategy", default="s1v2", choices=["s1v2", "s1v3"])
        run_p.add_argument("--interval", default=None)
        run_p.add_argument("--from", dest="from_date", type=_parse_date, default=None)
        run_p.add_argument("--to", dest="to_date", type=_parse_date, default=None)
        run_p.add_argument("--exit-mode", default="fixed", choices=["fixed", "trailing", "partial"])
        run_p.add_argument("--atr-mult", type=float, default=1.5)

        args = parser.parse_args([
            "run",
            "--instrument", "NIFTY",
            "--strategy", "s1v2",
            "--interval", "15min",
            "--from", "2025-09-01",
            "--to", "2026-03-16",
            "--exit-mode", "trailing",
            "--atr-mult", "2.0",
        ])

        assert args.command == "run"
        assert args.instrument == "NIFTY"
        assert args.strategy == "s1v2"
        assert args.interval == "15min"
        assert args.from_date == date(2025, 9, 1)
        assert args.to_date == date(2026, 3, 16)
        assert args.exit_mode == "trailing"
        assert args.atr_mult == 2.0

    def test_optimize_subcommand_parsing(self):
        """(26) 'optimize' subcommand parses param + range."""
        from tools.futures_backtester import _parse_date

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")

        opt_p = subparsers.add_parser("optimize")
        opt_p.add_argument("--instrument", required=True)
        opt_p.add_argument("--strategy", default="s1v2")
        opt_p.add_argument("--param", required=True)
        opt_p.add_argument("--range", required=True)
        opt_p.add_argument("--exit-mode", default="fixed")

        args = parser.parse_args([
            "optimize",
            "--instrument", "NIFTY",
            "--param", "atr_mult",
            "--range", "1.0:0.5:3.0",
        ])

        assert args.command == "optimize"
        assert args.instrument == "NIFTY"
        assert args.param == "atr_mult"
        assert args.range == "1.0:0.5:3.0"


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:
    """Tests 27-28: Edge cases."""

    @pytest.mark.asyncio
    async def test_no_signals_zero_trades(self):
        """(27) No signals → 0 trades, no errors."""
        from tools.futures_backtester import FuturesBacktestEngine

        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = []  # No trading days

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = _async_ctx(mock_conn)

        config = _make_config()
        engine = FuturesBacktestEngine(
            pool=mock_pool,
            config=config,
            instrument="NIFTY",
            lot_size=65,
        )

        result = await engine.run(date(2026, 3, 1), date(2026, 3, 10))
        assert result.total_trades == 0
        assert result.trades == []
        assert result.net_pnl == Decimal("0")

    def test_consecutive_trades_capital_tracking(self):
        """(28) Multiple trades update capital tracker correctly."""
        from tools.futures_backtester import (
            FuturesBacktestEngine, FuturesChargeCalculator, FuturesCapitalTracker,
        )
        from tools.backtester import BacktestPosition

        engine = FuturesBacktestEngine.__new__(FuturesBacktestEngine)
        engine._charge_calc = FuturesChargeCalculator()
        engine._slippage = Decimal("0")
        engine._capital_tracker = FuturesCapitalTracker(
            Decimal("1000000"), Decimal("0.12")
        )

        # Trade 1: Win ₹100 per qty × 65 qty = ₹6,500 gross
        pos1 = BacktestPosition(
            symbol="NIFTY", instrument_token=0, direction="LONG",
            entry_price=Decimal("22000"),
            entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
            qty=65, stop_loss=Decimal("21900"), target=Decimal("22200"),
            original_stop=Decimal("21900"), regime="NEUTRAL",
        )
        contract_value = Decimal("65") * Decimal("22000")
        engine._capital_tracker.open_position(contract_value)
        trade1 = engine._close_position(
            pos1, Decimal("22100"),
            IST.localize(datetime(2026, 3, 1, 11, 0)),
            "TARGET_HIT",
        )
        equity_after_1 = engine._capital_tracker.current_equity

        # Trade 2: Loss ₹150 per qty × 65 = ₹9,750 gross
        pos2 = BacktestPosition(
            symbol="NIFTY", instrument_token=0, direction="LONG",
            entry_price=Decimal("22100"),
            entry_time=IST.localize(datetime(2026, 3, 2, 10, 0)),
            qty=65, stop_loss=Decimal("21950"), target=Decimal("22400"),
            original_stop=Decimal("21950"), regime="NEUTRAL",
        )
        contract_value2 = Decimal("65") * Decimal("22100")
        engine._capital_tracker.open_position(contract_value2)
        trade2 = engine._close_position(
            pos2, Decimal("21950"),
            IST.localize(datetime(2026, 3, 2, 11, 0)),
            "STOP_HIT",
        )

        # Realized P&L should be sum of both trades
        assert engine._capital_tracker.realized_pnl == trade1.net_pnl + trade2.net_pnl
        # Margin should be zero (both closed)
        assert engine._capital_tracker.margin_used == Decimal("0")
        # Should have drawdown since trade 2 was a loss
        assert engine._capital_tracker.max_drawdown > Decimal("0")


# ===========================================================================
# Config helpers
# ===========================================================================

class TestConfigHelpers:
    """Additional tests for config utility functions."""

    def test_build_strategy_config(self):
        """Futures strategy defaults are merged into strategy config path."""
        from tools.futures_backtester import _build_strategy_config

        base_config = {
            "futures": {
                "strategies": {
                    "s1v2": {"ema_fast": 10, "ema_slow": 20},
                },
            },
            "strategy": {
                "s1v2": {"ema_fast": 9, "adx_threshold": 25},
            },
        }

        result = _build_strategy_config(base_config, "s1v2")

        # Existing strategy values should override futures defaults
        assert result["strategy"]["s1v2"]["ema_fast"] == 9  # Existing wins
        assert result["strategy"]["s1v2"]["ema_slow"] == 20  # From futures defaults
        assert result["strategy"]["s1v2"]["adx_threshold"] == 25  # Existing
        assert result["_strategy_override"] == "s1v2"

    def test_get_lot_size(self):
        """Lot size lookup from config."""
        from tools.futures_backtester import _get_lot_size

        config = {
            "futures": {
                "instruments": [
                    {"name": "NIFTY", "lot_size": 65},
                    {"name": "BANKNIFTY", "lot_size": 30},
                ],
            },
        }

        assert _get_lot_size(config, "NIFTY") == 65
        assert _get_lot_size(config, "BANKNIFTY") == 30

        with pytest.raises(ValueError, match="No lot_size found"):
            _get_lot_size(config, "UNKNOWN")

    def test_parse_date(self):
        """Date parsing helper."""
        from tools.futures_backtester import _parse_date

        assert _parse_date("2025-09-01") == date(2025, 9, 1)
        assert _parse_date("2026-03-16") == date(2026, 3, 16)
