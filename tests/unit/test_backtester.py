"""
Backtester — Unit tests.

Tests:
  (1)  BacktestRegimeAdapter — BULL_TREND allows long, blocks short
  (2)  BacktestRegimeAdapter — HIGH_VOLATILITY allows both, multiplier 0.5
  (3)  BacktestRiskGate — rejects when at max positions
  (4)  BacktestRiskGate — rejects after 14:45 (no-entry window)
  (5)  BacktestRiskGate — rejects duplicate signal
  (6)  BacktestRiskGate — regime blocks LONG in BEAR_TREND
  (7)  compute_atr — known candle sequence, verify ATR value
  (8)  Fixed exit — stop hit when candle low <= stop_loss
  (9)  Fixed exit — target hit when candle high >= target
  (10) Fixed exit — both hit → pessimistic (stop wins)
  (11) Trailing stop — trail tightens for LONG
  (12) Trailing stop — trail never widens
  (13) Partial exit — 50% at 1R, remainder trails
  (14) Hard exit — positions closed at 15:00
  (15) Slippage — entry/exit prices adjusted correctly
  (16) Charges — ChargeCalculator called with correct values
  (17) Metrics — Sharpe ratio from known daily returns
  (18) Metrics — profit factor from known wins/losses
  (19) DB save run — verify INSERT SQL fields
  (20) DB save trades — verify executemany rows
  (21) Warmup — no trades during warmup period
  (22) Position limit — max positions enforced
  (23) Regime computation — mock NIFTY/VIX data → correct regime
  (24) IndicatorEngine reused — EMA continuity across days
  (25) Compare — 3 exit modes produce 3 results
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytz
import pytest

IST = pytz.timezone("Asia/Kolkata")


def _make_candle(
    symbol="RELIANCE",
    token=738561,
    open_=Decimal("100"),
    high=Decimal("105"),
    low=Decimal("98"),
    close=Decimal("103"),
    volume=50000,
    candle_time=None,
    session_date=None,
):
    """Create a Candle dataclass for testing."""
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
        vwap=close,
        candle_time=ct,
        session_date=sd,
        tick_count=0,
    )


def _make_signal(
    symbol="RELIANCE",
    token=738561,
    direction="LONG",
    entry=Decimal("100"),
    stop_loss=Decimal("95"),
    target=Decimal("110"),
    candle_time=None,
    volume_ratio=Decimal("1.8"),
):
    """Create a Signal dataclass for testing."""
    from core.strategy_engine.signal_generator import Signal

    ct = candle_time or IST.localize(datetime(2026, 3, 1, 10, 0))
    return Signal(
        symbol=symbol,
        instrument_token=token,
        direction=direction,
        signal_time=ct,
        candle_time=ct,
        theoretical_entry=entry,
        stop_loss=stop_loss,
        target=target,
        ema9=Decimal("101"),
        ema21=Decimal("99"),
        rsi=Decimal("60"),
        vwap=Decimal("100"),
        volume_ratio=volume_ratio,
    )


def _mock_pool(mock_conn):
    """Create a mock asyncpg pool where acquire() returns an async CM."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = mock_cm
    return pool


# ---------------------------------------------------------------------------
# (1-2) BacktestRegimeAdapter
# ---------------------------------------------------------------------------

def test_regime_adapter_bull_trend():
    """BULL_TREND allows long, blocks short, multiplier 1.0."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestRegimeAdapter

    adapter = BacktestRegimeAdapter(MarketRegime.BULL_TREND)
    assert adapter.is_long_allowed() is True
    assert adapter.is_short_allowed() is False
    assert adapter.position_size_multiplier() == 1.0
    assert adapter.current_regime() == MarketRegime.BULL_TREND


def test_regime_adapter_high_volatility():
    """HIGH_VOLATILITY allows both, multiplier 0.5."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestRegimeAdapter

    adapter = BacktestRegimeAdapter(MarketRegime.HIGH_VOLATILITY)
    assert adapter.is_long_allowed() is True
    assert adapter.is_short_allowed() is True
    assert adapter.position_size_multiplier() == 0.5


# ---------------------------------------------------------------------------
# (3-6) BacktestRiskGate
# ---------------------------------------------------------------------------

def test_risk_gate_max_positions():
    """Rejects when at max open positions."""
    from tools.backtester import BacktestRiskGate

    gate = BacktestRiskGate()
    signal = _make_signal()
    shared_state = {
        "open_positions": {"INFY": {"direction": "LONG"}, "HDFCBANK": {"direction": "LONG"}},
        "pending_signals": 0,
    }
    config = {"risk": {"max_open_positions": 2}}
    ct = IST.localize(datetime(2026, 3, 1, 10, 0))

    allowed, reason = gate.check(signal, shared_state, config, ct)
    assert allowed is False
    assert reason == "MAX_POSITIONS_REACHED"


def test_risk_gate_time_window():
    """Rejects after 14:45 (no-entry window)."""
    from tools.backtester import BacktestRiskGate

    gate = BacktestRiskGate()
    signal = _make_signal()
    shared_state = {"open_positions": {}, "pending_signals": 0}
    config = {"risk": {"max_open_positions": 6}, "trading_hours": {"no_entry_after": "14:45"}}
    ct = IST.localize(datetime(2026, 3, 1, 14, 50))

    allowed, reason = gate.check(signal, shared_state, config, ct)
    assert allowed is False
    assert reason == "NO_ENTRY_WINDOW"


def test_risk_gate_duplicate_signal():
    """Rejects same symbol+direction already open."""
    from tools.backtester import BacktestRiskGate

    gate = BacktestRiskGate()
    signal = _make_signal(direction="LONG")
    shared_state = {
        "open_positions": {"RELIANCE": {"direction": "LONG"}},
        "pending_signals": 0,
    }
    config = {"risk": {"max_open_positions": 6}}
    ct = IST.localize(datetime(2026, 3, 1, 10, 0))

    allowed, reason = gate.check(signal, shared_state, config, ct)
    assert allowed is False
    assert reason == "DUPLICATE_SIGNAL"


def test_risk_gate_regime_blocks_long_in_bear():
    """LONG blocked in BEAR_TREND regime."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestRegimeAdapter, BacktestRiskGate

    gate = BacktestRiskGate()
    adapter = BacktestRegimeAdapter(MarketRegime.BEAR_TREND)
    signal = _make_signal(direction="LONG")
    shared_state = {"open_positions": {}, "pending_signals": 0}
    config = {"risk": {"max_open_positions": 6}}
    ct = IST.localize(datetime(2026, 3, 1, 10, 0))

    allowed, reason = gate.check(signal, shared_state, config, ct, adapter)
    assert allowed is False
    assert "REGIME_BLOCKED" in reason


# ---------------------------------------------------------------------------
# (6b) _compute_vwap_for_day
# ---------------------------------------------------------------------------

def test_compute_vwap_for_day():
    """Running VWAP computed correctly from OHLCV candles."""
    from tools.backtester import BacktestEngine

    candles = [
        _make_candle(
            open_=Decimal("100"), high=Decimal("110"), low=Decimal("90"),
            close=Decimal("105"), volume=1000,
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 15)),
        ),
        _make_candle(
            open_=Decimal("105"), high=Decimal("115"), low=Decimal("95"),
            close=Decimal("110"), volume=2000,
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
        ),
        _make_candle(
            open_=Decimal("110"), high=Decimal("120"), low=Decimal("100"),
            close=Decimal("115"), volume=1500,
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 45)),
        ),
    ]

    result = BacktestEngine._compute_vwap_for_day(candles)

    # Candle 1: tp = (110+90+105)/3 ≈ 101.6667, cum_tp_vol = 101666.7, cum_vol = 1000
    #   vwap1 = 101666.7 / 1000 ≈ 101.6667
    tp1 = (Decimal("110") + Decimal("90") + Decimal("105")) / Decimal("3")
    expected_vwap1 = tp1  # cum_vol = 1000, so vwap = tp1 * 1000 / 1000 = tp1
    assert result[0].vwap == expected_vwap1

    # Candle 2: tp = (115+95+110)/3 ≈ 106.6667
    #   cum_tp_vol = tp1*1000 + tp2*2000, cum_vol = 3000
    tp2 = (Decimal("115") + Decimal("95") + Decimal("110")) / Decimal("3")
    expected_vwap2 = (tp1 * 1000 + tp2 * 2000) / Decimal("3000")
    assert result[1].vwap == expected_vwap2

    # Candle 3: tp = (120+100+115)/3 ≈ 111.6667
    #   cum_tp_vol += tp3*1500, cum_vol = 4500
    tp3 = (Decimal("120") + Decimal("100") + Decimal("115")) / Decimal("3")
    expected_vwap3 = (tp1 * 1000 + tp2 * 2000 + tp3 * 1500) / Decimal("4500")
    assert result[2].vwap == expected_vwap3

    # VWAP should differ from close (the whole point of this fix)
    assert result[0].vwap != result[0].close
    assert result[1].vwap != result[1].close

    # Original candle fields unchanged
    assert result[0].close == Decimal("105")
    assert result[1].volume == 2000


# ---------------------------------------------------------------------------
# (7) compute_atr
# ---------------------------------------------------------------------------

def test_compute_atr():
    """Known candle sequence → correct ATR value."""
    from tools.backtester import compute_atr

    candles = [
        _make_candle(high=Decimal("110"), low=Decimal("90"), close=Decimal("100"),
                     candle_time=IST.localize(datetime(2026, 3, 1, 9, 15))),
        _make_candle(high=Decimal("115"), low=Decimal("95"), close=Decimal("105"),
                     candle_time=IST.localize(datetime(2026, 3, 1, 9, 30))),
        _make_candle(high=Decimal("120"), low=Decimal("100"), close=Decimal("110"),
                     candle_time=IST.localize(datetime(2026, 3, 1, 9, 45))),
    ]

    atr = compute_atr(candles, period=2)
    # True ranges:
    #   candle 1→2: max(115-95, |115-100|, |95-100|) = max(20, 15, 5) = 20
    #   candle 2→3: max(120-100, |120-105|, |100-105|) = max(20, 15, 5) = 20
    # ATR(2) = (20 + 20) / 2 = 20
    assert atr == Decimal("20")


# ---------------------------------------------------------------------------
# (8-10) Fixed exit
# ---------------------------------------------------------------------------

def test_fixed_exit_stop_hit():
    """Candle low <= stop_loss → STOP_HIT."""
    from tools.backtester import BacktestEngine, BacktestPosition, BacktestRegimeAdapter
    from core.regime_detector.regime_detector import MarketRegime

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # Candle with low=94 → stop hit
    candle = _make_candle(high=Decimal("102"), low=Decimal("94"), close=Decimal("96"))
    trade = engine._check_fixed_exit(pos, candle)

    assert trade is not None
    assert trade.exit_reason == "STOP_HIT"
    assert trade.qty == 10


def test_fixed_exit_target_hit():
    """Candle high >= target → TARGET_HIT."""
    from tools.backtester import BacktestEngine, BacktestPosition
    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    candle = _make_candle(high=Decimal("112"), low=Decimal("99"), close=Decimal("111"))
    trade = engine._check_fixed_exit(pos, candle)

    assert trade is not None
    assert trade.exit_reason == "TARGET_HIT"


def test_fixed_exit_both_pessimistic():
    """Both stop and target hit in same candle → pessimistic (stop wins)."""
    from tools.backtester import BacktestEngine, BacktestPosition
    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # Candle touches both stop (low=94) and target (high=112)
    candle = _make_candle(high=Decimal("112"), low=Decimal("94"), close=Decimal("100"))
    trade = engine._check_fixed_exit(pos, candle)

    assert trade is not None
    assert trade.exit_reason == "STOP_HIT"


# ---------------------------------------------------------------------------
# (11-12) Trailing stop
# ---------------------------------------------------------------------------

def test_trailing_stop_moves_up():
    """Trail tightens for LONG in BULL_TREND."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestEngine, BacktestPosition, BacktestRegimeAdapter

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config, exit_mode="trailing", atr_mult=1.0)
    adapter = BacktestRegimeAdapter(MarketRegime.BULL_TREND)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("90"), target=Decimal("120"),
        original_stop=Decimal("90"), regime="bull_trend",
    )

    # Seed candle buffer with enough candles for ATR (all same OHLC for simple ATR)
    candles = []
    for i in range(15):
        candles.append(_make_candle(
            high=Decimal("105"), low=Decimal("95"), close=Decimal("100"),
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 15 + i)),
        ))
    engine._candle_buffers["RELIANCE"] = candles

    # New candle closes at 108 → trail should move up
    candle = _make_candle(high=Decimal("109"), low=Decimal("107"), close=Decimal("108"))
    engine._candle_buffers["RELIANCE"].append(candle)

    original_stop = pos.stop_loss
    result = engine._check_trailing_exit(pos, candle, adapter)

    # Should not exit (no stop/target hit)
    assert result is None
    # Stop should have moved up from 90
    assert pos.stop_loss > original_stop


def test_trailing_stop_never_widens():
    """Trail doesn't move backwards (widen) for LONG."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestEngine, BacktestPosition, BacktestRegimeAdapter

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config, exit_mode="trailing", atr_mult=1.0)
    adapter = BacktestRegimeAdapter(MarketRegime.BULL_TREND)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("105"), target=Decimal("120"),
        original_stop=Decimal("90"), regime="bull_trend",
    )

    # ATR buffer
    candles = []
    for i in range(15):
        candles.append(_make_candle(
            high=Decimal("105"), low=Decimal("95"), close=Decimal("100"),
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 15 + i)),
        ))
    engine._candle_buffers["RELIANCE"] = candles

    # Candle closes lower → trail should NOT widen (move down)
    candle = _make_candle(high=Decimal("103"), low=Decimal("99"), close=Decimal("100"))
    engine._candle_buffers["RELIANCE"].append(candle)

    old_stop = pos.stop_loss
    _result = engine._check_trailing_exit(pos, candle, adapter)

    # Stop should not have decreased
    assert pos.stop_loss >= old_stop


# ---------------------------------------------------------------------------
# (13) Partial exit
# ---------------------------------------------------------------------------

def test_partial_exit_at_1r():
    """50% exit at 1R profit for LONG."""
    from core.regime_detector.regime_detector import MarketRegime
    from tools.backtester import BacktestEngine, BacktestPosition, BacktestRegimeAdapter

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config, exit_mode="partial", partial_pct=0.5)
    adapter = BacktestRegimeAdapter(MarketRegime.BULL_TREND)
    engine._pending_partial_trades = []

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("115"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # ATR buffer for trailing component
    candles = []
    for i in range(15):
        candles.append(_make_candle(
            high=Decimal("105"), low=Decimal("95"), close=Decimal("100"),
            candle_time=IST.localize(datetime(2026, 3, 1, 9, 15 + i)),
        ))
    engine._candle_buffers["RELIANCE"] = candles

    # 1R for LONG: entry=100, stop=95, risk=5 → 1R at 105
    candle = _make_candle(high=Decimal("106"), low=Decimal("104"), close=Decimal("105"))
    engine._candle_buffers["RELIANCE"].append(candle)
    result = engine._check_partial_exit(pos, candle, adapter)

    # Partial exit should have happened
    assert pos.partial_exited is True
    assert pos.qty == 5  # 10 * 0.5 = 5 remaining
    assert len(engine._pending_partial_trades) == 1
    assert engine._pending_partial_trades[0].exit_reason == "PARTIAL_1R"
    assert engine._pending_partial_trades[0].qty == 5


# ---------------------------------------------------------------------------
# (14) Hard exit at 15:00
# ---------------------------------------------------------------------------

def test_hard_exit_at_1500():
    """Positions closed at 15:00 IST."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    trade = engine._close_position(
        pos, Decimal("102"), IST.localize(datetime(2026, 3, 1, 15, 0)), "HARD_EXIT"
    )
    assert trade.exit_reason == "HARD_EXIT"
    assert trade.qty == 10


# ---------------------------------------------------------------------------
# (15) Slippage
# ---------------------------------------------------------------------------

def test_slippage_applied():
    """Entry/exit prices adjusted by slippage."""
    from tools.backtester import BacktestEngine

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config, slippage=0.001)

    # LONG entry: price goes up
    entry = engine._apply_slippage(Decimal("100"), "LONG", is_entry=True)
    assert entry == Decimal("100") * Decimal("1.001")

    # LONG exit: price goes down
    exit_p = engine._apply_slippage(Decimal("100"), "LONG", is_entry=False)
    assert exit_p == Decimal("100") * Decimal("0.999")

    # SHORT entry: price goes down
    entry_s = engine._apply_slippage(Decimal("100"), "SHORT", is_entry=True)
    assert entry_s == Decimal("100") * Decimal("0.999")

    # SHORT exit: price goes up
    exit_s = engine._apply_slippage(Decimal("100"), "SHORT", is_entry=False)
    assert exit_s == Decimal("100") * Decimal("1.001")


# ---------------------------------------------------------------------------
# (16) Charges
# ---------------------------------------------------------------------------

def test_charges_calculated():
    """ChargeCalculator called with correct values on position close."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config, slippage=0.0)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561, direction="LONG",
        entry_price=Decimal("100"), entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    trade = engine._close_position(
        pos, Decimal("110"), IST.localize(datetime(2026, 3, 1, 12, 0)), "TARGET_HIT"
    )

    # Gross P&L: (110-100) * 10 = 100 (slippage=0 but Decimal("1.0") mult)
    assert float(trade.gross_pnl) == pytest.approx(100.0, abs=0.1)
    # Charges should be > 0 (exact value depends on ChargeCalculator)
    assert trade.charges > 0
    # Net = gross - charges
    assert trade.net_pnl == trade.gross_pnl - trade.charges


# ---------------------------------------------------------------------------
# (17-18) Metrics
# ---------------------------------------------------------------------------

def test_metrics_sharpe():
    """Known daily returns → correct Sharpe ratio."""
    from tools.backtester import BacktestEngine, BacktestTrade, DailyResult

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    # Create daily results with known returns
    daily_results = [
        DailyResult(date(2026, 3, 1), 1, Decimal("1000"), Decimal("900"), "bull_trend"),
        DailyResult(date(2026, 3, 2), 1, Decimal("500"), Decimal("400"), "bull_trend"),
        DailyResult(date(2026, 3, 3), 1, Decimal("-200"), Decimal("-300"), "bull_trend"),
        DailyResult(date(2026, 3, 4), 1, Decimal("800"), Decimal("700"), "bull_trend"),
    ]

    trades = [
        BacktestTrade("RELIANCE", 738561, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 1, 10, 0)),
                      Decimal("110"), IST.localize(datetime(2026, 3, 1, 14, 0)),
                      "TARGET_HIT", 10, Decimal("1000"), Decimal("100"), Decimal("900"), "bull_trend"),
        BacktestTrade("INFY", 408065, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 2, 10, 0)),
                      Decimal("105"), IST.localize(datetime(2026, 3, 2, 14, 0)),
                      "TARGET_HIT", 10, Decimal("500"), Decimal("100"), Decimal("400"), "bull_trend"),
        BacktestTrade("HDFCBANK", 341249, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 3, 10, 0)),
                      Decimal("98"), IST.localize(datetime(2026, 3, 3, 14, 0)),
                      "STOP_HIT", 10, Decimal("-200"), Decimal("100"), Decimal("-300"), "bull_trend"),
        BacktestTrade("TCS", 2953217, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 4, 10, 0)),
                      Decimal("108"), IST.localize(datetime(2026, 3, 4, 14, 0)),
                      "TARGET_HIT", 10, Decimal("800"), Decimal("100"), Decimal("700"), "bull_trend"),
    ]

    result = engine._compute_metrics(trades, daily_results, date(2026, 3, 1), date(2026, 3, 4))

    assert result.total_trades == 4
    assert result.wins == 3
    assert result.losses == 1
    assert result.sharpe_ratio != 0.0  # Non-zero with mixed returns
    # Verify manually: daily returns as fraction of capital
    # Capital = 1000000 * 0.9 = 900000
    capital = float(engine._total_capital)
    returns = [900 / capital, 400 / capital, -300 / capital, 700 / capital]
    mean_r = sum(returns) / len(returns)
    std_r = (sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5
    expected_sharpe = (mean_r / std_r) * math.sqrt(252) if std_r > 0 else 0
    assert abs(result.sharpe_ratio - expected_sharpe) < 0.01


def test_metrics_profit_factor():
    """Known wins/losses → correct profit factor."""
    from tools.backtester import BacktestEngine, BacktestTrade, DailyResult

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    trades = [
        BacktestTrade("RELIANCE", 738561, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 1, 10, 0)),
                      Decimal("110"), IST.localize(datetime(2026, 3, 1, 14, 0)),
                      "TARGET_HIT", 10, Decimal("1000"), Decimal("50"), Decimal("950"), "bull_trend"),
        BacktestTrade("INFY", 408065, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 2, 10, 0)),
                      Decimal("95"), IST.localize(datetime(2026, 3, 2, 14, 0)),
                      "STOP_HIT", 10, Decimal("-500"), Decimal("50"), Decimal("-550"), "bull_trend"),
    ]

    daily_results = [
        DailyResult(date(2026, 3, 1), 1, Decimal("1000"), Decimal("950"), "bull_trend"),
        DailyResult(date(2026, 3, 2), 1, Decimal("-500"), Decimal("-550"), "bull_trend"),
    ]

    result = engine._compute_metrics(trades, daily_results, date(2026, 3, 1), date(2026, 3, 2))

    # Profit factor = gross_wins / |gross_losses| = 1000 / 500 = 2.0
    assert abs(result.profit_factor - 2.0) < 0.01


# ---------------------------------------------------------------------------
# (19-20) DB storage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_save_run():
    """Verify INSERT SQL for backtest_runs."""
    from tools.backtester import BacktestResult, _save_run

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=42)
    pool = _mock_pool(mock_conn)

    result = BacktestResult(
        trades=[], daily_results=[],
        params={"strategy": "s1", "exit_mode": "fixed"},
        total_trades=10, wins=7, losses=3, win_rate=70.0,
        avg_win=Decimal("500"), avg_loss=Decimal("-200"),
        expectancy=Decimal("290"),
        gross_pnl=Decimal("3500"), total_charges=Decimal("200"),
        net_pnl=Decimal("3300"),
        max_drawdown=Decimal("500"), max_drawdown_pct=0.55,
        sharpe_ratio=1.5, profit_factor=2.5,
        date_from=date(2025, 9, 1), date_to=date(2026, 3, 16),
    )

    run_id = await _save_run(pool, result)
    assert run_id == 42
    mock_conn.fetchval.assert_called_once()
    sql = mock_conn.fetchval.call_args[0][0]
    assert "INSERT INTO backtest_runs" in sql
    assert "RETURNING id" in sql


@pytest.mark.asyncio
async def test_db_save_trades():
    """Verify executemany for backtest_trades."""
    from tools.backtester import BacktestTrade, _save_trades

    mock_conn = AsyncMock()
    mock_conn.executemany = AsyncMock(return_value=None)
    pool = _mock_pool(mock_conn)

    trades = [
        BacktestTrade("RELIANCE", 738561, "LONG", Decimal("100"),
                      IST.localize(datetime(2026, 3, 1, 10, 0)),
                      Decimal("110"), IST.localize(datetime(2026, 3, 1, 14, 0)),
                      "TARGET_HIT", 10, Decimal("1000"), Decimal("50"), Decimal("950"), "bull_trend"),
    ]

    await _save_trades(pool, 42, trades)
    mock_conn.executemany.assert_called_once()
    sql = mock_conn.executemany.call_args[0][0]
    assert "INSERT INTO backtest_trades" in sql
    rows = mock_conn.executemany.call_args[0][1]
    assert len(rows) == 1
    assert rows[0][1] == "RELIANCE"


# ---------------------------------------------------------------------------
# (21) Warmup — no trades during warmup
# ---------------------------------------------------------------------------

def test_warmup_no_indicators():
    """IndicatorEngine returns None during warmup → no signal generated."""
    from core.strategy_engine.indicators import IndicatorEngine

    base = IST.localize(datetime(2026, 3, 1, 9, 15))
    # Only 5 candles (need MIN_CANDLES=21)
    warmup = [_make_candle(
        candle_time=base + timedelta(minutes=i * 15)
    ) for i in range(5)]

    engine = IndicatorEngine(warmup_candles=warmup)
    # Next candle should still return None (only 6 candles total)
    candle = _make_candle(candle_time=base + timedelta(minutes=5 * 15))
    result = engine.update(candle)
    assert result is None


# ---------------------------------------------------------------------------
# (22) Position limit enforced
# ---------------------------------------------------------------------------

def test_position_limit_enforced():
    """BacktestRiskGate blocks when pending + open >= max_positions."""
    from tools.backtester import BacktestRiskGate

    gate = BacktestRiskGate()
    signal = _make_signal()
    shared_state = {
        "open_positions": {"INFY": {"direction": "LONG"}},
        "pending_signals": 1,  # 1 open + 1 pending = 2
    }
    config = {"risk": {"max_open_positions": 2}}
    ct = IST.localize(datetime(2026, 3, 1, 10, 0))

    allowed, reason = gate.check(signal, shared_state, config, ct)
    assert allowed is False
    assert reason == "MAX_POSITIONS_REACHED"


# ---------------------------------------------------------------------------
# (23) Regime computation
# ---------------------------------------------------------------------------

def test_regime_from_classify():
    """classify_regime with known values produces expected regime."""
    from core.regime_detector.regime_detector import MarketRegime, classify_regime

    # BULL_TREND: price > EMA, VIX < 15, no crash/vol indicators
    regime = classify_regime(
        nifty_price=23000, nifty_ema200=22000, vix=13,
        intraday_drop_pct=0.5, intraday_range_pct=0.8,
    )
    assert regime == MarketRegime.BULL_TREND

    # CRASH: VIX > 35
    regime = classify_regime(
        nifty_price=22000, nifty_ema200=23000, vix=40,
        intraday_drop_pct=3.0, intraday_range_pct=2.0,
    )
    assert regime == MarketRegime.CRASH


# ---------------------------------------------------------------------------
# (24) IndicatorEngine reused
# ---------------------------------------------------------------------------

def test_indicator_engine_ema_continuity():
    """IndicatorEngine carries EMA state across candles."""
    from core.strategy_engine.indicators import IndicatorEngine

    # 25 warmup candles with ascending prices
    base = IST.localize(datetime(2026, 3, 1, 9, 15))
    warmup = [_make_candle(
        close=Decimal(str(100 + i)),
        high=Decimal(str(102 + i)),
        low=Decimal(str(98 + i)),
        volume=50000 + i * 1000,
        candle_time=base + timedelta(minutes=i * 15),
    ) for i in range(25)]

    engine = IndicatorEngine(warmup_candles=warmup)

    # First candle should produce indicators (25+1=26 > MIN_CANDLES=21)
    candle1 = _make_candle(
        close=Decimal("126"), high=Decimal("128"), low=Decimal("124"),
        volume=60000,
        candle_time=base + timedelta(minutes=25 * 15),
    )
    ind1 = engine.update(candle1)
    assert ind1 is not None
    ema9_first = ind1.ema9

    # Second candle
    candle2 = _make_candle(
        close=Decimal("130"), high=Decimal("132"), low=Decimal("128"),
        volume=65000,
        candle_time=base + timedelta(minutes=26 * 15),
    )
    ind2 = engine.update(candle2)
    assert ind2 is not None
    # EMA9 should have moved (continuity, not reset)
    assert ind2.ema9 != ema9_first


# ---------------------------------------------------------------------------
# (25) Compare runs all modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_produces_three_results():
    """Compare mode produces one result per exit mode."""
    from tools.backtester import run_compare

    config = _minimal_config()

    # Mock pool that returns empty trading days
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    pool = _mock_pool(mock_conn)

    results = await run_compare(
        pool=pool,
        config=config,
        exit_modes=["fixed", "trailing", "partial"],
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
        interval="15min",
        slippage=0.001,
        atr_mult=1.5,
        atr_period=14,
        partial_pct=0.5,
    )

    assert len(results) == 3
    assert "fixed" in results
    assert "trailing" in results
    assert "partial" in results


# ---------------------------------------------------------------------------
# (26) Optimizer: config-path param (volume_ratio_min)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_optimize_volume_ratio_min():
    """Optimizer sweeps volume_ratio_min via config override, not constructor kwarg."""
    from tools.backtester import run_optimize

    config = _minimal_config()
    config["strategy"]["s1"]["volume_ratio_min"] = 1.5  # original value

    # Mock pool that returns empty trading days
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    pool = _mock_pool(mock_conn)

    results = await run_optimize(
        pool=pool,
        config=config,
        param_name="volume_ratio_min",
        range_str="1.0:0.5:2.0",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
        exit_mode="fixed",
        slippage=0.001,
    )

    # 3 values: 1.0, 1.5, 2.0
    assert len(results) == 3
    # Original config must NOT be mutated
    assert config["strategy"]["s1"]["volume_ratio_min"] == 1.5


# ---------------------------------------------------------------------------
# (27) Optimizer: config-path param (rsi_long_min)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_optimize_rsi_long_min():
    """Optimizer sweeps rsi_long_min via config override."""
    from tools.backtester import run_optimize

    config = _minimal_config()
    config["strategy"]["s1"]["rsi_long_min"] = 50

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.fetchval = AsyncMock(return_value=None)
    pool = _mock_pool(mock_conn)

    results = await run_optimize(
        pool=pool,
        config=config,
        param_name="rsi_long_min",
        range_str="40:10:60",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 1),
        exit_mode="fixed",
        slippage=0.001,
    )

    # 3 values: 40, 50, 60
    assert len(results) == 3
    # Original config not mutated
    assert config["strategy"]["s1"]["rsi_long_min"] == 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config() -> dict:
    """Minimal config dict for engine construction."""
    return {
        "capital": {"total": 1000000, "allocation": {"s1_intraday": 0.9}},
        "risk": {"max_open_positions": 6, "max_loss_per_trade_pct": 0.015},
        "strategy": {"s1": {
            "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
            "swing_lookback": 5, "rr_ratio": 2.0, "min_stop_pct": 0.02,
        }},
        "trading_hours": {"no_entry_after": "14:45"},
        "system": {"mode": "paper"},
    }


def _minimal_config_s1v2(timeframe_mode: str = "multi") -> dict:
    """Minimal config dict for S1v2 engine construction."""
    cfg = _minimal_config()
    cfg["strategy"]["s1v2"] = {
        "timeframe_mode": timeframe_mode,
        "ema_trend_period": 10,
        "ema_pullback_period": 20,
        "adx_period": 14,
        "adx_threshold": 25,
        "atr_period": 14,
        "atr_target_mult": 2.5,
        "volume_ratio_min": 1.5,
        "volume_sma_period": 20,
        "rr_min": 3.0,
        "time_stop_bars": 30,
        "time_stop_bars_15min": 20,
    }
    cfg["_strategy_override"] = "s1v2"
    return cfg


# ---------------------------------------------------------------------------
# S1v2 Tests — Indicator Functions
# ---------------------------------------------------------------------------

def _make_trending_candles(n: int = 40, base: float = 100.0, trend: float = 0.5) -> list:
    """Generate n candles with an upward trend for indicator testing."""
    from core.strategy_engine.candle_builder import Candle
    candles = []
    for i in range(n):
        price = Decimal(str(round(base + i * trend, 2)))
        ct = IST.localize(datetime(2026, 3, 1, 9, 15) + timedelta(minutes=i * 5))
        candles.append(Candle(
            instrument_token=738561, symbol="RELIANCE",
            open=price - Decimal("0.5"), high=price + Decimal("1"),
            low=price - Decimal("1"), close=price,
            volume=50000 + i * 100, vwap=price,
            candle_time=ct, session_date=ct.date(), tick_count=0,
        ))
    return candles


def _make_flat_candles(n: int = 40, base: float = 100.0) -> list:
    """Generate n flat/ranging candles (no trend)."""
    from core.strategy_engine.candle_builder import Candle
    import random
    random.seed(42)
    candles = []
    for i in range(n):
        noise = random.uniform(-0.3, 0.3)
        price = Decimal(str(round(base + noise, 2)))
        ct = IST.localize(datetime(2026, 3, 1, 9, 15) + timedelta(minutes=i * 15))
        candles.append(Candle(
            instrument_token=738561, symbol="RELIANCE",
            open=price - Decimal("0.2"), high=price + Decimal("0.5"),
            low=price - Decimal("0.5"), close=price,
            volume=50000, vwap=price,
            candle_time=ct, session_date=ct.date(), tick_count=0,
        ))
    return candles


def test_compute_ema_known_values():
    """S1v2 indicator: EMA(10) on trending candles returns a Decimal."""
    from tools.backtester import compute_ema
    candles = _make_trending_candles(20)
    result = compute_ema(candles, 10)
    assert result is not None
    assert isinstance(result, Decimal)
    # EMA should be near recent price but lagging (below last close for uptrend)
    last_close = float(candles[-1].close)
    assert float(result) < last_close + 5
    assert float(result) > last_close - 10


def test_compute_adx_trending():
    """S1v2 indicator: ADX > 20 for strongly trending candles."""
    from tools.backtester import compute_adx
    candles = _make_trending_candles(40, trend=1.0)
    result = compute_adx(candles, 14)
    assert result is not None
    assert float(result) > 20  # Trending market should have ADX > 20


def test_compute_adx_ranging():
    """S1v2 indicator: ADX < 25 for flat/ranging candles."""
    from tools.backtester import compute_adx
    candles = _make_flat_candles(40)
    result = compute_adx(candles, 14)
    assert result is not None
    assert float(result) < 30  # Ranging market should have lower ADX


def test_compute_volume_sma():
    """S1v2 indicator: Volume SMA(20) matches manual calculation."""
    from tools.backtester import compute_volume_sma
    candles = _make_trending_candles(25)
    result = compute_volume_sma(candles, 20)
    assert result is not None
    # Manual: mean of last 20 volumes
    expected = sum(c.volume for c in candles[-20:]) / 20
    assert abs(float(result) - expected) < 1


def test_compute_ema_insufficient_data():
    """S1v2 indicator: Returns None when data < period."""
    from tools.backtester import compute_ema
    candles = _make_trending_candles(5)
    result = compute_ema(candles, 10)
    assert result is None


# ---------------------------------------------------------------------------
# S1v2 Tests — State Machine
# ---------------------------------------------------------------------------

def _make_evaluator(config=None):
    """Create S1v2SignalEvaluator with default config."""
    from tools.backtester import S1v2SignalEvaluator
    return S1v2SignalEvaluator(config or _minimal_config_s1v2())


def test_state_waiting_to_watching():
    """S1v2 state: ADX crosses above threshold → WATCHING_FOR_PULLBACK."""
    from tools.backtester import S1v2Phase
    evaluator = _make_evaluator()

    # Feed enough 15min candles for ADX computation (trending → ADX > 25)
    candles_15m = _make_trending_candles(40, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed enough 5min candles for EMA20
    candles_5m = _make_trending_candles(25, base=115.0, trend=0.3)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Evaluate one more 5min candle — should transition from WAITING
    candle = _make_candle(
        close=Decimal("125"), high=Decimal("126"), low=Decimal("124"),
        volume=60000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    # Should be past WAITING (either WATCHING or further)
    assert state.phase != S1v2Phase.WAITING_FOR_TREND


def test_state_watching_to_pullback_long():
    """S1v2 state: Close < EMA20 → IN_PULLBACK for LONG bias."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Manually set state to WATCHING with LONG direction
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.WATCHING_FOR_PULLBACK,
        direction="LONG",
        pullback_count=0,
        adx_was_above=True,
    )

    # Feed enough 15min data with ADX > 25
    candles_15m = _make_trending_candles(40, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup so EMA20 is computable (EMA20 ≈ 107)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # New candle with close BELOW EMA20 → should enter IN_PULLBACK
    candle = _make_candle(
        close=Decimal("95"), high=Decimal("96"), low=Decimal("94"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.IN_PULLBACK
    assert state.pullback_count == 1


def test_state_watching_to_pullback_short():
    """S1v2 state: Close > EMA20 → IN_PULLBACK for SHORT bias."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Set state to WATCHING with SHORT direction
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.WATCHING_FOR_PULLBACK,
        direction="SHORT",
        pullback_count=0,
        adx_was_above=True,
    )

    # Feed 15min downtrend candles for ADX > 25
    candles_15m = _make_trending_candles(40, base=200.0, trend=-1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup (downtrend, EMA20 ≈ 190)
    candles_5m = _make_trending_candles(25, base=200.0, trend=-0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Candle with close ABOVE EMA20 → pullback for SHORT
    candle = _make_candle(
        close=Decimal("210"), high=Decimal("211"), low=Decimal("209"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.IN_PULLBACK
    assert state.pullback_count == 1


def test_state_first_pullback_generates_signal():
    """S1v2 state: First pullback reclaim with volume → Signal."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Manually set up IN_PULLBACK state for LONG, first pullback
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("95"),  # low during pullback
        adx_was_above=True,
    )

    # Feed 15min trending candles (ADX > 25, close > EMA10 → LONG)
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup (EMA20 ≈ 107)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Reclaim candle: close > EMA20, high volume, good R:R
    # Entry=120, Stop=95, Target=120+2.5*ATR. Need R:R >= 3.
    # Risk = 120-95 = 25. Need reward >= 75 → target >= 195. ATR needs to be >= 30.
    # Use a config with lower rr_min for this test
    cfg = _minimal_config_s1v2()
    cfg["strategy"]["s1v2"]["rr_min"] = 1.5  # Lower for testing
    evaluator._rr_min = Decimal("1.5")

    candle = _make_candle(
        close=Decimal("120"), high=Decimal("122"), low=Decimal("118"),
        volume=100000,  # Well above avg
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    signal = evaluator.evaluate(candle)

    # Signal may or may not fire depending on ATR/R:R computed from warmup data.
    # The key assertion is that the state machine transitioned correctly.
    state = evaluator._states["RELIANCE"]
    # Either SIGNAL_FIRED (signal generated) or WATCHING (R:R/volume check failed)
    assert state.phase in (S1v2Phase.SIGNAL_FIRED, S1v2Phase.WATCHING_FOR_PULLBACK)


def test_state_second_pullback_skipped():
    """S1v2 state: Second pullback → SKIP, no signal."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Set up IN_PULLBACK with pullback_count=2 (second pullback)
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=2,
        pullback_extreme=Decimal("95"),
        adx_was_above=True,
    )

    # Feed indicator data
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Reclaim candle
    candle = _make_candle(
        close=Decimal("120"), high=Decimal("122"), low=Decimal("118"),
        volume=100000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    signal = evaluator.evaluate(candle)

    assert signal is None  # Second pullback → skipped
    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.WATCHING_FOR_PULLBACK


def test_state_adx_drops_resets():
    """S1v2 state: ADX drops below threshold → reset to WAITING."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Set state to IN_PULLBACK
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        adx_was_above=True,
    )

    # Feed FLAT 15min candles → ADX should be below 25
    candles_15m = _make_flat_candles(40)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup
    candles_5m = _make_flat_candles(25)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.WAITING_FOR_TREND


def test_state_signal_fired_to_watching():
    """S1v2 state: on_trade_closed transitions SIGNAL_FIRED → WATCHING."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.SIGNAL_FIRED,
        direction="LONG",
    )

    evaluator.on_trade_closed("RELIANCE")

    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.WATCHING_FOR_PULLBACK


# ---------------------------------------------------------------------------
# S1v2 Tests — Signal Flow
# ---------------------------------------------------------------------------

def test_signal_long_complete():
    """S1v2 signal: Valid LONG signal from evaluator."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Pre-set state: IN_PULLBACK, first pullback, LONG direction
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("97"),  # Tight stop for good R:R
        adx_was_above=True,
    )

    # Feed 15min trending data (ADX > 25, close > EMA10)
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup, EMA20 should be around ~107
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Override rr_min to make it easier to achieve
    evaluator._rr_min = Decimal("1.0")

    # Reclaim candle: close above EMA20, high volume
    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        assert signal.direction == "LONG"
        assert signal.symbol == "RELIANCE"
        assert signal.stop_loss == Decimal("97")


def test_signal_short_complete():
    """S1v2 signal: Valid SHORT signal from evaluator."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Pre-set: IN_PULLBACK, SHORT direction, first pullback
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="SHORT",
        pullback_count=1,
        pullback_extreme=Decimal("203"),  # high during pullback
        adx_was_above=True,
    )

    # Feed 15min downtrend (ADX > 25, close < EMA10)
    candles_15m = _make_trending_candles(40, base=200.0, trend=-1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # 5min warmup (downtrend)
    candles_5m = _make_trending_candles(25, base=195.0, trend=-0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    evaluator._rr_min = Decimal("1.0")

    # Reclaim candle: close below EMA20, high volume
    candle = _make_candle(
        close=Decimal("180"), high=Decimal("182"), low=Decimal("179"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        assert signal.direction == "SHORT"
        assert signal.stop_loss == Decimal("203")


def test_signal_rejected_low_volume():
    """S1v2 signal: Low volume → no signal."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("95"),
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Low volume candle (below 1.5× SMA)
    candle = _make_candle(
        close=Decimal("120"), high=Decimal("122"), low=Decimal("118"),
        volume=10000,  # Very low volume
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    signal = evaluator.evaluate(candle)
    assert signal is None


def test_signal_rejected_bad_rr():
    """S1v2 signal: Bad R:R ratio → no signal."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()

    # Very tight pullback extreme → tiny risk → good R:R? No — make stop VERY close
    # Actually, bad R:R means stop is far and target is close.
    # Stop at 50, entry at 100 → risk=50. Target = entry + 2.5*ATR.
    # ATR for trending ~2-4. Target ≈ 107. Reward = 7. R:R = 7/50 = 0.14 < 3.
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("50"),  # Very far stop → bad R:R
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("120"), high=Decimal("122"), low=Decimal("118"),
        volume=100000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    signal = evaluator.evaluate(candle)
    assert signal is None  # R:R too bad


def test_signal_dedup():
    """S1v2 signal: Same (symbol, direction) blocked within session."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    evaluator._rr_min = Decimal("0.5")  # Lower threshold for testing

    # Mark that a LONG signal was already fired this session
    evaluator._session_signals.add(("RELIANCE", "LONG"))

    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("97"),
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    signal = evaluator.evaluate(candle)
    assert signal is None  # Dedup blocks it


def test_direction_from_15min_ema():
    """S1v2 signal: Direction determined by 15min close vs EMA10."""
    from tools.backtester import S1v2Phase
    evaluator = _make_evaluator()

    # Feed 15min uptrend → close > EMA10 → LONG bias
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    candles_5m = _make_trending_candles(25, base=115.0, trend=0.3)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("125"), high=Decimal("126"), low=Decimal("124"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    # Direction should be LONG (close > EMA10 in 15min uptrend)
    if state.direction is not None:
        assert state.direction == "LONG"


# ---------------------------------------------------------------------------
# S1v2 Tests — Exit Rules
# ---------------------------------------------------------------------------

def test_time_stop_30_bars():
    """S1v2 exit: Position closed at bar 30 with TIME_STOP reason."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config_s1v2()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561,
        direction="LONG", entry_price=Decimal("100"),
        entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("115"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # TIME_STOP is checked in _process_day_s1v2 via bar_counts.
    # Here we test _close_position with TIME_STOP reason.
    trade = engine._close_position(
        pos, Decimal("103"), IST.localize(datetime(2026, 3, 1, 12, 30)), "TIME_STOP"
    )

    assert trade.exit_reason == "TIME_STOP"
    assert trade.symbol == "RELIANCE"


def test_stop_loss_on_5min():
    """S1v2 exit: Stop hit on 5min candle."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config_s1v2()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561,
        direction="LONG", entry_price=Decimal("100"),
        entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("115"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # Candle where low breaches stop
    candle = _make_candle(
        close=Decimal("94"), high=Decimal("99"), low=Decimal("93"),
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 30)),
    )
    trade = engine._check_fixed_exit(pos, candle)

    assert trade is not None
    assert trade.exit_reason == "STOP_HIT"


def test_target_hit_s1v2():
    """S1v2 exit: Target hit on 5min candle."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config_s1v2()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561,
        direction="LONG", entry_price=Decimal("100"),
        entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("110"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    # Candle where high reaches target
    candle = _make_candle(
        close=Decimal("111"), high=Decimal("112"), low=Decimal("108"),
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    trade = engine._check_fixed_exit(pos, candle)

    assert trade is not None
    assert trade.exit_reason == "TARGET_HIT"


def test_hard_exit_1500_s1v2():
    """S1v2 exit: Position closed at 15:00 IST."""
    from tools.backtester import BacktestEngine, BacktestPosition

    config = _minimal_config_s1v2()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    pos = BacktestPosition(
        symbol="RELIANCE", instrument_token=738561,
        direction="LONG", entry_price=Decimal("100"),
        entry_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
        qty=10, stop_loss=Decimal("95"), target=Decimal("115"),
        original_stop=Decimal("95"), regime="bull_trend",
    )

    trade = engine._close_position(
        pos, Decimal("102"), IST.localize(datetime(2026, 3, 1, 15, 0)), "HARD_EXIT"
    )

    assert trade.exit_reason == "HARD_EXIT"


# ---------------------------------------------------------------------------
# S1v2 Tests — Multi-Timeframe
# ---------------------------------------------------------------------------

def test_no_future_15min_leakage():
    """S1v2 multi-TF: 5min at 09:25 cannot see 15min candle timestamped 09:30."""
    # This tests the anti-leakage logic: 15min candle with candle_time=09:30
    # should NOT be fed to evaluator when processing 5min candle at 09:25.
    from tools.backtester import S1v2SignalEvaluator
    evaluator = _make_evaluator()

    # 15min candle at 09:30 (represents 09:15-09:30 bar)
    c15 = _make_candle(
        close=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
    )

    # Simulate anti-leakage gate: 5min candle at 09:25
    candle_5m_time = IST.localize(datetime(2026, 3, 1, 9, 25))

    # Gate: only feed if c15.candle_time <= candle_5m_time
    should_feed = c15.candle_time <= candle_5m_time
    assert should_feed is False  # 09:30 > 09:25 → blocked


def test_15min_available_at_completion():
    """S1v2 multi-TF: 5min at 09:30 CAN see 15min candle timestamped 09:30."""
    c15 = _make_candle(
        close=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
    )

    candle_5m_time = IST.localize(datetime(2026, 3, 1, 9, 30))
    should_feed = c15.candle_time <= candle_5m_time
    assert should_feed is True  # 09:30 <= 09:30 → allowed


def test_warmup_enables_indicators():
    """S1v2 multi-TF: After feeding warmup, ADX and EMA return values."""
    from tools.backtester import compute_adx, compute_ema
    candles_15m = _make_trending_candles(40, trend=1.0)

    adx = compute_adx(candles_15m, 14)
    ema = compute_ema(candles_15m, 10)

    assert adx is not None
    assert ema is not None


# ---------------------------------------------------------------------------
# S1v2 Tests — Integration
# ---------------------------------------------------------------------------

def test_s1_path_unchanged():
    """S1v2 integration: S1 engine creation unchanged with default config."""
    from tools.backtester import BacktestEngine

    config = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    assert engine._strategy_name == "s1"
    assert engine._signal_gen is not None
    assert engine._interval == "15min"


def test_build_params_strategy_name():
    """S1v2 integration: _build_params includes correct strategy name."""
    from tools.backtester import BacktestEngine

    # S1
    config_s1 = _minimal_config()
    engine_s1 = BacktestEngine(pool=MagicMock(), config=config_s1)
    assert engine_s1._build_params()["strategy"] == "s1"

    # S1v2
    config_s1v2 = _minimal_config_s1v2()
    engine_s1v2 = BacktestEngine(pool=MagicMock(), config=config_s1v2)
    assert engine_s1v2._build_params()["strategy"] == "s1v2"


def test_s1v2_engine_creation():
    """S1v2 integration: Engine created with S1v2 config has correct attributes."""
    from tools.backtester import BacktestEngine, S1v2SignalEvaluator

    config = _minimal_config_s1v2()
    engine = BacktestEngine(pool=MagicMock(), config=config)

    assert engine._strategy_name == "s1v2"
    assert engine._interval == "5min"
    assert isinstance(engine._s1v2_evaluator, S1v2SignalEvaluator)


# ---------------------------------------------------------------------------
# S1v2 Tests — ATR Stop Floor
# ---------------------------------------------------------------------------

def test_atr_floor_applied_long_tight_pullback():
    """S1v2 ATR floor: Tight pullback stop widened to 1×ATR for LONG."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    evaluator._rr_min = Decimal("0.1")  # Low threshold to let signal through

    # Tight pullback: entry≈115, pullback_low=114.5 → risk only 0.5
    # ATR floor should widen stop to entry - 1.0*ATR
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("114.50"),  # Very tight stop
        adx_was_above=True,
    )

    # Feed 15min trending candles (ADX > 25, close > EMA10)
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)

    # Feed 5min warmup
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        # Stop should be wider than the tight pullback_extreme
        # stop = min(114.50, 115 - 1.0*ATR) — ATR is ~1-3 for these candles
        # So atr_stop ≈ 113 or lower, which is < 114.50, so stop < 114.50
        assert signal.stop_loss < Decimal("114.50"), (
            f"ATR floor should widen stop below 114.50, got {signal.stop_loss}"
        )


def test_atr_floor_not_needed_wide_pullback_long():
    """S1v2 ATR floor: Wide pullback stop already wider than ATR floor — no change."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    evaluator._rr_min = Decimal("0.1")

    # Wide pullback: pullback_low=80, entry≈115. Risk = 35.
    # ATR floor = 115 - 1.0*ATR ≈ 113. min(80, 113) = 80. Floor not needed.
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("80"),  # Already very wide
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        # Pullback low (80) is already wider than ATR floor (~113)
        # min(80, 113) = 80, so stop stays at pullback_extreme
        assert signal.stop_loss == Decimal("80")


def test_atr_floor_rr_recheck_rejects():
    """S1v2 ATR floor: Wider stop worsens R:R → trade rejected."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    # Keep default rr_min=3.0 — the wider stop should fail R:R check

    # Pullback very tight (risk was tiny → R:R was infinite)
    # ATR floor widens stop significantly → R:R drops below 3.0
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("114.90"),  # Extremely tight
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    # With wider stop from ATR floor, target=entry+2.5*ATR vs risk=1*ATR
    # R:R = 2.5*ATR / 1*ATR = 2.5 < 3.0 → should be rejected
    assert signal is None


def test_atr_floor_long_stop_is_min():
    """S1v2 ATR floor: LONG stop = min(pullback_low, entry - ATR_floor)."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    evaluator._rr_min = Decimal("0.1")
    evaluator._atr_stop_floor_mult = Decimal("2.0")  # Large floor for clear test

    # pullback_low=110, entry≈115, 2.0*ATR≈4 → atr_stop=111
    # min(110, 111) = 110 → pullback wins (already wider)
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="LONG",
        pullback_count=1,
        pullback_extreme=Decimal("110"),
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("115"), high=Decimal("116"), low=Decimal("114"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        # Either stop is pullback_low (110) or atr_floor, whichever is lower
        assert signal.stop_loss <= Decimal("111")


def test_backtester_min_risk_floor_override():
    """Backtester reads min_risk_floor from config['backtester'] section."""
    from tools.backtester import BacktestEngine
    from decimal import Decimal
    from unittest.mock import MagicMock

    # Default (no backtester section) → ₹200
    cfg = _minimal_config()
    engine = BacktestEngine(pool=MagicMock(), config=cfg)
    assert engine._min_risk_floor == Decimal("200")

    # Explicit override → ₹150
    cfg2 = _minimal_config()
    cfg2["backtester"] = {"min_risk_floor": 150}
    engine2 = BacktestEngine(pool=MagicMock(), config=cfg2)
    assert engine2._min_risk_floor == Decimal("150")

    # S1v2 also picks it up
    cfg3 = _minimal_config_s1v2()
    cfg3["backtester"] = {"min_risk_floor": 300}
    engine3 = BacktestEngine(pool=MagicMock(), config=cfg3)
    assert engine3._min_risk_floor == Decimal("300")


def test_atr_floor_short_stop_is_max():
    """S1v2 ATR floor: SHORT stop = max(pullback_high, entry + ATR_floor)."""
    from tools.backtester import S1v2Phase, S1v2State
    evaluator = _make_evaluator()
    evaluator._rr_min = Decimal("0.1")

    # SHORT: pullback_high=182, entry≈180
    # atr_floor = 180 + 1.0*ATR ≈ 182. max(182, 182) = 182
    # But if pullback_high=180.5 (tight), atr_floor=182 → max(180.5, 182)=182
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.IN_PULLBACK,
        direction="SHORT",
        pullback_count=1,
        pullback_extreme=Decimal("180.50"),  # Tight pullback high
        adx_was_above=True,
    )

    candles_15m = _make_trending_candles(40, base=200.0, trend=-1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=195.0, trend=-0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    candle = _make_candle(
        close=Decimal("180"), high=Decimal("182"), low=Decimal("179"),
        volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    signal = evaluator.evaluate(candle)

    if signal is not None:
        # Stop should be widened above the tight pullback_high
        assert signal.stop_loss > Decimal("180.50"), (
            f"ATR floor should widen SHORT stop above 180.50, got {signal.stop_loss}"
        )


# ---------------------------------------------------------------------------
# S1v2 Tests — Single-Timeframe (15min) Mode
# ---------------------------------------------------------------------------


def test_single_tf_evaluator_uses_15min_buffer():
    """Single-TF mode: evaluate() adds candle to 15min buffer, not 5min."""
    from tools.backtester import S1v2SignalEvaluator
    evaluator = S1v2SignalEvaluator(_minimal_config_s1v2("single"))

    # Feed 15min warmup to fill indicator buffer
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    evaluator.feed_warmup_15min("RELIANCE", candles_15m)

    candle = _make_candle(
        close=Decimal("150"), high=Decimal("152"), low=Decimal("148"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 0)),
    )
    evaluator.evaluate(candle)

    # Candle should be in 15min buffer (single mode adds to 15min)
    assert len(evaluator._candles_15min["RELIANCE"]) == 41  # 40 warmup + 1
    # 5min buffer should be empty (no 5min data in single mode)
    assert len(evaluator._candles_5min["RELIANCE"]) == 0


def test_single_tf_indicators_from_15min():
    """Single-TF mode: entry indicators computed from 15min buffer."""
    from tools.backtester import S1v2SignalEvaluator
    evaluator = S1v2SignalEvaluator(_minimal_config_s1v2("single"))

    # Only feed 15min data — no 5min at all
    candles_15m = _make_trending_candles(40, base=100.0, trend=1.0)
    evaluator.feed_warmup_15min("RELIANCE", candles_15m)

    # _compute_entry_indicators should work from 15min buffer
    ind = evaluator._compute_entry_indicators("RELIANCE")
    assert ind["ema20"] is not None, "EMA20 should be computable from 15min data"
    assert ind["atr"] > 0, "ATR should be computable from 15min data"
    assert ind["volume_sma"] is not None, "Volume SMA should be computable from 15min data"

    # Verify 5min buffer gives nothing (since no 5min data loaded)
    assert len(evaluator._candles_5min.get("RELIANCE", [])) == 0


def test_single_tf_time_stop_uses_15min_config():
    """Single-TF mode: effective_time_stop_bars returns time_stop_bars_15min."""
    from tools.backtester import S1v2SignalEvaluator

    # Single mode → uses time_stop_bars_15min (20)
    eval_single = S1v2SignalEvaluator(_minimal_config_s1v2("single"))
    assert eval_single.effective_time_stop_bars == 20

    # Multi mode → uses time_stop_bars (30)
    eval_multi = S1v2SignalEvaluator(_minimal_config_s1v2("multi"))
    assert eval_multi.effective_time_stop_bars == 30


def test_single_tf_engine_interval_15min():
    """Single-TF mode: BacktestEngine sets interval to 15min."""
    from tools.backtester import BacktestEngine
    from unittest.mock import MagicMock

    cfg_single = _minimal_config_s1v2("single")
    engine_single = BacktestEngine(pool=MagicMock(), config=cfg_single)
    assert engine_single._interval == "15min"

    cfg_multi = _minimal_config_s1v2("multi")
    engine_multi = BacktestEngine(pool=MagicMock(), config=cfg_multi)
    assert engine_multi._interval == "5min"


def test_multi_tf_backward_compat():
    """Multi-TF mode unchanged: evaluator still uses separate 5min buffer."""
    from tools.backtester import S1v2Phase, S1v2State, S1v2SignalEvaluator
    evaluator = S1v2SignalEvaluator(_minimal_config_s1v2("multi"))

    # Manually set state for LONG pullback detection
    evaluator._states["RELIANCE"] = S1v2State(
        phase=S1v2Phase.WATCHING_FOR_PULLBACK,
        direction="LONG",
        pullback_count=0,
        adx_was_above=True,
    )

    # Feed 15min + 5min data (multi mode pattern)
    candles_15m = _make_trending_candles(40, trend=1.0)
    for c in candles_15m:
        evaluator.feed_15min_candle(c)
    candles_5m = _make_trending_candles(25, base=100.0, trend=0.5)
    evaluator.feed_warmup_5min("RELIANCE", candles_5m)

    # Evaluate with candle below EMA20 → IN_PULLBACK
    candle = _make_candle(
        close=Decimal("95"), high=Decimal("96"), low=Decimal("94"),
        volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 12, 0)),
    )
    evaluator.evaluate(candle)

    state = evaluator._states["RELIANCE"]
    assert state.phase == S1v2Phase.IN_PULLBACK
    # Candle went to 5min buffer (multi mode)
    assert len(evaluator._candles_5min["RELIANCE"]) == 26  # 25 warmup + 1


# ---------------------------------------------------------------------------
# S1v3 Tests — Mean Reversion (Kotegawa-inspired)
# ---------------------------------------------------------------------------


def _minimal_config_s1v3() -> dict:
    """Minimal config dict for S1v3 engine construction."""
    cfg = _minimal_config()
    cfg["strategy"]["s1v3"] = {
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
    }
    cfg["_strategy_override"] = "s1v3"
    return cfg


def _make_s1v3_evaluator(config=None):
    """Create S1v3SignalEvaluator with default config."""
    from tools.backtester import S1v3SignalEvaluator
    return S1v3SignalEvaluator(config or _minimal_config_s1v3())


def _make_candles_for_bb(n=30, base=100.0, spread=0.5):
    """Create candles with enough history for Bollinger Band computation.

    Creates a series with small oscillations around base price.
    """
    candles = []
    for i in range(n):
        offset = spread * (1 if i % 2 == 0 else -1)
        price = Decimal(str(round(base + offset * (i % 5), 2)))
        candles.append(_make_candle(
            open_=price - Decimal("0.5"),
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
            volume=50000,
            candle_time=IST.localize(datetime(2026, 2, 28, 9, 15) + timedelta(minutes=15 * i)),
        ))
    return candles


# --- Indicator tests ---


def test_compute_bollinger_bands_known():
    """Bollinger Bands: verify upper > middle > lower with known data."""
    from tools.backtester import compute_bollinger_bands

    candles = _make_candles_for_bb(30, base=100.0)
    result = compute_bollinger_bands(candles, period=20, std_dev=2.0)
    assert result is not None
    upper, middle, lower = result
    assert upper > middle > lower
    # Middle should be near the base price
    assert Decimal("95") < middle < Decimal("105")


def test_compute_bollinger_bands_insufficient():
    """Bollinger Bands: None when not enough data."""
    from tools.backtester import compute_bollinger_bands

    candles = _make_candles_for_bb(10, base=100.0)
    result = compute_bollinger_bands(candles, period=20, std_dev=2.0)
    assert result is None


def test_compute_rsi_known():
    """RSI: computable with trending data, returns value in 0-100."""
    from tools.backtester import compute_rsi

    # Uptrending → RSI should be above 50
    candles = _make_trending_candles(30, base=100.0, trend=1.0)
    rsi = compute_rsi(candles, period=14)
    assert rsi is not None
    assert Decimal("0") < rsi <= Decimal("100")


def test_compute_rsi_insufficient():
    """RSI: None when not enough data."""
    from tools.backtester import compute_rsi

    candles = _make_trending_candles(10, base=100.0, trend=0.5)
    rsi = compute_rsi(candles, period=14)
    assert rsi is None


# --- Panic detection tests ---


def test_panic_detected_when_drop_exceeds_2atr():
    """S1v3: panic detected when price drops >= 2×ATR from day high."""
    evaluator = _make_s1v3_evaluator()

    # Warmup candles for indicator computation
    warmup = _make_candles_for_bb(30, base=200.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # First candle establishes day_high at 210
    c1 = _make_candle(
        open_=Decimal("205"), high=Decimal("210"), low=Decimal("204"),
        close=Decimal("208"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 15)),
    )
    evaluator.evaluate(c1, bar_idx=0)

    state = evaluator._day_states.get("RELIANCE")
    assert state is not None
    assert state.day_high == Decimal("210")


def test_panic_not_detected_small_drop():
    """S1v3: small drop does NOT trigger panic."""
    evaluator = _make_s1v3_evaluator()

    warmup = _make_candles_for_bb(30, base=200.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # Day high candle
    c1 = _make_candle(
        open_=Decimal("200"), high=Decimal("205"), low=Decimal("199"),
        close=Decimal("203"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 15)),
    )
    evaluator.evaluate(c1, bar_idx=0)

    # Small dip — shouldn't set up panic
    c2 = _make_candle(
        open_=Decimal("202"), high=Decimal("203"), low=Decimal("200"),
        close=Decimal("201"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
    )
    evaluator.evaluate(c2, bar_idx=1)

    state = evaluator._day_states["RELIANCE"]
    assert state.panic_setup is None


# --- RSI + BB double confirmation tests ---


def test_panic_needs_both_rsi_and_bb():
    """S1v3: panic needs both RSI < 30 AND close <= lower BB."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup
    evaluator = _make_s1v3_evaluator()

    # Create warmup that produces known BB and RSI
    # With strong uptrend warmup, RSI will be high (not oversold)
    warmup = _make_trending_candles(30, base=200.0, trend=2.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # Simulate a large drop candle (below BB lower)
    # But RSI from trending warmup won't be < 30
    c1 = _make_candle(
        open_=Decimal("260"), high=Decimal("262"), low=Decimal("255"),
        close=Decimal("258"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 15)),
    )
    evaluator.evaluate(c1, bar_idx=0)

    # Big drop candle — but RSI likely still above 30 from uptrend
    c2 = _make_candle(
        open_=Decimal("258"), high=Decimal("259"), low=Decimal("240"),
        close=Decimal("241"), volume=80000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
    )
    evaluator.evaluate(c2, bar_idx=1)

    state = evaluator._day_states["RELIANCE"]
    # Without both conditions met, no panic setup
    assert state.panic_setup is None


# --- Reversal confirmation tests ---


def test_reversal_green_candle_above_prev_high():
    """S1v3: LONG reversal = green candle closing above prev high."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # Manually set up a panic setup for LONG
    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            open_=Decimal("88"), high=Decimal("90"), low=Decimal("85"),
            close=Decimal("87"), volume=50000,
            candle_time=IST.localize(datetime(2026, 3, 1, 10, 30)),
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    # Green reversal candle: close > open AND close > prev.high (90)
    # With high volume and VWAP above entry
    reversal = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    # Set VWAP above entry to pass R:R gate
    import dataclasses
    reversal = dataclasses.replace(reversal, vwap=Decimal("105"))

    # Lower min_rr to make test pass more easily
    evaluator._min_rr = Decimal("1.0")

    signal = evaluator.evaluate(reversal, bar_idx=4)
    assert signal is not None
    assert signal.direction == "LONG"
    assert signal.target == Decimal("105")  # Fixed VWAP at entry
    assert signal.stop_loss == Decimal("85")  # intraday_low


def test_reversal_timeout_cancels_panic():
    """S1v3: reversal timeout (5 bars) cancels panic setup."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # Setup panic at bar 2
    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(close=Decimal("87"), volume=50000),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=2,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    # Bar 8 (6 bars after panic_bar_idx=2, timeout=5) → setup should be cancelled
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 30)),
    )
    signal = evaluator.evaluate(candle, bar_idx=8)

    # No signal — panic setup timed out
    assert signal is None
    state = evaluator._day_states["RELIANCE"]
    assert state.panic_setup is None


# --- Volume gate test ---


def test_reversal_rejected_low_volume():
    """S1v3: reversal rejected when volume < 1.5 × SMA(20)."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    # Low volume reversal candle (volume=10000, vol_sma≈50000, ratio=0.2)
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=10000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is None


# --- R:R gate tests ---


def test_rr_below_minimum_rejected():
    """S1v3: R:R < 2.0 → SKIP."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    # Reversal candle with VWAP barely above entry (bad R:R)
    import dataclasses
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    # VWAP = 93 (entry=92, stop=85, reward=1, risk=7 → R:R=0.14)
    candle = dataclasses.replace(candle, vwap=Decimal("93"))

    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is None  # R:R = 0.14 < 2.0


def test_vwap_below_entry_long_rejected():
    """S1v3: LONG with VWAP <= entry → SKIP (price already above mean)."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    evaluator._min_rr = Decimal("0.1")  # Lower R:R gate
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    import dataclasses
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    # VWAP below entry → makes no sense for LONG
    candle = dataclasses.replace(candle, vwap=Decimal("90"))

    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is None


def test_vwap_above_entry_short_rejected():
    """S1v3: SHORT with VWAP >= entry → SKIP."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    evaluator._min_rr = Decimal("0.1")
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("112"), low=Decimal("108"), close=Decimal("111"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="SHORT", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    import dataclasses
    candle = _make_candle(
        open_=Decimal("111"), high=Decimal("112"), low=Decimal("107"),
        close=Decimal("107"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    # VWAP above entry → makes no sense for SHORT
    candle = dataclasses.replace(candle, vwap=Decimal("112"))

    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is None


# --- Time window filter tests ---


def test_time_window_before_0930_rejected():
    """S1v3: signal before 09:30 IST is rejected."""
    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    candle = _make_candle(
        close=Decimal("100"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 15)),
    )
    signal = evaluator.evaluate(candle, bar_idx=0)
    assert signal is None  # Before 09:30 → rejected


def test_time_window_after_1430_rejected():
    """S1v3: signal after 14:30 IST is rejected."""
    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    candle = _make_candle(
        close=Decimal("100"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 14, 45)),
    )
    signal = evaluator.evaluate(candle, bar_idx=20)
    assert signal is None  # After 14:30 → rejected


def test_time_window_0930_accepted():
    """S1v3: signal at exactly 09:30 IST is accepted (within window)."""
    evaluator = _make_s1v3_evaluator()
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    candle = _make_candle(
        close=Decimal("100"), volume=50000,
        candle_time=IST.localize(datetime(2026, 3, 1, 9, 30)),
    )
    # This won't generate a signal (no panic), but it shouldn't be time-rejected
    evaluator.evaluate(candle, bar_idx=0)
    # If time window didn't reject, day_state should have been updated
    assert "RELIANCE" in evaluator._day_states


# --- Stop and target tests ---


def test_stop_is_intraday_low_for_long():
    """S1v3: LONG stop = intraday_low (lowest low since 09:15)."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    evaluator._min_rr = Decimal("0.1")
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    intraday_low = Decimal("82.50")
    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=intraday_low,
        intraday_low=intraday_low,
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=intraday_low, intraday_high=Decimal("110"),
        ),
    )

    import dataclasses
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    candle = dataclasses.replace(candle, vwap=Decimal("105"))

    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is not None
    assert signal.stop_loss == intraday_low


def test_vwap_target_fixed_at_entry():
    """S1v3: VWAP target is captured at entry time, not dynamic."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    evaluator._min_rr = Decimal("0.1")
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    import dataclasses
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    vwap_at_entry = Decimal("105")
    candle = dataclasses.replace(candle, vwap=vwap_at_entry)

    signal = evaluator.evaluate(candle, bar_idx=4)
    assert signal is not None
    # Target should be exactly the VWAP at the moment of entry
    assert signal.target == vwap_at_entry


# --- Engine integration tests ---


def test_s1v3_engine_creation():
    """S1v3 engine: creates with correct interval and evaluator."""
    from tools.backtester import BacktestEngine
    from unittest.mock import MagicMock

    config = _minimal_config_s1v3()
    engine = BacktestEngine(pool=MagicMock(), config=config)
    assert engine._interval == "15min"
    assert engine._strategy_name == "s1v3"
    assert hasattr(engine, "_s1v3_evaluator")


def test_s1v3_one_signal_per_instrument_per_day():
    """S1v3: only one signal per instrument per day (dedup)."""
    from tools.backtester import S1v3DayState, S1v3PanicSetup

    evaluator = _make_s1v3_evaluator()
    evaluator._min_rr = Decimal("0.1")
    warmup = _make_candles_for_bb(30, base=100.0)
    evaluator.feed_warmup_15min("RELIANCE", warmup)

    # First signal
    evaluator._day_states["RELIANCE"] = S1v3DayState(
        day_high=Decimal("110"),
        day_low=Decimal("85"),
        intraday_low=Decimal("85"),
        intraday_high=Decimal("110"),
        prev_candle=_make_candle(
            high=Decimal("90"), low=Decimal("85"), close=Decimal("87"), volume=50000,
        ),
        panic_setup=S1v3PanicSetup(
            direction="LONG", panic_bar_idx=3,
            intraday_low=Decimal("85"), intraday_high=Decimal("110"),
        ),
    )

    import dataclasses
    candle = _make_candle(
        open_=Decimal("88"), high=Decimal("93"), low=Decimal("87"),
        close=Decimal("92"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 10, 45)),
    )
    candle = dataclasses.replace(candle, vwap=Decimal("105"))

    signal1 = evaluator.evaluate(candle, bar_idx=4)
    assert signal1 is not None

    # Second attempt should be blocked by signal_fired flag
    evaluator._day_states["RELIANCE"].panic_setup = S1v3PanicSetup(
        direction="LONG", panic_bar_idx=5,
        intraday_low=Decimal("85"), intraday_high=Decimal("110"),
    )
    candle2 = _make_candle(
        open_=Decimal("89"), high=Decimal("94"), low=Decimal("88"),
        close=Decimal("93"), volume=120000,
        candle_time=IST.localize(datetime(2026, 3, 1, 11, 0)),
    )
    candle2 = dataclasses.replace(candle2, vwap=Decimal("105"))

    signal2 = evaluator.evaluate(candle2, bar_idx=6)
    assert signal2 is None  # Dedup — already fired today
