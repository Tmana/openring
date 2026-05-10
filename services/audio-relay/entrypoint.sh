#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    chown openring:openring /data /config 2>/dev/null || true
    exec gosu openring "$@"
fi
exec "$@"
