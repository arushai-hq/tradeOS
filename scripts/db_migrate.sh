#!/bin/bash
# Apply schema using docker exec — bypasses Mac localhost/IPv6 issues
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
echo "Applying schema..."
docker exec -i tradeos-db psql -U tradeos -d tradeos < "$ROOT_DIR/schema.sql"
echo "✅ Schema applied"
docker exec tradeos-db psql -U tradeos -d tradeos -c "\dt"
