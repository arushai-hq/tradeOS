# docker/ — Infrastructure

Docker Compose infrastructure: TimescaleDB, Nginx reverse proxy, Let's Encrypt SSL.

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-operations | VPS deployment, daily workflow |

## Key Info

- `docker-compose.yml` — TimescaleDB (port 5432) + Nginx (port 11443) + Certbot
- Nginx proxies `/callback` to token_server (port 7291) for Zerodha OAuth
- SSL certs via Let's Encrypt, auto-renewed by certbot container
- Volumes: `timescaledb_data`, nginx config, SSL certs

## Conventions

- Never modify docker config during market hours (09:15-15:30 IST)
- Use `tradeos docker up|down|ps|logs` for management
