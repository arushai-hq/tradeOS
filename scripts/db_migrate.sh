#!/bin/bash
# Apply schema to running database
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
echo "Applying schema..."
PGPASSWORD=tradeos psql \
  -h localhost -U tradeos -d tradeos \
  -f "$ROOT_DIR/schema.sql"
echo "✅ Schema applied"
docker compose exec timescaledb psql -U tradeos -d tradeos -c "\dt"
