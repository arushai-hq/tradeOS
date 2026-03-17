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
