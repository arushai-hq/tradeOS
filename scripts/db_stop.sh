#!/bin/bash
echo "Stopping TradeOS database..."
docker compose stop timescaledb
echo "✅ Database stopped (data preserved in volume)"
