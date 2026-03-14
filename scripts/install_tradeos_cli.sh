#!/usr/bin/env bash
# install_tradeos_cli.sh — Install the tradeos CLI to /usr/local/bin
#
# Usage:
#   bash scripts/install_tradeos_cli.sh
#
# What it does:
#   1. Makes bin/tradeos executable
#   2. Creates symlink at /usr/local/bin/tradeos
#   3. Tests with `tradeos version`

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CLI_SOURCE="$PROJECT_DIR/bin/tradeos"
SYMLINK_TARGET="/usr/local/bin/tradeos"

echo "Installing tradeos CLI..."
echo "  Source:  $CLI_SOURCE"
echo "  Target:  $SYMLINK_TARGET"
echo ""

# Verify source exists
if [ ! -f "$CLI_SOURCE" ]; then
    echo "ERROR: bin/tradeos not found at $CLI_SOURCE"
    exit 1
fi

# Make executable
chmod +x "$CLI_SOURCE"
echo "Made bin/tradeos executable."

# Remove existing symlink if present
if [ -L "$SYMLINK_TARGET" ]; then
    echo "Removing existing symlink..."
    sudo rm "$SYMLINK_TARGET"
elif [ -f "$SYMLINK_TARGET" ]; then
    echo "WARNING: $SYMLINK_TARGET exists and is not a symlink — removing..."
    sudo rm "$SYMLINK_TARGET"
fi

# Create symlink
sudo ln -s "$CLI_SOURCE" "$SYMLINK_TARGET"
echo "Created symlink: $SYMLINK_TARGET -> $CLI_SOURCE"
echo ""

# Test
echo "Testing installation..."
tradeos version
echo ""
echo "Done. Run 'tradeos help' for available commands."
