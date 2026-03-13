# TradeOS Database Migrations

SQL migration files applied in numeric order.

## How to run manually

```bash
# Via docker exec
docker exec -it tradeos-db psql -U tradeos -d tradeos -f /migrations/001_create_sessions_table.sql

# Via psql directly
psql -U tradeos -d tradeos -f migrations/001_create_sessions_table.sql
```

## Auto-create at startup

`main.py` checks for the `sessions` table at startup and creates it if missing.
No manual migration step is needed for new tables — the system is self-healing.
