#!/bin/sh
set -e
# Fix volume ownership — setup.sh creates volumes as root; the openring
# user needs write access to /var/lib/openring (notifier state).
# Only run the recursive chown once (sentinel file prevents slow restarts).
# When running as non-root already (e.g. CI --user root override removed),
# skip chown/gosu and just exec the command directly.
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /var/lib/openring/.ownership-fixed-notifier ]; then
        chown -R openring:openring /var/lib/openring 2>/dev/null || echo "WARNING: chown failed on /var/lib/openring — check volume mounts" >&2
        chown -R openring:openring /data /config 2>/dev/null || echo "WARNING: chown failed on /data or /config — check volume mounts" >&2
        touch /var/lib/openring/.ownership-fixed-notifier 2>/dev/null || true
    else
        # On subsequent starts, just fix top-level dirs (fast)
        chown openring:openring /data /config /var/lib/openring 2>/dev/null || true
    fi
    exec gosu openring "$@"
fi
exec "$@"
