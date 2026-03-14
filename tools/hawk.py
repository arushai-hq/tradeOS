#!/usr/bin/env python3
"""
HAWK — AI Market Intelligence Engine

Standalone CLI tool for AI-powered daily watchlist generation.
No dependency on running TradeOS process.

Usage:
    python tools/hawk.py --run evening              # Evening analysis (single or consensus per config)
    python tools/hawk.py --run evening --consensus   # Force multi-model consensus
    python tools/hawk.py --run evening --single      # Force single model
    python tools/hawk.py --run morning              # Morning update
    python tools/hawk.py --run evening --dry-run     # Data only, no LLM
    python tools/hawk.py --run evening --date 2026-03-10  # Specific date
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

# Add project root to path so imports work standalone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytz
import structlog

IST = pytz.timezone("Asia/Kolkata")


def _configure_hawk_logging() -> None:
    """Configure structlog for HAWK: dual output to file + console."""
    log_dir = os.path.join(ROOT, "logs", "hawk")
    os.makedirs(log_dir, exist_ok=True)

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    log_path = os.path.join(log_dir, f"hawk_{today_str}.log")

    pre_chain = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
    ]

    # File handler — no colors
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(file_formatter)

    # Console handler — with colors
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)

    # Named logger for hawk (avoid root logger conflicts)
    hawk_logger = logging.getLogger("hawk")
    hawk_logger.handlers.clear()
    hawk_logger.addHandler(file_handler)
    hawk_logger.addHandler(console_handler)
    hawk_logger.setLevel(logging.DEBUG)
    hawk_logger.propagate = False

    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory("hawk"),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger()


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
        for pick in sorted(watchlist, key=lambda p: p.get("symbol", "").upper()):
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


def run_evening_consensus(
    target_date: date, dry_run: bool, hawk_config: dict, secrets: dict,
) -> int:
    """Execute evening consensus analysis pipeline (multi-model)."""
    from tools.hawk_engine.data_collector import collect_evening_data
    from tools.hawk_engine.llm_analyst import analyze_evening_consensus, get_evening_prompt
    from tools.hawk_engine.output_writer import (
        send_hawk_consensus_telegram,
        write_consensus_json,
        write_json,
        write_model_json,
    )

    # Step 1: Collect data (same as single mode)
    consensus_cfg = hawk_config.get("consensus", {})
    models = consensus_cfg.get("models", [])

    log.info("hawk_consensus_start", date=target_date.isoformat(),
             dry_run=dry_run, models=len(models))
    data = collect_evening_data(target_date, hawk_config)

    if not data.get("bhavcopy"):
        log.warning("hawk_no_bhavcopy", note="No bhavcopy data — cannot generate picks")

    # Build market context
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
        prompt = get_evening_prompt(data)
        print("\n" + "=" * 60)
        print("  DRY RUN (CONSENSUS) — LLM Prompt Preview")
        print(f"  Models: {', '.join(m['name'] for m in models)}")
        print("=" * 60)
        print(prompt)
        print("=" * 60 + "\n")
        log.info("hawk_consensus_dry_run_complete", date=target_date.isoformat())
        return 0

    # Step 2: LLM consensus analysis
    from tools.hawk_engine.config import get_llm_api_key, get_llm_provider, get_openrouter_site_name
    provider = get_llm_provider(secrets)
    api_key = get_llm_api_key(secrets)
    if not api_key:
        log.error("hawk_no_api_key", provider=provider,
                  note=f"Set llm.{provider}.api_key in config/secrets.yaml")
        return 1

    if not models:
        log.error("hawk_consensus_no_models",
                  note="No models configured in hawk.yaml consensus.models")
        return 1

    max_tokens = hawk_config.get("max_tokens", 4000)
    watchlist_size = hawk_config.get("watchlist_size", 15)
    site_name = get_openrouter_site_name(secrets)

    consensus_result = analyze_evening_consensus(
        data, models, api_key, max_tokens, watchlist_size,
        provider=provider, site_name=site_name,
    )

    # Step 3: Assemble full result
    result = {
        "date": target_date.isoformat(),
        "run": "evening",
        "mode": "consensus",
        "regime": data.get("regime", "unknown"),
        "market_context": market_context,
        "bhavcopy": data.get("bhavcopy", []),
        "models_used": consensus_result.get("models_used", []),
        "models_failed": consensus_result.get("models_failed", []),
        "total_models": consensus_result.get("total_models", len(models)),
        "consensus_picks": consensus_result.get("consensus_picks", []),
        "per_model": consensus_result.get("per_model", {}),
        # watchlist = consensus_picks for evaluator compatibility
        "watchlist": consensus_result.get("consensus_picks", []),
        "metadata": {
            **consensus_result.get("aggregate_metadata", {}),
            "data_sources": data.get("metadata", {}).get("data_sources", []),
            "fallbacks_used": data.get("metadata", {}).get("fallbacks_used", []),
        },
    }

    # Step 4: Write outputs
    output_dir = hawk_config.get("output_dir", "logs/hawk")
    write_consensus_json(result, output_dir)
    write_json(result, output_dir)  # evaluator-compatible {date}_evening.json

    # Write per-model JSONs
    for model_name, model_data in consensus_result.get("per_model", {}).items():
        write_model_json(
            target_date.isoformat(),
            model_name,
            model_data.get("picks", []),
            model_data.get("metadata", {}),
            output_dir,
        )

    # Step 5: Telegram
    send_hawk_consensus_telegram(result, secrets)

    # Step 6: Terminal summary
    _print_consensus_summary(result)

    log.info("hawk_consensus_evening_complete",
             date=target_date.isoformat(),
             models_used=len(consensus_result.get("models_used", [])),
             picks=len(result["consensus_picks"]))
    return 0


def _print_consensus_summary(result: dict) -> None:
    """Print consensus summary to terminal."""
    date_str = result.get("date", "?")
    models_used = result.get("models_used", [])
    models_failed = result.get("models_failed", [])
    total_models = result.get("total_models", 0)
    consensus_picks = result.get("consensus_picks", [])
    metadata = result.get("metadata", {})

    print("\n" + "=" * 70)
    print(f"  HAWK CONSENSUS — {date_str}")
    print(f"  Models: {', '.join(models_used)} ({len(models_used)}/{total_models})")
    if models_failed:
        print(f"  Failed: {', '.join(models_failed)}")
    print("=" * 70)

    if consensus_picks:
        for tag in ("UNANIMOUS", "STRONG", "MAJORITY", "SINGLE"):
            tagged = [p for p in consensus_picks if p.get("consensus_tag") == tag]
            if not tagged:
                continue
            tagged.sort(key=lambda p: p.get("symbol", "").upper())
            print(f"\n  {tag} ({len(tagged)} picks):")
            print(f"  {'#':<3} {'Symbol':<12} {'Dir':<6} {'Conv':<6} "
                  f"{'Votes':<6} {'Score':<7} {'Entry Zone':<14}")
            print("  " + "-" * 60)
            for pick in tagged:
                ez = pick.get("entry_zone", [0, 0])
                ez_str = f"{ez[0]:.0f}-{ez[1]:.0f}" if len(ez) == 2 else "N/A"
                print(
                    f"  {pick.get('rank', '?'):<3} "
                    f"{pick.get('symbol', '?'):<12} "
                    f"{pick.get('direction', '?'):<6} "
                    f"{pick.get('avg_conviction', '?'):<6} "
                    f"{pick.get('model_votes', 0):<6} "
                    f"{pick.get('consensus_score', 0):<7.1f} "
                    f"{ez_str:<14}"
                )
    else:
        print("\n  No consensus picks generated.")

    cost = metadata.get("total_cost_usd", 0)
    if cost:
        elapsed = metadata.get("total_elapsed_s", 0)
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        print(f"\n  Cost: ${cost:.4f} | "
              f"Tokens: {metadata.get('total_tokens_input', 0)}in/"
              f"{metadata.get('total_tokens_output', 0)}out | "
              f"Time: {time_str}")

    print("=" * 70 + "\n")


def main() -> int:
    _configure_hawk_logging()

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
    parser.add_argument(
        "--consensus", action="store_true",
        help="Force consensus mode (multi-model analysis)",
    )
    parser.add_argument(
        "--single", action="store_true",
        help="Force single-model mode (overrides consensus.enabled in config)",
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

    # Determine consensus vs single mode
    consensus_cfg = hawk_config.get("consensus", {})
    consensus_enabled = consensus_cfg.get("enabled", False)
    if args.consensus:
        consensus_enabled = True
    if args.single:
        consensus_enabled = False

    if args.run == "evening":
        if consensus_enabled:
            return run_evening_consensus(target_date, args.dry_run, hawk_config, secrets)
        return run_evening(target_date, args.dry_run, hawk_config, secrets)
    elif args.run == "morning":
        if args.dry_run:
            log.info("hawk_morning_dry_run_not_supported", note="Dry-run only applies to evening")
            return 0
        return run_morning(target_date, hawk_config, secrets)

    return 0


if __name__ == "__main__":
    sys.exit(main())
