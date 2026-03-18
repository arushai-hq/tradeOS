#!/usr/bin/env python3
"""
TradeOS — Futures Backtester

Replays historical futures candles (NIFTY/BANKNIFTY) through S1v2/S1v3 strategy
pipelines with futures-specific position sizing (lot-based), charge calculation
(futures MIS rates), and margin-based capital tracking.

Single-instrument mode: one instrument per run (not watchlist scan).

Run modes:
  run      — single backtest
  compare  — compare exit modes (fixed/trailing/partial)
  optimize — parameter sweep
  show     — display stored run results

Usage:
  tradeos futures backtest run --instrument NIFTY --strategy s1v2
  tradeos futures backtest compare --instrument NIFTY --strategy s1v2
  tradeos futures backtest optimize --instrument NIFTY --strategy s1v2 --param atr_mult --range 1.0:0.5:3.0
  tradeos futures backtest show --last-run
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Optional

import pytz
import structlog

# ---------------------------------------------------------------------------
# Ensure PYTHONPATH includes project root (same pattern as data_downloader)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.risk_manager.charge_calculator import ChargeBreakdown
from core.strategy_engine.candle_builder import Candle
from tools.backtester import (
    BacktestPosition,
    BacktestRegimeAdapter,
    BacktestResult,
    BacktestRiskGate,
    BacktestTrade,
    DailyResult,
    S1v2Phase,
    S1v2SignalEvaluator,
    S1v3SignalEvaluator,
    compute_atr,
    compute_bollinger_bands,
    compute_rsi,
    compute_volume_sma,
    _save_run,
    _save_trades,
    _load_run,
    _load_last_run,
    print_report,
    print_optimize_report,
    print_compare_report,
)
from tools.futures_strategies import (
    ORBStrategy,
    VWAPMeanReversionStrategy,
    MACDSupertrendStrategy,
)
from utils.progress import spinner, step_done, step_fail, step_info

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Futures-specific charge rates (Zerodha MIS, verified 2024)
# ---------------------------------------------------------------------------

_FUT_BROKERAGE_RATE = Decimal("0.0003")       # 0.03% per leg
_FUT_BROKERAGE_CAP = Decimal("20")            # ₹20 per leg
_FUT_STT_RATE = Decimal("0.0002")             # 0.02% sell-side only
_FUT_EXCHANGE_TXN_RATE = Decimal("0.0000183") # 0.00183% both legs
_FUT_SEBI_RATE = Decimal("0.000001")          # ₹10 per crore
_FUT_STAMP_DUTY_RATE = Decimal("0.00002")     # 0.002% buy-side
_FUT_GST_RATE = Decimal("0.18")               # 18% on (brokerage + exchange + SEBI)

# Hard exit time for MIS futures (IST)
HARD_EXIT_TIME = dt_time(15, 10)
NO_ENTRY_AFTER_DEFAULT = dt_time(14, 45)


# ---------------------------------------------------------------------------
# FuturesChargeCalculator
# ---------------------------------------------------------------------------

class FuturesChargeCalculator:
    """Compute Zerodha futures MIS intraday charges.

    All arithmetic uses Decimal. No float at any stage.
    Rates verified against Zerodha brokerage calculator (2024).
    """

    def calculate(
        self,
        qty: int,
        entry_price: Decimal,
        exit_price: Decimal,
        direction: str,
    ) -> ChargeBreakdown:
        """Calculate full charge breakdown for a closed futures trade.

        Args:
            qty:         Total shares (num_lots × lot_size).
            entry_price: Entry fill price (Decimal).
            exit_price:  Exit fill price (Decimal).
            direction:   'LONG' or 'SHORT'.

        Returns:
            ChargeBreakdown with all components and total.
        """
        qty_d = Decimal(str(qty))
        turnover_entry = qty_d * entry_price
        turnover_exit = qty_d * exit_price
        total_turnover = turnover_entry + turnover_exit

        # Brokerage: min(0.03% × turnover, ₹20) per leg
        brok_entry = min(turnover_entry * _FUT_BROKERAGE_RATE, _FUT_BROKERAGE_CAP)
        brok_exit = min(turnover_exit * _FUT_BROKERAGE_RATE, _FUT_BROKERAGE_CAP)
        brokerage = brok_entry + brok_exit

        # STT: 0.02% on sell-side only
        if direction == "LONG":
            stt = turnover_exit * _FUT_STT_RATE
        else:
            stt = turnover_entry * _FUT_STT_RATE

        # Exchange transaction charge: 0.00183% on total turnover
        exchange_txn = total_turnover * _FUT_EXCHANGE_TXN_RATE

        # SEBI charges: ₹10 per crore = 1e-6 × total_turnover
        sebi = total_turnover * _FUT_SEBI_RATE

        # Stamp duty: 0.002% on buy-side only
        if direction == "LONG":
            stamp_duty = turnover_entry * _FUT_STAMP_DUTY_RATE
        else:
            stamp_duty = turnover_exit * _FUT_STAMP_DUTY_RATE

        # GST: 18% on (brokerage + exchange + SEBI)
        gst = (brokerage + exchange_txn + sebi) * _FUT_GST_RATE

        total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst

        return ChargeBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exchange_txn,
            sebi=sebi,
            stamp_duty=stamp_duty,
            gst=gst,
            total=total,
        )


# ---------------------------------------------------------------------------
# FuturesPositionSizer
# ---------------------------------------------------------------------------

class FuturesPositionSizer:
    """Lot-based position sizing for futures.

    3-layer calculation:
      Layer 1 (Risk sizing): num_lots = floor(risk_amount / (stop_distance × lot_size))
      Layer 2 (Margin cap):  if margin > available_capital, scale down
      Layer 3 (Viability):   min 1 lot; reject if 1 lot margin exceeds capital
    """

    def calculate(
        self,
        entry_price: Decimal,
        stop_loss: Decimal,
        available_capital: Decimal,
        risk_pct: Decimal,
        lot_size: int,
        margin_rate: Decimal,
    ) -> tuple[int, int] | None:
        """Calculate lot count and total qty for a futures trade.

        Returns:
            (num_lots, total_qty) or None if trade should be rejected.
        """
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == Decimal("0"):
            log.warning("futures_sizer_zero_stop_distance",
                        entry=float(entry_price), stop=float(stop_loss))
            return None

        lot_size_d = Decimal(str(lot_size))

        # Layer 1: Risk-based sizing
        risk_amount = available_capital * risk_pct
        risk_per_lot = stop_distance * lot_size_d
        num_lots = int((risk_amount / risk_per_lot).to_integral_value(rounding=ROUND_DOWN))

        # Layer 2: Margin cap
        if num_lots > 0:
            margin_required = Decimal(str(num_lots)) * lot_size_d * entry_price * margin_rate
            while num_lots > 1 and margin_required > available_capital:
                num_lots -= 1
                margin_required = Decimal(str(num_lots)) * lot_size_d * entry_price * margin_rate

        # Layer 3: Minimum 1 lot; reject if unaffordable
        if num_lots < 1:
            num_lots = 1
        one_lot_margin = lot_size_d * entry_price * margin_rate
        if one_lot_margin > available_capital:
            log.warning("futures_sizer_insufficient_margin",
                        required=float(one_lot_margin),
                        available=float(available_capital))
            return None

        total_qty = num_lots * lot_size
        return (num_lots, total_qty)


# ---------------------------------------------------------------------------
# FuturesCapitalTracker
# ---------------------------------------------------------------------------

class FuturesCapitalTracker:
    """Track margin usage, realized P&L, and drawdown for futures backtesting."""

    def __init__(self, initial_capital: Decimal, margin_rate: Decimal) -> None:
        self.initial_capital = initial_capital
        self.margin_rate = margin_rate
        self.margin_used = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.peak_capital = initial_capital
        self.max_drawdown = Decimal("0")
        self.max_drawdown_pct: float = 0.0

    @property
    def available_capital(self) -> Decimal:
        """Capital available for new positions (total + realized P&L - locked margin)."""
        return self.initial_capital + self.realized_pnl - self.margin_used

    @property
    def current_equity(self) -> Decimal:
        """Current equity (initial + realized P&L)."""
        return self.initial_capital + self.realized_pnl

    def open_position(self, contract_value: Decimal) -> None:
        """Lock margin for a new position."""
        margin = contract_value * self.margin_rate
        self.margin_used += margin

    def close_position(self, contract_value: Decimal, net_pnl: Decimal) -> None:
        """Release margin and record realized P&L."""
        margin = contract_value * self.margin_rate
        self.margin_used = max(Decimal("0"), self.margin_used - margin)
        self.realized_pnl += net_pnl
        self._update_drawdown()

    def _update_drawdown(self) -> None:
        """Update peak capital and max drawdown."""
        equity = self.current_equity
        if equity > self.peak_capital:
            self.peak_capital = equity
        dd = self.peak_capital - equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd
            if self.peak_capital > 0:
                self.max_drawdown_pct = float(dd / self.peak_capital * 100)


# ---------------------------------------------------------------------------
# FuturesBacktestEngine
# ---------------------------------------------------------------------------

class FuturesBacktestEngine:
    """Replays historical futures candles through S1v2/S1v3 strategy pipelines.

    Single-instrument mode: one NIFTY or BANKNIFTY per run.
    Uses lot-based position sizing, futures charge model, and margin tracking.
    """

    def __init__(
        self,
        pool,
        config: dict,
        instrument: str,
        lot_size: int,
        exit_mode: str = "fixed",
        atr_mult: float = 1.5,
        atr_period: int = 14,
        partial_pct: float = 0.5,
        slippage: float = 0.001,
        interval: str = "15min",
    ) -> None:
        self._pool = pool
        self._config = config
        self._instrument = instrument
        self._lot_size = lot_size
        self._exit_mode = exit_mode
        self._atr_mult = Decimal(str(atr_mult))
        self._atr_period = atr_period
        self._partial_pct = Decimal(str(partial_pct))
        self._slippage = Decimal(str(slippage))
        self._interval = {"5minute": "5min", "15minute": "15min"}.get(interval, interval)

        # Strategy dispatch
        strategy_name = config.get("_strategy_override", "s1v2")
        self._strategy_name = strategy_name

        # Futures-specific components
        self._charge_calc = FuturesChargeCalculator()
        self._position_sizer = FuturesPositionSizer()
        self._risk_gate = BacktestRiskGate()

        # Capital from futures config
        fut_bt = config.get("futures", {}).get("backtest", {})
        initial_capital = Decimal(str(fut_bt.get("initial_capital", 1000000)))
        margin_rate = Decimal(str(fut_bt.get("margin_rate", 0.12)))
        self._risk_pct = Decimal(str(fut_bt.get("risk_per_trade_pct", 0.015)))
        self._margin_rate = margin_rate
        self._capital_tracker = FuturesCapitalTracker(initial_capital, margin_rate)

        # Time gates
        no_entry_str = fut_bt.get("no_entry_after", "14:45")
        h, m = map(int, no_entry_str.split(":"))
        self._no_entry_after = dt_time(h, m)
        hard_exit_str = fut_bt.get("hard_exit_time", "15:10")
        h2, m2 = map(int, hard_exit_str.split(":"))
        self._hard_exit_time = dt_time(h2, m2)

        # Reward ratio for fixed exit target
        self._reward_ratio = Decimal(str(fut_bt.get("reward_ratio", 2.0)))

        # Strategy evaluator
        self._evaluator = None
        self._strategy = None
        if strategy_name == "s1v3":
            self._evaluator = S1v3SignalEvaluator(config)
            self._s1v3_warmed_up = False
        elif strategy_name == "s1v2":
            self._evaluator = S1v2SignalEvaluator(config)
            self._s1v2_warmed_up = False
        elif strategy_name == "orb":
            self._strategy = ORBStrategy(config)
        elif strategy_name == "vwap_mr":
            self._strategy = VWAPMeanReversionStrategy(config)
            self._vwap_mr_warmed_up = False
        elif strategy_name == "macd_st":
            self._strategy = MACDSupertrendStrategy(config)
            self._macd_st_warmed_up = False
            self._daily_candles: list[Candle] = []
        else:
            raise ValueError(f"Unknown futures strategy: {strategy_name}")

        # Candle buffer for ATR trailing stop computation
        self._candle_buffer: list[Candle] = []

        # Pending partial exit trades
        self._pending_partial_trades: list[BacktestTrade] = []

        # OI data (parallel dict: candle_time → {oi, oi_change, oi_change_pct})
        self._oi_data: dict[datetime, dict] = {}

        # Signal diagnostic counters (accumulated across all days)
        self._signal_diag: dict[str, int] = defaultdict(int)

        # Near-month contract filter (resolved async before first run)
        self._tradingsymbol: str | None = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    async def _resolve_near_month_contract(
        self, date_from: date, date_to: date,
    ) -> str:
        """Determine the near-month tradingsymbol (earliest expiry with data).

        Intraday rows have tradingsymbol like 'NIFTY26MARFUT' with expiry date.
        Daily continuous rows have tradingsymbol='' — we skip those.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tradingsymbol, expiry "
                "FROM backtest_futures_candles "
                "WHERE instrument = $1 AND interval = $2 "
                "  AND tradingsymbol != '' "
                "  AND expiry IS NOT NULL "
                "  AND timestamp::date BETWEEN $3 AND $4 "
                "GROUP BY tradingsymbol, expiry "
                "ORDER BY expiry "
                "LIMIT 1",
                self._instrument, self._interval, date_from, date_to,
            )
        if row:
            ts = row["tradingsymbol"]
            log.info(
                "futures_backtest_near_month",
                instrument=self._instrument,
                tradingsymbol=ts,
                expiry=str(row["expiry"]),
            )
            return ts
        # Fallback: no intraday per-contract data, use all
        log.warning(
            "futures_backtest_no_contract",
            instrument=self._instrument,
            msg="No per-contract intraday data found, using all rows",
        )
        return ""

    async def _get_trading_days(self, date_from: date, date_to: date) -> list[date]:
        """Get distinct trading days from futures candle data."""
        ts_filter = self._tradingsymbol
        if ts_filter:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT timestamp::date AS session_date "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND tradingsymbol = $3 "
                    "  AND timestamp::date BETWEEN $4 AND $5 "
                    "ORDER BY session_date",
                    self._instrument, self._interval, ts_filter,
                    date_from, date_to,
                )
        else:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT timestamp::date AS session_date "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND timestamp::date BETWEEN $3 AND $4 "
                    "ORDER BY session_date",
                    self._instrument, self._interval, date_from, date_to,
                )
        return [r["session_date"] for r in rows]

    async def _load_day_candles(self, day: date) -> list[Candle]:
        """Load one day's futures candles for this instrument (near-month only)."""
        ts_filter = self._tradingsymbol
        if ts_filter:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND tradingsymbol = $3 "
                    "  AND timestamp::date = $4 "
                    "ORDER BY timestamp",
                    self._instrument, self._interval, ts_filter, day,
                )
        else:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND timestamp::date = $3 "
                    "ORDER BY timestamp",
                    self._instrument, self._interval, day,
                )

        candles: list[Candle] = []
        for r in rows:
            ts = r["timestamp"]
            # Always convert to IST — DB may store UTC (Bug 3 fix)
            if ts.tzinfo is None:
                ts = IST.localize(ts)
            else:
                ts = ts.astimezone(IST)
            candles.append(Candle(
                instrument_token=0,
                symbol=self._instrument,
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
                vwap=Decimal(str(r["close"])),  # Placeholder — overwritten by _compute_vwap
                candle_time=ts,
                session_date=day,
                tick_count=0,
            ))
        return candles

    async def _load_warmup_candles(self, day: date, count: int = 100) -> list[Candle]:
        """Load prior candles for indicator warmup (near-month only)."""
        ts_filter = self._tradingsymbol
        if ts_filter:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND tradingsymbol = $3 "
                    "  AND timestamp::date < $4 "
                    "ORDER BY timestamp DESC "
                    "LIMIT $5",
                    self._instrument, self._interval, ts_filter, day, count,
                )
        else:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = $2 "
                    "  AND timestamp::date < $3 "
                    "ORDER BY timestamp DESC "
                    "LIMIT $4",
                    self._instrument, self._interval, day, count,
                )

        candles: list[Candle] = []
        for r in reversed(rows):  # Reverse to chronological order
            ts = r["timestamp"]
            # Always convert to IST — DB may store UTC (Bug 3 fix)
            if ts.tzinfo is None:
                ts = IST.localize(ts)
            else:
                ts = ts.astimezone(IST)
            candles.append(Candle(
                instrument_token=0,
                symbol=self._instrument,
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
                vwap=Decimal(str(r["close"])),
                candle_time=ts,
                session_date=ts.date() if hasattr(ts, "date") else day,
                tick_count=0,
            ))
        return candles

    async def _load_daily_candles(
        self, up_to_date: date, lookback: int = 60,
    ) -> list[Candle]:
        """Load daily candles for multi-timeframe strategies (macd_st)."""
        ts_filter = self._tradingsymbol
        if ts_filter:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = 'day' "
                    "  AND tradingsymbol = $2 "
                    "  AND timestamp::date <= $3 "
                    "ORDER BY timestamp DESC "
                    "LIMIT $4",
                    self._instrument, ts_filter, up_to_date, lookback,
                )
        else:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT open, high, low, close, volume, oi, timestamp "
                    "FROM backtest_futures_candles "
                    "WHERE instrument = $1 AND interval = 'day' "
                    "  AND timestamp::date <= $2 "
                    "ORDER BY timestamp DESC "
                    "LIMIT $3",
                    self._instrument, up_to_date, lookback,
                )

        candles: list[Candle] = []
        for r in reversed(rows):  # Reverse to chronological order
            ts = r["timestamp"]
            if ts.tzinfo is None:
                ts = IST.localize(ts)
            else:
                ts = ts.astimezone(IST)
            candles.append(Candle(
                instrument_token=0,
                symbol=self._instrument,
                open=Decimal(str(r["open"])),
                high=Decimal(str(r["high"])),
                low=Decimal(str(r["low"])),
                close=Decimal(str(r["close"])),
                volume=int(r["volume"]),
                vwap=Decimal(str(r["close"])),
                candle_time=ts,
                session_date=ts.date() if hasattr(ts, "date") else up_to_date,
                tick_count=0,
            ))
        return candles

    # ------------------------------------------------------------------
    # Indicators
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_vwap_for_day(candles: list[Candle]) -> list[Candle]:
        """Compute running intraday VWAP — same as equity backtester.

        VWAP = cumulative(typical_price × volume) / cumulative(volume)
        typical_price = (high + low + close) / 3
        """
        cum_tp_vol = Decimal("0")
        cum_vol = Decimal("0")
        result: list[Candle] = []
        for c in candles:
            tp = (c.high + c.low + c.close) / Decimal("3")
            vol = Decimal(str(c.volume))
            cum_tp_vol += tp * vol
            cum_vol += vol
            vwap = cum_tp_vol / cum_vol if cum_vol > 0 else c.close
            result.append(dataclasses.replace(c, vwap=vwap))
        return result

    def _compute_oi_indicators(self, candles: list[Candle], day_oi: list[int | None]) -> None:
        """Compute OI change indicators, stored in self._oi_data.

        Not consumed by S1v2/S1v3 yet — available for future strategy versions.
        """
        prev_oi: int | None = None
        for candle, oi in zip(candles, day_oi):
            entry: dict = {"oi": oi, "oi_change": None, "oi_change_pct": None}
            if oi is not None and prev_oi is not None and prev_oi > 0:
                entry["oi_change"] = oi - prev_oi
                entry["oi_change_pct"] = (oi - prev_oi) / prev_oi * 100
            self._oi_data[candle.candle_time] = entry
            prev_oi = oi

    # ------------------------------------------------------------------
    # Exit modes
    # ------------------------------------------------------------------

    def _check_fixed_exit(
        self, pos: BacktestPosition, candle: Candle
    ) -> BacktestTrade | None:
        """Fixed exit: stop or target, pessimistic on same-candle conflict."""
        stop_hit = False
        target_hit = False

        if pos.direction == "LONG":
            stop_hit = candle.low <= pos.stop_loss
            target_hit = candle.high >= pos.target
        else:
            stop_hit = candle.high >= pos.stop_loss
            target_hit = candle.low <= pos.target

        if stop_hit and target_hit:
            return self._close_position(pos, pos.stop_loss, candle.candle_time, "STOP_HIT")
        if stop_hit:
            return self._close_position(pos, pos.stop_loss, candle.candle_time, "STOP_HIT")
        if target_hit:
            return self._close_position(pos, pos.target, candle.candle_time, "TARGET_HIT")
        return None

    def _check_trailing_exit(
        self, pos: BacktestPosition, candle: Candle
    ) -> BacktestTrade | None:
        """Trailing stop: ATR-based trail with 0.5% floor."""
        fixed_result = self._check_fixed_exit(pos, candle)
        if fixed_result is not None:
            return fixed_result

        # Update trailing stop
        if len(self._candle_buffer) >= self._atr_period:
            from tools.backtester import compute_atr
            atr = compute_atr(self._candle_buffer, self._atr_period)
            if atr and atr > 0:
                trail_distance = atr * self._atr_mult
                min_distance = candle.close * Decimal("0.005")
                trail_distance = max(trail_distance, min_distance)

                if pos.direction == "LONG":
                    new_stop = candle.close - trail_distance
                    if new_stop > pos.stop_loss:
                        pos.stop_loss = new_stop
                else:
                    new_stop = candle.close + trail_distance
                    if new_stop < pos.stop_loss:
                        pos.stop_loss = new_stop

        return None

    def _check_partial_exit(
        self, pos: BacktestPosition, candle: Candle
    ) -> BacktestTrade | None:
        """Partial exit: 50% at 1R profit, trail remainder."""
        if not pos.partial_exited:
            risk_distance = abs(pos.entry_price - pos.original_stop)
            at_1r = (pos.entry_price + risk_distance if pos.direction == "LONG"
                     else pos.entry_price - risk_distance)

            one_r_hit = (candle.high >= at_1r if pos.direction == "LONG"
                         else candle.low <= at_1r)

            if one_r_hit:
                partial_qty = int(
                    (Decimal(str(pos.qty)) * self._partial_pct)
                    .to_integral_value(rounding=ROUND_DOWN)
                )
                if partial_qty > 0 and partial_qty < pos.qty:
                    partial_trade = self._close_position_partial(
                        pos, at_1r, candle.candle_time, "PARTIAL_1R", partial_qty,
                    )
                    pos.partial_exited = True
                    pos.qty -= partial_qty
                    # Move stop to breakeven
                    pos.stop_loss = pos.entry_price
                    self._pending_partial_trades.append(partial_trade)

        # Trail remainder
        return self._check_trailing_exit(pos, candle)

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    def _close_position(
        self,
        pos: BacktestPosition,
        exit_price: Decimal,
        exit_time: datetime,
        reason: str,
    ) -> BacktestTrade:
        """Close a position and compute P&L with futures charges."""
        exit_price = self._apply_slippage(exit_price, pos.direction, is_entry=False)

        if pos.direction == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * Decimal(str(pos.qty))
        else:
            gross_pnl = (pos.entry_price - exit_price) * Decimal(str(pos.qty))

        charges_breakdown = self._charge_calc.calculate(
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
        )
        net_pnl = gross_pnl - charges_breakdown.total

        # Update capital tracker
        contract_value = Decimal(str(pos.qty)) * pos.entry_price
        self._capital_tracker.close_position(contract_value, net_pnl)

        return BacktestTrade(
            symbol=pos.symbol,
            instrument_token=pos.instrument_token,
            direction=pos.direction,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            qty=pos.qty,
            gross_pnl=gross_pnl,
            charges=charges_breakdown.total,
            net_pnl=net_pnl,
            regime=pos.regime,
        )

    def _close_position_partial(
        self,
        pos: BacktestPosition,
        exit_price: Decimal,
        exit_time: datetime,
        reason: str,
        qty: int,
    ) -> BacktestTrade:
        """Close partial quantity of a position."""
        exit_price = self._apply_slippage(exit_price, pos.direction, is_entry=False)

        if pos.direction == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * Decimal(str(qty))
        else:
            gross_pnl = (pos.entry_price - exit_price) * Decimal(str(qty))

        charges_breakdown = self._charge_calc.calculate(
            qty=qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
        )
        net_pnl = gross_pnl - charges_breakdown.total

        return BacktestTrade(
            symbol=pos.symbol,
            instrument_token=pos.instrument_token,
            direction=pos.direction,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            qty=qty,
            gross_pnl=gross_pnl,
            charges=charges_breakdown.total,
            net_pnl=net_pnl,
            regime=pos.regime,
        )

    def _apply_slippage(
        self, price: Decimal, direction: str, is_entry: bool
    ) -> Decimal:
        """Apply slippage: adverse direction for both entry and exit."""
        if is_entry:
            if direction == "LONG":
                return price * (Decimal("1") + self._slippage)
            else:
                return price * (Decimal("1") - self._slippage)
        else:
            if direction == "LONG":
                return price * (Decimal("1") - self._slippage)
            else:
                return price * (Decimal("1") + self._slippage)

    # ------------------------------------------------------------------
    # Signal diagnostics
    # ------------------------------------------------------------------

    def _evaluate_with_reason_s1v2(self, candle: Candle):
        """Wrapper around S1v2 evaluate() that tracks rejection reasons.

        Calls evaluate() normally (which mutates state), then inspects
        pre/post state + indicators to determine which step rejected.
        Returns (signal, reason_str).
        """
        evaluator = self._evaluator
        symbol = candle.symbol
        state = evaluator._states[symbol]

        # Snapshot pre-call state
        pre_phase = state.phase
        pre_direction = state.direction

        # Real evaluate — mutates state, appends candle to buffer
        signal = evaluator.evaluate(candle)

        if signal is not None:
            return signal, "signal_generated"

        # Post-hoc: compute same indicators evaluate() used
        ind_15m = evaluator._compute_15min_indicators(symbol)
        ind_entry = evaluator._compute_entry_indicators(symbol)

        ema10 = ind_15m["ema10"]
        adx = ind_15m["adx"]
        close_15m = ind_15m["close"]
        ema20 = ind_entry["ema20"]
        atr = ind_entry["atr"]
        vol_sma = ind_entry["volume_sma"]

        # Step 0: Insufficient indicator data
        if any(v is None for v in (ema10, adx, close_15m, ema20)):
            return None, "insufficient_indicators"
        if atr <= 0 or vol_sma is None or vol_sma <= 0:
            return None, "insufficient_indicators"

        # Step 1: No directional bias
        if close_15m == ema10:
            return None, "no_directional_bias"
        bias = "LONG" if close_15m > ema10 else "SHORT"

        # Step 2: ADX below threshold
        if adx <= evaluator._adx_threshold:
            return None, "adx_below_threshold"

        post_phase = state.phase

        # Just entered WATCHING from WAITING (ADX crossed above)
        if pre_phase == S1v2Phase.WAITING_FOR_TREND:
            return None, "entered_watching"

        # Already fired signal, waiting for trade close
        if pre_phase == S1v2Phase.SIGNAL_FIRED:
            return None, "already_fired"

        # Direction changed mid-trend
        if bias != pre_direction:
            return None, "direction_changed"

        # Was WATCHING — check if pullback detected or not
        if pre_phase == S1v2Phase.WATCHING_FOR_PULLBACK:
            if post_phase == S1v2Phase.IN_PULLBACK:
                return None, "pullback_entered"
            return None, "no_pullback"

        # Was IN_PULLBACK
        if pre_phase == S1v2Phase.IN_PULLBACK:
            if post_phase == S1v2Phase.IN_PULLBACK:
                return None, "not_reclaimed"

            # Reclaimed EMA20 but something failed — determine which check
            if state.pullback_count > 1:
                return None, "second_pullback"

            vol_ratio = Decimal(str(candle.volume)) / vol_sma
            if vol_ratio < evaluator._volume_ratio_min:
                return None, "volume_rejected"

            # R:R check (replicate evaluate's math)
            entry_price = candle.close
            raw_stop = state.pullback_extreme
            atr_floor = evaluator._atr_stop_floor_mult * atr
            if bias == "LONG":
                stop_loss = min(raw_stop, entry_price - atr_floor)
                risk = entry_price - stop_loss
                reward = (entry_price + evaluator._atr_target_mult * atr) - entry_price
            else:
                stop_loss = max(raw_stop, entry_price + atr_floor)
                risk = stop_loss - entry_price
                reward = entry_price - (entry_price - evaluator._atr_target_mult * atr)

            if risk <= 0:
                return None, "risk_zero"

            rr = reward / risk
            if rr < evaluator._rr_min:
                return None, "rr_rejected"

            return None, "session_dedup"

        return None, "unknown"

    def _evaluate_with_reason_s1v3(self, candle: Candle, bar_idx: int):
        """Wrapper around S1v3 evaluate() that tracks rejection reasons.

        Calls evaluate() normally, then inspects state to determine which
        step rejected. Returns (signal, reason_str).
        """
        evaluator = self._evaluator
        symbol = candle.symbol

        # Snapshot pre-call state
        pre_state = evaluator._day_states.get(symbol)
        was_signal_fired = pre_state.signal_fired if pre_state else False
        had_panic_setup = (pre_state.panic_setup is not None) if pre_state else False

        # Real evaluate
        signal = evaluator.evaluate(candle, bar_idx)

        if signal is not None:
            return signal, "signal_generated"

        state = evaluator._day_states.get(symbol)
        if state is None:
            return None, "unknown"

        # Step 1: Already fired today
        if was_signal_fired:
            return None, "already_fired_today"

        # Step 2: Time window
        ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time
        if ct < evaluator._signal_start or ct >= evaluator._signal_end:
            return None, "outside_time_window"

        # Step 3: Indicators
        buf = evaluator._candles_15min[symbol]
        atr = compute_atr(buf, evaluator._atr_period)
        rsi = compute_rsi(buf, evaluator._rsi_period)
        bb = compute_bollinger_bands(buf, evaluator._bb_period, evaluator._bb_std)
        vol_sma = compute_volume_sma(buf, evaluator._volume_sma_period)

        if atr <= 0 or rsi is None or bb is None or vol_sma is None or vol_sma <= 0:
            return None, "insufficient_indicators"

        # Step 4: Had a pending panic setup
        if had_panic_setup:
            if state.panic_setup is None:
                # Setup consumed — timeout or reversal check failed
                return None, "reversal_check_failed"
            # Setup still pending — not a reversal candle
            return None, "awaiting_reversal"

        # Step 5: No panic setup — check if one was just created
        if state.panic_setup is not None:
            return None, "panic_detected"

        # No panic detected at all
        return None, "no_panic_detected"

    def _log_signal_diagnostics(self) -> None:
        """Print signal diagnostic summary at end of run."""
        d = dict(self._signal_diag)
        if not d:
            return

        total = d.get("total_candles", 0)
        signals = d.get("signal_generated", 0)

        print("\n" + "=" * 60)
        print("  SIGNAL DIAGNOSTIC SUMMARY")
        print("=" * 60)
        print(f"  Strategy:    {self._strategy_name}")
        print(f"  Instrument:  {self._instrument}")
        print(f"  Interval:    {self._interval}")
        print("-" * 60)
        print(f"  {'Total candles processed':<38} {total:>6}")
        print(f"  {'Signals generated':<38} {signals:>6}")
        print("-" * 60)

        # Sort remaining reasons by count descending
        skip = {"total_candles", "signal_generated", "risk_gate_blocked",
                "sizing_failed", "position_open", "no_entry_after"}
        reasons = sorted(
            ((k, v) for k, v in d.items() if k not in skip),
            key=lambda x: x[1],
            reverse=True,
        )
        if reasons:
            print("  Rejection Breakdown:")
            for reason, count in reasons:
                label = reason.replace("_", " ").title()
                pct = count / total * 100 if total > 0 else 0
                print(f"    {label:<36} {count:>6}  ({pct:5.1f}%)")

        # Post-signal rejections
        print("-" * 60)
        post_signal = {k: d.get(k, 0) for k in
                       ("risk_gate_blocked", "sizing_failed")}
        for key, count in post_signal.items():
            if count > 0:
                label = key.replace("_", " ").title()
                print(f"  {label:<38} {count:>6}")

        print("=" * 60 + "\n")

        log.info("futures_backtest_signal_diagnostics", **d)

    # ------------------------------------------------------------------
    # Diagnostic wrappers — new strategies
    # ------------------------------------------------------------------

    def _evaluate_with_reason_orb(
        self, candle: Candle,
    ) -> tuple:
        """ORB diagnostic wrapper. Returns (signal, reason_str)."""
        strategy = self._strategy
        # Snapshot state before evaluate
        was_range_formed = strategy._range_formed
        was_range_invalid = strategy._range_invalid
        prev_trades = strategy._trades_today

        signal = strategy.evaluate(candle, self._candle_buffer)

        if signal is not None:
            return signal, "signal_generated"

        ct = candle.candle_time.time()
        if ct < dt_time(9, 15):
            return None, "before_market"
        if not was_range_formed and ct < strategy._range_end_time:
            return None, "range_forming"
        if strategy._range_invalid and not was_range_invalid:
            # Range was just invalidated this candle
            rh = strategy._range_high or Decimal("0")
            rl = strategy._range_low or Decimal("0")
            mid = (rh + rl) / Decimal("2") if (rh + rl) > 0 else Decimal("1")
            rpct = (rh - rl) / mid if mid != 0 else Decimal("0")
            if rpct < strategy._min_range_pct:
                return None, "range_too_narrow"
            if rpct > strategy._max_range_pct:
                return None, "range_too_wide"
            return None, "range_invalid"
        if strategy._range_invalid:
            return None, "range_invalid"
        if strategy._trades_today >= strategy._max_trades_per_day and prev_trades >= strategy._max_trades_per_day:
            return None, "max_trades_reached"
        if ct >= strategy._no_entry_after:
            return None, "after_cutoff"
        # Must be no breakout or volume insufficient
        if candle.close > (strategy._range_high or Decimal("999999")) or \
           candle.close < (strategy._range_low or Decimal("0")):
            return None, "volume_insufficient"
        return None, "no_breakout"

    def _evaluate_with_reason_vwap_mr(
        self, candle: Candle, day_candles_so_far: list[Candle],
    ) -> tuple:
        """VWAP MR diagnostic wrapper. Returns (signal, reason_str)."""
        strategy = self._strategy
        prev_trades = strategy._trades_today

        signal = strategy.evaluate(candle, self._candle_buffer, day_candles_so_far)

        if signal is not None:
            return signal, "signal_generated"

        ct = candle.candle_time.time()
        if ct < dt_time(9, 30) or ct >= strategy._no_entry_after:
            return None, "after_cutoff"
        if prev_trades >= strategy._max_trades_per_day:
            return None, "max_trades_reached"
        if len(day_candles_so_far) < 3:
            return None, "insufficient_indicators"

        # Check ADX
        from tools.futures_strategies import compute_adx
        adx = compute_adx(self._candle_buffer, strategy._adx_period)
        if adx is not None and adx >= strategy._adx_max_threshold:
            return None, "adx_too_high"

        # Check RSI
        from tools.futures_strategies import compute_rsi
        rsi = compute_rsi(self._candle_buffer, strategy._rsi_period)
        if rsi is None:
            return None, "insufficient_indicators"

        # Check band position
        from tools.futures_strategies import compute_vwap_with_bands
        vwap, upper, lower = compute_vwap_with_bands(day_candles_so_far, strategy._band_mult)
        if not (candle.close <= lower or candle.close >= upper):
            return None, "not_at_band"
        if candle.close <= lower and rsi >= strategy._rsi_oversold:
            return None, "rsi_neutral"
        if candle.close >= upper and rsi <= strategy._rsi_overbought:
            return None, "rsi_neutral"

        # Must be distance too small
        return None, "distance_too_small"

    def _evaluate_with_reason_macd_st(
        self, candle: Candle,
    ) -> tuple:
        """MACD/Supertrend diagnostic wrapper. Returns (signal, reason_str)."""
        strategy = self._strategy
        prev_trades = strategy._trades_today

        signal = strategy.evaluate(candle, self._candle_buffer)

        if signal is not None:
            return signal, "signal_generated"

        ct = candle.candle_time.time()
        if ct < dt_time(9, 30) or ct >= strategy._no_entry_after:
            return None, "after_cutoff"
        if strategy._daily_bias is None:
            return None, "no_daily_bias"
        if prev_trades >= strategy._max_trades_per_day:
            return None, "max_trades_reached"

        # Check EMA filter
        from tools.futures_strategies import compute_ema
        closes = [c.close for c in self._candle_buffer]
        ema50 = compute_ema(closes, strategy._ema_trend_period)
        if ema50 is None:
            return None, "insufficient_indicators"
        if strategy._daily_bias == "LONG" and candle.close <= ema50:
            return None, "ema_filter_failed"
        if strategy._daily_bias == "SHORT" and candle.close >= ema50:
            return None, "ema_filter_failed"

        # Check MACD
        from tools.futures_strategies import compute_macd
        macd_result = compute_macd(
            self._candle_buffer, strategy._macd_fast, strategy._macd_slow, strategy._macd_signal,
        )
        if macd_result is None:
            return None, "insufficient_indicators"

        _ml, _sl, histogram = macd_result
        if strategy._prev_histogram is None:
            return None, "insufficient_indicators"

        # Check for crossover vs direction mismatch
        prev_h = strategy._prev_histogram
        if (prev_h < 0 and histogram >= 0) or (prev_h > 0 and histogram <= 0):
            return None, "direction_mismatch"

        return None, "no_macd_crossover"

    # ------------------------------------------------------------------
    # Day processing
    # ------------------------------------------------------------------

    async def _process_day_s1v2(
        self, day: date, regime_adapter: BacktestRegimeAdapter,
    ) -> list[BacktestTrade]:
        """Simulate one trading day using S1v2 on single-instrument futures."""
        evaluator = self._evaluator
        evaluator.reset_session()

        candles = await self._load_day_candles(day)
        if not candles:
            return []

        candles = self._compute_vwap_for_day(candles)

        # Warmup (first day only)
        if not self._s1v2_warmed_up:
            warmup = await self._load_warmup_candles(day)
            evaluator.feed_warmup_15min(self._instrument, warmup)
            self._s1v2_warmed_up = True

        # Simulated state
        open_position: BacktestPosition | None = None
        bar_count = 0
        shared_state: dict = {
            "open_positions": {},
            "pending_signals": 0,
            "kill_switch_level": 0,
        }
        config = self._config
        day_trades: list[BacktestTrade] = []
        time_stop = evaluator.effective_time_stop_bars

        for candle in candles:
            # NOTE: Do NOT call feed_15min_candle() here — evaluate() already
            # appends the candle to the evaluator's internal buffer.
            self._candle_buffer.append(candle)
            if len(self._candle_buffer) > 200:
                self._candle_buffer = self._candle_buffer[-200:]

            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time

            # --- Check exits ---
            if open_position is not None:
                bar_count += 1

                # Time stop
                if bar_count >= time_stop:
                    trade = self._close_position(open_position, candle.close, candle.candle_time, "TIME_STOP")
                    day_trades.append(trade)
                    open_position = None
                    bar_count = 0
                    evaluator.on_trade_closed(self._instrument)
                    shared_state["open_positions"] = {}
                    continue

                # Exit mode dispatch
                trade = None
                if self._exit_mode == "trailing":
                    trade = self._check_trailing_exit(open_position, candle)
                elif self._exit_mode == "partial":
                    trade = self._check_partial_exit(open_position, candle)
                else:
                    trade = self._check_fixed_exit(open_position, candle)

                if trade is not None:
                    day_trades.append(trade)
                    open_position = None
                    bar_count = 0
                    evaluator.on_trade_closed(self._instrument)
                    shared_state["open_positions"] = {}

            # --- Hard exit ---
            if ct >= self._hard_exit_time and open_position is not None:
                trade = self._close_position(open_position, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                open_position = None
                bar_count = 0
                evaluator.on_trade_closed(self._instrument)
                shared_state["open_positions"] = {}
                continue

            # --- Signal generation ---
            if open_position is not None:
                self._signal_diag["position_open"] += 1
                continue  # Already in a position
            if ct >= self._no_entry_after:
                self._signal_diag["no_entry_after"] += 1
                continue

            self._signal_diag["total_candles"] += 1
            signal, diag_reason = self._evaluate_with_reason_s1v2(candle)
            self._signal_diag[diag_reason] += 1
            if signal is None:
                continue

            # Risk gate
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                self._signal_diag["risk_gate_blocked"] += 1
                continue

            # Position sizing (lot-based)
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True,
            )
            sizing = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                available_capital=self._capital_tracker.available_capital,
                risk_pct=self._risk_pct,
                lot_size=self._lot_size,
                margin_rate=self._margin_rate,
            )
            if sizing is None:
                self._signal_diag["sizing_failed"] += 1
                continue
            num_lots, total_qty = sizing

            # Lock margin
            contract_value = Decimal(str(total_qty)) * entry_price
            self._capital_tracker.open_position(contract_value)

            # Open position
            open_position = BacktestPosition(
                symbol=self._instrument,
                instrument_token=0,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=total_qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            bar_count = 0
            shared_state["open_positions"] = {
                self._instrument: {"direction": signal.direction},
            }

        # End of day: force-close remaining position
        if open_position is not None and candles:
            last_candle = candles[-1]
            trade = self._close_position(
                open_position, last_candle.close, last_candle.candle_time, "HARD_EXIT",
            )
            day_trades.append(trade)
            if hasattr(self._evaluator, "on_trade_closed"):
                self._evaluator.on_trade_closed(self._instrument)

        # Collect partial exit trades
        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []

        return day_trades

    async def _process_day_s1v3(
        self, day: date, regime_adapter: BacktestRegimeAdapter,
    ) -> list[BacktestTrade]:
        """Simulate one trading day using S1v3 on single-instrument futures."""
        evaluator = self._evaluator
        evaluator.reset_day()

        candles = await self._load_day_candles(day)
        if not candles:
            return []

        candles = self._compute_vwap_for_day(candles)

        # Warmup (first day only)
        if not self._s1v3_warmed_up:
            warmup = await self._load_warmup_candles(day)
            for c in warmup:
                evaluator._candles_15min[self._instrument].append(c)
            self._s1v3_warmed_up = True

        # Simulated state
        open_position: BacktestPosition | None = None
        shared_state: dict = {
            "open_positions": {},
            "pending_signals": 0,
            "kill_switch_level": 0,
        }
        config = self._config
        day_trades: list[BacktestTrade] = []

        for bar_idx, candle in enumerate(candles):
            # NOTE: Do NOT append to evaluator._candles_15min here — evaluate()
            # already appends the candle to the internal buffer.
            self._candle_buffer.append(candle)
            if len(self._candle_buffer) > 200:
                self._candle_buffer = self._candle_buffer[-200:]

            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time

            # --- Check exits ---
            if open_position is not None:
                trade = None
                if self._exit_mode == "trailing":
                    trade = self._check_trailing_exit(open_position, candle)
                elif self._exit_mode == "partial":
                    trade = self._check_partial_exit(open_position, candle)
                else:
                    trade = self._check_fixed_exit(open_position, candle)

                if trade is not None:
                    day_trades.append(trade)
                    open_position = None
                    shared_state["open_positions"] = {}

            # --- Hard exit ---
            if ct >= self._hard_exit_time and open_position is not None:
                trade = self._close_position(open_position, candle.close, candle.candle_time, "EOD_EXIT")
                day_trades.append(trade)
                open_position = None
                shared_state["open_positions"] = {}
                continue

            # --- Signal generation ---
            if open_position is not None:
                self._signal_diag["position_open"] += 1
                continue
            if ct >= self._no_entry_after:
                self._signal_diag["no_entry_after"] += 1
                continue

            self._signal_diag["total_candles"] += 1
            signal, diag_reason = self._evaluate_with_reason_s1v3(candle, bar_idx)
            self._signal_diag[diag_reason] += 1
            if signal is None:
                continue

            # Risk gate
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                self._signal_diag["risk_gate_blocked"] += 1
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True,
            )
            sizing = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                available_capital=self._capital_tracker.available_capital,
                risk_pct=self._risk_pct,
                lot_size=self._lot_size,
                margin_rate=self._margin_rate,
            )
            if sizing is None:
                self._signal_diag["sizing_failed"] += 1
                continue
            num_lots, total_qty = sizing

            contract_value = Decimal(str(total_qty)) * entry_price
            self._capital_tracker.open_position(contract_value)

            open_position = BacktestPosition(
                symbol=self._instrument,
                instrument_token=0,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=total_qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            shared_state["open_positions"] = {
                self._instrument: {"direction": signal.direction},
            }

        # End of day
        if open_position is not None and candles:
            last_candle = candles[-1]
            trade = self._close_position(
                open_position, last_candle.close, last_candle.candle_time, "EOD_EXIT",
            )
            day_trades.append(trade)

        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []

        return day_trades

    # ------------------------------------------------------------------
    # Day processing — new strategies (ORB, VWAP MR, MACD ST)
    # ------------------------------------------------------------------

    async def _process_day_orb(
        self, day: date, regime_adapter: BacktestRegimeAdapter,
    ) -> list[BacktestTrade]:
        """Simulate one trading day using ORB strategy."""
        strategy = self._strategy
        strategy.reset_day()

        candles = await self._load_day_candles(day)
        if not candles:
            return []
        candles = self._compute_vwap_for_day(candles)

        open_position: BacktestPosition | None = None
        shared_state: dict = {
            "open_positions": {}, "pending_signals": 0, "kill_switch_level": 0,
        }
        config = self._config
        day_trades: list[BacktestTrade] = []

        for candle in candles:
            self._candle_buffer.append(candle)
            if len(self._candle_buffer) > 200:
                self._candle_buffer = self._candle_buffer[-200:]

            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time

            # --- Check exits ---
            if open_position is not None:
                trade = None
                if self._exit_mode == "trailing":
                    trade = self._check_trailing_exit(open_position, candle)
                elif self._exit_mode == "partial":
                    trade = self._check_partial_exit(open_position, candle)
                else:
                    trade = self._check_fixed_exit(open_position, candle)

                if trade is not None:
                    day_trades.append(trade)
                    open_position = None
                    shared_state["open_positions"] = {}

            # --- Hard exit ---
            if ct >= self._hard_exit_time and open_position is not None:
                trade = self._close_position(open_position, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                open_position = None
                shared_state["open_positions"] = {}
                continue

            # --- Signal generation ---
            if open_position is not None:
                self._signal_diag["position_open"] += 1
                continue

            self._signal_diag["total_candles"] += 1
            signal, diag_reason = self._evaluate_with_reason_orb(candle)
            self._signal_diag[diag_reason] += 1
            if signal is None:
                continue

            # Risk gate
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                self._signal_diag["risk_gate_blocked"] += 1
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True,
            )
            sizing = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                available_capital=self._capital_tracker.available_capital,
                risk_pct=self._risk_pct,
                lot_size=self._lot_size,
                margin_rate=self._margin_rate,
            )
            if sizing is None:
                self._signal_diag["sizing_failed"] += 1
                continue
            num_lots, total_qty = sizing

            contract_value = Decimal(str(total_qty)) * entry_price
            self._capital_tracker.open_position(contract_value)

            open_position = BacktestPosition(
                symbol=self._instrument,
                instrument_token=0,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=total_qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            shared_state["open_positions"] = {
                self._instrument: {"direction": signal.direction},
            }

        # End of day
        if open_position is not None and candles:
            last_candle = candles[-1]
            trade = self._close_position(
                open_position, last_candle.close, last_candle.candle_time, "HARD_EXIT",
            )
            day_trades.append(trade)

        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []
        return day_trades

    async def _process_day_vwap_mr(
        self, day: date, regime_adapter: BacktestRegimeAdapter,
    ) -> list[BacktestTrade]:
        """Simulate one trading day using VWAP Mean Reversion strategy.

        Key difference: allows up to 3 trades per day (re-entry after close).
        """
        strategy = self._strategy
        strategy.reset_day()

        candles = await self._load_day_candles(day)
        if not candles:
            return []
        candles = self._compute_vwap_for_day(candles)

        # Warmup (first day only)
        if not self._vwap_mr_warmed_up:
            warmup = await self._load_warmup_candles(day)
            self._candle_buffer.extend(warmup)
            self._vwap_mr_warmed_up = True

        open_position: BacktestPosition | None = None
        shared_state: dict = {
            "open_positions": {}, "pending_signals": 0, "kill_switch_level": 0,
        }
        config = self._config
        day_trades: list[BacktestTrade] = []
        day_candles_so_far: list[Candle] = []

        for candle in candles:
            self._candle_buffer.append(candle)
            if len(self._candle_buffer) > 200:
                self._candle_buffer = self._candle_buffer[-200:]
            day_candles_so_far.append(candle)

            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time

            # --- Check exits (allow re-entry) ---
            if open_position is not None:
                trade = None
                if self._exit_mode == "trailing":
                    trade = self._check_trailing_exit(open_position, candle)
                elif self._exit_mode == "partial":
                    trade = self._check_partial_exit(open_position, candle)
                else:
                    trade = self._check_fixed_exit(open_position, candle)

                if trade is not None:
                    day_trades.append(trade)
                    open_position = None
                    shared_state["open_positions"] = {}
                    # Do NOT continue — allow re-entry on same candle iteration

            # --- Hard exit ---
            if ct >= self._hard_exit_time and open_position is not None:
                trade = self._close_position(open_position, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                open_position = None
                shared_state["open_positions"] = {}
                continue

            # --- Signal generation ---
            if open_position is not None:
                self._signal_diag["position_open"] += 1
                continue

            self._signal_diag["total_candles"] += 1
            signal, diag_reason = self._evaluate_with_reason_vwap_mr(candle, day_candles_so_far)
            self._signal_diag[diag_reason] += 1
            if signal is None:
                continue

            # Risk gate
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                self._signal_diag["risk_gate_blocked"] += 1
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True,
            )
            sizing = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                available_capital=self._capital_tracker.available_capital,
                risk_pct=self._risk_pct,
                lot_size=self._lot_size,
                margin_rate=self._margin_rate,
            )
            if sizing is None:
                self._signal_diag["sizing_failed"] += 1
                continue
            num_lots, total_qty = sizing

            contract_value = Decimal(str(total_qty)) * entry_price
            self._capital_tracker.open_position(contract_value)

            open_position = BacktestPosition(
                symbol=self._instrument,
                instrument_token=0,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=total_qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            shared_state["open_positions"] = {
                self._instrument: {"direction": signal.direction},
            }

        # End of day
        if open_position is not None and candles:
            last_candle = candles[-1]
            trade = self._close_position(
                open_position, last_candle.close, last_candle.candle_time, "HARD_EXIT",
            )
            day_trades.append(trade)

        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []
        return day_trades

    async def _process_day_macd_st(
        self, day: date, regime_adapter: BacktestRegimeAdapter,
    ) -> list[BacktestTrade]:
        """Simulate one trading day using MACD + Supertrend strategy.

        Multi-timeframe: daily Supertrend for bias, intraday MACD for entries.
        Allows up to 2 trades per day.
        """
        strategy = self._strategy
        strategy.reset_day()

        # Set daily bias from daily candles up to this day
        daily_slice = [c for c in self._daily_candles if c.session_date <= day]
        bias = strategy.set_daily_bias(daily_slice)
        if bias is None:
            return []

        candles = await self._load_day_candles(day)
        if not candles:
            return []
        candles = self._compute_vwap_for_day(candles)

        # Warmup (first day only)
        if not self._macd_st_warmed_up:
            warmup = await self._load_warmup_candles(day, count=200)
            self._candle_buffer.extend(warmup)
            self._macd_st_warmed_up = True

        open_position: BacktestPosition | None = None
        shared_state: dict = {
            "open_positions": {}, "pending_signals": 0, "kill_switch_level": 0,
        }
        config = self._config
        day_trades: list[BacktestTrade] = []

        for candle in candles:
            self._candle_buffer.append(candle)
            if len(self._candle_buffer) > 200:
                self._candle_buffer = self._candle_buffer[-200:]

            ct = candle.candle_time.time() if hasattr(candle.candle_time, "time") else candle.candle_time

            # --- Check exits ---
            if open_position is not None:
                # Supertrend trailing mode: update stop to intraday Supertrend
                if strategy._exit_mode == "supertrend_trail":
                    from tools.futures_strategies import compute_supertrend as _st
                    st_result = _st(
                        self._candle_buffer,
                        strategy._st_intraday_period,
                        strategy._st_intraday_mult,
                    )
                    if st_result is not None:
                        st_val, _ = st_result
                        # Only tighten stop, never widen
                        if open_position.direction == "LONG" and st_val > open_position.stop_loss:
                            open_position.stop_loss = st_val
                        elif open_position.direction == "SHORT" and st_val < open_position.stop_loss:
                            open_position.stop_loss = st_val

                trade = None
                if self._exit_mode == "trailing":
                    trade = self._check_trailing_exit(open_position, candle)
                elif self._exit_mode == "partial":
                    trade = self._check_partial_exit(open_position, candle)
                else:
                    trade = self._check_fixed_exit(open_position, candle)

                if trade is not None:
                    day_trades.append(trade)
                    open_position = None
                    shared_state["open_positions"] = {}

            # --- Hard exit ---
            if ct >= self._hard_exit_time and open_position is not None:
                trade = self._close_position(open_position, candle.close, candle.candle_time, "HARD_EXIT")
                day_trades.append(trade)
                open_position = None
                shared_state["open_positions"] = {}
                continue

            # --- Signal generation ---
            if open_position is not None:
                self._signal_diag["position_open"] += 1
                continue

            self._signal_diag["total_candles"] += 1
            signal, diag_reason = self._evaluate_with_reason_macd_st(candle)
            self._signal_diag[diag_reason] += 1
            if signal is None:
                continue

            # Risk gate
            allowed, reason = self._risk_gate.check(
                signal, shared_state, config, candle.candle_time, regime_adapter,
            )
            if not allowed:
                self._signal_diag["risk_gate_blocked"] += 1
                continue

            # Position sizing
            entry_price = self._apply_slippage(
                signal.theoretical_entry, signal.direction, is_entry=True,
            )
            sizing = self._position_sizer.calculate(
                entry_price=entry_price,
                stop_loss=signal.stop_loss,
                available_capital=self._capital_tracker.available_capital,
                risk_pct=self._risk_pct,
                lot_size=self._lot_size,
                margin_rate=self._margin_rate,
            )
            if sizing is None:
                self._signal_diag["sizing_failed"] += 1
                continue
            num_lots, total_qty = sizing

            contract_value = Decimal(str(total_qty)) * entry_price
            self._capital_tracker.open_position(contract_value)

            open_position = BacktestPosition(
                symbol=self._instrument,
                instrument_token=0,
                direction=signal.direction,
                entry_price=entry_price,
                entry_time=candle.candle_time,
                qty=total_qty,
                stop_loss=signal.stop_loss,
                target=signal.target,
                original_stop=signal.stop_loss,
                regime=regime_adapter.current_regime().value,
            )
            shared_state["open_positions"] = {
                self._instrument: {"direction": signal.direction},
            }

        # End of day
        if open_position is not None and candles:
            last_candle = candles[-1]
            trade = self._close_position(
                open_position, last_candle.close, last_candle.candle_time, "HARD_EXIT",
            )
            day_trades.append(trade)

        day_trades.extend(self._pending_partial_trades)
        self._pending_partial_trades = []
        return day_trades

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self, date_from: date, date_to: date) -> BacktestResult:
        """Run the full backtest over the date range."""
        from core.regime_detector.regime_detector import MarketRegime

        # Resolve near-month contract for this date range
        self._tradingsymbol = await self._resolve_near_month_contract(
            date_from, date_to,
        )

        days = await self._get_trading_days(date_from, date_to)
        if not days:
            return BacktestResult(
                trades=[], daily_results=[], params=self._build_params(),
                date_from=date_from, date_to=date_to,
            )

        # Index futures: default to HIGH_VOLATILITY — allows both long and short signals.
        # The 0.5 position_size_multiplier is unused (futures sizer is lot-based).
        regime_adapter = BacktestRegimeAdapter(MarketRegime.HIGH_VOLATILITY)
        all_trades: list[BacktestTrade] = []
        daily_results: list[DailyResult] = []

        # Pre-load daily candles for macd_st (multi-timeframe)
        if self._strategy_name == "macd_st":
            cfg_st = self._config.get("strategy", {}).get("macd_st", {})
            lookback = int(cfg_st.get("daily_candle_lookback", 60))
            self._daily_candles = await self._load_daily_candles(days[0], lookback)

        for day_idx, day in enumerate(days):
            if self._strategy_name == "s1v3":
                day_trades = await self._process_day_s1v3(day, regime_adapter)
            elif self._strategy_name == "orb":
                day_trades = await self._process_day_orb(day, regime_adapter)
            elif self._strategy_name == "vwap_mr":
                day_trades = await self._process_day_vwap_mr(day, regime_adapter)
            elif self._strategy_name == "macd_st":
                day_trades = await self._process_day_macd_st(day, regime_adapter)
            else:
                day_trades = await self._process_day_s1v2(day, regime_adapter)

            # Diagnostic log for first trading day
            if day_idx == 0:
                buf_key = self._instrument
                if self._evaluator is not None:
                    buf_len = len(self._evaluator._candles_15min.get(buf_key, []))
                else:
                    buf_len = len(self._candle_buffer)
                log.info(
                    "futures_backtest_day1_diag",
                    day=str(day),
                    instrument=self._instrument,
                    strategy=self._strategy_name,
                    candle_buffer_len=buf_len,
                    trades_generated=len(day_trades),
                )

            if day_trades:
                all_trades.extend(day_trades)
                day_gross = sum(t.gross_pnl for t in day_trades)
                day_net = sum(t.net_pnl for t in day_trades)
                daily_results.append(DailyResult(
                    session_date=day,
                    trades_closed=len(day_trades),
                    gross_pnl=day_gross,
                    net_pnl=day_net,
                    regime=regime_adapter.current_regime().value,
                ))

        # Signal diagnostic summary
        self._log_signal_diagnostics()

        return self._build_result(all_trades, daily_results, date_from, date_to)

    def _build_params(self) -> dict:
        """Build params dict for result storage."""
        return {
            "strategy": f"{self._strategy_name}-fut",
            "segment": "futures",
            "instrument": self._instrument,
            "tradingsymbol": self._tradingsymbol or "",
            "lot_size": self._lot_size,
            "exit_mode": self._exit_mode,
            "interval": self._interval,
            "capital": float(self._capital_tracker.initial_capital),
            "margin_rate": float(self._margin_rate),
            "risk_pct": float(self._risk_pct),
            "slippage": float(self._slippage),
            "atr_mult": float(self._atr_mult),
            "atr_period": self._atr_period,
        }

    def _build_result(
        self,
        trades: list[BacktestTrade],
        daily_results: list[DailyResult],
        date_from: date,
        date_to: date,
    ) -> BacktestResult:
        """Compute summary metrics from trades."""
        params = self._build_params()

        if not trades:
            return BacktestResult(
                trades=trades, daily_results=daily_results, params=params,
                date_from=date_from, date_to=date_to,
            )

        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]

        total_trades = len(trades)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total_trades * 100 if total_trades > 0 else 0.0

        avg_win = sum(t.net_pnl for t in wins) / Decimal(str(win_count)) if wins else Decimal("0")
        avg_loss = sum(t.net_pnl for t in losses) / Decimal(str(loss_count)) if losses else Decimal("0")

        gross_pnl = sum(t.gross_pnl for t in trades)
        total_charges = sum(t.charges for t in trades)
        net_pnl = sum(t.net_pnl for t in trades)

        # Expectancy
        expectancy = net_pnl / Decimal(str(total_trades)) if total_trades > 0 else Decimal("0")

        # Profit factor
        gross_wins = sum(t.net_pnl for t in wins)
        gross_losses = abs(sum(t.net_pnl for t in losses))
        profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else 0.0

        # Max drawdown from capital tracker
        max_drawdown = self._capital_tracker.max_drawdown
        max_drawdown_pct = self._capital_tracker.max_drawdown_pct

        # Sharpe ratio (daily returns)
        if len(daily_results) >= 2:
            import statistics
            daily_returns = [float(dr.net_pnl) for dr in daily_results]
            mean_return = statistics.mean(daily_returns)
            std_return = statistics.stdev(daily_returns)
            sharpe_ratio = (mean_return / std_return * (252 ** 0.5)) if std_return > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        # Consecutive wins/losses
        max_con_wins = 0
        max_con_losses = 0
        cur_wins = 0
        cur_losses = 0
        for t in trades:
            if t.net_pnl > 0:
                cur_wins += 1
                cur_losses = 0
                max_con_wins = max(max_con_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_con_losses = max(max_con_losses, cur_losses)

        return BacktestResult(
            trades=trades,
            daily_results=daily_results,
            params=params,
            total_trades=total_trades,
            wins=win_count,
            losses=loss_count,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            expectancy=expectancy,
            gross_pnl=gross_pnl,
            total_charges=total_charges,
            net_pnl=net_pnl,
            max_drawdown=max_drawdown,
            max_drawdown_pct=max_drawdown_pct,
            sharpe_ratio=sharpe_ratio,
            profit_factor=profit_factor,
            max_consecutive_wins=max_con_wins,
            max_consecutive_losses=max_con_losses,
            date_from=date_from,
            date_to=date_to,
        )


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

async def run_futures_optimize(
    pool,
    config: dict,
    instrument: str,
    lot_size: int,
    param_name: str,
    range_str: str,
    date_from: date,
    date_to: date,
    **engine_kwargs,
) -> list[tuple[float, BacktestResult]]:
    """Sweep a parameter and collect results."""
    start, step_val, end = map(float, range_str.split(":"))
    values: list[float] = []
    v = start
    while v <= end + 1e-9:
        values.append(round(v, 4))
        v += step_val

    _ENGINE_KWARG_PARAMS = {
        "atr_mult": "atr_mult",
        "atr_period": "atr_period",
        "partial_pct": "partial_pct",
        "slippage": "slippage",
        "reward_ratio": "reward_ratio",
    }
    _CONFIG_PATH_PARAMS = {
        "ema_fast": ["strategy", "s1v2", "ema_fast"],
        "ema_slow": ["strategy", "s1v2", "ema_slow"],
        "adx_threshold": ["strategy", "s1v2", "adx_threshold"],
        "rsi_oversold": ["strategy", "s1v3", "rsi_oversold"],
        "rsi_overbought": ["strategy", "s1v3", "rsi_overbought"],
        "bb_period": ["strategy", "s1v3", "bb_period"],
    }

    results: list[tuple[float, BacktestResult]] = []
    step_info(f"Futures optimizer: sweeping {param_name} over {len(values)} values")

    for i, val in enumerate(values):
        kwargs = dict(engine_kwargs)
        iter_config = config

        if param_name in _ENGINE_KWARG_PARAMS:
            kwarg_name = _ENGINE_KWARG_PARAMS[param_name]
            kwargs[kwarg_name] = int(val) if kwarg_name == "atr_period" else val
        elif param_name in _CONFIG_PATH_PARAMS:
            iter_config = copy.deepcopy(config)
            path = _CONFIG_PATH_PARAMS[param_name]
            obj = iter_config
            for key in path[:-1]:
                obj = obj.setdefault(key, {})
            obj[path[-1]] = int(val) if param_name in ("ema_fast", "ema_slow", "bb_period") else val
        else:
            raise ValueError(
                f"Unknown optimizer param: {param_name}. "
                f"Valid: {sorted(set(_ENGINE_KWARG_PARAMS) | set(_CONFIG_PATH_PARAMS))}"
            )

        with spinner(f"[{i + 1}/{len(values)}] {param_name}={val}"):
            engine = FuturesBacktestEngine(
                pool, iter_config, instrument, lot_size, **kwargs,
            )
            result = await engine.run(date_from, date_to)
        results.append((val, result))
        net = float(result.net_pnl)
        step_done(f"{param_name}={val} → {result.total_trades} trades, net=₹{net:,.0f}")

    results.sort(key=lambda x: x[1].net_pnl, reverse=True)
    return results


async def run_futures_compare(
    pool,
    config: dict,
    instrument: str,
    lot_size: int,
    exit_modes: list[str],
    date_from: date,
    date_to: date,
    **engine_kwargs,
) -> dict[str, BacktestResult]:
    """Run backtest for each exit mode and return results."""
    results: dict[str, BacktestResult] = {}

    for mode in exit_modes:
        kwargs = dict(engine_kwargs)
        kwargs["exit_mode"] = mode
        with spinner(f"Running {mode} mode"):
            engine = FuturesBacktestEngine(
                pool, config, instrument, lot_size, **kwargs,
            )
            result = await engine.run(date_from, date_to)
        results[mode] = result
        net = float(result.net_pnl)
        step_done(f"{mode} → {result.total_trades} trades, net=₹{net:,.0f}")

    return results


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load settings.yaml."""
    import yaml
    config_path = os.path.join(_PROJECT_ROOT, "config", "settings.yaml")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    return config


def _load_dsn(config: dict) -> str | None:
    """Extract DSN from config."""
    return config.get("database", {}).get("dsn")


def _get_lot_size(config: dict, instrument: str) -> int:
    """Get lot size for instrument from config."""
    for inst in config.get("futures", {}).get("instruments", []):
        if inst["name"] == instrument:
            return inst["lot_size"]
    raise ValueError(f"No lot_size found for {instrument} in config")


def _build_strategy_config(config: dict, strategy_name: str) -> dict:
    """Build config dict with strategy overrides for the evaluator.

    The S1v2/S1v3 evaluators read from config['strategy']['s1v2'] or config['strategy']['s1v3'].
    For futures, we source defaults from config['futures']['strategies'][strategy_name]
    and place them under config['strategy'][strategy_name].
    """
    result = copy.deepcopy(config)
    result["_strategy_override"] = strategy_name

    # Copy futures strategy defaults into the standard strategy config path
    fut_strat = config.get("futures", {}).get("strategies", {}).get(strategy_name, {})
    if fut_strat:
        if "strategy" not in result:
            result["strategy"] = {}
        if strategy_name not in result["strategy"]:
            result["strategy"][strategy_name] = {}
        # Futures defaults as base, let existing strategy config override
        merged = dict(fut_strat)
        merged.update(result["strategy"][strategy_name])
        result["strategy"][strategy_name] = merged

    return result


# ---------------------------------------------------------------------------
# CLI async entry points
# ---------------------------------------------------------------------------

async def _run_backtest(args) -> int:
    """Run a single futures backtest."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn(config)
    if not dsn:
        print("ERROR: No database DSN found in config/settings.yaml")
        return 1

    instrument = args.instrument.upper()
    strategy = args.strategy
    lot_size = _get_lot_size(config, instrument)
    bt_config = _build_strategy_config(config, strategy)

    # Date range
    fut_bt = config.get("futures", {}).get("backtest", {})
    default_interval = fut_bt.get("default_interval", "15min")
    interval = args.interval or default_interval

    date_to = args.to_date or date.today()
    date_from = args.from_date or (date_to - timedelta(days=180))

    print(f"\nFutures Backtest: {instrument} | {strategy.upper()} | {interval}")
    print(f"Period: {date_from} → {date_to} | Lot size: {lot_size}")
    print(f"Exit mode: {args.exit_mode} | Capital: ₹{fut_bt.get('initial_capital', 1000000):,}")
    print()

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        with spinner("Running backtest..."):
            engine = FuturesBacktestEngine(
                pool, bt_config, instrument, lot_size,
                exit_mode=args.exit_mode,
                atr_mult=args.atr_mult,
                atr_period=args.atr_period,
                partial_pct=args.partial_pct,
                slippage=args.slippage,
                interval=interval,
            )
            result = await engine.run(date_from, date_to)

        step_done(f"Backtest complete — {result.total_trades} trades")

        # Save to DB
        run_id = await _save_run(pool, result)
        if result.trades:
            await _save_trades(pool, run_id, result.trades)
        result.run_id = run_id
        step_done(f"Saved as run #{run_id}")

        # Print report
        print_report(result)

    finally:
        await pool.close()

    return 0


async def _run_compare(args) -> int:
    """Compare exit modes for futures backtest."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn(config)
    if not dsn:
        print("ERROR: No database DSN found")
        return 1

    instrument = args.instrument.upper()
    strategy = args.strategy
    lot_size = _get_lot_size(config, instrument)
    bt_config = _build_strategy_config(config, strategy)

    fut_bt = config.get("futures", {}).get("backtest", {})
    interval = args.interval or fut_bt.get("default_interval", "15min")
    date_to = args.to_date or date.today()
    date_from = args.from_date or (date_to - timedelta(days=180))

    modes = [m.strip() for m in args.modes.split(",")]

    print(f"\nFutures Compare: {instrument} | {strategy.upper()} | {interval}")
    print(f"Period: {date_from} → {date_to} | Modes: {', '.join(modes)}")
    print()

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        results = await run_futures_compare(
            pool, bt_config, instrument, lot_size, modes,
            date_from, date_to, interval=interval,
            slippage=args.slippage,
        )
        print_compare_report(results)
    finally:
        await pool.close()

    return 0


async def _run_optimize(args) -> int:
    """Run futures parameter optimizer."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn(config)
    if not dsn:
        print("ERROR: No database DSN found")
        return 1

    instrument = args.instrument.upper()
    strategy = args.strategy
    lot_size = _get_lot_size(config, instrument)
    bt_config = _build_strategy_config(config, strategy)

    fut_bt = config.get("futures", {}).get("backtest", {})
    interval = args.interval or fut_bt.get("default_interval", "15min")
    date_to = args.to_date or date.today()
    date_from = args.from_date or (date_to - timedelta(days=180))

    print(f"\nFutures Optimize: {instrument} | {strategy.upper()} | {interval}")
    print(f"Period: {date_from} → {date_to}")
    print(f"Parameter: {args.param} | Range: {args.range}")
    print()

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        results = await run_futures_optimize(
            pool, bt_config, instrument, lot_size,
            args.param, args.range,
            date_from, date_to,
            exit_mode=args.exit_mode, interval=interval,
            slippage=args.slippage,
        )
        print_optimize_report(results, args.param)
    finally:
        await pool.close()

    return 0


async def _run_show(args) -> int:
    """Show stored backtest results."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn(config)
    if not dsn:
        print("ERROR: No database DSN found")
        return 1

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        if args.run_id:
            result = await _load_run(pool, args.run_id)
        else:
            result = await _load_last_run(pool)

        if result is None:
            print("No backtest run found.")
            return 1

        print_report(result)
    finally:
        await pool.close()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    """Parse YYYY-MM-DD date string."""
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    """CLI entry point for futures backtester."""
    parser = argparse.ArgumentParser(
        description="TradeOS Futures Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- run ---
    run_p = subparsers.add_parser("run", help="Run a single futures backtest")
    run_p.add_argument("--instrument", required=True, help="NIFTY or BANKNIFTY")
    run_p.add_argument("--strategy", default="s1v2", choices=["s1v2", "s1v3", "orb", "vwap_mr", "macd_st"])
    run_p.add_argument("--interval", default=None, help="Candle interval (default: from config)")
    run_p.add_argument("--from", dest="from_date", type=_parse_date, default=None)
    run_p.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    run_p.add_argument("--exit-mode", default="fixed", choices=["fixed", "trailing", "partial"])
    run_p.add_argument("--atr-mult", type=float, default=1.5)
    run_p.add_argument("--atr-period", type=int, default=14)
    run_p.add_argument("--partial-pct", type=float, default=0.5)
    run_p.add_argument("--slippage", type=float, default=0.001)

    # --- compare ---
    cmp_p = subparsers.add_parser("compare", help="Compare exit modes")
    cmp_p.add_argument("--instrument", required=True)
    cmp_p.add_argument("--strategy", default="s1v2", choices=["s1v2", "s1v3", "orb", "vwap_mr", "macd_st"])
    cmp_p.add_argument("--interval", default=None)
    cmp_p.add_argument("--from", dest="from_date", type=_parse_date, default=None)
    cmp_p.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    cmp_p.add_argument("--modes", default="fixed,trailing,partial")
    cmp_p.add_argument("--slippage", type=float, default=0.001)

    # --- optimize ---
    opt_p = subparsers.add_parser("optimize", help="Parameter sweep")
    opt_p.add_argument("--instrument", required=True)
    opt_p.add_argument("--strategy", default="s1v2", choices=["s1v2", "s1v3", "orb", "vwap_mr", "macd_st"])
    opt_p.add_argument("--interval", default=None)
    opt_p.add_argument("--from", dest="from_date", type=_parse_date, default=None)
    opt_p.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    opt_p.add_argument("--param", required=True, help="Parameter to sweep")
    opt_p.add_argument("--range", required=True, help="start:step:end")
    opt_p.add_argument("--exit-mode", default="fixed", choices=["fixed", "trailing", "partial"])
    opt_p.add_argument("--slippage", type=float, default=0.001)

    # --- show ---
    show_p = subparsers.add_parser("show", help="Show stored run results")
    show_p.add_argument("--run-id", type=int, default=None)
    show_p.add_argument("--last-run", action="store_true")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "run":
        return asyncio.run(_run_backtest(args))
    elif args.command == "compare":
        return asyncio.run(_run_compare(args))
    elif args.command == "optimize":
        return asyncio.run(_run_optimize(args))
    elif args.command == "show":
        return asyncio.run(_run_show(args))

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
