#!/bin/sh
# If Let's Encrypt cert doesn't exist yet, create a temporary self-signed
# cert so Nginx can start (certbot needs Nginx running on port 80).
CERT_DIR="/etc/letsencrypt/live/bot.marklu.page"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -days 7 \
        -newkey rsa:2048 \
        -keyout "$CERT_DIR/privkey.pem" \
        -out "$CERT_DIR/fullchain.pem" \
        -subj "/CN=bot.marklu.page"
    echo "Temporary self-signed cert created. Run certbot to get a real one."
fi

exec nginx -g "daemon off;"
