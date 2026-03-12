"""
HAWK — Pick Evaluator.

Compares HAWK evening/morning picks against next trading day actual data.
Core evaluation logic — used by tools/hawk_eval.py CLI.

Metrics:
  - Direction accuracy: did the stock move in the predicted direction? (open→close)
  - Entry zone accuracy: did the stock's intraday range touch the entry zone?
  - Conviction calibration: HIGH > MEDIUM > LOW accuracy ordering
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PickResult:
    """Evaluation result for a single HAWK pick."""
    rank: int
    symbol: str
    direction: str
    conviction: str
    entry_zone: list[float]
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    change_pct: float = 0.0
    direction_hit: bool = False
    entry_zone_hit: bool = False
    reasoning: str = ""
    risk_flag: str | None = None
    no_data: bool = False


@dataclass
class EvalSummary:
    """Aggregated evaluation summary for a set of picks."""
    pick_date: str
    actual_date: str
    picks_total: int = 0
    direction_hits: int = 0
    entry_zone_hits: int = 0
    conviction_breakdown: dict = field(default_factory=dict)
    avg_move_correct: float = 0.0
    avg_move_wrong: float = 0.0
    results: list[PickResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------

def load_nse_holidays() -> set[date]:
    """Load NSE holidays from config/nse_holidays.yaml."""
    holidays_path = Path("config/nse_holidays.yaml")
    if not holidays_path.exists():
        return set()
    try:
        with open(holidays_path) as f:
            data = yaml.safe_load(f) or {}
        result: set[date] = set()
        for _year, dates in data.items():
            if isinstance(dates, list):
                for d in dates:
                    result.add(date.fromisoformat(str(d)))
        return result
    except Exception as exc:
        log.warning("nse_holidays_load_failed", error=str(exc))
        return set()


def next_trading_day(d: date, holidays: set[date] | None = None) -> date:
    """Return the next NSE trading day after date d (skip weekends + holidays)."""
    if holidays is None:
        holidays = load_nse_holidays()
    candidate = d + timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in holidays:  # 5=Sat, 6=Sun
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_picks(pick_date: str, output_dir: str = "logs/hawk") -> list[dict]:
    """
    Load HAWK picks for a given date.

    Tries evening first, then morning (morning may have updated picks).
    Returns the best available watchlist.
    """
    # Prefer morning picks (refined) if available
    for run_type in ("morning", "evening"):
        filepath = os.path.join(output_dir, f"{pick_date}_{run_type}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    data = json.load(f)
                picks = data.get("watchlist", [])
                if picks:
                    log.info("hawk_picks_loaded", date=pick_date, run=run_type, count=len(picks))
                    return picks
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("hawk_picks_parse_error", path=filepath, error=str(exc))
    return []


def load_full_result(pick_date: str, output_dir: str = "logs/hawk") -> dict | None:
    """Load the full HAWK result JSON for a date (evening preferred)."""
    for run_type in ("evening", "morning"):
        filepath = os.path.join(output_dir, f"{pick_date}_{run_type}.json")
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                continue
    return None


def load_actual_data(actual_date: str, output_dir: str = "logs/hawk") -> dict[str, dict]:
    """
    Load next-day actual market data (bhavcopy) for evaluation.

    Priority:
      1. Embedded bhavcopy in that day's HAWK evening JSON
      2. Empty dict (KiteConnect fallback could be added later)

    Returns: {symbol: {open, high, low, close, volume, change_pct}}
    """
    result_data = load_full_result(actual_date, output_dir)
    if result_data:
        bhavcopy = result_data.get("bhavcopy", [])
        if not bhavcopy:
            # Also check inside market_data key (some formats)
            bhavcopy = result_data.get("market_data", {}).get("bhavcopy", [])
        if bhavcopy:
            log.info("hawk_actual_data_from_json", date=actual_date, stocks=len(bhavcopy))
            return {
                row["symbol"]: row
                for row in bhavcopy
                if isinstance(row, dict) and "symbol" in row
            }

    # Fallback: try loading from data_collector output embedded in the evening JSON
    # The evening JSON has bhavcopy of THAT day — we need the actual_date's bhavcopy
    # which should be in actual_date's evening JSON
    log.info("hawk_actual_data_not_found", date=actual_date)
    return {}


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def evaluate_pick(pick: dict, actual: dict) -> PickResult:
    """
    Evaluate a single HAWK pick against actual next-day data.

    Direction hit: open→close moved in predicted direction.
    Entry zone hit: intraday price range touched the entry zone.
    """
    symbol = pick.get("symbol", "???")
    direction = pick.get("direction", "?")
    conviction = pick.get("conviction", "?")
    entry_zone = pick.get("entry_zone", [0, 0])
    rank = pick.get("rank", 0)

    if not actual:
        return PickResult(
            rank=rank, symbol=symbol, direction=direction,
            conviction=conviction, entry_zone=entry_zone,
            reasoning=pick.get("reasoning", ""),
            risk_flag=pick.get("risk_flag"),
            no_data=True,
        )

    o = float(actual.get("open", 0))
    h = float(actual.get("high", 0))
    lo = float(actual.get("low", 0))
    c = float(actual.get("close", 0))

    # Change %: open → close
    change_pct = ((c - o) / o * 100) if o > 0 else 0.0

    # Direction accuracy
    if direction == "LONG":
        direction_hit = c > o
    elif direction == "SHORT":
        direction_hit = c < o
    else:
        direction_hit = False

    # Entry zone accuracy
    entry_lo = min(entry_zone) if len(entry_zone) == 2 else 0
    entry_hi = max(entry_zone) if len(entry_zone) == 2 else 0
    if direction == "LONG":
        # For LONG, check if low dipped into or below entry zone
        entry_zone_hit = lo <= entry_hi
    elif direction == "SHORT":
        # For SHORT, check if high reached into or above entry zone
        entry_zone_hit = h >= entry_lo
    else:
        entry_zone_hit = False

    return PickResult(
        rank=rank,
        symbol=symbol,
        direction=direction,
        conviction=conviction,
        entry_zone=entry_zone,
        open=o,
        high=h,
        low=lo,
        close=c,
        change_pct=change_pct,
        direction_hit=direction_hit,
        entry_zone_hit=entry_zone_hit,
        reasoning=pick.get("reasoning", ""),
        risk_flag=pick.get("risk_flag"),
    )


def evaluate_day(
    pick_date: str,
    output_dir: str = "logs/hawk",
    holidays: set[date] | None = None,
) -> EvalSummary | None:
    """
    Evaluate all picks for a given date against next trading day actuals.

    Returns EvalSummary or None if picks or actual data not available.
    """
    picks = load_picks(pick_date, output_dir)
    if not picks:
        log.warning("hawk_eval_no_picks", date=pick_date)
        return None

    actual_date = next_trading_day(date.fromisoformat(pick_date), holidays)
    actual_date_str = actual_date.isoformat()
    actual_data = load_actual_data(actual_date_str, output_dir)

    if not actual_data:
        log.warning("hawk_eval_no_actual_data", pick_date=pick_date, actual_date=actual_date_str)
        return None

    results: list[PickResult] = []
    for pick in picks:
        symbol = pick.get("symbol", "")
        actual = actual_data.get(symbol, {})
        result = evaluate_pick(pick, actual if actual else None)
        results.append(result)

    # Aggregation
    evaluated = [r for r in results if not r.no_data]
    direction_hits = sum(1 for r in evaluated if r.direction_hit)
    entry_zone_hits = sum(1 for r in evaluated if r.entry_zone_hit)

    # Conviction breakdown
    conviction_breakdown: dict[str, dict] = {}
    for level in ("HIGH", "MEDIUM", "LOW"):
        level_results = [r for r in evaluated if r.conviction == level]
        if level_results:
            hits = sum(1 for r in level_results if r.direction_hit)
            conviction_breakdown[level] = {
                "total": len(level_results),
                "hits": hits,
                "pct": hits / len(level_results) * 100,
            }

    # Average moves
    correct_moves = [abs(r.change_pct) for r in evaluated if r.direction_hit]
    wrong_moves = [abs(r.change_pct) for r in evaluated if not r.direction_hit]
    avg_correct = sum(correct_moves) / len(correct_moves) if correct_moves else 0.0
    avg_wrong = sum(wrong_moves) / len(wrong_moves) if wrong_moves else 0.0

    return EvalSummary(
        pick_date=pick_date,
        actual_date=actual_date_str,
        picks_total=len(evaluated),
        direction_hits=direction_hits,
        entry_zone_hits=entry_zone_hits,
        conviction_breakdown=conviction_breakdown,
        avg_move_correct=avg_correct,
        avg_move_wrong=avg_wrong,
        results=results,
    )


def evaluate_all(
    output_dir: str = "logs/hawk",
) -> list[EvalSummary]:
    """
    Evaluate all historical HAWK picks.

    Scans logs/hawk/ for *_evening.json files, evaluates each.
    Returns list of EvalSummary (only days with data).
    """
    hawk_dir = Path(output_dir)
    if not hawk_dir.exists():
        return []

    holidays = load_nse_holidays()
    summaries: list[EvalSummary] = []

    # Find all evening JSON files, sorted by date
    evening_files = sorted(hawk_dir.glob("*_evening.json"))
    pick_dates: list[str] = []
    for fp in evening_files:
        # Extract date from filename: YYYY-MM-DD_evening.json
        date_str = fp.stem.replace("_evening", "")
        try:
            date.fromisoformat(date_str)
            pick_dates.append(date_str)
        except ValueError:
            continue

    for pick_date in pick_dates:
        summary = evaluate_day(pick_date, output_dir, holidays)
        if summary is not None:
            summaries.append(summary)

    return summaries


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(summary: EvalSummary, output_dir: str = "logs/hawk") -> str:
    """Export evaluation results to CSV. Returns filepath."""
    import csv

    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"eval_{summary.pick_date}.csv")

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Rank", "Symbol", "Direction", "Conviction",
            "Entry Zone", "Open", "High", "Low", "Close",
            "Change%", "Dir Hit", "Entry Hit",
        ])
        for r in summary.results:
            ez = f"{r.entry_zone[0]:.0f}-{r.entry_zone[1]:.0f}" if len(r.entry_zone) == 2 else "N/A"
            writer.writerow([
                r.rank, r.symbol, r.direction, r.conviction,
                ez,
                f"{r.open:.2f}" if not r.no_data else "N/A",
                f"{r.high:.2f}" if not r.no_data else "N/A",
                f"{r.low:.2f}" if not r.no_data else "N/A",
                f"{r.close:.2f}" if not r.no_data else "N/A",
                f"{r.change_pct:+.2f}" if not r.no_data else "N/A",
                "HIT" if r.direction_hit else ("MISS" if not r.no_data else "N/A"),
                "HIT" if r.entry_zone_hit else ("MISS" if not r.no_data else "N/A"),
            ])

    log.info("hawk_eval_csv_exported", path=filepath)
    return filepath


def export_all_csv(summaries: list[EvalSummary], output_dir: str = "logs/hawk") -> str:
    """Export historical summary to CSV. Returns filepath."""
    import csv

    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, "eval_all.csv")

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Date", "Picks", "Hits", "Miss", "Hit%",
            "HIGH Hit%", "Avg Move Correct", "Avg Move Wrong",
        ])
        for s in summaries:
            high = s.conviction_breakdown.get("HIGH", {})
            high_pct = f"{high['pct']:.1f}" if high else "N/A"
            writer.writerow([
                s.pick_date, s.picks_total, s.direction_hits,
                s.picks_total - s.direction_hits,
                f"{s.direction_hits / s.picks_total * 100:.1f}" if s.picks_total else "0",
                high_pct,
                f"+{s.avg_move_correct:.1f}%",
                f"-{s.avg_move_wrong:.1f}%",
            ])

    log.info("hawk_eval_all_csv_exported", path=filepath)
    return filepath


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def format_eval_telegram(summary: EvalSummary) -> str:
    """Format evaluation summary for HAWK Telegram channel."""
    total = summary.picks_total
    hits = summary.direction_hits
    pct = hits / total * 100 if total else 0

    status = "✅" if pct >= 55 else "❌"
    lines = [
        f"🦅 HAWK Eval — {summary.pick_date} picks vs {summary.actual_date} actual",
        f"Direction: {hits}/{total} ({pct:.1f}%) {status}",
    ]

    # Conviction breakdown
    conv_parts = []
    for level in ("HIGH", "MEDIUM", "LOW"):
        cb = summary.conviction_breakdown.get(level)
        if cb:
            conv_parts.append(f"{level[:3]}: {cb['hits']}/{cb['total']} ({cb['pct']:.0f}%)")
    if conv_parts:
        lines.append(" | ".join(conv_parts))

    # Best and worst
    evaluated = [r for r in summary.results if not r.no_data]
    if evaluated:
        correct = [r for r in evaluated if r.direction_hit]
        wrong = [r for r in evaluated if not r.direction_hit]

        if correct:
            best = max(correct, key=lambda r: abs(r.change_pct))
            lines.append(f"Best: {best.symbol} {best.direction} {best.change_pct:+.2f}% ✅")
        if wrong:
            worst = max(wrong, key=lambda r: abs(r.change_pct))
            lines.append(f"Worst: {worst.symbol} {worst.direction} {worst.change_pct:+.2f}% ❌")

    return "\n".join(lines)


def send_eval_telegram(summary: EvalSummary, secrets: dict) -> None:
    """Send evaluation summary to HAWK Telegram channel."""
    from tools.hawk_engine.config import get_hawk_telegram_credentials

    bot_token, chat_id = get_hawk_telegram_credentials(secrets)
    if not bot_token or not chat_id:
        log.info("hawk_eval_telegram_skipped", note="HAWK channel not configured")
        return

    message = format_eval_telegram(summary)
    try:
        import requests as req
        resp = req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
        resp.raise_for_status()
        log.info("hawk_eval_telegram_sent", date=summary.pick_date)
    except Exception as exc:
        log.warning("hawk_eval_telegram_failed", error=str(exc))
