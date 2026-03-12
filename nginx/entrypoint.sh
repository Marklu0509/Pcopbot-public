#!/bin/sh
DOMAIN="${DOMAIN:?DOMAIN env var is required}"
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"

# Remove any default configs shipped with nginx:alpine
rm -f /etc/nginx/conf.d/*.conf

# Substitute ${DOMAIN} in the template config
# Only replace $DOMAIN, preserve nginx variables like $host $http_upgrade etc.
if [ -f "$CERT_DIR/fullchain.pem" ]; then
    echo "SSL cert found for $DOMAIN. Starting nginx with HTTPS."
    envsubst '$DOMAIN' < /etc/nginx/templates/ssl.conf > /etc/nginx/conf.d/default.conf
else
    echo "No SSL cert found for $DOMAIN. Starting nginx with HTTP only."
    echo "Run certbot, then recreate nginx to enable HTTPS."
    envsubst '$DOMAIN' < /etc/nginx/templates/http-only.conf > /etc/nginx/conf.d/default.conf
fi

exec nginx -g "daemon off;"
