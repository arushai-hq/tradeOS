# TradeOS — Rollback Procedure

> OSD v1.9.0 Standard #18. Last updated: 2026-03-16.

## Prerequisites

- SSH access to VPS
- Git access to `arushai-hq/tradeOS`
- Familiarity with `tradeos` CLI

## 1. Emergency Stop

```bash
# Stop TradeOS immediately (graceful shutdown)
tradeos stop

# If tradeos stop fails, kill the tmux session directly
tmux kill-session -t tradeos

# Verify no Python processes remain
ps aux | grep main.py
```

**Important**: Do NOT stop during market hours (09:15–15:30 IST) unless there is a critical bug affecting live positions. If positions are open, let the hard exit at 15:00 close them first.

## 2. Rollback to Previous Git Version

```bash
# Check current version
git log --oneline -5

# Option A: Revert last commit (creates new commit, preserves history)
git revert HEAD

# Option B: Rollback to specific tag
git checkout v0.5.0

# Option C: Rollback to specific commit
git checkout <commit-hash>

# After rollback, restart
tradeos start
```

**Never use `git reset --hard` on production** — it destroys history. Always use `git revert` for traceability.

## 3. Database Restore

### Create Backup (preventive)

```bash
# Backup full database
docker exec tradeos-db pg_dump -U tradeos tradeos > backup_$(date +%Y%m%d).sql

# Backup specific table
docker exec tradeos-db pg_dump -U tradeos -t signals tradeos > signals_backup.sql
```

### Restore from Backup

```bash
# Stop TradeOS first
tradeos stop

# Restore full database (destructive — drops existing data)
docker exec -i tradeos-db psql -U tradeos tradeos < backup_20260316.sql

# Restore specific table
docker exec -i tradeos-db psql -U tradeos tradeos < signals_backup.sql

# Restart
tradeos start
```

### Reset Database (nuclear option)

```bash
# Stop everything
tradeos stop
docker compose -f docker/docker-compose.yml down

# Remove volume (DESTROYS ALL DATA)
docker volume rm tradeos_db

# Recreate
docker volume create tradeos_db
docker compose -f docker/docker-compose.yml up -d

# Tables are auto-created on next startup
tradeos start
```

## 4. Docker Service Recovery

```bash
# Check service health
docker compose -f docker/docker-compose.yml ps

# Restart specific service
docker compose -f docker/docker-compose.yml restart timescaledb
docker compose -f docker/docker-compose.yml restart nginx

# Full restart
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml up -d

# Check logs
docker logs tradeos-db --tail 50
docker logs tradeos-nginx --tail 50
```

## 5. Token Recovery

If the daily token flow fails:

```bash
# Manual token server start
tradeos auth start

# Or start token cron manually
tradeos auth cron

# Check token status
tradeos auth status
```

## 6. Post-Rollback Verification

After any rollback:

1. `tradeos preflight` — run pre-market health check
2. `tradeos test -x -q` — verify all tests pass
3. `tradeos status` — check system state
4. Check Telegram — verify bot is responding
5. Review logs: `tradeos logs tail`

## Version Tags

| Tag | Description | Date |
|-----|-------------|------|
| v0.5.0 | ASPS restructure + B15 fix + OSD compliance | 2026-03-16 |
