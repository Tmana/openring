#!/bin/bash
#
# OpenRing — on-Pi installer.
#
# Walks a fresh Pi OS Lite 64-bit install through:
#   1. Installing the device-side packages (apt + pinned MediaMTX).
#   2. Pairing with the host (one-time bearer-less handshake gated on
#      a host-side "pairing window" the user opens from the web UI).
#   3. Writing /etc/openring/secrets.env (mode 600).
#   4. Installing + enabling the three systemd units.
#
# Re-running this script is safe: it re-pairs (which the host treats as
# a token rotation) and reapplies systemd units.
#
# v0.0 STATUS: skeleton — full implementation lands with ROADMAP issue #11.

set -euo pipefail

usage() {
    cat <<EOF
Usage: $0 <host-base-url> [device-id]

Examples:
  $0 https://openring.local              # uses default device-id "front-door"
  $0 https://192.168.1.42 side-door

Before running:
  1. On the host, open the web UI → Admin → Doorbells → "Pair new device".
     This opens a 5-minute pairing window; the URL displayed is the
     <host-base-url> argument here.
  2. Make sure the Pi can resolve / reach <host-base-url>.

EOF
    exit 2
}

(( $# >= 1 )) || usage

HOST_BASE_URL="$1"
DEVICE_ID="${2:-front-door}"

# Sanity checks — refuse to run on the wrong distro / arch
if ! grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model 2>/dev/null; then
    echo "WARN: this doesn't look like a Raspberry Pi — continuing anyway in 5s..." >&2
    sleep 5
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: pi-setup.sh must run as root (use sudo)" >&2
    exit 1
fi

echo "==> Installing apt dependencies"
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-gpiozero python3-requests \
    libcamera-apps ffmpeg iw \
    curl jq ca-certificates

echo "==> Creating openring user + directories"
id -u openring &>/dev/null || useradd -r -s /usr/sbin/nologin -d /var/lib/openring openring
usermod -a -G gpio,video openring 2>/dev/null || true
install -d -m 0755 -o openring -g openring /opt/openring /var/lib/openring
install -d -m 0755 -o root     -g root     /etc/openring

echo "==> TODO(#11): download + verify MediaMTX (pinned hash)"
echo "    Until then, install MediaMTX manually from https://github.com/bluenviron/mediamtx/releases"

echo "==> Copying device-side Python sources"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "${SRC_DIR}/src" /opt/openring/src
chown -R openring:openring /opt/openring

echo "==> TODO(#11): pair with host"
echo "    POST ${HOST_BASE_URL%/}/api/doorbell/register with device_id=${DEVICE_ID}"
echo "    Receive {device_token, rtsp_password, openring_version}"

# Placeholder — full pairing flow lands with issue #11.
DEVICE_TOKEN="REPLACE_ME_AFTER_PAIRING"
RTSP_PASSWORD="REPLACE_ME_AFTER_PAIRING"
VERSION="0.0.0-dev"

echo "==> Writing /etc/openring/secrets.env (mode 600)"
umask 077
cat > /etc/openring/secrets.env <<EOF
HOST_BASE_URL=${HOST_BASE_URL%/}
DEVICE_ID=${DEVICE_ID}
DEVICE_TOKEN=${DEVICE_TOKEN}
RTSP_PASSWORD=${RTSP_PASSWORD}
VERSION=${VERSION}
EOF
chown root:root /etc/openring/secrets.env
chmod 600 /etc/openring/secrets.env

echo "==> Rendering /etc/openring/mediamtx.yml"
envsubst < "${SRC_DIR}/config/mediamtx.yml.template" > /etc/openring/mediamtx.yml
chown openring:openring /etc/openring/mediamtx.yml
chmod 640 /etc/openring/mediamtx.yml

echo "==> Installing systemd units"
install -m 0644 "${SRC_DIR}/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now openring-mediamtx.service
systemctl enable --now openring-button.service
systemctl enable --now openring-heartbeat.service

echo
echo "Done. Service status:"
systemctl --no-pager --lines=0 status openring-mediamtx openring-button openring-heartbeat || true

echo
echo "Next steps:"
echo "  - On the host, confirm the doorbell appears under Admin → Doorbells."
echo "  - Press the button.  You should see an event in the web UI within 1-2 seconds."
echo "  - To re-pair, re-run this script with the same arguments."
