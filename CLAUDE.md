# OpenRing — guidance for AI collaborators

If you're an AI agent working on this repo, read this file first. It
captures what's intentional and what is not.

## What this is

A local-only smart doorbell. A thin Raspberry Pi at the door streams
RTSP and reports button presses to a fat host computer running a
Docker Compose stack that does inference, storage, and notification.
See [README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
and [docs/FIRMWARE.md](docs/FIRMWARE.md).

## Charter

OpenRing is **local-only software** for an end-user device. Two hard
constraints follow:

1. **No outbound traffic to anything we control, ever.** The only
   network calls OpenRing makes go to user-configured destinations
   (their ntfy server, their Discord webhook, their SMTP relay). Any
   PR that adds telemetry, "anonymous usage stats", auto-update
   pings, or a default-on cloud relay is wrong. If you find yourself
   adding `requests.post("https://api.openring.dev/...")` anywhere,
   stop.
2. **The host is the security boundary.** The doorbell device only
   talks to the host. The host re-publishes events onto its internal
   bus with HMAC. A compromised doorbell cannot directly inject
   signed events. Don't undo this — don't put Redis on the doorbell,
   don't have the doorbell speak to the user's notifier directly,
   don't add a "for performance" shortcut that bypasses the host.

## Tech stack (reference)

- **Host:** Docker Compose (web, detector, notifier, redis, caddy,
  log-streamer, backup), Python 3.11, FastAPI + Jinja, SQLite, Redis
  pub/sub, Caddy for TLS.
- **Device:** Raspberry Pi Zero 2 W, Pi OS Bookworm 64-bit, MediaMTX
  for RTSP, three systemd units, Python 3.11.
- **CI:** GitHub Actions, ruff + mypy + pytest per service, image
  builds with Trivy scanning.

## Design decisions — do not change without discussion

1. **Docker Compose** is the host deployment target. No Kubernetes.
2. **Single config file** (`config/openring.yml`) is the source of
   truth for the host stack. Do not fragment per-service.
3. **Redis pub/sub** is the inter-service bus on the host. No Kafka,
   no RabbitMQ.
4. **SQLite** is the database. No Postgres.
5. **Python 3.11** across all services.
6. **MediaMTX** is the device-side RTSP server. No bespoke protocol.
7. **Bearer-authenticated HTTP** is the device → host channel. The
   device does not talk to Redis directly.
8. **HMAC-signed Redis publishes** are required for any event that
   triggers user-visible action.
9. **Snapshots are files on disk**, served by the web service.
   Clips too. No blob store.
10. **Data directory is external to the repo.** `git pull` never
    touches user config, models, database, snapshots, or clips.
11. **Local-only.** See Charter §1 above. This is the load-bearing
    one.

## Development guidelines

- **Type hints on every function.** mypy in CI.
- **Pydantic models for data structures** (events, config sections).
- **Logging via Python `logging`**, structured args (not f-strings) so
  log scrapers can index. Respect `system.log_level` from config.
- **Error handling:** RTSP and Wi-Fi will drop. Reconnect with
  exponential backoff; never crash on a transient network error.
- **Testing:** pytest. Mock GPIO via `gpiozero`'s `MockFactory` so
  device tests don't need real hardware. Don't over-test for v0.1.
- **No over-engineering.** A doorbell is a doorbell.
- **Config-item UI parity:** any new config knob should be editable
  in the web UI, not just YAML.
- **Documentation keeps pace with code:** when you change behavior,
  update README, CONFIG_REFERENCE, ARCHITECTURE — whichever applies.

## Linting & type checking

These commands mirror CI exactly. Run before considering any change
done.

```bash
# Ruff — all services
ruff check services/detector/src services/web/src \
            services/notifier/src services/doorbell-firmware/src \
            services/clipper/src services/audio-relay/src \
            services/recognizer/src \
            services/camera-bridge/src \
            shared

# mypy — web
MYPYPATH=services/web/src:shared \
  python3 -m mypy services/web/src shared \
    --ignore-missing-imports --explicit-package-bases

# mypy — notifier
MYPYPATH=services/notifier/src:shared \
  python3 -m mypy services/notifier/src shared \
    --ignore-missing-imports --explicit-package-bases

# mypy — doorbell-firmware
MYPYPATH=services/doorbell-firmware/src:shared \
  python3 -m mypy services/doorbell-firmware/src shared \
    --ignore-missing-imports --explicit-package-bases

# mypy — recognizer
MYPYPATH=services/recognizer/src:shared \
  python3 -m mypy services/recognizer/src shared \
    --ignore-missing-imports --explicit-package-bases

# mypy — camera-bridge
MYPYPATH=services/camera-bridge/src:shared \
  python3 -m mypy services/camera-bridge/src shared \
    --ignore-missing-imports --explicit-package-bases
```

`detector` is excluded from mypy in CI because torch / opencv /
ultralytics are only present on the inference image.  `recognizer`
relies on `face_recognition` / `dlib` which are also too heavy for
the CI image — they're declared `ignore_missing_imports` so the
type-check passes without the wheel installed.

Required dev packages: `pip install ruff mypy types-PyYAML
types-requests types-redis pytest pillow`.

## Code review protocol

Non-trivial changes — anything in `services/` or `shared/`,
docker-compose, Dockerfiles, schema changes — get a self-review pass
from a code-review subagent before they're considered done. The
review prompt is in [CONTRIBUTING.md](CONTRIBUTING.md).

## What lives where

| Path | Purpose |
|---|---|
| `services/web/` | FastAPI app, Jinja templates, web-served snapshots and clips |
| `services/detector/` | RTSP ingestion, YOLO inference, snapshot capture |
| `services/notifier/` | Redis subscriber, dispatches to ntfy / Discord / email / webhook |
| `services/doorbell-firmware/` | Pi-side: button, heartbeat, MediaMTX wrapper |
| `services/audio-relay/` | (v0.3) bidirectional audio bridge |
| `services/recognizer/` | (v0.4) on-host face recognition sidecar; owns `recognizer.db` |
| `services/camera-bridge/` | (v0.6) MediaMTX sidecar that ingests USB webcams + video files and serves RTSP to the detector |
| `shared/` | Cross-service Python (event signing, config watcher, URL safety, atomic ref) |
| `docs/` | Specs, walkthroughs, reference |
| `config/` | Default templates (no secrets) |
| `scripts/` | One-shot installers and dev helpers |
| `infra/` | Docker compose overrides, runner setup |

## Reference documents

| Document | When to read |
|---|---|
| `README.md` | Always — the elevator pitch |
| `ROADMAP.md` | Before starting any new milestone work |
| `docs/ARCHITECTURE.md` | Before changing service boundaries |
| `docs/FIRMWARE.md` | Before touching `services/doorbell-firmware/` |
| `docs/HARDWARE.md` | When validating against real-device behavior |
| `CONTRIBUTING.md` | Before opening a PR |
