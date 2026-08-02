#!/bin/sh
if [ -n "$EXT_KEY" ] && [ -f /opt/ext-dist.enc ]; then
    python3 /opt/decrypt_dist.py /opt/ext-dist.enc /usr/share/nginx/html "$EXT_KEY" 2>/dev/null
fi
exec nginx -g 'daemon off;'
