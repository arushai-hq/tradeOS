# ADR-002: Token Automation via Telegram

**Date:** 2026-03-10
**Status:** Accepted
**Deciders:** Irfan (Arushai Systems)

## Context

Zerodha KiteConnect requires daily token refresh via OAuth login. The previous workflow required manual SSH to VPS, opening the login URL, completing TOTP, and copying the token — error-prone and blocked by timezone (Doha is IST -1:30).

Missing or late token meant the entire trading session was lost.

## Decision

Adopt **Telegram-triggered semi-automation**:

1. **Cron trigger** — `token_cron.py` runs at 07:00 IST (Mon-Fri), starts `token_server.py` and sends login URL to Telegram
2. **4-stage escalation** — Reminders at 07:30, 08:00 IST. Final warning at 08:30 IST. Server killed at 08:45 if no auth
3. **Callback capture** — Nginx reverse proxy (port 11443, SSL) forwards `/callback` to token_server (port 7291). Server exchanges `request_token` for `access_token`, writes to `config/secrets.yaml`
4. **Auto-start** — On successful token capture, `main.py` launches in named tmux session (weekdays only)
5. **Confirmation** — Telegram message confirms token capture and main.py start

## Consequences

- Zero-SSH morning routine — tap Telegram link, complete TOTP, system starts automatically
- Single point of failure is Telegram delivery (mitigated by 4-stage escalation)
- SSL certificate renewal via Let's Encrypt certbot container
- Config-driven timing: all thresholds configurable in `config/settings.yaml` under `token_automation`
- Weekend/holiday handling: auto-start skipped on Sat/Sun via `weekdays_only` flag
