#!/bin/sh
CERT_DIR="/etc/letsencrypt/live/bot.marklu.page"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    echo "No SSL cert found. Starting nginx with HTTP only."
    echo "Run certbot, then restart nginx to enable HTTPS."
    cp /etc/nginx/conf.d/http-only.conf /etc/nginx/conf.d/default.conf
fi

exec nginx -g "daemon off;"
