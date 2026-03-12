#!/usr/bin/env python3
"""
HAWK — AI Market Intelligence Engine

Standalone CLI tool for AI-powered daily watchlist generation.
No dependency on running TradeOS process.

Usage:
    python tools/hawk.py --run evening              # Evening analysis
    python tools/hawk.py --run morning              # Morning update
    python tools/hawk.py --run evening --dry-run     # Data only, no LLM
    python tools/hawk.py --run evening --date 2026-03-10  # Specific date
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

# Add project root to path so imports work standalone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytz
import structlog

# Configure structlog for HAWK CLI output
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
)

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


def _print_summary(result: dict) -> None:
    """Print a formatted terminal summary."""
    date_str = result.get("date", "?")
    run_type = result.get("run", "?")
    watchlist = result.get("watchlist", [])
    metadata = result.get("metadata", {})

    print("\n" + "=" * 60)
    print(f"  HAWK {run_type.upper()} — {date_str}")
    print("=" * 60)

    context = result.get("market_context", {})
    if context:
        print(f"\n  Nifty: {context.get('nifty_close', 'N/A')} "
              f"({context.get('nifty_change_pct', 'N/A')}%)")
        print(f"  VIX: {context.get('vix', 'N/A')} | "
              f"FII: {context.get('fii_net_cr', 'N/A')} Cr")
        print(f"  Regime: {result.get('regime', 'unknown')}")

    if watchlist:
        print(f"\n  {len(watchlist)} PICKS:")
        print(f"  {'#':<3} {'Symbol':<12} {'Dir':<6} {'Conv':<6} {'Entry Zone':<16} Reasoning")
        print("  " + "-" * 70)
        for pick in watchlist:
            ez = pick.get("entry_zone", [0, 0])
            ez_str = f"{ez[0]:.0f}-{ez[1]:.0f}" if len(ez) == 2 else "N/A"
            print(
                f"  {pick.get('rank', '?'):<3} "
                f"{pick.get('symbol', '?'):<12} "
                f"{pick.get('direction', '?'):<6} "
                f"{pick.get('conviction', '?'):<6} "
                f"{ez_str:<16} "
                f"{pick.get('reasoning', '')[:40]}"
            )
    else:
        print("\n  No picks generated.")

    if metadata.get("tokens_input"):
        cost = metadata.get("cost_usd", 0)
        print(f"\n  LLM: {metadata.get('model', '?')} | "
              f"Tokens: {metadata['tokens_input']}in/{metadata.get('tokens_output', 0)}out | "
              f"Cost: ${cost:.4f}")

    sources = metadata.get("data_sources", [])
    fallbacks = metadata.get("fallbacks_used", [])
    if sources:
        print(f"  Sources: {', '.join(sources)}")
    if fallbacks:
        print(f"  Fallbacks: {', '.join(fallbacks)}")

    print("=" * 60 + "\n")


def run_evening(target_date: date, dry_run: bool, hawk_config: dict, secrets: dict) -> int:
    """Execute evening analysis pipeline."""
    from tools.hawk_engine.data_collector import collect_evening_data
    from tools.hawk_engine.llm_analyst import analyze_evening, get_evening_prompt
    from tools.hawk_engine.output_writer import (
        format_telegram_message,
        send_hawk_telegram,
        write_json,
    )

    # Step 1: Collect data
    log.info("hawk_evening_start", date=target_date.isoformat(), dry_run=dry_run)
    data = collect_evening_data(target_date, hawk_config)

    if not data.get("bhavcopy"):
        log.warning("hawk_no_bhavcopy", note="No bhavcopy data — cannot generate picks")

    # Build market context from collected data
    indices = data.get("indices", {})
    fii_dii = data.get("fii_dii", {})
    market_context = {
        "nifty_close": indices.get("nifty_50", {}).get("close", 0),
        "nifty_change_pct": indices.get("nifty_50", {}).get("change_pct", 0),
        "banknifty_close": indices.get("bank_nifty", {}).get("close", 0),
        "vix": indices.get("india_vix", {}).get("close", 0),
        "fii_net_cr": fii_dii.get("fii_net_equity", 0),
        "dii_net_cr": fii_dii.get("dii_net_equity", 0),
    }

    if dry_run:
        # Show the prompt that would be sent to LLM
        prompt = get_evening_prompt(data)
        print("\n" + "=" * 60)
        print("  DRY RUN — LLM Prompt Preview")
        print("=" * 60)
        print(prompt)
        print("=" * 60 + "\n")

        result = {
            "date": target_date.isoformat(),
            "run": "evening",
            "regime": data.get("regime", "unknown"),
            "market_context": market_context,
            "bhavcopy": data.get("bhavcopy", []),
            "watchlist": [],
            "metadata": {
                "model": hawk_config.get("model", "claude-sonnet-4-20250514"),
                "dry_run": True,
                "data_sources": data.get("metadata", {}).get("data_sources", []),
                "fallbacks_used": data.get("metadata", {}).get("fallbacks_used", []),
                "bhavcopy_count": data.get("metadata", {}).get("bhavcopy_count", 0),
            },
        }
        _print_summary(result)
        output_dir = hawk_config.get("output_dir", "logs/hawk")
        write_json(result, output_dir)
        log.info("hawk_evening_dry_run_complete", date=target_date.isoformat())
        return 0

    # Step 2: LLM analysis
    from tools.hawk_engine.config import get_llm_api_key, get_llm_provider, get_openrouter_site_name
    provider = get_llm_provider(secrets)
    api_key = get_llm_api_key(secrets)
    if not api_key:
        log.error("hawk_no_api_key", provider=provider, note=f"Set llm.{provider}.api_key in config/secrets.yaml")
        return 1

    model = hawk_config.get("model", "claude-sonnet-4-20250514")
    max_tokens = hawk_config.get("max_tokens", 2000)
    watchlist_size = hawk_config.get("watchlist_size", 15)
    site_name = get_openrouter_site_name(secrets)

    llm_result = analyze_evening(
        data, api_key, model, max_tokens, watchlist_size,
        provider=provider, site_name=site_name,
    )

    # Step 3: Assemble full result
    result = {
        "date": target_date.isoformat(),
        "run": "evening",
        "regime": data.get("regime", "unknown"),
        "market_context": market_context,
        "bhavcopy": data.get("bhavcopy", []),
        "watchlist": llm_result.get("watchlist", []),
        "metadata": {
            **llm_result.get("metadata", {}),
            "data_sources": data.get("metadata", {}).get("data_sources", []),
            "fallbacks_used": data.get("metadata", {}).get("fallbacks_used", []),
        },
    }

    # Step 4: Write outputs
    output_dir = hawk_config.get("output_dir", "logs/hawk")
    write_json(result, output_dir)

    # Step 5: Telegram
    send_hawk_telegram(result, secrets)

    _print_summary(result)
    log.info("hawk_evening_complete", date=target_date.isoformat(), picks=len(result["watchlist"]))
    return 0


def run_morning(target_date: date, hawk_config: dict, secrets: dict) -> int:
    """Execute morning update pipeline (MVP stub)."""
    from tools.hawk_engine.output_writer import load_evening_picks, write_json, send_hawk_telegram

    log.info("hawk_morning_start", date=target_date.isoformat())

    output_dir = hawk_config.get("output_dir", "logs/hawk")
    evening_picks = load_evening_picks(target_date.isoformat(), output_dir)

    if not evening_picks:
        log.warning("hawk_no_evening_picks", note="Run evening analysis first")
        return 1

    # MVP: return evening picks unchanged (morning LLM update is a stub)
    from tools.hawk_engine.llm_analyst import analyze_morning
    from tools.hawk_engine.config import get_llm_api_key, get_llm_provider, get_openrouter_site_name

    provider = get_llm_provider(secrets)
    api_key = get_llm_api_key(secrets)
    model = hawk_config.get("model", "claude-sonnet-4-20250514")
    site_name = get_openrouter_site_name(secrets)

    morning_result = analyze_morning(
        evening_picks, None, api_key, model,
        provider=provider, site_name=site_name,
    )

    result = {
        "date": target_date.isoformat(),
        "run": "morning",
        "regime": "unknown",
        "market_context": {},
        "watchlist": morning_result.get("watchlist", []),
        "metadata": morning_result.get("metadata", {}),
    }

    write_json(result, output_dir)
    send_hawk_telegram(result, secrets)

    _print_summary(result)
    log.info("hawk_morning_complete", date=target_date.isoformat(), picks=len(result["watchlist"]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="hawk",
        description="HAWK — AI Market Intelligence Engine",
    )
    parser.add_argument(
        "--run", required=True, choices=["evening", "morning"],
        help="Run type: evening (post-market) or morning (pre-market)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch data but skip LLM call (data pipeline test)",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date in YYYY-MM-DD format (default: today IST)",
    )
    args = parser.parse_args()

    # Resolve target date
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"Invalid date format: {args.date}. Use YYYY-MM-DD.", file=sys.stderr)
            return 1
    else:
        target_date = datetime.now(IST).date()

    # Load config
    from tools.hawk_engine.config import load_hawk_config, load_secrets
    hawk_config = load_hawk_config()
    secrets = load_secrets()

    if args.run == "evening":
        return run_evening(target_date, args.dry_run, hawk_config, secrets)
    elif args.run == "morning":
        if args.dry_run:
            log.info("hawk_morning_dry_run_not_supported", note="Dry-run only applies to evening")
            return 0
        return run_morning(target_date, hawk_config, secrets)

    return 0


if __name__ == "__main__":
    sys.exit(main())
