#!/bin/sh
# Generate self-signed certificate if it doesn't exist
CERT_DIR="/etc/nginx/certs"
if [ ! -f "$CERT_DIR/selfsigned.crt" ]; then
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes -days 365 \
        -newkey rsa:2048 \
        -keyout "$CERT_DIR/selfsigned.key" \
        -out "$CERT_DIR/selfsigned.crt" \
        -subj "/CN=pcopbot/O=Pcopbot/C=US"
    echo "Self-signed certificate generated."
else
    echo "Certificate already exists, skipping generation."
fi

exec nginx -g "daemon off;"
