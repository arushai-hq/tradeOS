---
name: tradeos-operations
description: >
  Daily operational workflow for TradeOS on VPS. Use when generating deployment
  or operations instructions, understanding the tradeos CLI, or planning
  maintenance windows. Invoke for deployment, VPS operations, daily workflow,
  tradeos CLI usage, tmux session management, or log checking.
  Do NOT invoke for: local development workflows, test running, non-VPS
  environments, or code changes.
related-skills: tradeos-session-guardian, tradeos-observability, tradeos-architecture
---

# TradeOS Operations Guide

## Daily Workflow

### Pre-Market (Before 09:15 IST)

```bash
# 1. Preflight check — verifies all 6 pre-market gates
tradeos preflight

# 2. Authenticate with Zerodha (if not using token_cron)
tradeos auth

# 3. Start trading session in tmux
tradeos start
```

### During Market Hours (09:15 - 15:30 IST)

```bash
# Check system health
tradeos status

# Monitor Telegram notifications — check every 30 minutes
# If no heartbeat in 30 minutes → investigate immediately

# Check logs if Telegram notifications missed
tradeos logs                    # View today's logs
tradeos logs --tail             # Live tail
```

### Post-Market (After 15:30 IST)

```bash
# Graceful shutdown (if not auto-stopped by D9)
tradeos stop

# Generate end-of-day report
tradeos report auto

# Verify report matches Zerodha console
tradeos report verify
```

## CLI Reference

| Command | Purpose |
|---------|---------|
| `tradeos preflight` | Run 6 pre-market gate checks |
| `tradeos auth` | Zerodha token authentication |
| `tradeos start` | Start trading in tmux session |
| `tradeos status` | System health check |
| `tradeos stop` | Graceful shutdown |
| `tradeos report auto` | Generate EOD report |
| `tradeos report verify` | Verify against Zerodha |
| `tradeos test` | Run test suite |
| `tradeos logs` | View today's logs |
| `tradeos hawk run` | Run HAWK analysis |
| `tradeos hawk status` | HAWK system status |

## Deployment Rules

### CRITICAL: Never Deploy During Market Hours

```
Market hours: 09:15 AM - 3:30 PM IST (Monday-Friday)
Deployment window: ONLY before 09:00 or after 16:00 IST
```

**Why:** A deployment during market hours could restart the system, causing:
- Open positions to lose monitoring
- Kill switch state to reset
- WebSocket reconnection delay
- Missed exit signals

### Deployment Checklist

1. Verify market is closed: `tradeos status` should show "session_ended" or "pre_market"
2. Run full test suite: `tradeos test -x -q` — zero failures required
3. Pull changes: `cd /opt/tradeOS && git pull origin main`
4. Verify config: `cat config/settings.yaml | grep mode` — must show `paper`
5. Run preflight: `tradeos preflight`
6. Test start/stop cycle: `tradeos start && sleep 5 && tradeos status`

## tmux Session Management

```bash
# TradeOS runs in a tmux session named "tradeos"
tmux attach -t tradeos          # Attach to running session
tmux ls                         # List sessions
# Ctrl-B then D to detach (leave running)

# NEVER kill the tmux session during market hours
# Use `tradeos stop` for graceful shutdown
```

## Log Checking Protocol

When Telegram notifications are missed:

```bash
# 1. Check if process is running
tradeos status

# 2. Check today's main log
tail -100 logs/tradeos/tradeos_$(date +%Y-%m-%d).log

# 3. Check for errors
grep -i "error\|exception\|kill_switch" logs/tradeos/tradeos_$(date +%Y-%m-%d).log

# 4. Check HAWK log
tail -50 logs/hawk/hawk_$(date +%Y-%m-%d).log

# 5. Check token log
tail -20 logs/token/token_$(date +%Y-%m-%d).log
```

## Log File Convention

```
logs/
├── tradeos/tradeos_2026-03-15.log    # Main trading log
├── hawk/hawk_2026-03-15.log          # HAWK AI log
└── token/token_2026-03-15.log        # Token auth log
```

- Rotation: 30-day compress, 90-day delete (via `scripts/log_rotation.sh`)
- Format: structlog JSON output
- Never commit log files to git

## Emergency Procedures

### Kill Switch Triggered
1. Check Telegram for trigger reason
2. `tradeos status` — confirm kill switch state
3. Review logs for the trigger event
4. DO NOT restart during market hours — wait for EOD
5. After market close: investigate, fix, test, deploy

### WebSocket Disconnected
- D3 auto-reconnect handles this (exponential backoff 2→30s)
- If reconnect fails after 5 minutes → check Zerodha status page
- Manual intervention: `tradeos stop && tradeos start`

### Process Crashed
1. `tradeos status` — check tmux session
2. Check logs for crash reason
3. If during market hours: `tradeos start` (D9 will reconcile on startup via D7)
4. Monitor closely for 15 minutes after restart
