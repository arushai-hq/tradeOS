# scripts/ — Automation Scripts

Token management, cron setup, SSL setup, log rotation.

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-operations | VPS deployment, daily workflow |

## Key Scripts

| Script | Purpose |
|--------|---------|
| `token_server.py` | HTTP callback server for Zerodha token capture |
| `token_cron.py` | Daily cron orchestrator with 4-stage Telegram escalation |
| `log_rotation.py` | Compress >30d logs, delete >90d archives |
| `refresh_token.py` | Manual token refresh utility |

## Conventions

- Scripts are wrapped by `tradeos` CLI — never called directly in production
- Use `tradeos auth` (not `python scripts/token_cron.py`)
- Use `tradeos cron install` to set up cron entries
