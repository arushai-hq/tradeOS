#!/bin/bash
# Stop TradeOS database (data preserved in volume)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
echo "Stopping TradeOS database..."
docker compose -f "$ROOT_DIR/docker/docker-compose.yml" stop timescaledb
echo "✅ Database stopped (data preserved in volume)"
