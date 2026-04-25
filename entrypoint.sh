#!/bin/sh
# Fix /data ownership — named volumes may have root-owned subdirs from old bind mounts
chown -R tubemail:tubemail /data

: "${MCP_PORT:?MCP_PORT is required}"

# HTTPS auto-detect: if certs exist in the data volume, serve TLS; otherwise plain HTTP.
if [ -f /data/server.crt ] && [ -f /data/server.key ]; then
    echo "tubemail: serving HTTPS (found /data/server.crt + /data/server.key)"
    exec su -s /bin/sh tubemail -c "uvicorn tubemail_hub.server:create_app --factory --host 0.0.0.0 --port ${MCP_PORT} --reload --reload-dir /app/src/tubemail_hub --ssl-certfile /data/server.crt --ssl-keyfile /data/server.key"
else
    echo "tubemail: serving HTTP (no TLS certs in /data/)"
    exec su -s /bin/sh tubemail -c "uvicorn tubemail_hub.server:create_app --factory --host 0.0.0.0 --port ${MCP_PORT} --reload --reload-dir /app/src/tubemail_hub"
fi
