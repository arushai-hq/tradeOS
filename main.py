"""
TradeOS — Single Entry Point

Session lifecycle (D9):
  Phase 0 — Pre-market gate (synchronous, 6 checks, before event loop)
  Phase 1 — Startup sequence (async, engines start in dependency order)
  Phase 2 — Active trading (5 concurrent D6 tasks)
  Phase 3 — EOD shutdown (15:00–15:30 IST, ordered cleanup)

Usage:
    python main.py
"""
from __future__ import annotations

# ===========================================================================
# SECTION 1 — Imports and structlog configuration
# ===========================================================================

import asyncio
import logging
import os
import signal
import sys
import time as time_module
from datetime import date, datetime, time
from typing import Optional

import asyncpg
import pytz
import requests  # type: ignore[import-untyped]
import structlog
import yaml  # type: ignore[import-untyped]
from kiteconnect import KiteConnect

from data_engine import DataEngine
from execution_engine import ExecutionEngine
from risk_manager import RiskManager
from strategy_engine import StrategyEngine
from utils.db_events import write_system_event
from utils.telegram import send_daily_summary, send_telegram
from utils.telegram_notifier import TelegramNotifier
from utils.time_utils import now_ist, today_ist

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


def _configure_structlog(dev_mode: bool = False) -> None:
    """Configure structlog: JSON to logs/tradeos.log + console in dev mode."""
    os.makedirs("logs", exist_ok=True)

    shared_processors = [
        structlog.stdlib.add_log_level,
        # add_logger_name requires stdlib logging.Logger (.name attr) — incompatible with PrintLoggerFactory
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if dev_mode:
        shared_processors.append(structlog.dev.ConsoleRenderer())
    else:
        shared_processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=shared_processors,  # type: ignore[arg-type]
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ===========================================================================
# SECTION 2 — Shared state initialisation
# ===========================================================================

def _init_shared_state() -> dict:
    """
    Initialise all D6 canonical contract keys + session-lifecycle keys.

    The D6 contract (shared-state-contract.md) defines 20 keys.
    Additional session keys (system_ready, accepting_signals, session_date,
    session_start_time, zerodha_user_id, pre_market_gate_passed,
    telegram_active) are D9 session-guardian keys.

    Queues are created here so heartbeat can report queue depths before
    main() runs. They are overwritten with new Queue instances in main().
    """
    return {
        # D3 — ws_listener owns all WebSocket state keys
        "ws_connected": False,
        "last_tick_timestamp": None,
        "reconnect_attempt": 0,
        "disconnect_timestamp": None,
        # D3/heartbeat — heartbeat writes, ws_listener reads and clears
        "reconnect_requested": False,
        # D6 — signal_processor
        "last_signal": None,
        "signals_generated_today": 0,
        "signals_rejected_today": 0,
        # D2/D6 — order_monitor
        "open_orders": {},
        "open_positions": {},
        "fills_today": 0,
        # D6 — risk_watchdog (kill_switch_level written by trigger_kill_switch only)
        "daily_pnl_pct": 0.0,
        "daily_pnl_rs": 0.0,
        "consecutive_losses": 0,
        "kill_switch_level": 0,
        # D6 — heartbeat
        "system_start_time": None,
        "tasks_alive": {
            "ws_listener": True,
            "signal_processor": True,
            "order_monitor": True,
            "risk_watchdog": True,
            "heartbeat": True,
        },
        # D7 — reconciler
        "recon_in_progress": False,
        "locked_instruments": set(),
        # B4: last known price per symbol, written by DataEngine on every validated tick
        "last_tick_prices": {},
        # B4: total trading capital (₹), set in main() after config load
        "capital": 0.0,
        # D6 — queues (also in shared_state for heartbeat queue-depth reporting)
        # tick_queue_storage → data_engine (validation + DB write)
        # tick_queue_strategy → strategy_engine (candle builder + signal gen)
        # tick_queue → alias for tick_queue_strategy (backward compat for drain/monitoring)
        "tick_queue_storage": asyncio.Queue(maxsize=1000),
        "tick_queue_strategy": asyncio.Queue(maxsize=1000),
        "tick_queue": asyncio.Queue(maxsize=1000),
        "order_queue": asyncio.Queue(maxsize=100),
        # D9 — session-guardian (not in D6 contract — lifecycle keys)
        "system_ready": False,
        "accepting_signals": True,
        "session_date": None,
        "session_start_time": None,
        "zerodha_user_id": None,
        "pre_market_gate_passed": False,
        "telegram_active": False,
        # Regime detector
        "market_regime": None,
        "regime_position_multiplier": 1.0,
    }


# ===========================================================================
# SECTION 3 — Phase 0: Pre-market gate
# ===========================================================================

# Required key paths (dot-notation) checked in CHECK 1
REQUIRED_SETTINGS_KEYS = [
    "system.mode",
    "capital.total",
    "capital.allocation.s1_intraday",
    "risk.max_loss_per_trade_pct",
    "risk.max_daily_loss_pct",
    "risk.max_open_positions",
]
REQUIRED_SECRETS_KEYS = [
    "zerodha.api_key",
    "zerodha.api_secret",
    "zerodha.access_token",
    "zerodha.token_date",
    "telegram.bot_token",
    "telegram.chat_id",
]


def _get_nested(d: dict, dotted_key: str) -> object:
    """Traverse a nested dict by dot-separated key path. Returns None if any part is missing."""
    val: object = d
    for part in dotted_key.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _send_startup_alert_sync(secrets: dict, message: str) -> None:
    """
    Synchronous Telegram send for pre-event-loop startup alerts.

    Uses requests (sync) — acceptable here because no event loop exists yet.
    Failure is silent — structured log already written before this call.
    """
    try:
        bot_token = secrets.get("telegram", {}).get("bot_token", "")
        chat_id = secrets.get("telegram", {}).get("chat_id", "")
        if bot_token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=5,
            )
    except Exception:
        pass  # Startup alert failure is non-fatal — log already written


def run_config_check() -> tuple[dict, dict]:
    """
    CHECK 1: Load config/settings.yaml + config/secrets.yaml.
    Validate all required keys present.

    Returns (config, secrets) on success.
    Calls sys.exit(1) if any file is missing or any key is absent.
    """
    try:
        with open("config/settings.yaml") as f:
            config = yaml.safe_load(f)
        with open("config/secrets.yaml") as f:
            secrets = yaml.safe_load(f)
    except FileNotFoundError as exc:
        log.critical("config_file_missing", error=str(exc))
        sys.exit(1)

    missing = []
    for key in REQUIRED_SETTINGS_KEYS:
        if _get_nested(config, key) is None:
            missing.append(f"settings.yaml:{key}")
    for key in REQUIRED_SECRETS_KEYS:
        if _get_nested(secrets, key) is None:
            missing.append(f"secrets.yaml:{key}")

    if missing:
        log.critical("config_incomplete", missing_keys=missing)
        sys.exit(1)

    # Validate strategy allocation sums to 1.00
    allocations = config.get("capital", {}).get("allocation", {})
    if allocations:
        alloc_total = sum(float(v) for v in allocations.values())
        if abs(alloc_total - 1.0) > 0.001:
            log.critical(
                "allocation_invalid",
                total=round(alloc_total, 4),
                expected=1.0,
                allocations=allocations,
            )
            print(
                f"FATAL: Strategy allocations sum to {alloc_total:.4f}, "
                f"must equal 1.00. Fix config/settings.yaml",
                file=sys.stderr,
            )
            sys.exit(1)
        log.info(
            "allocation_validated",
            **{k: v for k, v in allocations.items()},
            total=round(alloc_total, 4),
        )

    # Validate minimum slot capital
    total_capital = float(config.get("capital", {}).get("total", 0))
    s1_alloc = float(allocations.get("s1_intraday", 0))
    max_positions = int(config.get("risk", {}).get("max_open_positions", 4))
    pos_sizing = config.get("position_sizing", {})
    min_slot_capital = float(pos_sizing.get("min_slot_capital", 40000))

    slot_capital = (total_capital * s1_alloc) / max_positions if max_positions > 0 else 0

    if slot_capital < min_slot_capital:
        log.critical(
            "slot_capital_too_small",
            slot_capital=round(slot_capital, 2),
            min_required=min_slot_capital,
            total_capital=total_capital,
            s1_allocation=s1_alloc,
            max_positions=max_positions,
        )
        print(
            f"FATAL: Slot capital ₹{slot_capital:,.0f} is below minimum ₹{min_slot_capital:,.0f}. "
            f"Increase total_capital, increase S1 allocation, or reduce max_positions.",
            file=sys.stderr,
        )
        sys.exit(1)
    log.info(
        "slot_size_validated",
        slot_capital=round(slot_capital, 2),
        min_required=min_slot_capital,
    )

    return config, secrets


def run_token_freshness_check(secrets: dict) -> None:
    """
    CHECK 2: Verify secrets.zerodha.token_date == today IST.

    Zerodha access_token expires at midnight IST daily — no auto-refresh.
    Stale token means all API calls will fail silently.

    Calls sys.exit(1) if token_date is missing or does not match today.
    """
    token_date = secrets.get("zerodha", {}).get("token_date", "").strip()
    today_str = datetime.now(IST).date().isoformat()

    if not token_date:
        log.critical("startup_blocked_no_token_date")
        _send_startup_alert_sync(
            secrets,
            "⛔ TradeOS blocked: token_date missing from secrets.yaml.\n"
            "Run: python scripts/zerodha_auth.py",
        )
        sys.exit(1)

    if token_date != today_str:
        log.critical(
            "startup_blocked_stale_token",
            token_date=token_date,
            today=today_str,
        )
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS blocked: Zerodha access_token expired.\n"
            f"Token date: {token_date} | Today: {today_str}\n"
            f"Run: python scripts/zerodha_auth.py",
        )
        sys.exit(1)


def run_token_validity_check(secrets: dict) -> KiteConnect:
    """
    CHECK 3: kite.profile() live probe.

    Returns a ready KiteConnect instance on success.
    Calls sys.exit(1) on any API error (401, 403, network error).
    """
    api_key = secrets["zerodha"]["api_key"]
    access_token = secrets["zerodha"]["access_token"]

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        profile = kite.profile()  # synchronous — pre-event-loop
        log.info(
            "startup_token_valid",
            user_id=profile.get("user_id"),
            user_name=profile.get("user_name"),
        )
        return kite
    except Exception as exc:
        log.critical("startup_blocked_invalid_token", error=str(exc))
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS blocked: Zerodha token invalid or API unreachable.\n"
            f"Error: {str(exc)}\n"
            f"Run: python scripts/zerodha_auth.py",
        )
        sys.exit(1)


def run_holiday_check(secrets: dict) -> None:
    """
    CHECK 4: NSE holiday / weekend check.

    sys.exit(0) if today is a weekend or NSE holiday (clean exit, not an error).
    Continues silently if today is a trading day.

    Reads from config/nse_holidays.yaml. If the file is missing, proceeds
    without the check (non-fatal — warns in log).
    """
    now = datetime.now(IST)
    today_str = now.date().isoformat()
    weekday = now.weekday()  # Monday=0, Sunday=6

    if weekday >= 5:
        day_name = "Saturday" if weekday == 5 else "Sunday"
        log.info("market_closed_weekend", date=today_str, day=day_name)
        _send_startup_alert_sync(
            secrets,
            f"📅 TradeOS: {day_name} — NSE closed. No trading today.",
        )
        sys.exit(0)

    try:
        with open("config/nse_holidays.yaml") as f:
            holidays_config = yaml.safe_load(f)
        year = now.year
        holidays: list = (
            holidays_config.get(str(year), holidays_config.get(year, []))
        )
    except FileNotFoundError:
        log.warning(
            "nse_holidays_file_missing",
            note="Cannot check NSE holidays — proceeding without check",
        )
        return

    if today_str in holidays:
        log.info("market_closed_holiday", date=today_str)
        _send_startup_alert_sync(
            secrets,
            f"📅 TradeOS: NSE holiday today ({today_str}) — no trading.",
        )
        sys.exit(0)  # Clean exit — not an error


def run_telegram_check(secrets: dict, shared_state: dict) -> None:
    """
    CHECK 5: Telegram path validation.

    Non-blocking — trading continues even if Telegram is broken.
    On failure: sets shared_state["telegram_active"] = False so subsequent
    alerts are logged with [TELEGRAM_FAILED] prefix instead of sent.
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        bot_token = secrets.get("telegram", {}).get("bot_token", "")
        chat_id = secrets.get("telegram", {}).get("chat_id", "")

        if not bot_token or not chat_id:
            raise ValueError("telegram.bot_token or telegram.chat_id missing")

        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🟡 TradeOS {today_str}: Alert path active."},
            timeout=5,
        )
        resp.raise_for_status()
        shared_state["telegram_active"] = True
        log.info("startup_telegram_ok")

    except Exception as exc:
        shared_state["telegram_active"] = False
        log.warning(
            "TELEGRAM_ALERT_PATH_BROKEN",
            error=str(exc),
            note="All subsequent alerts will be logged with [TELEGRAM_FAILED] prefix",
        )
        # Do NOT sys.exit() — trading continues with file-only alerts


def run_time_window_check(secrets: dict) -> None:
    """
    CHECK 6: IST time window.

    < 08:45       → sleep until 08:45
    08:45–09:10   → optimal window, proceed
    09:10–12:00   → WARNING "Late start", proceed (partial session)
    > 12:00       → ERROR + sys.exit(1) (insufficient history for S1)
    """
    now = datetime.now(IST)
    current_time = now.time()

    if current_time < time(8, 45):
        target = now.replace(hour=8, minute=45, second=0, microsecond=0)
        wait_seconds = (target - now).total_seconds()
        log.info(
            "startup_sleeping_until_0845",
            wait_seconds=round(wait_seconds),
            current_time=str(current_time),
        )
        time_module.sleep(wait_seconds)
        return

    if current_time > time(12, 0):
        log.error(
            "startup_too_late",
            current_time=str(current_time),
            reason="Past 12:00 IST — insufficient trading window for S1 indicator history",
        )
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS: Start after 12:00 IST ({current_time.strftime('%H:%M')}). "
            f"Aborting — insufficient indicator history for S1.",
        )
        sys.exit(1)

    if current_time > time(9, 10):
        log.warning(
            "startup_late_start",
            current_time=str(current_time),
            note="First candle(s) may be missed",
        )
        _send_startup_alert_sync(
            secrets,
            f"⚠️ TradeOS late start at {current_time.strftime('%H:%M')} IST. "
            f"First candle(s) missed.",
        )
        # Continue — partial session is better than no session


def run_pre_market_gate(shared_state: dict) -> tuple[KiteConnect, dict, dict]:
    """
    Phase 0: Orchestrates all 6 pre-market checks in strict sequential order.

    Returns (kite, config, secrets) on success.
    Calls sys.exit() on any hard-stop condition — never raises, never retries.

    Called synchronously before asyncio.run() — no event loop exists yet.
    """
    # CHECK 1: Config + secrets validation
    config, secrets = run_config_check()

    # CHECK 2: Token date freshness
    run_token_freshness_check(secrets)

    # CHECK 3: Token live validation — returns KiteConnect
    kite = run_token_validity_check(secrets)
    shared_state["zerodha_user_id"] = kite.profile().get("user_id", "unknown")

    # CHECK 4: NSE holiday / weekend
    run_holiday_check(secrets)

    # CHECK 5: Telegram path validation
    run_telegram_check(secrets, shared_state)

    # CHECK 6: IST time window
    run_time_window_check(secrets)

    # All 6 checks passed
    shared_state["pre_market_gate_passed"] = True
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    log.info("pre_market_gate_passed", date=today_str)
    _send_startup_alert_sync(
        secrets,
        f"🟢 TradeOS {today_str}: Pre-market gate passed. Starting up.",
    )

    return kite, config, secrets


# ===========================================================================
# SECTION 8 — Kill switch handler
# ===========================================================================

async def trigger_kill_switch(
    level: int,
    reason: str,
    shared_state: dict,
    config: dict,
    secrets: dict,
    exec_engine: Optional[ExecutionEngine] = None,
) -> None:
    """
    Update kill_switch_level and perform level-appropriate actions.

    Level 1: Stop accepting signals.
    Level 2: Stop signals + emergency exit all open positions.
    Level 3: Stop signals + emergency exit + cancel all running tasks.

    No-op if level <= current kill_switch_level (never downgrade).
    Sends Telegram CRITICAL alert if telegram_active is True.

    Args:
        level:       Target kill switch level (1, 2, or 3).
        reason:      Human-readable trigger reason (logged + alerted).
        shared_state: D6 shared state dict.
        config:      Loaded settings.yaml dict.
        secrets:     Loaded secrets.yaml dict.
        exec_engine: ExecutionEngine instance (needed for level 2 position close).
    """
    current = shared_state.get("kill_switch_level", 0)
    if level <= current:
        return  # Already at this level or higher — never downgrade

    shared_state["kill_switch_level"] = level
    log.critical(
        "kill_switch_triggered",
        level=level,
        reason=reason,
        previous_level=current,
    )

    if level >= 1:
        shared_state["accepting_signals"] = False

    if level >= 2 and exec_engine is not None:
        exit_manager = getattr(exec_engine, "_exit_manager", None)
        if exit_manager is not None:
            try:
                await exit_manager.emergency_exit_all(reason)
            except Exception as exc:
                log.error("kill_switch_l2_exit_failed", error=str(exc))

    if level >= 3:
        # Cancel all running tasks except this one
        current_task = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current_task:
                task.cancel()

    # Telegram alert
    await send_telegram(
        f"🔴 KILL SWITCH L{level}: {reason}",
        shared_state,
        secrets,
    )


# ===========================================================================
# SECTION 6 — Phase 2: risk_watchdog, heartbeat, run_trading_session
# ===========================================================================

def _compute_unrealized_pnl(open_positions: dict, tick_prices: dict) -> float:
    """
    Compute total unrealized P&L (₹) from open positions and current tick prices.

    Handles two schemas written to shared_state["open_positions"]:
      - PnlTracker:  {"direction": "LONG/SHORT", "entry_price": X, "qty": N}
      - ExitManager: {"side": "BUY/SELL", "avg_price": X, "qty": ±N}

    Args:
        open_positions: shared_state["open_positions"] — keyed by symbol.
        tick_prices:    shared_state["last_tick_prices"] — keyed by symbol.

    Returns:
        Total unrealized P&L in ₹. Positions with no tick price contribute ₹0.
    """
    unrealized = 0.0
    for symbol, pos in open_positions.items():
        current_price = tick_prices.get(symbol)
        if current_price is None or float(current_price) <= 0:
            log.debug("pnl_skip_no_tick", symbol=symbol,
                      reason="no_tick_price_available")
            continue
        entry_price = float(pos.get("entry_price", pos.get("avg_price", 0.0)))
        qty = abs(int(pos.get("qty", 0)))
        direction = pos.get("direction")
        if direction is None:
            side = pos.get("side", "BUY")
            direction = "LONG" if side == "BUY" else "SHORT"
        if direction == "LONG":
            unrealized += (float(current_price) - entry_price) * qty
        else:  # SHORT
            unrealized += (entry_price - float(current_price)) * qty
    return unrealized


async def risk_watchdog(
    shared_state: dict,
    config: dict,
    secrets: dict,
    exec_engine: Optional[ExecutionEngine] = None,
    regime_detector=None,
    notifier=None,
) -> None:
    """
    D6 Task 4 — Risk watchdog. Checks kill switch conditions every 1 second.

    Trigger conditions (D1 canonical):
      L2: daily_pnl_pct <= -0.03  (3% daily loss cap)
      L1: consecutive_losses >= 5 AND daily_pnl_pct <= -0.015  (compound condition)

    Phase 3 handling (D9):
      15:00 IST: sets accepting_signals = False, drains tick_queue.
      15:30 IST: cancels all other tasks and returns (triggers clean shutdown).

    Session date drift (D9 Phase 2 Monitor B):
      session_date != today_ist() → Level 3 kill switch.

    On crash: CRITICAL log + Level 3 kill switch. Never silently restarts.
    """
    max_daily_loss = config.get("risk", {}).get("max_daily_loss_pct", 0.03)
    hard_exit_triggered = False
    eod_shutdown_triggered = False
    regime_refresh_counter: int = 0

    while True:
        await asyncio.sleep(1)
        regime_refresh_counter += 1

        if not shared_state.get("system_ready"):
            continue

        try:
            # Regime refresh every 60s
            if regime_detector is not None and regime_refresh_counter % 60 == 0:
                try:
                    await regime_detector.refresh()
                except Exception as exc:
                    log.error("regime_refresh_error", error=str(exc))

            now = now_ist()
            now_time = now.time()
            pnl = shared_state["daily_pnl_pct"]
            losses = shared_state["consecutive_losses"]

            # Phase 3: 15:30 — orderly shutdown (cancels tasks, triggers return)
            if not eod_shutdown_triggered and now_time >= time(15, 30):
                eod_shutdown_triggered = True
                shared_state["system_ready"] = False
                log.info("eod_shutdown_15_30", note="Cancelling tasks — clean EOD")
                current_task = asyncio.current_task()
                for task in asyncio.all_tasks():
                    if task is not current_task:
                        task.cancel()
                return  # risk_watchdog exits normally — gather() returns

            # Phase 3: 15:00 — hard exit (runs once, NOT a kill switch event)
            if not hard_exit_triggered and now_time >= time(15, 0):
                hard_exit_triggered = True
                shared_state["accepting_signals"] = False
                # Drain tick_queue without processing
                tick_q = shared_state.get("tick_queue")
                if tick_q is not None:
                    while not tick_q.empty():
                        try:
                            tick_q.get_nowait()
                            tick_q.task_done()
                        except asyncio.QueueEmpty:
                            break
                open_count = len(shared_state.get("open_positions", {}))
                log.info(
                    "hard_exit_triggered",
                    time="15:00 IST",
                    open_positions=open_count,
                    note="Scheduled EOD — NOT a kill switch event",
                )
                # Do NOT trigger kill_switch — this is scheduled, not anomalous

                # Cancel any pending (unfilled) orders before closing positions
                if exec_engine is not None:
                    osm = getattr(exec_engine, "_osm", None)
                    if osm is not None:
                        from execution_engine.state_machine import (
                            OrderState as _OSMState,
                        )
                        pending = [
                            o for o in osm.get_active_orders()
                            if o.state != _OSMState.FILLED
                        ]
                        for order in pending:
                            try:
                                osm.transition(order.order_id, _OSMState.CANCELLED)
                            except Exception as exc:
                                log.warning(
                                    "pending_order_cancel_failed",
                                    order_id=order.order_id,
                                    state=order.state.value,
                                    error=str(exc),
                                )
                        if pending:
                            log.info(
                                "pending_orders_cancelled",
                                count=len(pending),
                            )

                # B1 fix: immediately force-close all open positions
                if exec_engine is not None and open_count > 0:
                    exit_manager = getattr(exec_engine, "_exit_manager", None)
                    if exit_manager is not None:
                        # Snapshot positions BEFORE closing so notification has entry data
                        if notifier is not None:
                            _pos_snap = dict(shared_state.get("open_positions", {}))
                            _tick_snap = dict(shared_state.get("last_tick_prices", {}))
                            _session_pnl = shared_state.get("daily_pnl_rs", 0.0)
                            notifier.notify_hard_exit(_pos_snap, _tick_snap, _session_pnl)
                        try:
                            await exit_manager.emergency_exit_all("hard_exit_1500")
                            log.info(
                                "hard_exit_positions_closed",
                                positions_closed=open_count,
                            )
                        except Exception as exc:
                            log.error("hard_exit_close_failed", error=str(exc))

            # L2: daily loss >= 3%
            if pnl <= -max_daily_loss and shared_state["kill_switch_level"] < 2:
                await trigger_kill_switch(
                    2, "daily_loss_3pct", shared_state, config, secrets, exec_engine
                )

            # L1: compound condition (both must be true)
            elif (
                losses >= 5
                and pnl <= -0.015
                and shared_state["kill_switch_level"] < 1
            ):
                await trigger_kill_switch(
                    1, "consecutive_losses_compound",
                    shared_state, config, secrets, exec_engine,
                )

            # D9 Phase 2 Monitor B: session date drift
            if (
                shared_state.get("session_date") is not None
                and shared_state["session_date"] != today_ist()
            ):
                await trigger_kill_switch(
                    3, "session_date_drift",
                    shared_state, config, secrets, exec_engine,
                )

        except asyncio.CancelledError:
            raise  # D6 rule: never suppress CancelledError

        except Exception as exc:
            log.critical("risk_watchdog_crashed", error=str(exc), exc_info=True)
            await trigger_kill_switch(
                3, "risk_watchdog_crashed",
                shared_state, config, secrets, exec_engine,
            )
            raise  # Re-raise — resilient_task wrapper sees this


async def heartbeat(shared_state: dict, secrets: dict, notifier=None) -> None:
    """
    D6 Task 5 — Heartbeat. Emits alive log every 30s.

    Checks all other tasks still running via tasks_alive dict.
    Detects silent WS disconnect (no ticks for 30s during market hours).
    Triggers reconnect via reconnect_requested flag (D3 protocol).
    When notifier is provided, sends a rich Telegram summary at the
    configured heartbeat_interval_min cadence (default: every 30 min).
    """
    telegram_cycle: int = 0
    while True:
        await asyncio.sleep(30)
        telegram_cycle += 1
        shared_state["tasks_alive"]["heartbeat"] = now_ist()

        # B4: Update daily_pnl_pct with realized + unrealized P&L every 30s
        _open_pos = shared_state.get("open_positions", {})
        _tick_prices = shared_state.get("last_tick_prices", {})
        _realized_rs = shared_state.get("daily_pnl_rs", 0.0)
        _unrealized_rs = _compute_unrealized_pnl(_open_pos, _tick_prices)
        _total_rs = _realized_rs + _unrealized_rs
        _capital_rs = shared_state.get("capital", 0.0)
        if _capital_rs > 0:
            shared_state["daily_pnl_pct"] = _total_rs / _capital_rs
        log.info(
            "pnl_update",
            realized_pnl=round(_realized_rs, 2),
            unrealized_pnl=round(_unrealized_rs, 2),
            daily_pnl_pct=round(shared_state.get("daily_pnl_pct", 0.0), 6),
            open_position_count=len(_open_pos),
        )

        # Check for silent WS disconnect
        last_tick = shared_state.get("last_tick_timestamp")
        if last_tick is not None and shared_state.get("ws_connected"):
            silence_seconds = (now_ist() - last_tick).total_seconds()
            if silence_seconds > 30:
                log.warning(
                    "heartbeat_no_ticks_30s",
                    silence_seconds=round(silence_seconds),
                )
                shared_state["reconnect_requested"] = True

        storage_q = shared_state.get("tick_queue_storage")
        strategy_q = shared_state.get("tick_queue_strategy")
        order_q = shared_state.get("order_queue")
        log.info(
            "system_heartbeat",
            tasks_alive=list(shared_state.get("tasks_alive", {}).keys()),
            ws_connected=shared_state.get("ws_connected"),
            kill_switch_level=shared_state.get("kill_switch_level"),
            daily_pnl_pct=shared_state.get("daily_pnl_pct"),
            open_positions=len(shared_state.get("open_positions", {})),
            queue_depths={
                "storage_q": storage_q.qsize() if storage_q is not None else 0,
                "strategy_q": strategy_q.qsize() if strategy_q is not None else 0,
                "order_q": order_q.qsize() if order_q is not None else 0,
            },
        )

        # Telegram heartbeat — cadence driven by notifier config (default 30 min)
        interval_cycles = notifier.heartbeat_interval_cycles() if notifier is not None else 60
        if telegram_cycle % interval_cycles == 0:
            if notifier is not None:
                notifier.notify_heartbeat()
            else:
                regime = shared_state.get("market_regime") or "unknown"
                positions = len(shared_state.get("open_positions", {}))
                pnl_rs = shared_state.get("daily_pnl_rs", 0.0)
                ts = now_ist().strftime("%H:%M IST")
                await send_telegram(
                    f"💓 TradeOS alive\n"
                    f"Regime: {regime}\n"
                    f"Positions: {positions}\n"
                    f"Session PnL: ₹{pnl_rs:.0f}\n"
                    f"Time: {ts}",
                    shared_state,
                    secrets,
                )


async def run_trading_session(
    data_engine: DataEngine,
    strategy_engine: StrategyEngine,
    exec_engine: ExecutionEngine,
    risk_manager: RiskManager,
    shared_state: dict,
    config: dict,
    secrets: dict,
    regime_detector=None,
    notifier=None,
) -> None:
    """
    Phase 2: Run all 5 D6 tasks concurrently.

    Returns when risk_watchdog cancels tasks at 15:30 IST (orderly EOD),
    or when any critical task failure triggers Level 3 kill switch.

    Task exceptions are logged but do not re-raise (return_exceptions=True).
    """
    try:
        results = await asyncio.gather(
            data_engine.run(),           # D6 Task 1: ws_listener + tick storage
            strategy_engine.run(),       # D6 Task 2: signal_processor
            exec_engine.run(),           # D6 Tasks 3: order placer + order monitor
            risk_watchdog(shared_state, config, secrets, exec_engine, regime_detector, notifier),  # D6 Task 4
            heartbeat(shared_state, secrets, notifier),  # D6 Task 5
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        # risk_watchdog cancelled main() task at 15:30 — EOD shutdown, not an error
        log.info("run_trading_session_cancelled", reason="eod_shutdown_15_30")
        return

    task_names = [
        "data_engine", "strategy_engine", "exec_engine",
        "risk_watchdog", "heartbeat",
    ]
    for name, result in zip(task_names, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.error("task_failed_in_gather", task=name, error=str(result))


# ===========================================================================
# SECTION 7 — Phase 3: End-of-day shutdown
# ===========================================================================

async def end_of_day_shutdown(
    exec_engine: ExecutionEngine,
    risk_manager: RiskManager,
    shared_state: dict,
    db_pool: asyncpg.Pool,
    config: dict,
    secrets: dict,
) -> None:
    """
    Phase 3: EOD shutdown sequence (called after run_trading_session returns).

    15:00 — stop new signals (already set by risk_watchdog)
    15:00–15:15 — wait for open positions to close naturally
    15:15 — emergency exit any remaining positions
    15:20 — final D7 reconciliation (write system event to DB)
    15:25 — daily summary Telegram
    15:30 — clean shutdown (already triggered by risk_watchdog)
    """
    shared_state["accepting_signals"] = False
    log.info("eod_shutdown_begin", note="No new entries.")

    # Wait up to 15 minutes for open positions to close naturally
    for _ in range(15 * 60):
        if not shared_state.get("open_positions"):
            break
        await asyncio.sleep(1)

    # Emergency exit any remaining positions
    remaining = shared_state.get("open_positions", {})
    if remaining:
        symbols = list(remaining.keys())
        log.warning("positions_still_open_at_eod", symbols=symbols)
        exit_manager = getattr(exec_engine, "_exit_manager", None)
        if exit_manager is not None:
            try:
                await exit_manager.emergency_exit_all("hard_exit_1500")
            except Exception as exc:
                log.error("eod_emergency_exit_failed", error=str(exc))

    # 15:20 — final reconciliation event
    try:
        await write_system_event(
            db_pool, "RECONCILIATION_COMPLETE", "INFO", shared_state
        )
    except Exception as exc:
        log.error("eod_reconciliation_event_failed", error=str(exc))

    # 15:25 — daily summary
    await send_daily_summary(shared_state, db_pool, secrets)

    # 15:30 — mark system as stopped
    shared_state["system_ready"] = False
    today_str = today_ist().isoformat()
    log.info(
        "session_complete",
        date=today_str,
        final_pnl_pct=shared_state.get("daily_pnl_pct"),
        fills_today=shared_state.get("fills_today"),
    )
    await send_telegram(
        f"TradeOS shutdown: {today_str} session complete.",
        shared_state,
        secrets,
    )


# ===========================================================================
# SECTION 5 — Phase 1: Startup sequence + async main
# ===========================================================================

async def main(
    kite: KiteConnect,
    config: dict,
    secrets: dict,
    shared_state: dict,
) -> None:
    """
    Phase 1: Start all engines in dependency order, then run Phase 2.

    Startup order (D9 phase1-startup-sequence.md):
      1. DB pool
      2. DataEngine (connects WebSocket — blocks until WS CONNECTED)
      3. RiskManager
      4. StrategyEngine (warmup candles loaded)
      5. ExecutionEngine (startup reconciliation)
      6. system_ready = True
      7. run_trading_session() — Phase 2
      8. end_of_day_shutdown() — Phase 3
    """
    # Session metadata
    session_start = now_ist()
    shared_state["session_date"] = today_ist()
    shared_state["session_start_time"] = session_start
    shared_state["system_start_time"] = session_start

    mode = config.get("system", {}).get("mode", "paper")
    capital = config.get("capital", {}).get("total", 500000)
    shared_state["capital"] = float(capital)  # B4: expose capital for heartbeat unrealized P&L

    log.info(
        "startup_phase1_begin",
        session_date=str(shared_state["session_date"]),
        mode=mode,
    )

    # Resolve DB DSN — check multiple config key paths for compatibility
    db_dsn: str = str(
        _get_nested(config, "database.dsn")
        or _get_nested(config, "db.dsn")
        or _get_nested(secrets, "database.dsn")
        or ""
    )

    # Create fresh queues for this session (overwrite the placeholder queues in
    # shared_state that were created by _init_shared_state).
    # Two separate tick queues — each consumer gets its own queue to prevent
    # data_engine from racing with strategy_engine for the same ticks.
    tick_queue_storage: asyncio.Queue = asyncio.Queue(maxsize=1000)   # data_engine
    tick_queue_strategy: asyncio.Queue = asyncio.Queue(maxsize=1000)  # strategy_engine
    order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    shared_state["tick_queue_storage"] = tick_queue_storage
    shared_state["tick_queue_strategy"] = tick_queue_strategy
    shared_state["tick_queue"] = tick_queue_strategy   # backward compat (drain + monitoring)
    shared_state["order_queue"] = order_queue

    # Phase 1: Start engines in dependency order
    async with asyncpg.create_pool(db_dsn) as db_pool:
        # Regime detector: initialize before engines start
        from regime_detector import RegimeDetector
        regime_detector = RegimeDetector(kite, config, shared_state, secrets)
        initial_regime = await regime_detector.initialize()
        log.info("regime_initialized", regime=initial_regime.value)

        async with DataEngine(
            kite, config, shared_state, tick_queue_storage,
            strategy_queue=tick_queue_strategy,
        ) as data_engine:
            async with RiskManager(config, shared_state, db_pool) as risk_manager:
                notifier = TelegramNotifier(
                    "config/telegram_alerts.yaml", shared_state, secrets
                )
                async with StrategyEngine(
                    kite, config, shared_state, tick_queue_strategy, order_queue, db_pool,
                    regime_detector=regime_detector,
                    notifier=notifier,
                ) as strategy_engine:
                    async with ExecutionEngine(
                        kite, config, shared_state, order_queue,
                        risk_manager, db_pool,
                        notifier=notifier,
                    ) as exec_engine:

                        # All engines are up and WebSocket is connected
                        # (DataEngine.__aenter__ blocks until WS CONNECTED)
                        shared_state["session_date"] = today_ist()
                        shared_state["session_start_time"] = now_ist()
                        shared_state["system_ready"] = True

                        log.info(
                            "startup_system_ready",
                            mode=mode,
                            capital=capital,
                            session_date=str(shared_state["session_date"]),
                        )

                        # Telegram: system ready
                        ready_time = now_ist().strftime("%H:%M")
                        await send_telegram(
                            f"✅ TradeOS LIVE: System ready at {ready_time} IST.\n"
                            f"Mode={mode} Capital=₹{capital:,}",
                            shared_state,
                            secrets,
                        )

                        # Phase 2: Active trading (returns when 15:30 reached)
                        await run_trading_session(
                            data_engine,
                            strategy_engine,
                            exec_engine,
                            risk_manager,
                            shared_state,
                            config,
                            secrets,
                            regime_detector=regime_detector,
                            notifier=notifier,
                        )

                        # Phase 3: EOD shutdown
                        await end_of_day_shutdown(
                            exec_engine,
                            risk_manager,
                            shared_state,
                            db_pool,
                            config,
                            secrets,
                        )


# ===========================================================================
# SECTION 9 — Entry point
# ===========================================================================

def _handle_sigterm(signum, frame) -> None:  # type: ignore[type-arg]
    """SIGTERM handler — log and exit cleanly."""
    log.info("tradeos_sigterm_received", signal=signum)
    sys.exit(0)


if __name__ == "__main__":
    # Set timezone environment variable (affects stdlib time functions)
    os.environ["TZ"] = "Asia/Kolkata"

    # Configure structlog before any log calls
    _configure_structlog(dev_mode=True)

    # Register SIGTERM and SIGINT handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    # Phase 0: Pre-market gate (synchronous — before event loop)
    _shared_state = _init_shared_state()

    try:
        _kite, _config, _secrets = run_pre_market_gate(_shared_state)
    except SystemExit:
        raise  # sys.exit() propagates naturally

    # Phase 1–3: Async main
    try:
        asyncio.run(main(_kite, _config, _secrets, _shared_state))
    except KeyboardInterrupt:
        log.info("tradeos_keyboard_interrupt_clean_shutdown")
    except SystemExit as exc:
        log.info("tradeos_system_exit", code=exc.code)
    except asyncio.CancelledError:
        # Normal EOD shutdown — risk_watchdog cancelled tasks cleanly at 15:30
        log.info("tradeos_system_exit", code=0, reason="eod_shutdown")
        sys.exit(0)
    except Exception as exc:
        log.critical("tradeos_unhandled_exception", error=str(exc), exc_info=True)
        sys.exit(1)
