# Daily Trading Runbook

## Morning Checklist (07:00-09:15 IST)

1. **07:00** — Telegram login link arrives (auto via cron)
2. **Tap link** — Complete Zerodha login + TOTP in browser
3. **Auto-start** — `main.py` starts in tmux automatically after token capture
4. **Verify** — Run `tradeos status` to confirm:
   - Trading process running
   - Token valid for today
   - Database connected
   - Nginx healthy

### Pre-market check (if auto-start fails)

```bash
tradeos preflight                # 8-point health check
tradeos start                    # Manual start in tmux
tradeos status                   # Verify running
```

## During Market Hours (09:15-15:30 IST)

- System runs autonomously — do not deploy or restart
- Monitor via Telegram alerts (signals, trades, errors)
- `tradeos status` for health checks
- If kill switch triggers: review logs, do NOT reset manually

## EOD Checklist (after 15:30 IST)

1. **Auto-shutdown** — System exits gracefully at 15:00 IST (hard exit)
2. **Report** — `tradeos report auto` generates EOD report + log vs DB verification
3. **Review** — Check Telegram for report summary, look for anomalies
4. **Logs** — `tradeos logs tail` if anything unusual

## Emergency Procedures

### System unresponsive

```bash
tradeos stop                     # Graceful stop (force kill after 3s)
tradeos logs tail                # Check last errors
tradeos start                    # Restart
```

### Database issues

```bash
tradeos docker ps                # Check container status
tradeos docker logs              # Check TimescaleDB logs
tradeos docker down && tradeos docker up  # Restart containers
```

### Kill switch triggered

1. Check `tradeos logs tail` for trigger reason
2. Review P&L — was it a real loss or false positive?
3. If false positive (B7/B8 pattern), fix and restart
4. If real loss, the system correctly stopped — review strategy

### Token expired mid-session

```bash
tradeos auth-server              # Start callback server
# Complete Zerodha login via Telegram link
tradeos restart                  # Restart with new token
```
