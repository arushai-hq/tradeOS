#!/usr/bin/env python3
"""
HAWK — Pick Evaluator CLI.

Evaluates HAWK evening/morning picks against next trading day actual data.

Usage:
    python tools/hawk_eval.py                    # Yesterday's picks
    python tools/hawk_eval.py --date 2026-03-11  # Specific date
    python tools/hawk_eval.py --all              # All historical picks
    python tools/hawk_eval.py --all --export csv # Export to CSV
    python tools/hawk_eval.py --date 2026-03-11 --telegram  # Send to Telegram
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import pytz

# Ensure project root is on path for imports
sys.path.insert(0, ".")

from tools.hawk_engine.evaluator import (
    EvalSummary,
    PickResult,
    evaluate_all,
    evaluate_day,
    export_all_csv,
    export_csv,
    load_nse_holidays,
    next_trading_day,
    send_eval_telegram,
)

IST = pytz.timezone("Asia/Kolkata")

# Integration thresholds from hawk_spec.md
DIRECTION_THRESHOLD = 55.0   # > 55%
HIGH_CONV_THRESHOLD = 65.0   # > 65%
MIN_EVAL_DAYS = 10


# ---------------------------------------------------------------------------
# Terminal formatting
# ---------------------------------------------------------------------------

def _print_day_report(summary: EvalSummary) -> None:
    """Print detailed single-day evaluation report."""
    total = summary.picks_total
    hits = summary.direction_hits
    pct = hits / total * 100 if total else 0

    print()
    print("=" * 90)
    print(f"  HAWK Evaluation — Picks from {summary.pick_date} vs Actual {summary.actual_date}")
    print("=" * 90)
    print()

    # Header
    print(
        f"{'#':<4} {'Symbol':<13} {'Dir':<7} {'Conv':<6} {'Entry Zone':<13}"
        f"{'Open':>9} {'High':>9} {'Low':>9} {'Close':>9} {'Chg%':>8}  Result"
    )
    print("-" * 100)

    for r in summary.results:
        if r.no_data:
            print(
                f"{r.rank:<4} {r.symbol:<13} {r.direction:<7} {r.conviction:<6}"
                f" {_fmt_entry_zone(r.entry_zone):<13}"
                f"{'N/A':>9} {'N/A':>9} {'N/A':>9} {'N/A':>9} {'N/A':>8}  -- NO DATA"
            )
            continue

        result_str = "\u2705 HIT" if r.direction_hit else "\u274c MISS"
        print(
            f"{r.rank:<4} {r.symbol:<13} {r.direction:<7} {r.conviction:<6}"
            f" {_fmt_entry_zone(r.entry_zone):<13}"
            f"{r.open:>9.1f} {r.high:>9.1f} {r.low:>9.1f} {r.close:>9.1f}"
            f" {r.change_pct:>+7.2f}%  {result_str}"
        )

    print("-" * 100)
    print()

    # Summary box
    print("SUMMARY:")
    _print_box([
        f"Direction Accuracy:  {hits}/{total} ({pct:.1f}%)",
        *_conviction_lines(summary),
        f"Entry Zone Hit:      {summary.entry_zone_hits}/{total}"
        f" ({summary.entry_zone_hits / total * 100:.1f}%)" if total else "Entry Zone Hit:      N/A",
        f"Avg Move (correct):  +{summary.avg_move_correct:.1f}%",
        f"Avg Move (wrong):    -{summary.avg_move_wrong:.1f}%",
    ])
    print()


def _print_all_report(summaries: list[EvalSummary]) -> None:
    """Print historical cumulative evaluation report."""
    total_picks = sum(s.picks_total for s in summaries)
    total_hits = sum(s.direction_hits for s in summaries)
    total_pct = total_hits / total_picks * 100 if total_picks else 0

    print()
    print("=" * 80)
    print(f"  HAWK Historical Performance — {len(summaries)} trading days evaluated")
    print("=" * 80)
    print()

    print(
        f"{'Date':<13} {'Picks':>6} {'Hits':>6} {'Miss':>6}"
        f" {'Hit%':>7}  {'HIGH Hit%':>10} {'Avg Move':>9}"
    )
    print("-" * 68)

    for s in summaries:
        pct = s.direction_hits / s.picks_total * 100 if s.picks_total else 0
        high = s.conviction_breakdown.get("HIGH", {})
        high_str = f"{high['pct']:.1f}%" if high else "N/A"
        print(
            f"{s.pick_date:<13} {s.picks_total:>6} {s.direction_hits:>6}"
            f" {s.picks_total - s.direction_hits:>6}"
            f" {pct:>6.1f}%  {high_str:>10} {s.avg_move_correct:>+8.1f}%"
        )

    print("-" * 68)

    # Cumulative HIGH conviction
    all_high_total = sum(
        s.conviction_breakdown.get("HIGH", {}).get("total", 0) for s in summaries
    )
    all_high_hits = sum(
        s.conviction_breakdown.get("HIGH", {}).get("hits", 0) for s in summaries
    )
    all_high_pct = all_high_hits / all_high_total * 100 if all_high_total else 0

    all_avg_correct = sum(s.avg_move_correct * s.direction_hits for s in summaries)
    total_correct = sum(s.direction_hits for s in summaries)
    avg_correct = all_avg_correct / total_correct if total_correct else 0

    print(
        f"{'TOTAL':<13} {total_picks:>6} {total_hits:>6}"
        f" {total_picks - total_hits:>6}"
        f" {total_pct:>6.1f}%  {all_high_pct:>9.1f}% {avg_correct:>+8.1f}%"
    )
    print()

    # Verdict
    days_ok = len(summaries) >= MIN_EVAL_DAYS
    dir_ok = total_pct > DIRECTION_THRESHOLD
    high_ok = all_high_pct > HIGH_CONV_THRESHOLD

    if days_ok and dir_ok and high_ok:
        verdict = "\u2705 Above thresholds \u2014 consider integration"
    elif not days_ok:
        verdict = f"\u23f3 Need {MIN_EVAL_DAYS - len(summaries)} more trading days for verdict"
    else:
        issues = []
        if not dir_ok:
            issues.append(f"direction {total_pct:.1f}% < {DIRECTION_THRESHOLD}%")
        if not high_ok:
            issues.append(f"HIGH {all_high_pct:.1f}% < {HIGH_CONV_THRESHOLD}%")
        verdict = f"\u274c Below threshold \u2014 {', '.join(issues)}"

    print(f"VERDICT: {verdict}")
    print("=" * 80)
    print()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_entry_zone(ez: list[float]) -> str:
    if len(ez) == 2:
        return f"{ez[0]:.0f}-{ez[1]:.0f}"
    return "N/A"


def _conviction_lines(summary: EvalSummary) -> list[str]:
    lines = []
    for level in ("HIGH", "MEDIUM", "LOW"):
        cb = summary.conviction_breakdown.get(level)
        if cb:
            lines.append(
                f"{level} conviction:    {cb['hits']}/{cb['total']}"
                f"  ({cb['pct']:.1f}%)"
            )
    return lines


def _print_box(lines: list[str]) -> None:
    max_len = max(len(line) for line in lines) if lines else 40
    width = max_len + 4
    print(f"\u250c{'─' * width}\u2510")
    for line in lines:
        print(f"\u2502 {line:<{width - 2}} \u2502")
    print(f"\u2514{'─' * width}\u2518")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="HAWK Pick Evaluator — Compare predictions vs actual market data.",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Evaluate picks from this date (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Evaluate all historical picks.",
    )
    parser.add_argument(
        "--export", choices=["csv"], default=None,
        help="Export results to CSV.",
    )
    parser.add_argument(
        "--telegram", action="store_true",
        help="Send evaluation summary to HAWK Telegram channel.",
    )
    parser.add_argument(
        "--output-dir", type=str, default="logs/hawk",
        help="Directory containing HAWK JSON files (default: logs/hawk).",
    )

    args = parser.parse_args()

    if args.all:
        # Historical report
        summaries = evaluate_all(args.output_dir)
        if not summaries:
            print("No evaluable HAWK data found in", args.output_dir)
            return
        _print_all_report(summaries)
        if args.export == "csv":
            path = export_all_csv(summaries, args.output_dir)
            print(f"Exported to {path}")
    else:
        # Single day report
        pick_date = args.date
        if pick_date is None:
            from datetime import datetime
            yesterday = datetime.now(IST).date() - timedelta(days=1)
            pick_date = yesterday.isoformat()

        summary = evaluate_day(pick_date, args.output_dir)
        if summary is None:
            print(f"Cannot evaluate: no picks or no actual data for {pick_date}")
            return
        _print_day_report(summary)

        if args.export == "csv":
            path = export_csv(summary, args.output_dir)
            print(f"Exported to {path}")

        if args.telegram:
            try:
                from tools.hawk_engine.config import load_secrets
                secrets = load_secrets()
                send_eval_telegram(summary, secrets)
                print("Telegram notification sent.")
            except Exception as exc:
                print(f"Telegram send failed: {exc}")


if __name__ == "__main__":
    main()
