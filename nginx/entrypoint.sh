#!/bin/sh
CERT_DIR="/etc/letsencrypt/live/${DOMAIN:-YOUR_DOMAIN}"

# Remove any default configs shipped with nginx:alpine
rm -f /etc/nginx/conf.d/*.conf

if [ -f "$CERT_DIR/fullchain.pem" ]; then
    echo "SSL cert found. Starting nginx with HTTPS."
    cp /etc/nginx/templates/ssl.conf /etc/nginx/conf.d/default.conf
else
    echo "No SSL cert found. Starting nginx with HTTP only."
    echo "Run certbot, then restart nginx to enable HTTPS."
    cp /etc/nginx/templates/http-only.conf /etc/nginx/conf.d/default.conf
fi

exec nginx -g "daemon off;"
