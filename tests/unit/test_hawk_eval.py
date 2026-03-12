"""
Unit tests for HAWK Pick Evaluator (tools/hawk_engine/evaluator.py).

Test catalogue:
  (a) SHORT pick with price drop → direction HIT
  (b) LONG pick with price rise → direction HIT
  (c) Direction miss calculated correctly
  (d) Entry zone hit detection (LONG and SHORT)
  (e) Conviction grouping and accuracy
  (f) Missing next-day data handled gracefully
  (g) Weekend/holiday skip for next_trading_day
  (h) evaluate_day end-to-end with mock data
  (i) CSV export produces valid file
  (j) Telegram format contains key metrics
  (k) evaluate_all scans directory correctly
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date

import pytest

from tools.hawk_engine.evaluator import (
    EvalSummary,
    PickResult,
    evaluate_day,
    evaluate_all,
    evaluate_pick,
    export_csv,
    format_eval_telegram,
    load_actual_data,
    load_picks,
    next_trading_day,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _pick(
    symbol: str = "HCLTECH",
    direction: str = "SHORT",
    conviction: str = "HIGH",
    entry_zone: list | None = None,
    rank: int = 1,
) -> dict:
    return {
        "rank": rank,
        "symbol": symbol,
        "direction": direction,
        "conviction": conviction,
        "entry_zone": entry_zone or [1380, 1395],
        "support": 1345,
        "resistance": 1410,
        "reasoning": "Test pick",
        "risk_flag": None,
    }


def _actual(
    open: float = 1390.0,
    high: float = 1400.0,
    low: float = 1350.0,
    close: float = 1360.0,
) -> dict:
    return {
        "symbol": "HCLTECH",
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": 5000000,
        "change_pct": (close - open) / open * 100,
    }


def _write_hawk_json(
    tmpdir: str,
    date_str: str,
    run: str,
    watchlist: list[dict],
    bhavcopy: list[dict] | None = None,
) -> str:
    """Write a HAWK result JSON file to tmpdir."""
    data: dict = {
        "date": date_str,
        "run": run,
        "regime": "bear_trend",
        "market_context": {"nifty_close": 24000, "nifty_change_pct": -1.2},
        "watchlist": watchlist,
        "metadata": {"model": "test", "cost_usd": 0.01},
    }
    if bhavcopy is not None:
        data["bhavcopy"] = bhavcopy
    filepath = os.path.join(tmpdir, f"{date_str}_{run}.json")
    with open(filepath, "w") as f:
        json.dump(data, f)
    return filepath


# ---------------------------------------------------------------------------
# (a) SHORT pick with price drop → HIT
# ---------------------------------------------------------------------------

def test_short_pick_price_drop_is_hit():
    pick = _pick(direction="SHORT")
    actual = _actual(open=1390, close=1360)  # price fell
    result = evaluate_pick(pick, actual)
    assert result.direction_hit is True
    assert result.change_pct < 0


# ---------------------------------------------------------------------------
# (b) LONG pick with price rise → HIT
# ---------------------------------------------------------------------------

def test_long_pick_price_rise_is_hit():
    pick = _pick(symbol="SUNPHARMA", direction="LONG", entry_zone=[1800, 1815])
    actual = {"open": 1810.0, "high": 1850.0, "low": 1795.0, "close": 1840.0}
    result = evaluate_pick(pick, actual)
    assert result.direction_hit is True
    assert result.change_pct > 0


# ---------------------------------------------------------------------------
# (c) Direction miss calculated correctly
# ---------------------------------------------------------------------------

def test_short_pick_price_rise_is_miss():
    pick = _pick(direction="SHORT")
    actual = _actual(open=1380, close=1400)  # price rose
    result = evaluate_pick(pick, actual)
    assert result.direction_hit is False
    assert result.change_pct > 0


def test_long_pick_price_drop_is_miss():
    pick = _pick(direction="LONG", entry_zone=[1380, 1395])
    actual = _actual(open=1390, close=1370)  # price fell
    result = evaluate_pick(pick, actual)
    assert result.direction_hit is False
    assert result.change_pct < 0


# ---------------------------------------------------------------------------
# (d) Entry zone hit detection
# ---------------------------------------------------------------------------

def test_entry_zone_hit_long_low_touches():
    """LONG entry zone hit: stock's low dips into entry zone."""
    pick = _pick(direction="LONG", entry_zone=[1380, 1395])
    actual = _actual(open=1400, high=1420, low=1385, close=1415)  # low=1385 inside zone
    result = evaluate_pick(pick, actual)
    assert result.entry_zone_hit is True


def test_entry_zone_miss_long_low_above():
    """LONG entry zone miss: stock's low stays above entry zone."""
    pick = _pick(direction="LONG", entry_zone=[1380, 1395])
    actual = _actual(open=1410, high=1430, low=1400, close=1425)  # low=1400 > 1395
    result = evaluate_pick(pick, actual)
    assert result.entry_zone_hit is False


def test_entry_zone_hit_short_high_reaches():
    """SHORT entry zone hit: stock's high reaches into entry zone."""
    pick = _pick(direction="SHORT", entry_zone=[1380, 1395])
    actual = _actual(open=1370, high=1390, low=1350, close=1355)  # high=1390 in zone
    result = evaluate_pick(pick, actual)
    assert result.entry_zone_hit is True


def test_entry_zone_miss_short_high_below():
    """SHORT entry zone miss: stock's high stays below entry zone."""
    pick = _pick(direction="SHORT", entry_zone=[1380, 1395])
    actual = _actual(open=1360, high=1375, low=1340, close=1345)  # high=1375 < 1380
    result = evaluate_pick(pick, actual)
    assert result.entry_zone_hit is False


# ---------------------------------------------------------------------------
# (e) Conviction grouping and accuracy
# ---------------------------------------------------------------------------

def test_conviction_breakdown_in_summary():
    """evaluate_day groups picks by conviction and calculates per-level accuracy."""
    with tempfile.TemporaryDirectory() as tmpdir:
        picks = [
            _pick(symbol="A", direction="SHORT", conviction="HIGH", rank=1),
            _pick(symbol="B", direction="SHORT", conviction="HIGH", rank=2),
            _pick(symbol="C", direction="SHORT", conviction="MEDIUM", rank=3),
            _pick(symbol="D", direction="LONG", conviction="LOW", rank=4),
        ]
        bhavcopy = [
            {"symbol": "A", "open": 100, "high": 102, "low": 95, "close": 96},   # SHORT HIT
            {"symbol": "B", "open": 100, "high": 105, "low": 99, "close": 103},  # SHORT MISS
            {"symbol": "C", "open": 200, "high": 210, "low": 190, "close": 192}, # SHORT HIT
            {"symbol": "D", "open": 300, "high": 320, "low": 295, "close": 310}, # LONG HIT
        ]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", picks)
        _write_hawk_json(tmpdir, "2026-03-12", "evening", [], bhavcopy=bhavcopy)

        summary = evaluate_day("2026-03-11", tmpdir)
        assert summary is not None
        assert summary.picks_total == 4
        assert summary.direction_hits == 3  # A, C, D hit

        high = summary.conviction_breakdown["HIGH"]
        assert high["total"] == 2
        assert high["hits"] == 1  # Only A
        assert high["pct"] == 50.0

        medium = summary.conviction_breakdown["MEDIUM"]
        assert medium["total"] == 1
        assert medium["hits"] == 1

        low = summary.conviction_breakdown["LOW"]
        assert low["total"] == 1
        assert low["hits"] == 1


# ---------------------------------------------------------------------------
# (f) Missing next-day data handled gracefully
# ---------------------------------------------------------------------------

def test_missing_actual_data_returns_none():
    """evaluate_day returns None when next-day actual data is missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        picks = [_pick()]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", picks)
        # No 2026-03-12 file → no actual data
        summary = evaluate_day("2026-03-11", tmpdir)
        assert summary is None


def test_missing_picks_returns_none():
    """evaluate_day returns None when picks file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        summary = evaluate_day("2026-03-11", tmpdir)
        assert summary is None


def test_pick_with_no_actual_symbol_marked_no_data():
    """When a picked symbol is not in next-day bhavcopy, mark as no_data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        picks = [_pick(symbol="GHOSTSTOCK")]
        bhavcopy = [
            {"symbol": "OTHERSTOCK", "open": 100, "high": 105, "low": 95, "close": 102},
        ]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", picks)
        _write_hawk_json(tmpdir, "2026-03-12", "evening", [], bhavcopy=bhavcopy)

        summary = evaluate_day("2026-03-11", tmpdir)
        assert summary is not None
        assert summary.picks_total == 0  # no_data picks not counted
        assert summary.results[0].no_data is True


# ---------------------------------------------------------------------------
# (g) Weekend/holiday skip for next_trading_day
# ---------------------------------------------------------------------------

def test_next_trading_day_skips_weekend():
    """Friday → next trading day is Monday."""
    friday = date(2026, 3, 13)  # Friday
    assert friday.weekday() == 4
    nxt = next_trading_day(friday, holidays=set())
    assert nxt == date(2026, 3, 16)  # Monday
    assert nxt.weekday() == 0


def test_next_trading_day_skips_holiday():
    """Skip holiday on Monday."""
    friday = date(2026, 3, 13)
    holidays = {date(2026, 3, 16)}  # Monday is holiday
    nxt = next_trading_day(friday, holidays)
    assert nxt == date(2026, 3, 17)  # Tuesday


def test_next_trading_day_normal_weekday():
    """Tuesday → Wednesday (no skip)."""
    tuesday = date(2026, 3, 10)
    assert tuesday.weekday() == 1
    nxt = next_trading_day(tuesday, holidays=set())
    assert nxt == date(2026, 3, 11)


def test_next_trading_day_skips_consecutive_holidays():
    """Friday + Monday holiday + Tuesday holiday → Wednesday."""
    friday = date(2026, 3, 13)
    holidays = {date(2026, 3, 16), date(2026, 3, 17)}
    nxt = next_trading_day(friday, holidays)
    assert nxt == date(2026, 3, 18)  # Wednesday


# ---------------------------------------------------------------------------
# (h) evaluate_day end-to-end
# ---------------------------------------------------------------------------

def test_evaluate_day_full_flow():
    """Full evaluation flow with mixed HITs and MISSes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        picks = [
            _pick(symbol="INFY", direction="SHORT", conviction="HIGH", rank=1,
                  entry_zone=[1500, 1510]),
            _pick(symbol="RELIANCE", direction="LONG", conviction="MEDIUM", rank=2,
                  entry_zone=[2480, 2500]),
        ]
        bhavcopy = [
            {"symbol": "INFY", "open": 1505, "high": 1510, "low": 1470, "close": 1475},
            {"symbol": "RELIANCE", "open": 2490, "high": 2520, "low": 2470, "close": 2510},
        ]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", picks)
        _write_hawk_json(tmpdir, "2026-03-12", "evening", [], bhavcopy=bhavcopy)

        summary = evaluate_day("2026-03-11", tmpdir)
        assert summary is not None
        assert summary.pick_date == "2026-03-11"
        assert summary.actual_date == "2026-03-12"
        assert summary.picks_total == 2
        assert summary.direction_hits == 2  # both hit (SHORT fell, LONG rose)


# ---------------------------------------------------------------------------
# (i) CSV export produces valid file
# ---------------------------------------------------------------------------

def test_csv_export_creates_file():
    summary = EvalSummary(
        pick_date="2026-03-11",
        actual_date="2026-03-12",
        picks_total=1,
        direction_hits=1,
        results=[
            PickResult(
                rank=1, symbol="INFY", direction="SHORT", conviction="HIGH",
                entry_zone=[1500, 1510], open=1505, high=1510, low=1470,
                close=1475, change_pct=-1.99, direction_hit=True,
                entry_zone_hit=True,
            )
        ],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        path = export_csv(summary, tmpdir)
        assert os.path.exists(path)
        assert path.endswith("eval_2026-03-11.csv")
        with open(path) as f:
            content = f.read()
        assert "INFY" in content
        assert "SHORT" in content
        assert "HIT" in content


# ---------------------------------------------------------------------------
# (j) Telegram format contains key metrics
# ---------------------------------------------------------------------------

def test_telegram_format_contains_metrics():
    summary = EvalSummary(
        pick_date="2026-03-11",
        actual_date="2026-03-12",
        picks_total=10,
        direction_hits=7,
        conviction_breakdown={
            "HIGH": {"total": 4, "hits": 3, "pct": 75.0},
            "MEDIUM": {"total": 4, "hits": 3, "pct": 75.0},
            "LOW": {"total": 2, "hits": 1, "pct": 50.0},
        },
        results=[
            PickResult(
                rank=1, symbol="BAJFINANCE", direction="SHORT", conviction="HIGH",
                entry_zone=[895, 905], open=878, high=892, low=852.5,
                close=855, change_pct=-2.62, direction_hit=True, entry_zone_hit=True,
            ),
            PickResult(
                rank=2, symbol="RELIANCE", direction="SHORT", conviction="MEDIUM",
                entry_zone=[1390, 1400], open=1380, high=1398, low=1370,
                close=1395, change_pct=1.09, direction_hit=False, entry_zone_hit=True,
            ),
        ],
    )
    msg = format_eval_telegram(summary)
    assert "HAWK Eval" in msg
    assert "2026-03-11" in msg
    assert "7/10" in msg
    assert "70.0%" in msg
    assert "HIGH" in msg or "HIG" in msg
    assert "Best:" in msg
    assert "BAJFINANCE" in msg
    assert "Worst:" in msg
    assert "RELIANCE" in msg


# ---------------------------------------------------------------------------
# (k) evaluate_all scans directory correctly
# ---------------------------------------------------------------------------

def test_evaluate_all_finds_evaluable_days():
    """evaluate_all returns summaries only for days with both picks and actuals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        picks_1 = [_pick(symbol="INFY", direction="SHORT", rank=1, entry_zone=[1500, 1510])]
        bhavcopy_1 = [
            {"symbol": "INFY", "open": 1505, "high": 1510, "low": 1470, "close": 1475},
        ]
        # Day 1: 2026-03-11 picks, 2026-03-12 actuals
        _write_hawk_json(tmpdir, "2026-03-11", "evening", picks_1)
        _write_hawk_json(tmpdir, "2026-03-12", "evening", picks_1, bhavcopy=bhavcopy_1)

        # Day 2: 2026-03-12 picks, but NO 2026-03-13 data → not evaluable
        # (2026-03-12 evening already written above with picks_1)

        summaries = evaluate_all(tmpdir)
        # Only 2026-03-11 is evaluable (2026-03-12 has no next-day data)
        assert len(summaries) == 1
        assert summaries[0].pick_date == "2026-03-11"


# ---------------------------------------------------------------------------
# (l) load_picks prefers morning over evening
# ---------------------------------------------------------------------------

def test_load_picks_prefers_morning():
    """If morning picks exist, load those (refined by overnight data)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evening_picks = [_pick(symbol="EVENING_STOCK")]
        morning_picks = [_pick(symbol="MORNING_STOCK")]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", evening_picks)
        _write_hawk_json(tmpdir, "2026-03-11", "morning", morning_picks)

        picks = load_picks("2026-03-11", tmpdir)
        assert len(picks) == 1
        assert picks[0]["symbol"] == "MORNING_STOCK"


def test_load_picks_falls_back_to_evening():
    """If no morning picks, load evening picks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        evening_picks = [_pick(symbol="EVENING_STOCK")]
        _write_hawk_json(tmpdir, "2026-03-11", "evening", evening_picks)

        picks = load_picks("2026-03-11", tmpdir)
        assert len(picks) == 1
        assert picks[0]["symbol"] == "EVENING_STOCK"
