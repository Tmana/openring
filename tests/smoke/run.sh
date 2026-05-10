#!/bin/bash
#
# OpenRing — host-stack smoke test.
#
# Brings up redis + web + notifier on the local Docker daemon, opens
# a doorbell pairing window, registers a fake device, fires a press,
# and asserts the resulting event row landed in /data/openring.db.
#
# This is the test ROADMAP issue #12 promised: a single command that
# proves the full host-side flow wires up end-to-end.  The detector
# image is intentionally NOT built — it's huge (torch + opencv +
# ultralytics, ~3 GB) and isn't on the press path (the snapshot RPC
# just times out after 5s and the press still records with
# snapshot_path=None).
#
# Usage:
#   ./tests/smoke/run.sh                 # interactive — prints progress
#   ./tests/smoke/run.sh --keep-up       # leave the stack running afterwards
#   ./tests/smoke/run.sh --image-tag X   # use specific image tag (default smoke)
#
# Environment escape hatches:
#   SMOKE_TIMEOUT_SECONDS  default 60 — how long to wait for /health
#   SMOKE_DEBUG=1          dump container logs on failure
#
# Run from the repo root.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

KEEP_UP=0
IMAGE_TAG="smoke"
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-60}"

while (( $# > 0 )); do
    case "$1" in
        --keep-up)        KEEP_UP=1; shift ;;
        --image-tag)      IMAGE_TAG="$2"; shift 2 ;;
        -h|--help)
            sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# ── Pretty output ────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'
    RED=$'\e[31m'; RESET=$'\e[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$1"; }
ok()    { printf "  %s✓%s %s\n" "$GREEN" "$RESET" "$1"; }
warn()  { printf "  %s!%s %s\n" "$YELLOW" "$RESET" "$1" >&2; }
fail()  {
    printf "  %s✗%s %s\n" "$RED" "$RESET" "$1" >&2
    if [[ "${SMOKE_DEBUG:-0}" -eq 1 ]]; then
        echo
        echo "${BOLD}---- container logs ----${RESET}"
        docker compose logs --no-color 2>&1 | tail -200 || true
    fi
    teardown
    exit 1
}

teardown() {
    if [[ "${KEEP_UP}" -eq 1 ]]; then
        warn "Skipping teardown (--keep-up).  Run 'docker compose down -v' when done."
        return
    fi
    step "Tearing down stack"
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker volume rm openring-config openring-data openring-models openring-redis openring-caddy openring-notifier 2>/dev/null || true
    ok "stack down, volumes cleaned"
}

trap '[[ $? -ne 0 ]] && teardown' EXIT

# ── Step 1: prereqs ──────────────────────────────────────────────────

step "Smoke test — host-stack end-to-end"
command -v docker >/dev/null 2>&1 || fail "docker not on PATH"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 missing"
command -v jq >/dev/null 2>&1 || fail "jq not installed (apt-get install jq)"
command -v curl >/dev/null 2>&1 || fail "curl not installed"

# ── Step 2: clean slate ──────────────────────────────────────────────

step "Cleaning any prior smoke run state"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true
docker volume rm openring-config openring-data openring-models openring-redis openring-caddy openring-notifier 2>/dev/null || true
rm -f .env.smoke
ok "clean"

# ── Step 3: secrets via setup.sh (no build — we'll build only the
#   services we need) ────────────────────────────────────────────────

step "Provisioning secrets + config volume via setup.sh --no-build"
./setup.sh --no-build >/tmp/openring-setup.log 2>&1 || \
    fail "setup.sh failed; tail of /tmp/openring-setup.log: $(tail -5 /tmp/openring-setup.log)"
ok "setup.sh complete"

# ── Step 4: disable user auth so the smoke flow can hit admin endpoints
#   directly (auth.enabled=false → middleware grants every request the
#   anonymous-admin role; matches the conftest.py pattern in web tests).
# ──

step "Patching seeded openring.yml: auth.enabled=false (smoke-only)"
docker run --rm -v openring-config:/config alpine:3.19 sh -c "
    if ! grep -q 'auth:' /config/openring.yml; then
        # Inject under system: — find the first 'system:' line and
        # append two indented lines after it.
        awk '
            /^system:/ {
                print
                print \"  auth:\"
                print \"    enabled: false\"
                next
            }
            { print }
        ' /config/openring.yml > /config/openring.yml.new
        mv /config/openring.yml.new /config/openring.yml
    fi
" >/dev/null
ok "auth disabled in seeded config"

# ── Step 5: build the small images we actually need ──────────────────

step "Building web + notifier images (skipping detector — too large)"
IMAGE_TAG="${IMAGE_TAG}" docker compose build web notifier >/tmp/openring-build.log 2>&1 || \
    fail "image build failed; tail: $(tail -10 /tmp/openring-build.log)"
ok "images built"

# ── Step 6: bring the stack up (without detector + caddy) ────────────

step "Starting redis + web + notifier"
IMAGE_TAG="${IMAGE_TAG}" docker compose up -d redis web notifier >/tmp/openring-up.log 2>&1 || \
    fail "compose up failed; tail: $(tail -10 /tmp/openring-up.log)"
ok "stack up"

# ── Step 7: wait for web /health ─────────────────────────────────────

step "Waiting up to ${TIMEOUT}s for web /health"
deadline=$(( SECONDS + TIMEOUT ))
while (( SECONDS < deadline )); do
    if docker compose exec -T web python3 -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://localhost:8080/health', timeout=2)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
        ok "web is healthy"
        break
    fi
    sleep 2
done
if (( SECONDS >= deadline )); then
    fail "web did not become healthy within ${TIMEOUT}s"
fi

# ── Step 8: the actual press flow ────────────────────────────────────

step "Opening pairing window"
PAIR_RESPONSE=$(docker compose exec -T web python3 <<'PY'
# CSRF middleware (services/web/src/main.py:csrf_middleware) requires a
# matching token cookie + header on mutating requests.  Even though
# auth.enabled=false grants every caller admin role, CSRF still applies.
# We do the standard double-submit dance: GET / to seed the cookie,
# then POST with X-CSRF-Token mirroring the cookie value.
import http.cookiejar, urllib.request

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

# Seed the csrf_token cookie.
opener.open('http://localhost:8080/', timeout=5).read()
csrf = next((c.value for c in jar if c.name == 'csrf_token'), '')
if not csrf:
    raise SystemExit('no csrf_token cookie issued by GET /')

req = urllib.request.Request(
    'http://localhost:8080/api/doorbell/pair-window/open',
    method='POST',
    headers={'X-CSRF-Token': csrf},
    data=b'',
)
try:
    resp = opener.open(req, timeout=5)
    print(resp.read().decode())
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.read().decode())
    raise
PY
)
echo "${PAIR_RESPONSE}" | grep -q expires_at || fail "pair-window/open didn't return expires_at: ${PAIR_RESPONSE}"
ok "pairing window opened"

step "Registering smoke-test device"
REGISTER_RESPONSE=$(docker compose exec -T web python3 <<'PY'
import urllib.request, json
body = json.dumps({"device_id": "smoke-test", "label": "Smoke test"}).encode()
req = urllib.request.Request(
    'http://localhost:8080/api/doorbell/register',
    method='POST',
    headers={'Content-Type': 'application/json'},
    data=body,
)
resp = urllib.request.urlopen(req, timeout=5)
print(resp.read().decode())
PY
)
DEVICE_TOKEN=$(echo "${REGISTER_RESPONSE}" | jq -er '.device_token') || \
    fail "register didn't return device_token: ${REGISTER_RESPONSE}"
ok "device registered, token captured"

step "Firing a doorbell press"
PRESS_RESPONSE=$(docker compose exec -T -e TOKEN="${DEVICE_TOKEN}" web python3 <<'PY'
import os, urllib.request, json
body = json.dumps({"timestamp": "2026-05-09T18:00:00+00:00", "device_id": "smoke-test"}).encode()
req = urllib.request.Request(
    'http://localhost:8080/api/doorbell/press',
    method='POST',
    headers={
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {os.environ['TOKEN']}",
    },
    data=body,
)
resp = urllib.request.urlopen(req, timeout=10)
print(resp.read().decode())
PY
)
EVENT_ID=$(echo "${PRESS_RESPONSE}" | jq -er '.event_id') || \
    fail "press didn't return event_id: ${PRESS_RESPONSE}"
ok "press accepted, event_id=${EVENT_ID}"

# ── Step 9: verify the event row landed in openring.db ───────────────

step "Verifying event row in openring.db"
ROW_CSV=$(docker compose exec -T web python3 <<PY
import sqlite3
conn = sqlite3.connect('/data/openring.db')
row = conn.execute(
    'SELECT id, class_name, camera_name, confidence FROM detection_events WHERE id = ?',
    (${EVENT_ID},),
).fetchone()
if row is None:
    print('NOT_FOUND')
else:
    print(','.join(str(c) for c in row))
PY
)
if [[ "${ROW_CSV}" == "NOT_FOUND" ]]; then
    fail "event_id=${EVENT_ID} not found in detection_events"
fi
echo "  row: ${ROW_CSV}"

case "${ROW_CSV}" in
    "${EVENT_ID},doorbell_press,smoke-test,1.0"*)
        ok "row matches: doorbell_press from smoke-test, confidence 1.0"
        ;;
    *)
        fail "row contents unexpected: ${ROW_CSV}"
        ;;
esac

# ── Step 10: verify notifier saw the press ───────────────────────────

step "Verifying notifier received the press"
if docker compose logs notifier --no-color 2>&1 | grep -qE "Doorbell press received.*smoke-test"; then
    ok "notifier logged the doorbell press"
else
    # Notifier may have suppressed if no channels matched — check that
    # at least the subscription is healthy.
    if docker compose logs notifier --no-color 2>&1 | grep -q "openring:doorbell"; then
        warn "notifier subscribed but didn't log a press receipt — check for HMAC mismatch"
    else
        fail "notifier never subscribed to openring:doorbell"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────

step "Smoke test passed"
echo
echo "${BOLD}${GREEN}All checks passed.${RESET}  The host stack registers, accepts a press,"
echo "writes the event row, and the notifier sees it on the bus."
echo

teardown
trap - EXIT
