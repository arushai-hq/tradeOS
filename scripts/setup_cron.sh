#!/usr/bin/env bash
set -euo pipefail

TRADEOS_DIR="/opt/tradeOS"
VENV_PYTHON="$TRADEOS_DIR/.venv/bin/python"

# NOTE: VPS system clock runs IST (Asia/Kolkata). Cron times are IST directly.
# If VPS timezone changes, these cron entries must be recalculated.

# Token auth: Mon-Fri at 07:00 IST
CRON_CMD="0 7 * * 1-5 cd $TRADEOS_DIR && $VENV_PYTHON scripts/token_cron.py >> logs/token_cron.log 2>&1"

echo "=== TradeOS Token Cron Setup ==="

if crontab -l 2>/dev/null | grep -q "token_cron.py"; then
    echo "Token cron already exists. Removing old entry first..."
    crontab -l 2>/dev/null | grep -v "token_cron.py" | crontab -
fi

(crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
echo "✅ Token cron installed:"
echo "   $CRON_CMD"
echo "   Runs: Mon-Fri at 07:00 IST (VPS clock is IST)"

echo ""
echo "=== Log Rotation Cron Setup ==="

# Log rotation: Sunday at 02:00 IST
LOG_CRON_CMD="0 2 * * 0 cd $TRADEOS_DIR && $VENV_PYTHON scripts/log_rotation.py >> logs/rotation.log 2>&1"

if crontab -l 2>/dev/null | grep -q "log_rotation.py"; then
    echo "Log rotation cron already exists. Removing old entry first..."
    crontab -l 2>/dev/null | grep -v "log_rotation.py" | crontab -
fi

(crontab -l 2>/dev/null; echo "$LOG_CRON_CMD") | crontab -
echo "✅ Log rotation cron installed:"
echo "   $LOG_CRON_CMD"
echo "   Runs: Every Sunday at 02:00 IST (VPS clock is IST)"

echo ""
echo "Current crontab:"
crontab -l
