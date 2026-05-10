#!/bin/bash
#
# OpenRing — on-Pi installer.  Walks a fresh Pi OS Lite 64-bit install
# through:
#
#   1. Confirms we're on a Raspberry Pi running a supported distro.
#   2. apt-get installs the Python + capture toolchain.
#   3. Downloads + verifies a pinned MediaMTX release (SHA256-checked
#      against `mediamtx.sha256` in this directory).
#   4. Generates an RTSP password and a device label.
#   5. POSTs `/api/doorbell/register` to the OpenRing host, capturing
#      the returned device token (must be done during a pairing window
#      that the operator opens from the web UI).
#   6. Writes /etc/openring/secrets.env (chmod 600).
#   7. Renders /etc/openring/mediamtx.yml from the template.
#   8. Installs three systemd units and `systemctl enable --now`s them.
#   9. Prints the YAML snippet the operator pastes into openring.yml on
#      the host so the detector can consume the new RTSP stream.
#
# Idempotent: every step is safe to re-run.  Re-running the registration
# rotates the device token (host-side: same device_id = INSERT-or-UPDATE
# in device_tokens), and ownership / file writes are idempotent.

set -euo pipefail

# ── Defaults / pins ──────────────────────────────────────────────────

# Pinned MediaMTX version.  Bump in lockstep with `mediamtx.sha256` in
# this directory.  Hash verification is mandatory unless --skip-hash
# is passed (only acceptable on a development workstation; production
# installs MUST verify).
MEDIAMTX_VERSION_DEFAULT="1.18.1"
MEDIAMTX_RELEASE_BASE="https://github.com/bluenviron/mediamtx/releases/download"

OPENRING_USER="openring"
OPENRING_GROUP="openring"
INSTALL_DIR="/opt/openring"
STATE_DIR="/var/lib/openring"
CONFIG_DIR="/etc/openring"
SECRETS_FILE="${CONFIG_DIR}/secrets.env"

VERSION_TAG="0.1.0-dev"

# ── Pretty output helpers ────────────────────────────────────────────

if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'
    RED=$'\e[31m'; CYAN=$'\e[36m'; RESET=$'\e[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

step()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$1"; }
ok()    { printf "  %s✓%s %s\n" "$GREEN" "$RESET" "$1"; }
warn()  { printf "  %s!%s %s\n" "$YELLOW" "$RESET" "$1" >&2; }
fail()  { printf "  %s✗%s %s\n" "$RED" "$RESET" "$1" >&2; exit 1; }
info()  { printf "  %s%s%s\n" "$DIM" "$1" "$RESET"; }

# ── Argument parsing ─────────────────────────────────────────────────

usage() {
    cat <<EOF
${BOLD}Usage:${RESET} $0 [options]

Required (one way or another):
  --host-url URL        Base URL of the OpenRing host (e.g. https://openring.local)
  --device-id ID        Lowercase hostname-style id (default: front-door)

Common options:
  --label LABEL         Human-readable label shown in the web UI (default: same as device-id)
  --rtsp-password PASS  Force a specific RTSP password (default: auto-generated 32 chars)
  --mediamtx-version V  Pin a different MediaMTX version (default: ${MEDIAMTX_VERSION_DEFAULT})
  --skip-hash           Skip MediaMTX SHA256 verification (DEV ONLY — not for production)
  --insecure            Pass -k to curl (LAN-only, self-signed certs).  Disables TLS verification.
  --dry-run             Print steps without changing the system or making the network call

Workflow:
  1. On the host, open the web UI → Admin → Doorbells → "Pair new device".
     This opens a 5-minute pairing window.
  2. On the Pi, run this script with --host-url pointing at the host.
  3. Wait for "Pairing complete" — the script prints a YAML camera
     snippet for you to paste into the host's openring.yml.

Re-running rotates the device token but preserves the device's
identity in the host registry.

Examples:
  sudo $0 --host-url https://openring.local
  sudo $0 --host-url https://192.168.1.42 --device-id side-gate --label "Side gate"
  sudo $0 --host-url http://10.0.0.5:8080 --insecure --device-id front-door
EOF
    exit "${1:-2}"
}

HOST_BASE_URL=""
DEVICE_ID="front-door"
LABEL=""
RTSP_PASSWORD=""
MEDIAMTX_VERSION="${MEDIAMTX_VERSION_DEFAULT}"
SKIP_HASH=0
INSECURE=0
DRY_RUN=0

while (( $# > 0 )); do
    case "$1" in
        --host-url)         HOST_BASE_URL="$2"; shift 2 ;;
        --device-id)        DEVICE_ID="$2"; shift 2 ;;
        --label)            LABEL="$2"; shift 2 ;;
        --rtsp-password)    RTSP_PASSWORD="$2"; shift 2 ;;
        --mediamtx-version) MEDIAMTX_VERSION="$2"; shift 2 ;;
        --skip-hash)        SKIP_HASH=1; shift ;;
        --insecure)         INSECURE=1; shift ;;
        --dry-run)          DRY_RUN=1; shift ;;
        -h|--help)          usage 0 ;;
        *)                  echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

[[ -n "$HOST_BASE_URL" ]] || { echo "${RED}--host-url is required${RESET}" >&2; usage 2; }
HOST_BASE_URL="${HOST_BASE_URL%/}"
LABEL="${LABEL:-$DEVICE_ID}"

# ── Validate device_id matches the host-side regex ───────────────────
# Mirrors `_DEVICE_ID_RE` in services/web/src/routes/doorbell.py:
#   ^[a-z0-9][a-z0-9-]{0,62}$
if ! [[ "$DEVICE_ID" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    fail "device-id '${DEVICE_ID}' is invalid — must be lowercase a-z, 0-9, dashes, max 63 chars, starting with a letter or digit"
fi

# ── Sanity: distro / privilege checks ────────────────────────────────

step "Sanity checks"

if [[ -r /sys/firmware/devicetree/base/model ]] && \
   grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model; then
    PI_MODEL=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
    ok "Hardware: ${PI_MODEL}"
else
    warn "This doesn't look like a Raspberry Pi — continuing, but you're on your own"
fi

case "$(uname -m)" in
    aarch64) ARCH="arm64" ;;
    armv7l) ARCH="armv7" ;;
    armv6l) ARCH="armv6" ;;
    x86_64) ARCH="amd64" ;;
    *) fail "Unsupported architecture: $(uname -m)" ;;
esac
ok "Architecture: $(uname -m) → mediamtx ${ARCH} build"

if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
    fail "pi-setup.sh must run as root (use sudo)"
fi

CURL_FLAGS=(-fsSL)
if [[ "$INSECURE" -eq 1 ]]; then
    CURL_FLAGS+=(-k)
    warn "TLS verification disabled (--insecure).  LAN deployments only."
fi

run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "would run: $*"
    else
        "$@"
    fi
}

# ── Step 1: apt-get dependencies ─────────────────────────────────────

step "Installing apt dependencies"
APT_PACKAGES=(
    python3 python3-gpiozero python3-requests python3-websockets
    libcamera-apps ffmpeg iw
    alsa-utils opus-tools                 # v0.3: arecord, aplay, opusenc, opusdec
    curl jq ca-certificates gettext-base
)
if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would: apt-get install ${APT_PACKAGES[*]}"
else
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -qq -y --no-install-recommends \
        "${APT_PACKAGES[@]}" >/dev/null
    ok "apt packages installed"
fi

# ── Step 2: openring user + directories ──────────────────────────────

step "Creating openring user + directories"
if [[ "$DRY_RUN" -eq 0 ]]; then
    if ! id -u "${OPENRING_USER}" &>/dev/null; then
        useradd -r -s /usr/sbin/nologin -d "${STATE_DIR}" "${OPENRING_USER}"
        ok "user ${OPENRING_USER} created"
    else
        ok "user ${OPENRING_USER} already exists"
    fi
    usermod -a -G gpio,video "${OPENRING_USER}" 2>/dev/null || true
    install -d -m 0755 -o "${OPENRING_USER}" -g "${OPENRING_GROUP}" \
        "${INSTALL_DIR}" "${STATE_DIR}"
    install -d -m 0755 -o root -g root "${CONFIG_DIR}"
    ok "directories ready: ${INSTALL_DIR}, ${STATE_DIR}, ${CONFIG_DIR}"
fi

# ── Step 3: MediaMTX download + hash verification ────────────────────

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
MEDIAMTX_TARBALL="mediamtx_v${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz"
MEDIAMTX_URL="${MEDIAMTX_RELEASE_BASE}/v${MEDIAMTX_VERSION}/${MEDIAMTX_TARBALL}"
MEDIAMTX_DEST="/usr/local/bin/mediamtx"

step "Installing MediaMTX v${MEDIAMTX_VERSION} (${ARCH})"
need_install=1
if [[ -x "${MEDIAMTX_DEST}" ]]; then
    if existing_version=$("${MEDIAMTX_DEST}" --version 2>/dev/null | head -1 | awk '{print $NF}'); then
        if [[ "${existing_version}" == "v${MEDIAMTX_VERSION}" || "${existing_version}" == "${MEDIAMTX_VERSION}" ]]; then
            ok "MediaMTX ${existing_version} already installed — skipping download"
            need_install=0
        else
            info "found MediaMTX ${existing_version}, replacing with v${MEDIAMTX_VERSION}"
        fi
    fi
fi

if [[ "${need_install}" -eq 1 ]]; then
    tmp_dir="$(mktemp -d -t openring-mediamtx.XXXXXX)"
    trap 'rm -rf "${tmp_dir}"' EXIT
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "would download: ${MEDIAMTX_URL}"
    else
        curl "${CURL_FLAGS[@]}" -o "${tmp_dir}/${MEDIAMTX_TARBALL}" "${MEDIAMTX_URL}"
        ok "downloaded ${MEDIAMTX_TARBALL}"

        if [[ "${SKIP_HASH}" -eq 0 ]]; then
            sha_file="${SRC_DIR}/mediamtx.sha256"
            [[ -f "${sha_file}" ]] || fail "missing ${sha_file} — required for hash verification (or pass --skip-hash for dev)"
            expected="$(grep -E "^[0-9a-f]+ +${MEDIAMTX_TARBALL}\$" "${sha_file}" | awk '{print $1}')"
            [[ -n "${expected}" ]] || fail "no SHA256 entry for ${MEDIAMTX_TARBALL} in ${sha_file} — pin this version's hash and re-run"
            actual="$(sha256sum "${tmp_dir}/${MEDIAMTX_TARBALL}" | awk '{print $1}')"
            if [[ "${expected}" != "${actual}" ]]; then
                fail "SHA256 mismatch for ${MEDIAMTX_TARBALL}:
    expected ${expected}
    actual   ${actual}"
            fi
            ok "SHA256 verified"
        else
            warn "SHA256 verification skipped — DEV ONLY"
        fi

        tar -C "${tmp_dir}" -xzf "${tmp_dir}/${MEDIAMTX_TARBALL}"
        install -m 0755 -o root -g root "${tmp_dir}/mediamtx" "${MEDIAMTX_DEST}"
        ok "installed ${MEDIAMTX_DEST}"
    fi
fi

# ── Step 4: device-side Python sources ───────────────────────────────

step "Copying device-side Python sources"
if [[ "$DRY_RUN" -eq 0 ]]; then
    install -d -m 0755 -o "${OPENRING_USER}" -g "${OPENRING_GROUP}" "${INSTALL_DIR}/src"
    install -m 0644 -o "${OPENRING_USER}" -g "${OPENRING_GROUP}" \
        "${SRC_DIR}/src/"*.py "${INSTALL_DIR}/src/"
    # v0.3: install the cross-service shared/ tree alongside the device
    # sources so audio_relay.py can import audio_frames.  The repo's
    # shared/ is two directories up from services/doorbell-firmware/.
    SHARED_SRC="$(cd "${SRC_DIR}/../../shared" 2>/dev/null && pwd || true)"
    if [[ -n "${SHARED_SRC}" && -d "${SHARED_SRC}" ]]; then
        install -d -m 0755 -o "${OPENRING_USER}" -g "${OPENRING_GROUP}" "${INSTALL_DIR}/shared"
        install -m 0644 -o "${OPENRING_USER}" -g "${OPENRING_GROUP}" \
            "${SHARED_SRC}/"*.py "${INSTALL_DIR}/shared/" 2>/dev/null || true
    fi
    ok "Python sources installed at ${INSTALL_DIR}/{src,shared}/"
fi

# ── Step 5: pair with the host ───────────────────────────────────────

step "Pairing with host: ${HOST_BASE_URL}"

if [[ -z "${RTSP_PASSWORD}" ]]; then
    RTSP_PASSWORD="$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom 2>/dev/null | head -c 32 || echo "")"
    [[ -n "${RTSP_PASSWORD}" ]] || fail "could not generate RTSP password — /dev/urandom unavailable?"
    info "auto-generated 32-char RTSP password"
fi

REGISTER_URL="${HOST_BASE_URL}/api/doorbell/register"
REGISTER_BODY=$(jq -n --arg id "${DEVICE_ID}" --arg label "${LABEL}" \
    '{device_id: $id, label: $label}')

if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would POST: ${REGISTER_URL}"
    info "with body: ${REGISTER_BODY}"
    DEVICE_TOKEN="DRYRUN_TOKEN"
else
    if ! REGISTER_RESPONSE=$(curl "${CURL_FLAGS[@]}" -X POST \
            -H 'Content-Type: application/json' \
            -d "${REGISTER_BODY}" \
            -w '\n%{http_code}\n' \
            "${REGISTER_URL}" 2>&1); then
        fail "could not reach ${REGISTER_URL} — is the host up + reachable?"
    fi
    REGISTER_HTTP=$(echo "${REGISTER_RESPONSE}" | tail -1)
    REGISTER_BODY_RESP=$(echo "${REGISTER_RESPONSE}" | sed '$d' | sed '$d')
    case "${REGISTER_HTTP}" in
        200)
            DEVICE_TOKEN=$(echo "${REGISTER_BODY_RESP}" | jq -er '.device_token')
            ok "paired successfully (device_id=${DEVICE_ID})"
            ;;
        403)
            fail "host rejected pairing — open the pairing window from the web UI first
    On the host: Admin → Doorbells → Pair new device (a 5-minute window)"
            ;;
        *)
            err_msg=$(echo "${REGISTER_BODY_RESP}" | jq -r '.error // .' 2>/dev/null || echo "${REGISTER_BODY_RESP}")
            fail "register returned HTTP ${REGISTER_HTTP}: ${err_msg}"
            ;;
    esac
fi

# ── Step 6: secrets.env ──────────────────────────────────────────────

step "Writing ${SECRETS_FILE}"
if [[ "$DRY_RUN" -eq 0 ]]; then
    umask 077
    cat > "${SECRETS_FILE}" <<EOF
# Generated by pi-setup.sh on $(date -u +%FT%TZ).  chmod 600.
# Re-running pi-setup.sh rotates DEVICE_TOKEN and may rotate RTSP_PASSWORD.
HOST_BASE_URL=${HOST_BASE_URL}
DEVICE_ID=${DEVICE_ID}
DEVICE_TOKEN=${DEVICE_TOKEN}
RTSP_PASSWORD=${RTSP_PASSWORD}
VERSION=${VERSION_TAG}
EOF
    chown root:"${OPENRING_GROUP}" "${SECRETS_FILE}"
    chmod 0640 "${SECRETS_FILE}"
    ok "${SECRETS_FILE} written (root:${OPENRING_GROUP} 0640)"
fi

# ── Step 7: mediamtx.yml ─────────────────────────────────────────────

step "Rendering ${CONFIG_DIR}/mediamtx.yml"
if [[ "$DRY_RUN" -eq 0 ]]; then
    RTSP_PASSWORD="${RTSP_PASSWORD}" envsubst '${RTSP_PASSWORD}' \
        < "${SRC_DIR}/config/mediamtx.yml.template" \
        > "${CONFIG_DIR}/mediamtx.yml"
    chown root:"${OPENRING_GROUP}" "${CONFIG_DIR}/mediamtx.yml"
    chmod 0640 "${CONFIG_DIR}/mediamtx.yml"
    ok "mediamtx.yml rendered"
fi

# ── Step 8: systemd units ────────────────────────────────────────────

step "Installing + enabling systemd units"
if [[ "$DRY_RUN" -eq 0 ]]; then
    install -m 0644 "${SRC_DIR}/systemd/"*.service /etc/systemd/system/
    systemctl daemon-reload
    for unit in openring-mediamtx openring-button openring-heartbeat openring-audio; do
        systemctl enable --now "${unit}.service" >/dev/null 2>&1 || true
        if systemctl is-active --quiet "${unit}.service"; then
            ok "${unit}.service active"
        else
            warn "${unit}.service did not start — check 'journalctl -u ${unit}'"
        fi
    done
fi

# ── Step 9: print openring.yml camera snippet ────────────────────────

PI_HOSTNAME=$(hostname -f 2>/dev/null || hostname)
RTSP_URL="rtsp://openring:${RTSP_PASSWORD}@${PI_HOSTNAME}:8554/door"

cat <<EOF

${BOLD}${GREEN}Pairing complete.${RESET}

Add this camera entry to ${CYAN}openring.yml${RESET} on the host (under ${CYAN}cameras:${RESET}):

  - name: ${DEVICE_ID}
    rtsp_url: "${RTSP_URL}"
    enabled: true
    resolution: 720
    notification_rules:
      - class_name: doorbell_press
        channels: [phone-ntfy]
      - class_name: person
        channels: [phone-ntfy]

Then ${CYAN}docker compose restart detector${RESET} on the host so it picks up the new camera.

Diagnostics:
  ssh into this Pi and run:
    journalctl -u openring-mediamtx -f
    journalctl -u openring-button -f
  press the doorbell button — you should see a press event in the host's web UI.

To re-pair (rotate the token), re-run this script with the same arguments
during a fresh pairing window.
EOF
