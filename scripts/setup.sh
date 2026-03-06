#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$ROOT_DIR/.venv"

echo "================================================"
echo "  TradeOS — Environment Setup"
echo "  Root: $ROOT_DIR"
echo "================================================"

# Detect Python 3.11+
PYTHON=""
for cmd in python3.11 python3.12 python3; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c 'import sys; exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.11+ not found."
    echo "   Mac:   brew install python@3.11"
    echo "   Rocky: dnf install python3.11"
    exit 1
fi

echo "✅ Python: $($PYTHON --version) at $(which $PYTHON)"

# Create virtual environment
if [ -d "$VENV_DIR" ]; then
    echo "⚠️  .venv already exists at $VENV_DIR"
    read -p "   Recreate? (y/N): " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        rm -rf "$VENV_DIR"
        echo "   Removed existing .venv"
    else
        echo "   Keeping existing .venv"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "✅ .venv created"
fi

# Activate and install
source "$VENV_DIR/bin/activate"
echo "✅ Activated: $VIRTUAL_ENV"

# Upgrade pip silently
pip install --upgrade pip --quiet

echo "Installing dependencies from requirements.txt..."
pip install -r "$ROOT_DIR/requirements.txt"

# Verify critical imports
echo ""
echo "Verifying critical imports..."
python -c "
from kiteconnect import KiteConnect, KiteTicker
import asyncpg
import structlog
import pandas as pd
import yaml
print('✅ All critical imports verified')
"

# Create activation helper
cat > "$ROOT_DIR/activate.sh" << 'ACTIVATE'
#!/bin/bash
# Source this file to activate TradeOS venv
# Usage: source activate.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate"
echo "✅ TradeOS venv activated: $VIRTUAL_ENV"
ACTIVATE
chmod +x "$ROOT_DIR/activate.sh"

echo ""
echo "================================================"
echo "  Setup complete."
echo ""
echo "  To activate venv:"
echo "    source activate.sh          (from repo root)"
echo "    source .venv/bin/activate   (directly)"
echo ""
echo "  To run TradeOS:"
echo "    source activate.sh"
echo "    python main.py"
echo ""
echo "  To deactivate:"
echo "    deactivate"
echo "================================================"
