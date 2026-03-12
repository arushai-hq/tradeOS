"""
HAWK — Output Writer (JSON + Telegram).

Saves analysis results to JSON files and sends formatted
picks to the HAWK-Picks Telegram channel.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import structlog

log = structlog.get_logger()


def write_json(result: dict, output_dir: str = "logs/hawk") -> str:
    """
    Save HAWK result to logs/hawk/YYYY-MM-DD_<run>.json.

    Args:
        result:     Full result dict (date, run, watchlist, metadata, etc.).
        output_dir: Output directory path.

    Returns:
        Path to the written file.
    """
    os.makedirs(output_dir, exist_ok=True)

    date_str = result.get("date", "unknown")
    run_type = result.get("run", "evening")
    filename = f"{date_str}_{run_type}.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log.info("hawk_json_written", path=filepath, picks=len(result.get("watchlist", [])))
    return filepath


def load_evening_picks(date_str: str, output_dir: str = "logs/hawk") -> list[dict]:
    """Load evening picks for morning update."""
    filepath = os.path.join(output_dir, f"{date_str}_evening.json")
    try:
        with open(filepath) as f:
            data = json.load(f)
        return data.get("watchlist", [])
    except FileNotFoundError:
        log.warning("hawk_evening_picks_not_found", path=filepath)
        return []
    except json.JSONDecodeError as exc:
        log.error("hawk_evening_picks_invalid_json", path=filepath, error=str(exc))
        return []


def format_telegram_message(result: dict) -> str:
    """
    Format HAWK picks as a Telegram message.

    Follows the format from hawk_spec.md section 6.
    """
    date_str = result.get("date", "unknown")
    run_type = result.get("run", "evening").capitalize()
    regime = result.get("regime", "unknown")

    context = result.get("market_context", {})
    vix = context.get("vix", "N/A")
    fii = context.get("fii_net_cr", "N/A")

    # Header
    fii_str = f"{fii:+,.0f}" if isinstance(fii, (int, float)) else str(fii)
    lines = [
        f"🦅 HAWK {run_type} — {date_str}",
        f"Regime: {regime} | VIX: {vix} | FII: ₹{fii_str}Cr",
        "",
    ]

    watchlist = result.get("watchlist", [])
    if not watchlist:
        lines.append("No picks generated.")
        return "\n".join(lines)

    lines.append("TOP PICKS:")
    for pick in sorted(watchlist, key=lambda p: p.get("symbol", "").upper())[:10]:
        rank = pick.get("rank", "?")
        symbol = pick.get("symbol", "???")
        direction = pick.get("direction", "?")
        conviction = pick.get("conviction", "?")
        entry_zone = pick.get("entry_zone", [0, 0])
        reasoning = pick.get("reasoning", "")
        risk_flag = pick.get("risk_flag")

        icon = "🟢" if direction == "LONG" else "🔴"
        entry_str = f"{entry_zone[0]:.0f}-{entry_zone[1]:.0f}" if len(entry_zone) == 2 else "N/A"

        lines.append(f"{rank}. {icon} {symbol} {direction} [{conviction}] Entry: {entry_str}")
        if reasoning:
            lines.append(f"   {reasoning[:80]}")
        if risk_flag:
            lines.append(f"   ⚠️ {risk_flag}")

    remaining = len(watchlist) - 10
    if remaining > 0:
        lines.append(f"\n+{remaining} more picks in full report")

    return "\n".join(lines)


def send_hawk_telegram(result: dict, secrets: dict) -> None:
    """
    Send HAWK picks to the HAWK-Picks Telegram channel.

    Uses the multi-channel Telegram infrastructure (channel="hawk").
    If hawk channel not configured, logs warning and skips.
    """
    from tools.hawk_engine.config import get_hawk_telegram_credentials

    bot_token, chat_id = get_hawk_telegram_credentials(secrets)
    if not bot_token or not chat_id:
        log.info("hawk_telegram_skipped", note="HAWK channel not configured in secrets.yaml")
        return

    message = format_telegram_message(result)

    try:
        import requests as req
        resp = req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
        resp.raise_for_status()
        log.info("hawk_telegram_sent", picks=len(result.get("watchlist", [])))
    except Exception as exc:
        log.warning("hawk_telegram_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Consensus output
# ---------------------------------------------------------------------------

def write_consensus_json(result: dict, output_dir: str = "logs/hawk") -> str:
    """
    Save consensus result to logs/hawk/YYYY-MM-DD_evening_consensus.json.

    Returns path to the written file.
    """
    os.makedirs(output_dir, exist_ok=True)

    date_str = result.get("date", "unknown")
    filename = f"{date_str}_evening_consensus.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log.info("hawk_consensus_json_written", path=filepath,
             picks=len(result.get("consensus_picks", [])))
    return filepath


def write_model_json(
    date_str: str,
    model_name: str,
    picks: list[dict],
    metadata: dict,
    output_dir: str = "logs/hawk",
) -> str:
    """
    Save an individual model's result.

    Filename: {date}_evening_{model_name_lower}.json
    """
    os.makedirs(output_dir, exist_ok=True)

    safe_name = model_name.lower().replace(" ", "_").replace(".", "")
    filename = f"{date_str}_evening_{safe_name}.json"
    filepath = os.path.join(output_dir, filename)

    model_result = {
        "date": date_str,
        "run": "evening",
        "model": model_name,
        "watchlist": picks,
        "metadata": metadata,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(model_result, f, indent=2, ensure_ascii=False)

    log.info("hawk_model_json_written", path=filepath, model=model_name,
             picks=len(picks))
    return filepath


def format_consensus_telegram(result: dict) -> str:
    """
    Format consensus picks as a Telegram message.

    Sections: header, model status, UNANIMOUS, STRONG, MAJORITY, summary.
    """
    date_str = result.get("date", "unknown")
    models_used = result.get("models_used", [])
    models_failed = result.get("models_failed", [])
    total_models = result.get("total_models", 0)
    consensus_picks = result.get("consensus_picks", [])
    metadata = result.get("metadata", result.get("aggregate_metadata", {}))

    # Header
    lines = [f"🦅 HAWK Consensus — Evening {date_str}"]

    # Model status line
    model_parts = []
    for name in models_used:
        model_parts.append(f"{name} ✅")
    for name in models_failed:
        model_parts.append(f"{name} ❌")
    lines.append(f"Models: {' | '.join(model_parts)}")

    # Cost + time
    cost = metadata.get("total_cost_usd", 0)
    elapsed = metadata.get("total_elapsed_s", 0)
    if cost:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        lines.append(f"Cost: ${cost:.3f} | Time: {time_str}")

    if not consensus_picks:
        lines.append("\nNo consensus picks generated.")
        return "\n".join(lines)

    # Group by tag
    tag_order = ["UNANIMOUS", "STRONG", "MAJORITY", "SINGLE"]
    tag_labels = {
        "UNANIMOUS": f"🏆 UNANIMOUS ({total_models}/{total_models} agree):",
        "STRONG": "⚡ STRONG (3/4 agree):",
        "MAJORITY": "📊 MAJORITY (2/4 agree):",
        "SINGLE": "📌 SINGLE (1 model):",
    }
    # Adjust labels for actual model counts
    if models_used:
        n = len(models_used)
        tag_labels["UNANIMOUS"] = f"🏆 UNANIMOUS ({n}/{n} agree):"
        if n >= 3:
            tag_labels["STRONG"] = f"⚡ STRONG ({n - 1}/{n} agree):"

    shown = 0
    max_shown = 12
    remaining_counts: dict[str, int] = {}

    for tag in tag_order:
        tagged = sorted(
            [p for p in consensus_picks if p.get("consensus_tag") == tag],
            key=lambda p: p.get("symbol", "").upper(),
        )
        if not tagged:
            continue

        if shown >= max_shown:
            remaining_counts[tag] = remaining_counts.get(tag, 0) + len(tagged)
            continue

        lines.append(f"\n{tag_labels.get(tag, tag + ':')}")

        for pick in tagged:
            if shown >= max_shown:
                remaining_counts[tag] = remaining_counts.get(tag, 0) + 1
                continue

            icon = "🟢" if pick.get("direction") == "LONG" else "🔴"
            ez = pick.get("entry_zone", [0, 0])
            ez_str = f"{ez[0]:.0f}-{ez[1]:.0f}" if len(ez) == 2 else "N/A"
            conv = pick.get("avg_conviction", "?")

            lines.append(
                f"{pick.get('rank', '?')}. {icon} {pick.get('symbol', '?')} "
                f"{pick.get('direction', '?')} [{conv}] Entry: {ez_str}"
            )
            shown += 1

    # Summary of remaining
    remaining_total = sum(remaining_counts.values())
    if remaining_total:
        parts = [f"{v} {k.lower()}" for k, v in remaining_counts.items()]
        lines.append(f"\n+{remaining_total} more picks ({', '.join(parts)}) in full report")

    # Totals by tag
    tag_counts = {}
    for p in consensus_picks:
        t = p.get("consensus_tag", "SINGLE")
        tag_counts[t] = tag_counts.get(t, 0) + 1

    count_parts = []
    for tag in tag_order:
        if tag in tag_counts:
            count_parts.append(f"{tag_counts[tag]} {tag.lower()}")
    if count_parts:
        lines.append(f"\n📊 Total: {', '.join(count_parts)}")

    return "\n".join(lines)


def send_hawk_consensus_telegram(result: dict, secrets: dict) -> None:
    """Send consensus picks to HAWK Telegram channel."""
    from tools.hawk_engine.config import get_hawk_telegram_credentials

    bot_token, chat_id = get_hawk_telegram_credentials(secrets)
    if not bot_token or not chat_id:
        log.info("hawk_telegram_skipped", note="HAWK channel not configured in secrets.yaml")
        return

    message = format_consensus_telegram(result)

    try:
        import requests as req
        resp = req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
        resp.raise_for_status()
        log.info("hawk_consensus_telegram_sent",
                 picks=len(result.get("consensus_picks", [])))
    except Exception as exc:
        log.warning("hawk_consensus_telegram_failed", error=str(exc))
