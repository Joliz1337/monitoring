#!/bin/sh
# Frontend entrypoint: decrypt EXT build if EXT_KEY is present
if [ -n "$EXT_KEY" ] && [ -f /opt/ext-dist.enc ]; then
    python3 /opt/decrypt_dist.py /opt/ext-dist.enc /usr/share/nginx/html "$EXT_KEY" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "[entrypoint] EXT modules decrypted"
    else
        echo "[entrypoint] EXT decryption failed, using base build" >&2
    fi
fi
exec nginx -g 'daemon off;'
