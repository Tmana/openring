#!/bin/bash
#
# OpenRing — host-side first-run setup.
#
# Generates the host's `.env` with strong random secrets, seeds the
# `openring-config` Docker volume with `openring.yml`, and prints the
# follow-up steps (compose up, bootstrap-token retrieval, doorbell
# pairing).
#
# Idempotent: re-running detects existing state and skips work that's
# already done.  Use `--regenerate-secrets` to rotate REDIS_PASSWORD and
# DETECTION_HMAC_KEY, which obviously requires re-pairing every doorbell.

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
EXAMPLE_CFG="${REPO_ROOT}/config/openring.example.yml"
CONFIG_VOLUME="openring-config"
TARGET_CFG_PATH="/config/openring.yml"

# Pin a small Alpine image used for the one-shot config-volume seeder.
SEEDER_IMAGE="alpine:3.19"

# ── Pretty output ────────────────────────────────────────────────────

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

Options:
  --regenerate-secrets   Overwrite an existing .env with fresh secrets.
                         Requires re-pairing every doorbell afterwards
                         (the new HMAC key invalidates existing tokens
                         on the bus, and changing REDIS_PASSWORD breaks
                         the running stack until restart).
  --no-build             Skip 'docker compose build' even on first run.
                         Useful when you've already built images or
                         intend to pull from GHCR (set IMAGE_TAG yourself).
  --dry-run              Print every step without changing anything.
  -h, --help             Show this help.

What this script does:
  1. Verifies docker + docker compose v2 are available.
  2. On first run, generates a .env with REDIS_PASSWORD and
     DETECTION_HMAC_KEY (32-byte random secrets).  Skips on subsequent
     runs unless --regenerate-secrets is passed.
  3. Seeds the openring-config Docker volume with the example config
     iff the volume is empty (so re-running never clobbers your
     existing openring.yml).
  4. Optionally builds the local images with 'docker compose build'.
  5. Prints next steps.

The web service generates a one-time bootstrap token on its first
start and logs it to stdout.  Watch 'docker compose logs web' after
'docker compose up -d' to grab it, then browse to
http://<host>/setup?token=<token> to create the first admin user.
EOF
    exit "${1:-2}"
}

REGENERATE_SECRETS=0
NO_BUILD=0
DRY_RUN=0

while (( $# > 0 )); do
    case "$1" in
        --regenerate-secrets) REGENERATE_SECRETS=1; shift ;;
        --no-build)           NO_BUILD=1; shift ;;
        --dry-run)            DRY_RUN=1; shift ;;
        -h|--help)            usage 0 ;;
        *)                    echo "Unknown option: $1" >&2; usage 2 ;;
    esac
done

cd "${REPO_ROOT}"

# ── Step 1: prerequisites ────────────────────────────────────────────

step "Checking prerequisites"

command -v docker >/dev/null 2>&1 || fail "docker not on PATH — install Docker Engine or Docker Desktop first"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin missing — need v2 (the 'docker compose' command, not 'docker-compose')"
ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"
ok "docker compose $(docker compose version --short 2>/dev/null || echo '?')"

if [[ ! -f "${EXAMPLE_CFG}" ]]; then
    fail "${EXAMPLE_CFG} missing — are you running setup.sh from the repo root?"
fi

# ── Step 2: secrets in .env ──────────────────────────────────────────

step "Provisioning .env"

gen_redis_password() {
    # 32 bytes of urlsafe randomness; printable, no shell-special chars.
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' 2>/dev/null \
        || openssl rand -base64 32 | tr -d '=+/' | head -c 43
}

gen_hmac_key() {
    # shared/event_signing.py:load_key_from_env expects base64-encoded
    # 32+ bytes.  32 bytes raw → 44 chars base64 (with padding).
    python3 -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())' 2>/dev/null \
        || openssl rand -base64 32
}

write_env() {
    local redis_pw hmac_key
    redis_pw="$(gen_redis_password)"
    hmac_key="$(gen_hmac_key)"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "would write ${ENV_FILE} with newly-generated REDIS_PASSWORD + DETECTION_HMAC_KEY"
        return
    fi
    umask 077
    cat > "${ENV_FILE}" <<EOF
# Generated by setup.sh on $(date -u +%FT%TZ).  chmod 600.
#
# OpenRing host-stack secrets.  Keep this file out of version control.
# Anyone with read access to .env can sign events on the internal Redis
# bus, so guard it the way you'd guard /etc/shadow.

REDIS_PASSWORD=${redis_pw}
DETECTION_HMAC_KEY=${hmac_key}

# Optional — uncomment + set if you want to pull pre-built images from
# GHCR instead of 'docker compose build'.
# GHCR_OWNER=tmana
# IMAGE_TAG=v0.1.0

# Optional — bind ports.  Defaults: 80 / 443.
# HTTP_PORT=8080
# HTTPS_PORT=8443
EOF
    chmod 0600 "${ENV_FILE}"
}

if [[ -f "${ENV_FILE}" ]]; then
    if [[ "$REGENERATE_SECRETS" -eq 1 ]]; then
        warn "Overwriting existing .env (--regenerate-secrets) — existing doorbell tokens will need re-pairing"
        if [[ "$DRY_RUN" -eq 0 ]]; then
            cp "${ENV_FILE}" "${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
        fi
        write_env
        [[ "$DRY_RUN" -eq 0 ]] && ok "wrote fresh .env (previous backed up as .env.bak.*)"
    else
        ok "${ENV_FILE} already exists — skipping (pass --regenerate-secrets to rotate)"
    fi
else
    write_env
    [[ "$DRY_RUN" -eq 0 ]] && ok "wrote ${ENV_FILE} (chmod 600)"
fi

# ── Step 3: seed openring-config volume ──────────────────────────────

step "Seeding ${CONFIG_VOLUME} volume"

if [[ "$DRY_RUN" -eq 1 ]]; then
    info "would create the ${CONFIG_VOLUME} volume if missing and copy ${EXAMPLE_CFG} → ${TARGET_CFG_PATH}"
else
    docker volume inspect "${CONFIG_VOLUME}" >/dev/null 2>&1 || \
        docker volume create "${CONFIG_VOLUME}" >/dev/null

    # Inspect existing contents non-destructively.  If openring.yml is
    # already in the volume we leave the user's edits alone.  If it's
    # missing, we drop the example in.
    existing="$(
        docker run --rm \
            -v "${CONFIG_VOLUME}:/config:ro" \
            "${SEEDER_IMAGE}" \
            sh -c "[ -f ${TARGET_CFG_PATH} ] && echo present || true"
    )"
    if [[ "${existing}" == "present" ]]; then
        ok "${CONFIG_VOLUME}${TARGET_CFG_PATH} already present — leaving it alone"
    else
        # Mount the example into the seeder by file rather than the
        # whole config dir so we don't shadow anything else operators
        # may have placed there.
        docker run --rm \
            -v "${CONFIG_VOLUME}:/config" \
            -v "${EXAMPLE_CFG}:/src/openring.example.yml:ro" \
            "${SEEDER_IMAGE}" \
            sh -c "cp /src/openring.example.yml ${TARGET_CFG_PATH}
                   chmod 0640 ${TARGET_CFG_PATH}
                   mkdir -p /config/certs"
        ok "seeded ${CONFIG_VOLUME}${TARGET_CFG_PATH} from openring.example.yml"
        warn "edit it for your environment: cameras, notification channels, retention, etc."
        info "  docker run --rm -it -v ${CONFIG_VOLUME}:/config ${SEEDER_IMAGE} vi ${TARGET_CFG_PATH}"
        info "  (or use the web UI's config editor after first-run setup)"
    fi
fi

# ── Step 4: build images ─────────────────────────────────────────────

if [[ "${NO_BUILD}" -eq 1 ]]; then
    step "Skipping image build (--no-build)"
    info "make sure your .env sets GHCR_OWNER + IMAGE_TAG to a published image,"
    info "or run 'docker compose build' yourself before 'docker compose up'."
elif [[ "$DRY_RUN" -eq 1 ]]; then
    step "Would build local images via 'docker compose build'"
else
    step "Building local images (this may take several minutes the first time)"
    docker compose build
    ok "images built"
fi

# ── Step 5: print next steps ─────────────────────────────────────────

cat <<EOF

${BOLD}${GREEN}Setup complete.${RESET}

${BOLD}Next steps:${RESET}

  1. Bring the stack up:
       ${CYAN}docker compose up -d${RESET}

  2. Watch the web service logs for the one-time bootstrap token:
       ${CYAN}docker compose logs -f web | grep -A2 'First-run setup'${RESET}
     The token expires after 24h.  If you miss it, restart web while
     the auth.db has no users and a fresh one will be issued.

  3. Browse to the URL the log line points at — typically:
       ${CYAN}http://localhost/setup?token=<token>${RESET}
     and create the first admin account.

  4. Edit ${CYAN}openring.yml${RESET} (in the openring-config volume) for your
     cameras + notification channels.  The web UI's Config page is the
     easiest entry point once you've completed step 3.

  5. Pair a doorbell device:
       Web UI → Admin → Doorbells → "Pair new device" (5-min window)
       On the Pi:
         ${CYAN}sudo ./services/doorbell-firmware/pi-setup.sh \\
             --host-url http://<your-host>${RESET}

${DIM}To rotate secrets in the future, re-run with --regenerate-secrets.
This invalidates every paired doorbell's token; re-pair them after
the stack restarts.${RESET}
EOF
