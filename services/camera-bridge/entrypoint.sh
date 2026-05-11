#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    chown openring:openring /tmp/mediamtx 2>/dev/null || true
    chown openring:openring /data /config 2>/dev/null || true
    # If the operator wired in a video group via group_add for /dev/video*,
    # gosu will preserve it.  Otherwise the openring user can read whatever
    # the device's world-read bits allow.
    exec gosu openring "$@"
fi
exec "$@"
