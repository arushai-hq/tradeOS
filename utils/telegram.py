"""
TradeOS — Telegram alert utilities.

send_telegram()     — async Telegram send with telegram_active gate.
send_daily_summary()— queries trades table and sends formatted daily summary.
resolve_telegram_credentials() — multi-channel credential resolver with backward compat.

Rules (D4):
  - Only sends if shared_state["telegram_active"] is True.
  - On failure: sets telegram_active = False, logs [TELEGRAM_FAILED].
  - Never raises — catches all exceptions.
  - asyncio.to_thread() for the HTTP call (D6 — never block event loop).

Channel architecture:
  - Each engine/module sends to its own Telegram channel (Session Rule 7).
  - Channels configured under secrets.telegram.<channel_name>.{bot_token, chat_id}.
  - Backward compat: old flat format (secrets.telegram.bot_token) auto-mapped to "trading".
"""
from __future__ import annotations

import asyncio
import json
import structlog
from datetime import date
from typing import Optional

import requests  # type: ignore[import-untyped]

from utils.time_utils import today_ist

log = structlog.get_logger()

# Track whether the flat-format deprecation warning has been logged this session
_flat_format_warned: bool = False


def resolve_telegram_credentials(
    secrets: dict,
    channel: str = "trading",
) -> tuple[str, str]:
    """
    Resolve bot_token and chat_id for a Telegram channel.

    Supports two config formats:
      New (multi-channel):  secrets.telegram.<channel>.bot_token / chat_id
      Old (flat, deprecated): secrets.telegram.bot_token / chat_id

    If old flat format is detected and channel is "trading", uses it with a
    deprecation warning (logged once per session). For non-trading channels,
    old format returns empty credentials.

    Args:
        secrets:  Loaded secrets.yaml dict.
        channel:  Channel name — "trading", "hawk", or any future module.

    Returns:
        (bot_token, chat_id) tuple. Both empty strings if unconfigured.
    """
    global _flat_format_warned
    tg = secrets.get("telegram", {})
    if not isinstance(tg, dict):
        return ("", "")

    # New format: secrets.telegram.<channel>.{bot_token, chat_id}
    channel_cfg = tg.get(channel, {})
    if isinstance(channel_cfg, dict) and channel_cfg.get("bot_token"):
        return (
            str(channel_cfg.get("bot_token", "")),
            str(channel_cfg.get("chat_id", "")),
        )

    # Backward compat: old flat format (secrets.telegram.bot_token)
    flat_token = tg.get("bot_token", "")
    if isinstance(flat_token, str) and flat_token:
        if channel == "trading":
            if not _flat_format_warned:
                _flat_format_warned = True
                log.warning(
                    "telegram_config_deprecated",
                    note="Flat telegram.bot_token format detected. "
                         "Migrate to telegram.trading.bot_token. "
                         "See config/secrets.yaml.template for new structure.",
                )
            return (str(flat_token), str(tg.get("chat_id", "")))
        # Non-trading channels can't use old flat format
        return ("", "")

    return ("", "")


async def send_telegram(
    msg: str,
    shared_state: dict,
    secrets: dict,
    parse_mode: str = "",
    channel: str = "trading",
) -> None:
    """
    Send a Telegram message to a specific channel.

    Non-blocking (asyncio.to_thread). Never raises.
    If telegram_active is False, logs with [TELEGRAM_FAILED] prefix instead.

    Args:
        msg:          Message text to send.
        shared_state: D6 shared state dict.
        secrets:      Loaded secrets.yaml dict.
        parse_mode:   Optional Telegram parse mode (e.g. "HTML"). Empty = no mode set.
        channel:      Telegram channel name — "trading" (default) or "hawk".
    """
    if not shared_state.get("telegram_active", True):
        log.warning(
            "telegram_alert_suppressed",
            message_preview=msg[:100],
            prefix="[TELEGRAM_FAILED]",
        )
        return

    bot_token, chat_id = resolve_telegram_credentials(secrets, channel)

    if not bot_token or not chat_id:
        if channel == "trading":
            shared_state["telegram_active"] = False
            log.warning("telegram_credentials_missing", channel=channel)
        return

    try:
        payload: dict = {"chat_id": chat_id, "text": msg}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await asyncio.to_thread(
            requests.post,
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=5,
        )
    except Exception as exc:
        log.warning("telegram_send_failed", error=str(exc), channel=channel)
        if channel == "trading":
            shared_state["telegram_active"] = False


async def send_daily_summary(
    shared_state: dict,
    db_pool,  # asyncpg.Pool | None
    secrets: dict,
) -> None:
    """
    Query today's trades and send a formatted daily summary via Telegram.

    Queries trades table for today's session. Gracefully handles DB errors.

    Args:
        shared_state: D6 shared state dict.
        db_pool:      asyncpg connection pool (may be None in tests).
        secrets:      Loaded secrets.yaml dict.
    """
    today: date = today_ist()
    trades: list = []

    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                trades = await conn.fetch(
                    "SELECT net_pnl, exit_reason FROM trades WHERE session_date = $1",
                    today,
                )
        except Exception as exc:
            log.error("daily_summary_db_query_failed", error=str(exc))

    total = len(trades)
    wins = sum(1 for t in trades if float(t["net_pnl"]) > 0) if trades else 0
    losses = total - wins
    net_pnl = sum(float(t["net_pnl"]) for t in trades) if trades else 0.0
    win_rate = (wins / total * 100) if total > 0 else 0.0
    max_dd = shared_state.get("daily_pnl_pct", 0.0)

    summary = (
        f"📊 TradeOS Daily Summary — {today}\n"
        f"Trades: {total} | Wins: {wins} | Losses: {losses}\n"
        f"Net P&L: ₹{net_pnl:.2f} | Win Rate: {win_rate:.1f}%\n"
        f"Max Drawdown: {max_dd * 100:.2f}%\n"
        f"Kill Switch Triggers: {shared_state.get('kill_switch_level', 0)}"
    )

    log.info(
        "daily_summary_generated",
        trades=total,
        wins=wins,
        net_pnl=round(net_pnl, 2),
    )
    await send_telegram(summary, shared_state, secrets)
