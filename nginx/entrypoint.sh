#!/bin/sh
# If SSL certs don't exist, start without SSL first.
# After certbot runs, restart nginx to pick up the real certs.
CERT_DIR="/etc/letsencrypt/live/bot.marklu.page"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    echo "No SSL cert found. Starting nginx with HTTP only."
    echo "Run certbot, then restart nginx to enable HTTPS."
    # Use HTTP-only config (remove ssl server block if cert missing)
    sed -i '/listen 443/,/^}/d' /etc/nginx/conf.d/default.conf
fi

exec nginx -g "daemon off;"
