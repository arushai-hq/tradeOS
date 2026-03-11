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
    for pick in watchlist[:10]:  # Show top 10 in Telegram
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
