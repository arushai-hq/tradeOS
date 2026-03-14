#!/usr/bin/env bash
set -euo pipefail

DOMAIN="srv1332119.hstgr.cloud"
COMPOSE_DIR="/opt/tradeOS/docker"
EMAIL="${1:?Usage: ./scripts/setup_ssl.sh your-email@example.com}"

echo "=== TradeOS SSL Setup ==="
echo "Domain: $DOMAIN"
echo "Email: $EMAIL"
echo ""

# Step 1: Open firewall ports
echo "Step 1: Configuring firewall..."
firewall-cmd --permanent --add-service=http
firewall-cmd --permanent --add-service=https
firewall-cmd --reload
echo "✅ Firewall: HTTP + HTTPS open"

# Step 2: Start Nginx with HTTP-only config for cert verification
echo ""
echo "Step 2: Starting Nginx (HTTP only for cert verification)..."
cd "$COMPOSE_DIR"
cp nginx/conf.d/tradeos.conf nginx/conf.d/tradeos.conf.ssl_backup
cp nginx/conf.d/http_only.conf.bak nginx/conf.d/tradeos.conf
docker compose up -d nginx
sleep 3

# Verify Nginx is running
if ! docker compose ps nginx | grep -q "running"; then
    echo "❌ Nginx failed to start. Check: docker compose logs nginx"
    exit 1
fi
echo "✅ Nginx running (HTTP only)"

# Step 3: Get Let's Encrypt certificate
echo ""
echo "Step 3: Obtaining SSL certificate..."
docker compose run --rm certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email

if [ ! -f "certbot/conf/live/$DOMAIN/fullchain.pem" ]; then
    echo "❌ Certificate not found. Certbot may have failed."
    exit 1
fi
echo "✅ SSL certificate obtained"

# Step 4: Restore SSL Nginx config
echo ""
echo "Step 4: Enabling SSL config..."
cp nginx/conf.d/tradeos.conf.ssl_backup nginx/conf.d/tradeos.conf
rm nginx/conf.d/tradeos.conf.ssl_backup

# Step 5: Reload Nginx with SSL
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload
echo "✅ Nginx reloaded with SSL"

# Step 6: Verify HTTPS
echo ""
echo "Step 5: Verifying HTTPS..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "000" ] || [ "$HTTP_CODE" = "444" ]; then
    echo "✅ HTTPS working (returned $HTTP_CODE — expected, / is blocked)"
else
    echo "⚠️  HTTPS returned $HTTP_CODE — check config"
fi

# Step 7: Setup certbot auto-renewal cron
echo ""
echo "Step 6: Setting up auto-renewal cron..."
CRON_CMD="0 3,15 * * * cd $COMPOSE_DIR && docker compose run --rm certbot renew --quiet && docker compose exec nginx nginx -s reload"
if crontab -l 2>/dev/null | grep -q "certbot renew"; then
    echo "Certbot renewal cron already exists. Skipping."
else
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "✅ Certbot auto-renewal cron added (03:00 + 15:00 daily)"
fi

echo ""
echo "=========================================="
echo "  SSL Setup Complete"
echo "  Domain: https://$DOMAIN"
echo "  Next: Update Zerodha redirect URL to:"
echo "  https://$DOMAIN/callback"
echo "=========================================="
