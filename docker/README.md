# TradeOS — Docker Infrastructure

## Services

- **timescaledb**: TimescaleDB (PostgreSQL 15) — stores ticks, candles, signals, trades, system events

## Commands

```bash
# From repo root:
bash scripts/db_start.sh      # start DB (waits for ready)
bash scripts/db_stop.sh       # stop DB (data preserved)
bash scripts/db_migrate.sh    # apply schema.sql (idempotent)

# Direct compose commands:
docker compose -f docker/docker-compose.yml logs -f
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml down
```

## Volume

Named volume: `tradeos_db`

Declared as `external: true` in docker-compose.yml — Docker Compose does not
own or auto-delete this volume. Data persists across container restarts,
rebuilds, and compose project renames.

```bash
# Backup:
docker exec tradeos-db pg_dump -U tradeos tradeos > backup.sql

# Restore (full schema + data):
docker exec -i tradeos-db psql -U tradeos tradeos < backup.sql
```

## Port Binding

| Environment | Binding | Reason |
|-------------|---------|--------|
| Mac (Docker Desktop) | `0.0.0.0:5432` | Docker Desktop requirement |
| VPS (Linux Docker CE) | `127.0.0.1:5432` | Localhost-only for security |

Override on VPS via `docker-compose.override.yml` (gitignored):
```yaml
services:
  timescaledb:
    ports:
      - "127.0.0.1:5432:5432"
```

## Future Services (uncomment in docker-compose.yml when ready)

| Service | Phase | Description |
|---------|-------|-------------|
| `observer_api` | V2.0 | FastAPI backend for Observer dashboard |
| `observer_web` | V2.0 | Next.js frontend for Observer dashboard |
| `redis` | V2.0 | Cache layer for API |
| `nginx` | V2.0 | Reverse proxy |
