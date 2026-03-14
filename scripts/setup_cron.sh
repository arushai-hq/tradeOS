#!/usr/bin/env bash
set -euo pipefail

TRADEOS_DIR="/opt/tradeOS"
VENV_PYTHON="$TRADEOS_DIR/.venv/bin/python"

# 07:00 IST = 01:30 UTC
CRON_CMD="30 1 * * 1-5 cd $TRADEOS_DIR && $VENV_PYTHON scripts/token_cron.py >> logs/token_cron.log 2>&1"

echo "=== TradeOS Token Cron Setup ==="

if crontab -l 2>/dev/null | grep -q "token_cron.py"; then
    echo "Token cron already exists. Skipping."
    crontab -l | grep "token_cron"
else
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "✅ Token cron installed:"
    echo "   $CRON_CMD"
    echo "   Runs: Mon-Fri at 07:00 IST (01:30 UTC)"
fi

echo ""
echo "=== Log Rotation Cron Setup ==="

# Run log rotation every Sunday at 02:00 IST (20:30 UTC Saturday)
LOG_CRON_CMD="30 20 * * 6 cd $TRADEOS_DIR && $VENV_PYTHON scripts/log_rotation.py >> logs/rotation.log 2>&1"

if crontab -l 2>/dev/null | grep -q "log_rotation.py"; then
    echo "Log rotation cron already exists. Skipping."
    crontab -l | grep "log_rotation"
else
    (crontab -l 2>/dev/null; echo "$LOG_CRON_CMD") | crontab -
    echo "✅ Log rotation cron installed:"
    echo "   $LOG_CRON_CMD"
    echo "   Runs: Every Sunday at 02:00 IST (20:30 UTC Saturday)"
fi

echo ""
echo "Current crontab:"
crontab -l
