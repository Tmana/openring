#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /var/lib/openring/.ownership-fixed-recognizer ]; then
        chown -R openring:openring /var/lib/openring 2>/dev/null || true
        chown openring:openring /data /config 2>/dev/null || true
        touch /var/lib/openring/.ownership-fixed-recognizer 2>/dev/null || true
    else
        chown openring:openring /data /config /var/lib/openring 2>/dev/null || true
    fi
    exec gosu openring "$@"
fi
exec "$@"
