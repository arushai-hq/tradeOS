#!/bin/bash
# Start TradeOS database
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
echo "Starting TradeOS database..."
docker compose -f "$ROOT_DIR/docker/docker-compose.yml" up -d timescaledb
echo "Waiting for database to be ready..."
docker compose -f "$ROOT_DIR/docker/docker-compose.yml" exec timescaledb pg_isready -U tradeos
echo "✅ Database ready"
