# Phase 2 — Active Trading Monitors

## Overview

Phase 2 runs from `system_ready = True` (end of Phase 1) through 15:00 IST (Phase 3 trigger).
The 5 D6 async tasks handle all trading activity. Phase 2 adds three **background health monitors**
that run inside the existing tasks — they are not separate tasks, they are periodic checks
embedded within the already-running tasks.

These monitors address failure modes that are only possible during an active session:
mid-session token expiry, session date drifting past midnight, and stale holiday calendar.

---

## MONITOR A — Mid-Session Token Expiry

**Embedded in:** `risk_watchdog_fn`
**Check interval:** Every 60 minutes (`asyncio.sleep(3600)`)
**Trigger:** Zerodha returns 401 or 403 on any API call

Zerodha tokens expire at midnight IST. During a normal 09:15–15:30 session this cannot happen.
But if the system is misconfigured (wrong timezone, clock drift, or started near midnight for testing),
the token can expire mid-session. This monitor detects it before silent API failures cascade.

**On detection:** Level 3 kill switch immediately — this is unrecoverable during session.
Token refresh requires browser OAuth. There is no automated path.

```python
async def risk_watchdog_fn(shared_state: dict) -> None:
    """
    Primary risk watchdog — runs every 1s for trading checks.
    Also runs mid-session token validity probe every 60 minutes.
    """
    last_token_check = asyncio.get_event_loop().time()
    TOKEN_CHECK_INTERVAL = 3600  # 60 minutes

    while True:
        try:
            # --- Standard 1-second checks (D1 kill switch conditions) ---
            if shared_state["daily_pnl_pct"] <= -MAX_DAILY_LOSS_PCT:
                kill_switch.trigger(level=2, reason="daily_loss_exceeded")

            if (shared_state["consecutive_losses"] >= 5
                    and shared_state["daily_pnl_pct"] <= -0.015):
                kill_switch.trigger(level=1, reason="consecutive_losses")

            # --- 60-minute token check (Phase 2 monitor) ---
            now = asyncio.get_event_loop().time()
            if now - last_token_check >= TOKEN_CHECK_INTERVAL:
                await _check_mid_session_token_validity(shared_state)
                last_token_check = now

        except Exception as e:
            log.critical("risk_watchdog_crashed", error=str(e))
            kill_switch.trigger(level=3, reason="risk_watchdog_crashed")
            raise

        await asyncio.sleep(1)


async def _check_mid_session_token_validity(shared_state: dict) -> None:
    """
    Calls kite.profile() to verify token is still valid.
    On 401/403 → Level 3 kill switch + Telegram.
    Does NOT attempt token refresh — Zerodha requires browser OAuth.
    Uses asyncio.to_thread() because kite.profile() is synchronous.
    """
    try:
        kite = shared_state["kite"]
        await asyncio.to_thread(kite.profile)  # non-blocking call
        log.debug("mid_session_token_valid")
    except Exception as e:
        error_str = str(e).lower()
        if "401" in error_str or "403" in error_str or "token" in error_str:
            log.critical("mid_session_token_expired",
                         error=str(e),
                         note="Token expired mid-session — unrecoverable")
            await send_critical_alert(
                "token_expired_mid_session",
                {"Error": str(e), "Action": "Emergency halt — token refresh required"},
                shared_state=shared_state
            )
            kill_switch.trigger(level=3, reason="mid_session_token_expired")
        else:
            # Non-auth error (network glitch) — log and continue
            log.warning("mid_session_token_check_failed_non_auth", error=str(e))
```

---

## MONITOR B — Session Date Drift

**Embedded in:** `heartbeat_fn`
**Check interval:** Every 30 minutes (runs on each heartbeat cycle)
**Trigger:** IST date changes while system is running

This should never happen in normal operation (markets close at 15:30, system shuts down by 15:30).
But it can happen in testing (system started late at night) or if the system has been running
for many hours across a midnight boundary. If the date changes, all logs, reconciliation records,
and session counters carry the wrong date — silent data corruption.

```python
async def heartbeat_fn(shared_state: dict) -> None:
    """
    System heartbeat every 30 seconds.
    Also checks session date drift on each cycle.
    """
    while True:
        await asyncio.sleep(30)
        shared_state["tasks_alive"]["heartbeat"] = True

        # Check all other tasks are still alive
        for task_name, alive in shared_state["tasks_alive"].items():
            if not alive:
                log.critical("task_not_alive", task=task_name)
                await send_warning_alert("task_not_alive", "task_not_alive",
                                         {"Task": task_name}, shared_state=shared_state)

        # Session date drift check (Phase 2 monitor)
        await _check_session_date_drift(shared_state)

        # Emit heartbeat log
        log.info("system_heartbeat",
                 tasks_alive=list(shared_state["tasks_alive"].keys()),
                 ws_connected=shared_state["ws_connected"],
                 kill_switch_level=shared_state["kill_switch_level"],
                 daily_pnl_pct=shared_state["daily_pnl_pct"],
                 open_positions=len(shared_state["open_positions"]),
                 session_date=shared_state["session_date"])


async def _check_session_date_drift(shared_state: dict) -> None:
    """
    Verifies that IST date hasn't changed since session start.
    On drift: Level 3 kill switch + Telegram.
    """
    current_date = datetime.now(pytz.timezone("Asia/Kolkata")).date().isoformat()
    session_date = shared_state.get("session_date")

    if session_date and current_date != session_date:
        log.critical("session_date_drift",
                     session_date=session_date, current_date=current_date,
                     note="Midnight crossed — session date no longer matches")
        await send_critical_alert(
            "session_date_drift",
            {"Session Date": session_date,
             "Current Date": current_date,
             "Action": "Emergency halt — session has drifted past midnight"},
            shared_state=shared_state
        )
        kill_switch.trigger(level=3, reason="session_date_drift")
```

---

## MONITOR C — Holiday Calendar Staleness Warning

**Embedded in:** `heartbeat_fn` (checked once at startup, then annually)
**Check condition:** Today is December 31 AND next year is not in `nse_holidays.yaml`
**Action:** WARNING only — no halt

The `config/nse_holidays.yaml` covers one calendar year and must be manually updated each December
with the new year's NSE holiday list. This monitor reminds the operator before it becomes a problem.

```python
async def _check_holiday_calendar_staleness(shared_state: dict) -> None:
    """
    If today is December 31 and next year's holidays aren't in the calendar:
    warn via Telegram. This is a reminder to update nse_holidays.yaml.
    Called once per session from heartbeat at startup time.
    """
    now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    if now_ist.month != 12 or now_ist.day != 31:
        return

    next_year = now_ist.year + 1
    try:
        with open("config/nse_holidays.yaml") as f:
            holidays_config = yaml.safe_load(f)
        has_next_year = str(next_year) in holidays_config or next_year in holidays_config
        if not has_next_year:
            log.warning("nse_holidays_update_needed",
                        next_year=next_year,
                        note="Update config/nse_holidays.yaml before year end")
            await send_warning_alert(
                "holiday_calendar_staleness",
                "nse_holidays_update_needed",
                {"Next year": next_year,
                 "Action": "Update config/nse_holidays.yaml — see nse-holidays-maintenance.md"},
                shared_state=shared_state
            )
    except FileNotFoundError:
        pass  # Already handled by Phase 0 CHECK 4
```
