#!/bin/bash
# Start TradeOS database
set -e
echo "Starting TradeOS database..."
docker compose up -d timescaledb
echo "Waiting for database to be ready..."
docker compose exec timescaledb pg_isready -U tradeos
echo "✅ Database ready"
