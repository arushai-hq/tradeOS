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
echo "Current crontab:"
crontab -l
