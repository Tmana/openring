# OpenRing — Roadmap

Each milestone has a single-sentence goal, a definition of "done"
that's testable, and a punch list of issues. Milestones ship when
every checked item is checked. Pre-v1.0 we feel free to break the
config schema; from v1.0 onward we mean it.

---

## v0.0 — Skeleton (this commit)

**Goal:** repo exists, anyone can read the docs and understand what
we're building.

- [x] `README.md` with the elevator pitch and the architecture diagram
- [x] `docs/ARCHITECTURE.md`, `docs/FIRMWARE.md`, `docs/HARDWARE.md`
- [x] `LICENSE` (MIT)
- [x] `CONTRIBUTING.md` and `CLAUDE.md` (workflow conventions)
- [x] Skeleton service directories so people know where things land
- [x] This roadmap

No working software ships in v0.0. That's fine — the next milestone
fixes that.

---

## v0.1 — MVP doorbell event flow

**Goal:** press the button on a real Pi, get a notification on your
phone with a snapshot of whoever's on the porch.

A reasonable user flow at the end of v0.1:

1. Run `./setup.sh` on a Linux box, walks you through generating
   secrets and dropping a `config/openring.yml`.
2. `docker compose up -d` brings the host stack online.
3. Flash Pi OS Lite to an SD card, boot the Pi, run `pi-setup.sh
   <host-hostname>`.
4. Press the button. Within ~2 seconds, your phone (ntfy or Discord
   webhook) shows "OpenRing: someone at the front door" with a still
   image attached.

### Issues (open these in order)

- [ ] **#1: Repo bootstrap.** Add `pyproject.toml` (ruff/mypy/pytest
  config matching the project conventions), `.editorconfig`,
  `.github/workflows/ci.yml` (lint + typecheck per service).
- [ ] **#2: Port host stack from ScarGuard.** Vendor in the upstream
  `services/{web,detector,notifier,redis,caddy}` and `shared/`
  directories under `services/` here, rename `scarguard:*` Redis
  channels to `openring:*`, swap `scarguard.yml` to `openring.yml`,
  swap `scarguard.db` to `openring.db`. Single sweep PR; no logic
  changes beyond renames. Detection target classes default to
  `["person"]`.
- [ ] **#3: Doorbell device API.** Add `POST /api/doorbell/press`,
  `POST /api/doorbell/heartbeat`, `POST /api/doorbell/register` (the
  one-time pairing endpoint, gated on a 5-minute pairing window the
  user opens from the web UI). Store device tokens hashed in
  `auth.db` alongside web-user tokens.
- [ ] **#4: Doorbell event publish.** On press, pull the latest frame
  via the detector's "grab snapshot" endpoint, persist as a snapshot
  file, write a synthetic event row, publish on `openring:doorbell`
  with HMAC. The notifier subscribes and dispatches.
- [ ] **#5: Doorbell firmware — MediaMTX RTSP.** `services/doorbell-firmware/`
  with the systemd unit + `mediamtx.yml.template`. `pi-setup.sh`
  installs MediaMTX from a pinned release with SHA256 verification.
- [ ] **#6: Doorbell firmware — button + heartbeat.**
  `src/button.py` and `src/heartbeat.py` plus the systemd unit. Use
  `gpiozero` with `MockFactory` in tests so CI can run without
  hardware.
- [ ] **#7: Person-tuned model guidance.** Pick a default YOLO
  variant (probably `yolov8n.pt` for CPU hosts, `yolov8s.pt` for GPU)
  and document in `docs/MODELS.md` how to swap. Ship a confidence
  threshold default of 0.4.
- [ ] **#8: Exclusion zones default.** ScarGuard's exclusion-zone
  editor ports over directly. v0.1 ships with a documented "exclude
  the sidewalk" example so users know where to start.
- [ ] **#9: Notifier channels — ntfy + Discord + email.** Inherit
  exactly. Webhook channel is a free bonus.
- [ ] **#10: Single-host setup.sh.** Generates `OPENRING_HMAC_KEY`,
  Redis password, web admin password; renders `docker-compose.yml`
  with named volumes; prints next steps.
- [ ] **#11: Single-Pi pi-setup.sh.** Walks through the provisioning
  checklist in `FIRMWARE.md`. Idempotent on re-run.
- [ ] **#12: Smoke test.** A `tests/smoke/` script that boots the
  compose stack, fakes a doorbell press via the API, asserts an
  event row appears and a notifier dispatch was attempted.
- [ ] **#13: Walkthrough docs.** Top-level `docs/QUICKSTART.md` that
  takes a new user from zero to working doorbell in 30 minutes.

**Out of scope for v0.1, repeat for emphasis:** video clips, two-way
audio, face recognition, mobile app, remote access (beyond a
"point Tailscale at the host" pointer).

---

## v0.2 — Video clips + offline detection of the doorbell device

**Goal:** every doorbell press and every detected person gets a
10-second clip you can scrub. Doorbell going offline notifies you.

- [ ] **#14: Pre/post-roll video clips.** Detector keeps a rolling
  ring buffer of the last 10 s of decoded H.264 NALUs in memory
  (cheap on x86, doable on Pi 5 host); on event, writes 5 s pre +
  5 s post as a fragmented MP4 to `/data/clips/`. SQLite gets a new
  `clips` table joined to events by id.
- [ ] **#15: Web UI clip player.** `<video>` tag with the same
  feedback / labelling controls as the snapshot overlay.
- [ ] **#16: Heartbeat watchdog.** Web service tracks last-seen per
  device; a configurable timeout (default 90 s) flips the device to
  "offline" and publishes on `openring:device`. Notifier handles it
  the same way it handles camera-offline alerts in ScarGuard.
- [ ] **#17: Retention engine extension.** Clips inherit
  `system.retention_days`. Labelled events keep their clips
  indefinitely.

---

## v0.3 — Two-way audio

**Goal:** push-to-talk from the web UI to the doorbell speaker, and
hear what's happening at the door.

- [ ] **#18: `audio-relay` Pi service.** Single WebSocket to host;
  Opus frames in both directions, half-duplex with a clear "speaking"
  / "listening" state.
- [ ] **#19: Host audio bridge.** A new `services/audio-relay/`
  service that owns the Pi WebSocket and exposes a browser-facing
  WebSocket via the web service.
- [ ] **#20: Web UI talk button.** Push-to-talk with browser mic
  capture. Visual indicator that the mic is hot. Releases on blur.
- [ ] **#21: Auth on the audio bridge.** Browser holds a short-lived
  JWT issued by the web service. Pi side uses its existing device
  Bearer.
- [ ] **#22: Audio hardware tested-on matrix.** Confirm at least two
  USB DAC + mic combinations work end-to-end.

---

## v0.4 — Face recognition (opt-in, on-host)

**Goal:** the notification can say "Mom" instead of "Person" when
known faces are configured. All embeddings stay on the host. No API
calls anywhere.

- [x] **#23: `recognizer` sidecar.** Subscribes to detections, crops
  the face region, computes embeddings via the on-host
  `face_recognition` (dlib) library. Persists to its own
  `/data/recognizer.db` SQLite file. Publishes outcomes on
  `openring:recognition` (HMAC-signed). Off by default. Design doc:
  `docs/FACE_RECOGNITION.md`.
- [x] **#24: Known-face enrollment UI.** Admin-only `/admin/recognizer`
  page lets the operator add a face (label + notes), upload 3-5
  reference photos, soft-delete (toggle enabled), or hard-delete the
  face entirely (cascades to embeddings + on-disk photos). Web saves
  photos + a `known_faces` row; the recognizer subscribes to
  `openring:enrollment` and computes embeddings on the sidecar so
  dlib stays out of the web image. A startup catch-up sweep makes the
  flow tolerant to a recognizer restart mid-enrollment.
- [ ] **#25: Notification template extension.** The notifier learns
  to render "Sarah is at the front door" when a match clears the
  similarity threshold; falls back to "Person at the front door"
  otherwise.
- [ ] **#26: Privacy hardening.** Off by default. Documented in
  `docs/FACE_RECOGNITION.md` with a clear "this is yours, you
  manage who's in your address book."

---

## v0.5 — Mobile-friendly web UI + Tailscale guide

**Goal:** the web UI is a usable phone PWA, and there's a step-by-step
"watch the door from outside the house" guide that doesn't require
trusting us.

- [ ] **#27: Responsive CSS pass.** Dashboard, events, live view,
  feedback flow all usable on a 390 px viewport.
- [ ] **#28: Add-to-home-screen manifest.** PWA basics — name, icon,
  start_url, display: standalone.
- [ ] **#29: Push notifications via Web Push.** Hooked into the
  notifier as a new channel type. Lets you skip ntfy if you want one
  fewer moving part.
- [ ] **#30: `docs/REMOTE_ACCESS.md`.** Tailscale walk-through plus
  pointers for WireGuard, Cloudflare Tunnel (with the appropriate
  caveat that Cloudflare sees the metadata), and a port-forward
  with-best-practices section for advanced users.

---

## v1.0 — "I'd put this on my own porch"

**Goal:** the project is sturdy enough that the maintainers run it on
their own front doors and recommend it to a friend.

The v1.0 bar is empirical, not a feature list:

- 30 days of continuous uptime on the maintainers' own units
- All v0.1-v0.5 issues closed
- A second-source independent code review of the device-side and
  host-side security boundaries (analogous to ScarGuard's v1.14 review)
- SBOMs for both the host images and the device-side packaging
- An "isolated mode" decision: do we ship a fallback chime + local
  notification when the host is offline? If yes, scope it; if no,
  document the limitation prominently in the README. Either is
  acceptable; not deciding is not.

---

## Future (unprioritized, may never happen)

- ESP32-S3 "openring-mini" track for battery-powered installations.
  Massively different firmware, partial feature parity (no live view,
  PIR-driven wake, low-resolution wake shots only). Only pursued if
  the demand signal is loud.
- WebRTC live view via MediaMTX WHEP. Lower latency, paves a clean
  path for two-way audio over the same transport.
- Multi-doorbell support. The host stack already speaks multi-camera
  (ScarGuard's per-camera config) so this is mostly a UI exercise.
- Smart-lock integration. High blast radius; will not ship without
  an explicit per-installation acknowledgement and a hardware-side
  manual override.
- Package manager integration (apt repo for the Pi, Homebrew tap for
  the host). Once we're past v1.0 and the install isn't churning.
- Clipboard tour: "give me a 30-second highlight reel of the
  weekend's visitors" — clip-stitching helper. Cute, not load-bearing.
