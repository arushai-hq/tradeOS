# Pre-Market Checks — TradeOS Startup Gate Sequence

These checks run **synchronously before any market connection is attempted**. If any hard-stop check fails, the system exits with `sys.exit(1)`. No asyncio event loop. No WebSocket. No Zerodha API call (except the token probe in step 4).

The sequence is strictly ordered — each step must pass before the next begins.

---

## Full Sequence

```
1. Load config/secrets.yaml
2. Validate access_token exists + dated today (IST)
3. If token missing or stale → CRITICAL log + Telegram + sys.exit(1)
4. Probe token: kite.profile() — if API error → CRITICAL log + Telegram + sys.exit(1)
5. (Token clean) → D7 startup reconciliation may proceed
6. Check NSE holiday calendar for today's IST date
7. If today is a holiday → INFO log + Telegram + sleep loop + sys.exit(0) at EOD
8. Send Telegram test message "🟡 TradeOS startup check — alerts active"
9. If Telegram fails → log CRITICAL to file only, set shared_state["telegram_active"] = False
```

---

## Step 1-5 — Zerodha Access Token Validation

Zerodha `access_token` expires at **midnight IST every day**. Zerodha's OAuth flow requires browser interaction — there is no auto-refresh. The user must manually run the auth script each morning and paste the new token into `config/secrets.yaml`.

### secrets.yaml schema (relevant section)

```yaml
# config/secrets.yaml (gitignored — never commit)
zerodha:
  api_key: "your_api_key"
  api_secret: "your_api_secret"
  access_token: "your_access_token"
  token_date: "2026-03-05"   # ISO format YYYY-MM-DD — update daily with token
```

The `token_date` field is set manually by the user when they generate a new token each morning. The system uses this to detect a stale token without making an API call.

### Implementation

```python
import sys
import yaml
import pytz
import structlog
from datetime import datetime
from kiteconnect import KiteConnect

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


def run_startup_token_check(config: dict, secrets: dict) -> KiteConnect:
    """
    Steps 1-5: Validate Zerodha access_token.
    Returns a ready KiteConnect instance on success.
    Calls sys.exit(1) on any failure — never raises, never continues silently.
    """
    zerodha = secrets.get("zerodha", {})
    token = zerodha.get("access_token", "").strip()
    token_date = zerodha.get("token_date", "").strip()
    api_key = zerodha.get("api_key", "").strip()

    today_ist = datetime.now(IST).date().isoformat()

    # Step 2: Check token exists
    if not token:
        log.critical("startup_blocked_no_token",
                     reason="access_token missing from secrets.yaml")
        _send_startup_alert_sync(
            secrets,
            "⛔ STARTUP BLOCKED: Zerodha access_token missing. "
            "Run token refresh and update config/secrets.yaml."
        )
        sys.exit(1)

    # Step 2: Check token is dated today
    if token_date != today_ist:
        log.critical("startup_blocked_stale_token",
                     token_date=token_date, today=today_ist)
        _send_startup_alert_sync(
            secrets,
            f"⛔ STARTUP BLOCKED: Zerodha access_token expired.\n"
            f"Token date: {token_date} | Today: {today_ist}\n"
            f"Run token refresh and update config/secrets.yaml."
        )
        sys.exit(1)

    # Step 4: Probe token with kite.profile()
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        profile = kite.profile()  # Synchronous — pre-event-loop
        log.info("startup_token_valid",
                 user_id=profile.get("user_id"),
                 user_name=profile.get("user_name"))
    except Exception as e:
        log.critical("startup_blocked_api_error",
                     error=str(e), error_type=type(e).__name__)
        _send_startup_alert_sync(
            secrets,
            f"⛔ STARTUP BLOCKED: Zerodha API error during token probe.\n"
            f"Error: {str(e)}\n"
            f"Check token validity and network."
        )
        sys.exit(1)

    return kite  # Step 5: token clean, return for use in D7 reconciliation


def _send_startup_alert_sync(secrets: dict, message: str) -> None:
    """
    Synchronous Telegram send for pre-event-loop startup alerts.
    Uses requests (sync) — NOT httpx. Only for startup checks.
    Failure is silent — startup error is already in the log.
    """
    try:
        import requests  # sync — only acceptable here (no event loop yet)
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
```

**Token refresh process (manual):**
1. User visits Zerodha login URL (generated by `kite.login_url()`)
2. Completes browser OAuth → Zerodha redirects with `request_token`
3. User runs auth script: `python scripts/zerodha_auth.py <request_token>`
4. Script generates `access_token` and writes it to `config/secrets.yaml` with today's date

---

## Step 6-7 — NSE Exchange Holiday Check

NSE does not trade on gazetted holidays and bank holidays. Trading on a holiday means heartbeat would fire WS disconnect Level 2 for a day when no market connection is expected.

### Recommended approach: local YAML calendar (reliable, no external dependency)

Maintain `config/nse_holidays.yaml` — updated once per year at the start of the trading year. NSE publishes the holiday list in December for the following year.

```yaml
# config/nse_holidays.yaml
# NSE equity market holidays — updated annually
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
2026:
  - "2026-01-14"  # Makar Sankranti / Pongal
  - "2026-01-26"  # Republic Day
  - "2026-02-19"  # Chhatrapati Shivaji Maharaj Jayanti
  - "2026-03-13"  # Holi
  - "2026-03-30"  # Id-Ul-Fitr (Ramzan Id) — confirm closer to date
  - "2026-04-02"  # Shri Ram Navami
  - "2026-04-03"  # Good Friday
  - "2026-04-14"  # Dr.Baba Saheb Ambedkar Jayanti
  - "2026-05-01"  # Maharashtra Day
  # ... complete list for the year
```

### Alternative: nsepython (live, auto-updated)

```python
# pip install nsepython
from nsepython import nse_holidays

def fetch_holidays_from_nse() -> list[str]:
    """Returns ISO date strings for NSE holidays."""
    raw = nse_holidays()  # Returns list of dicts with 'tradingDate' field
    return [h["tradingDate"] for h in raw.get("CM", [])]  # CM = Capital Markets
```

Use nsepython only if you prefer live data. The local YAML is more reliable at startup when network may be slow.

### Implementation

```python
def run_holiday_check(config: dict, secrets: dict) -> None:
    """
    Step 6-7: Check if today is an NSE holiday.
    If yes: log INFO + Telegram INFO + sys.exit(0) — clean exit, not a failure.
    """
    today_ist = datetime.now(IST).date().isoformat()
    year = datetime.now(IST).year

    try:
        with open("config/nse_holidays.yaml") as f:
            holidays_config = yaml.safe_load(f)
        holidays_this_year = holidays_config.get(year, [])
    except FileNotFoundError:
        log.warning("nse_holidays_file_missing",
                    path="config/nse_holidays.yaml",
                    note="Cannot check holidays — proceeding without check")
        return  # Non-fatal if file missing — system proceeds

    if today_ist in holidays_this_year:
        message = (
            f"📅 NSE holiday today ({today_ist}) — TradeOS idle.\n"
            f"System will not trade. Restart tomorrow."
        )
        log.info("market_closed_holiday", date=today_ist)
        _send_startup_alert_sync(secrets, message)
        sys.exit(0)  # Clean exit — not a failure
```

---

## Step 8-9 — Telegram Credential Validation

Telegram is TradeOS's only real-time alert channel. If alerts are broken, the operator is blind to kill switch events, position mismatches, and system failures. Validate the path at startup.

Trading is **not blocked** by a Telegram failure — the system continues. But `shared_state["telegram_active"]` is set to `False` so the async alert functions know to prefix log entries with `[TELEGRAM_FAILED]` for post-session recovery.

### Implementation

```python
def run_telegram_check(secrets: dict, shared_state: dict) -> None:
    """
    Step 8-9: Validate Telegram alert path.
    Failure → system continues, but shared_state["telegram_active"] = False.
    """
    try:
        import requests
        bot_token = secrets.get("telegram", {}).get("bot_token", "")
        chat_id = secrets.get("telegram", {}).get("chat_id", "")

        if not bot_token or not chat_id:
            raise ValueError("telegram.bot_token or telegram.chat_id missing from secrets.yaml")

        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "🟡 TradeOS startup check — alerts active"
            },
            timeout=5,
        )
        resp.raise_for_status()
        shared_state["telegram_active"] = True
        log.info("startup_telegram_ok")

    except Exception as e:
        # Non-fatal — trading continues
        shared_state["telegram_active"] = False
        log.critical("TELEGRAM_ALERT_PATH_BROKEN",
                     error=str(e),
                     note="All subsequent alerts will be logged with [TELEGRAM_FAILED] prefix")
```

**Alert degradation when `telegram_active == False`:**

In `send_telegram()` (D4 `telegram-alerting-rules.md`), check the flag:

```python
async def send_telegram(message: str, bot_token: str, chat_id: str,
                        shared_state: dict, critical: bool = False) -> None:
    if not shared_state.get("telegram_active", True):
        # Log with recoverable prefix instead of sending
        log.warning("telegram_alert_suppressed",
                    message_preview=message[:100],
                    prefix="[TELEGRAM_FAILED]")
        return
    # ... existing send logic
```

---

## Full `run_pre_market_checks()` Entry Point

```python
def run_pre_market_checks(shared_state: dict) -> KiteConnect:
    """
    Orchestrates all startup checks in strict order.
    Returns a validated KiteConnect instance on success.
    Exits the process on any hard-stop failure.
    """
    # Load config and secrets
    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)
    with open("config/secrets.yaml") as f:
        secrets = yaml.safe_load(f)

    # Steps 1-5: Token validation
    kite = run_startup_token_check(config, secrets)

    # Steps 6-7: Holiday check
    run_holiday_check(config, secrets)

    # Steps 8-9: Telegram validation
    run_telegram_check(secrets, shared_state)

    log.info("pre_market_checks_passed",
             date=datetime.now(IST).date().isoformat())

    return kite
```

Call from `main.py` before `asyncio.run()`:

```python
if __name__ == "__main__":
    shared_state = _init_shared_state()
    kite = run_pre_market_checks(shared_state)  # ← synchronous, before event loop
    asyncio.run(main(kite, shared_state))
```

---

## What This Prevents

| Without this check | With this check |
|-------------------|----------------|
| Startup connects to Zerodha with stale token → all API calls fail silently | Token validation at startup → explicit sys.exit(1) with actionable message |
| Trading fires on NSE holidays → WebSocket never connects → WS disconnect triggers Level 2 | Holiday detected → clean exit before any connection attempt |
| Telegram alerts broken → kill switch fires silently during crisis | Alert path tested at startup → operator aware immediately |
