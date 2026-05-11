#!/bin/sh
# tubemail (hub) container entrypoint.
#
# Defaults to production: uvicorn without --reload, no source bind-mounts
# expected. Dev mode is opted in via the MCP_RELOAD=1 env var, set by
# docker-compose.override.yml. The override file also bind-mounts host
# source so saving a file restarts the server in place.
#
# TLS policy: HTTP is the right answer for localhost — loopback traffic
# never leaves the host, and a self-signed cert would only add browser
# warnings. The auto-detect below upgrades to HTTPS only when a real
# cert has been placed at /data/server.{crt,key}, which is the
# production path (Let's Encrypt, internal CA, or copied from a
# reverse-proxy terminator). Do not generate self-signed certs here.

# Fix /data ownership — named volumes may have root-owned subdirs from old bind mounts
chown -R tubemail:tubemail /data

: "${MCP_PORT:?MCP_PORT is required}"

UVICORN_ARGS="tubemail_hub.server:create_app --factory --host 0.0.0.0 --port ${MCP_PORT}"

if [ "${MCP_RELOAD:-0}" = "1" ]; then
    echo "tubemail: dev mode (uvicorn --reload watching /app/src/tubemail_hub)"
    UVICORN_ARGS="${UVICORN_ARGS} --reload --reload-dir /app/src/tubemail_hub"
else
    echo "tubemail: production mode (no --reload)"
fi

if [ -f /data/server.crt ] && [ -f /data/server.key ]; then
    echo "tubemail: HTTPS (found /data/server.crt + /data/server.key — production cert path)"
    UVICORN_ARGS="${UVICORN_ARGS} --ssl-certfile /data/server.crt --ssl-keyfile /data/server.key"
else
    echo "tubemail: HTTP on localhost (no certs in /data/ — correct default for loopback)"
fi

exec su -s /bin/sh tubemail -c "uvicorn ${UVICORN_ARGS}"
