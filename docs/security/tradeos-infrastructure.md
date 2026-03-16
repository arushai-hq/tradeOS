# TradeOS — Infrastructure Access Register

> OSD v1.9.0 Standard #27. Last updated: 2026-03-16.
> **Action required**: Founder to verify MFA status for each service and update this document.

## Access Points

| Service | Access Method | Account Holder | MFA Status | Notes |
|---------|--------------|----------------|------------|-------|
| GitHub (`arushai-hq/tradeOS`) | SSH key + HTTPS | Founder | **Verify** | Private repo. Single collaborator. |
| VPS (Rocky Linux 9.7) | SSH root (key-based) | Founder | N/A (key auth) | Hostinger VPS. Password auth disabled. |
| TimescaleDB | Docker container (port 5432) | Local only | N/A | Credentials in docker-compose.yml (tradeos/tradeos). Not exposed to internet. |
| Zerodha KiteConnect | API key + daily token | Founder | **Verify** (Zerodha account) | Token refreshed daily via token_server.py. |
| Telegram Bot | Bot token + chat ID | Founder | N/A (bot API) | Trading channel + HAWK channel. |
| Nginx + SSL | Docker container (port 11443) | Automated | N/A | Let's Encrypt cert via certbot. Auto-renewal. |
| OpenRouter API | API key in secrets.yaml | Founder | **Verify** | HAWK AI engine — 4 LLM providers. |

## Network Architecture

```
Internet → Nginx (port 11443, SSL) → token_server.py (port 7291, localhost only)
VPS → TimescaleDB (port 5432, Docker bridge, not exposed)
VPS → Zerodha WebSocket (outbound wss://)
VPS → Telegram API (outbound HTTPS)
VPS → OpenRouter API (outbound HTTPS, HAWK only)
```

## Security Controls

- **SSH**: Key-based authentication only. Password auth disabled on VPS.
- **Database**: Docker container with non-default port binding (`0.0.0.0:5432`). Credentials in docker-compose.yml (development credentials — acceptable for single-operator paper trading).
- **Secrets**: `config/secrets.yaml` gitignored. Template at `config/secrets.example.yaml`.
- **SSL**: Let's Encrypt certificate for OAuth callback endpoint. Auto-renewal via certbot.
- **Firewall**: VPS firewall rules managed by Hostinger. Only ports 22 (SSH), 80 (certbot), 11443 (Nginx) open.

## Rotation Schedule

| Credential | Rotation | Method |
|------------|----------|--------|
| Zerodha access token | Daily (automated) | token_cron.py + token_server.py |
| SSH keys | Manual (as needed) | `ssh-keygen` + VPS update |
| Let's Encrypt cert | 90 days (automated) | certbot renewal cron |
| Telegram bot token | Never (unless compromised) | BotFather regeneration |
| OpenRouter API key | Never (unless compromised) | OpenRouter dashboard |
