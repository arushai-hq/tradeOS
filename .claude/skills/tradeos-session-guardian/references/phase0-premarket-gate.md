# Phase 0 — Pre-Market Gate

## Table of Contents
1. [Overview](#overview)
2. [CHECK 1 — Config Validation](#check-1--config-validation)
3. [CHECK 2 — Token Date Freshness](#check-2--token-date-freshness)
4. [CHECK 3 — Token Live Validation](#check-3--token-live-validation)
5. [CHECK 4 — NSE Holiday / Weekend](#check-4--nse-holiday--weekend)
6. [CHECK 5 — Telegram Path Validation](#check-5--telegram-path-validation)
7. [CHECK 6 — IST Time Window](#check-6--ist-time-window)
8. [Entry Point](#entry-point)
9. [Shared Helper](#shared-helper)

---

## Overview

Phase 0 runs **synchronously before any network connection or asyncio event loop**.
Its job is to fail fast on conditions that would make a trading session invalid —
before any market data flows, before any orders are possible, before the operator
loses the opportunity to fix the problem.

All 6 checks run in strict sequential order. A failure in CHECK 2 does not trigger CHECK 3.
Each check that ends with `sys.exit()` never returns — the caller (`run_pre_market_gate()`)
reads this as clean termination.

The two exit codes have different meanings:
- `sys.exit(1)` — ERROR: something is wrong, operator action required
- `sys.exit(0)` — NORMAL: today is not a trading day (holiday/weekend)

---

## CHECK 1 — Config Validation

Load `config/settings.yaml` and `config/secrets.yaml`. Verify all required keys exist before any API call.

```python
REQUIRED_SETTINGS_KEYS = ["system.mode", "capital.total", "capital.s1_allocation",
                           "risk.max_loss_per_trade_pct", "risk.max_daily_loss_pct",
                           "risk.max_open_positions"]
REQUIRED_SECRETS_KEYS  = ["zerodha.api_key", "zerodha.api_secret", "zerodha.access_token",
                           "zerodha.token_date", "telegram.bot_token", "telegram.chat_id"]

def run_config_check() -> tuple[dict, dict]:
    """
    Loads settings.yaml and secrets.yaml, verifies required keys.
    Returns (config, secrets) on success.
    Calls sys.exit(1) if any file is missing or any key absent.
    """
    import yaml, sys
    try:
        with open("config/settings.yaml") as f:
            config = yaml.safe_load(f)
        with open("config/secrets.yaml") as f:
            secrets = yaml.safe_load(f)
    except FileNotFoundError as e:
        log.critical("config_file_missing", error=str(e))
        sys.exit(1)

    missing = []
    for dotted_key in REQUIRED_SETTINGS_KEYS:
        parts = dotted_key.split(".")
        val = config
        for p in parts:
            val = (val or {}).get(p)
        if val is None:
            missing.append(f"settings.yaml:{dotted_key}")

    for dotted_key in REQUIRED_SECRETS_KEYS:
        parts = dotted_key.split(".")
        val = secrets
        for p in parts:
            val = (val or {}).get(p)
        if val is None:
            missing.append(f"secrets.yaml:{dotted_key}")

    if missing:
        log.critical("config_incomplete", missing_keys=missing)
        sys.exit(1)

    return config, secrets
```

---

## CHECK 2 — Token Date Freshness

Zerodha `access_token` expires at **midnight IST every day**. No auto-refresh is possible —
the Zerodha OAuth flow requires browser interaction. The user must run `python scripts/zerodha_auth.py`
each morning and the resulting token + date are written to `config/secrets.yaml`.

The `token_date` field (YYYY-MM-DD ISO format) is compared to today's IST date. If they differ,
the token is stale and all subsequent API calls will fail silently — far worse than an early abort.

```python
import sys
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

def run_token_freshness_check(secrets: dict) -> None:
    """
    Compares secrets["zerodha"]["token_date"] to today's IST date.
    Calls sys.exit(1) if token_date != today_ist.
    """
    token_date = secrets.get("zerodha", {}).get("token_date", "").strip()
    today_ist = datetime.now(IST).date().isoformat()  # e.g. "2026-03-05"

    if not token_date:
        log.critical("startup_blocked_no_token_date",
                     reason="token_date missing from secrets.yaml")
        _send_startup_alert_sync(
            secrets,
            "⛔ TradeOS blocked: token_date missing from secrets.yaml.\n"
            "Run: python scripts/zerodha_auth.py"
        )
        sys.exit(1)

    if token_date != today_ist:
        log.critical("startup_blocked_stale_token",
                     token_date=token_date, today=today_ist)
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS blocked: Zerodha access_token expired.\n"
            f"Token date: {token_date} | Today: {today_ist}\n"
            f"Run: python scripts/zerodha_auth.py"
        )
        sys.exit(1)
```

---

## CHECK 3 — Token Live Validation

Even if `token_date` matches today, the token itself may be invalid (revoked, regenerated elsewhere).
A live probe via `kite.profile()` catches this before any market connection attempt.

```python
from kiteconnect import KiteConnect

def run_token_validity_check(secrets: dict) -> KiteConnect:
    """
    Instantiates KiteConnect and calls kite.profile() to validate the token.
    Returns a ready KiteConnect instance on success.
    Calls sys.exit(1) on any API error (401, 403, network error).
    Stores user_id in shared_state for audit trail.
    """
    api_key = secrets["zerodha"]["api_key"]
    access_token = secrets["zerodha"]["access_token"]

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        profile = kite.profile()  # synchronous — pre-event-loop
        log.info("startup_token_valid",
                 user_id=profile.get("user_id"),
                 user_name=profile.get("user_name"))
        return kite
    except Exception as e:
        log.critical("startup_blocked_invalid_token",
                     error=str(e), error_type=type(e).__name__)
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS blocked: Zerodha token invalid or API unreachable.\n"
            f"Error: {str(e)}\n"
            f"Run: python scripts/zerodha_auth.py"
        )
        sys.exit(1)
```

---

## CHECK 4 — NSE Holiday / Weekend

NSE does not trade on gazetted holidays, bank holidays, Saturdays, or Sundays.
If the system starts on a non-trading day:
- No WebSocket connection will succeed (exchange is closed)
- D3's heartbeat would trigger WS disconnect Level 2 after 60s — wrong escalation
- The system would burn tokens and CPU doing nothing all day

Weekends are detected by `weekday()` (Mon=0, Sun=6 → Sat=5, Sun=6 are non-trading).
Holidays are read from `config/nse_holidays.yaml` (see `nse-holidays-maintenance.md`).

Exit code is `sys.exit(0)` — this is a normal, expected condition, not an error.

```python
import yaml
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

def run_holiday_check(secrets: dict) -> None:
    """
    Checks if today is an NSE holiday or weekend.
    sys.exit(0) if non-trading day (clean exit — not an error).
    Continues silently if trading day.
    """
    now_ist = datetime.now(IST)
    today_ist = now_ist.date().isoformat()
    weekday = now_ist.weekday()  # Monday=0, Sunday=6

    # Saturday or Sunday
    if weekday >= 5:
        day_name = "Saturday" if weekday == 5 else "Sunday"
        log.info("market_closed_weekend", date=today_ist, day=day_name)
        _send_startup_alert_sync(
            secrets,
            f"📅 TradeOS: {day_name} — NSE closed. No trading today."
        )
        sys.exit(0)

    # NSE holiday calendar
    try:
        with open("config/nse_holidays.yaml") as f:
            holidays_config = yaml.safe_load(f)
        year = now_ist.year
        holidays_this_year = holidays_config.get(str(year), holidays_config.get(year, []))
    except FileNotFoundError:
        log.warning("nse_holidays_file_missing",
                    note="Cannot check NSE holidays — proceeding without check")
        return  # Non-fatal if file missing

    if today_ist in holidays_this_year:
        log.info("market_closed_holiday", date=today_ist)
        _send_startup_alert_sync(
            secrets,
            f"📅 TradeOS: NSE holiday today ({today_ist}) — no trading."
        )
        sys.exit(0)  # sys.exit(0) — clean, expected, not an error
```

---

## CHECK 5 — Telegram Path Validation

Telegram is TradeOS's only real-time alert channel. A broken alert path means the operator
is blind to kill switch events, position mismatches, and system failures during the trading day.
Validating at startup ensures early detection of configuration problems.

Telegram failure is **non-blocking** — trading continues. The reason: a Telegram outage should
not prevent profitable trading. But subsequent alerts are degraded to structured log entries
with a `[TELEGRAM_FAILED]` prefix so they can be recovered post-session.

```python
def run_telegram_check(secrets: dict, shared_state: dict) -> None:
    """
    Sends a test message to verify the Telegram alert path.
    On failure: sets shared_state["telegram_active"] = False and continues.
    On success: sets shared_state["telegram_active"] = True.
    Never calls sys.exit() — trading continues regardless.
    """
    import requests
    from datetime import datetime
    import pytz

    today_str = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")

    try:
        bot_token = secrets.get("telegram", {}).get("bot_token", "")
        chat_id = secrets.get("telegram", {}).get("chat_id", "")

        if not bot_token or not chat_id:
            raise ValueError("telegram.bot_token or telegram.chat_id missing from secrets.yaml")

        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🟡 TradeOS {today_str}: Alert path active."},
            timeout=5,
        )
        resp.raise_for_status()
        shared_state["telegram_active"] = True
        log.info("startup_telegram_ok")

    except Exception as e:
        shared_state["telegram_active"] = False
        log.warning("TELEGRAM_ALERT_PATH_BROKEN",
                    error=str(e),
                    note="All subsequent alerts will be logged with [TELEGRAM_FAILED] prefix")
        # Do NOT sys.exit() — trading continues with file-only alerts
```

When `telegram_active` is False, all subsequent `send_telegram()` calls must check the flag:
```python
async def send_telegram(message: str, shared_state: dict, ...) -> None:
    if not shared_state.get("telegram_active", True):
        log.warning("telegram_alert_suppressed",
                    message_preview=message[:100],
                    prefix="[TELEGRAM_FAILED]")
        return
    # ... normal send logic
```

---

## CHECK 6 — IST Time Window

Starting the system outside the optimal pre-market window wastes the first candle or starts
mid-session with incomplete indicator history. The time window rules prevent these scenarios:

| IST Time | Action | Reason |
|----------|--------|--------|
| < 08:45 | Sleep until 08:45 | Too early — pre-market startup window |
| 08:45–09:10 | Proceed | Optimal window |
| 09:10–12:00 | WARNING + proceed | Late start — first candle(s) missed |
| > 12:00 | ERROR + `sys.exit(1)` | Past midday — indicator history too short for S1 |

```python
import time as time_module
from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")

def run_time_window_check(secrets: dict) -> None:
    """
    Validates that startup is within the acceptable IST time window.
    Sleeps if too early; warns if late; exits if past noon.
    All time comparisons use IST timezone.
    """
    now_ist = datetime.now(IST)
    current_time = now_ist.time()

    # Too early — sleep until 08:45
    if current_time < time(8, 45):
        target = now_ist.replace(hour=8, minute=45, second=0, microsecond=0)
        wait_seconds = (target - now_ist).total_seconds()
        log.info("startup_sleeping_until_0845",
                 wait_seconds=round(wait_seconds), current_time=str(current_time))
        time_module.sleep(wait_seconds)
        return

    # Past noon — abort
    if current_time > time(12, 0):
        log.error("startup_too_late",
                  current_time=str(current_time),
                  reason="Past 12:00 IST — insufficient trading window for S1 indicator history")
        _send_startup_alert_sync(
            secrets,
            f"⛔ TradeOS: Start after 12:00 IST ({current_time.strftime('%H:%M')}). "
            f"Aborting — insufficient trading history for S1."
        )
        sys.exit(1)

    # Late start (09:10–12:00) — warn and continue
    if current_time > time(9, 10):
        log.warning("startup_late_start",
                    current_time=str(current_time),
                    note="First candle(s) may be missed")
        _send_startup_alert_sync(
            secrets,
            f"⚠️ TradeOS late start at {current_time.strftime('%H:%M')} IST. "
            f"First candle(s) missed."
        )
        # Continue — partial session is better than no session
```

---

## Entry Point

```python
import sys
import structlog
import pytz
from kiteconnect import KiteConnect
from datetime import datetime

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

def run_pre_market_gate(shared_state: dict) -> KiteConnect:
    """
    Orchestrates all 6 Phase 0 checks in strict sequential order.
    Returns a validated KiteConnect instance on success.
    Calls sys.exit() on any hard-stop condition — never raises, never retries.
    """
    # CHECK 1: Config / secrets validation
    config, secrets = run_config_check()

    # CHECK 2: Token date freshness
    run_token_freshness_check(secrets)

    # CHECK 3: Token live validation
    kite = run_token_validity_check(secrets)
    shared_state["zerodha_user_id"] = kite.profile().get("user_id", "unknown")

    # CHECK 4: NSE holiday / weekend
    run_holiday_check(secrets)

    # CHECK 5: Telegram path validation
    run_telegram_check(secrets, shared_state)

    # CHECK 6: IST time window
    run_time_window_check(secrets)

    # All 6 passed
    shared_state["pre_market_gate_passed"] = True
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    log.info("pre_market_gate_passed", date=today_str)
    _send_startup_alert_sync(
        secrets,
        f"🟢 TradeOS {today_str}: Pre-market gate passed. Starting up."
    )

    return kite
```

---

## Shared Helper

```python
def _send_startup_alert_sync(secrets: dict, message: str) -> None:
    """
    Synchronous Telegram send for pre-event-loop startup alerts.
    Uses requests (sync) — NOT httpx. Acceptable here because no event loop exists yet.
    Failure is silent — startup error is already in the structured log.
    """
    try:
        import requests
        bot_token = secrets.get("telegram", {}).get("bot_token", "")
        chat_id = secrets.get("telegram", {}).get("chat_id", "")
        if bot_token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=5,
            )
    except Exception:
        pass  # Startup alert failure is non-fatal — structured log already written
```
