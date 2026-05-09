# OpenRing — Architecture

## One-line summary

A thin doorbell device streams RTSP + button events to a fat host
running a Docker Compose stack that does inference, storage, and
notification.

## Why two components and not one

The whole product splits cleanly into "things you want at the door
(camera, mic, button, speaker)" and "things you want indoors (CPU,
storage, the 24/7 reliability of your home server)." Putting the
brain at the door means putting an SoC in the rain, doing software
updates on something climbing a ladder, and either accepting offline
inference or ducking back to the cloud anyway. Putting the brain on a
host the user already trusts to be on means the device side is small,
replaceable, and uninteresting — which is what we want for the part
that lives outside.

This is also why ScarGuard's existing Docker Compose stack ports over
without much friction: ScarGuard already assumed a fat brain (a Jetson
or x86 Linux box) consuming RTSP from a dumb camera. We're swapping
the camera vendor and adding two new event types (button press, future
audio).

## Services on the host

Inherited largely as-is from ScarGuard, with renames where the names
were domain-specific.

| Service | Role | Status for v0.1 |
|---|---|---|
| `redis` | Internal pub/sub bus + rate-limit counters | direct copy |
| `caddy` | TLS termination, reverse proxy to `web` | direct copy |
| `detector` | Pulls RTSP frames, runs YOLO, publishes `openring:detections` | port from ScarGuard, swap target_classes to `[person]` |
| `web` | FastAPI + Jinja UI, SQLite events DB, live SSE feed, presses-via-API endpoint | port from ScarGuard, add `/api/doorbell/*` routes |
| `notifier` | Subscribes to detections + door events, dispatches to ntfy / Discord / email / webhook | direct copy |
| `log-streamer` | Tails container logs, surfaces in admin UI | direct copy |
| `backup` | SQLite online-backup sidecar | direct copy |

Services we deliberately don't have:

- No `deterrent`. ScarGuard fires sprinklers at herons; OpenRing does
  not actuate anything at the front door. (This is also where a future
  "trigger smart lock on face match" feature would live; deferred well
  past v1.0 because it's a high-blast-radius decision to ship.)
- No `speciesnet`. The OpenRing equivalent might be face recognition,
  but that's a v0.4 opt-in and uses on-host embeddings, not an external
  API.

## Services on the doorbell

See [FIRMWARE.md](FIRMWARE.md) for the full spec. Three systemd units,
no Docker, all installed by `pi-setup.sh`:

| Unit | Role |
|---|---|
| `openring-mediamtx.service` | RTSP server, sources `libcamera-vid` |
| `openring-button.service` | GPIO button + heartbeat → host POST |
| `openring-audio.service` | Two-way audio (v0.3 stub for v0.1) |

## Event taxonomy

| Source | Channel | Payload |
|---|---|---|
| `detector` (host) | `openring:detections` | person bbox, confidence, snapshot path, frame size, feedback_token |
| `web` (host, on doorbell button POST) | `openring:doorbell` | timestamp, device_id, snapshot_path (latest frame from RTSP) |
| `web` (host, on heartbeat timeout) | `openring:device` | type=offline\|recovered, device_id, last_seen |

All channel publishes from authoritative services are HMAC-signed
using a `OPENRING_HMAC_KEY` generated at first-run by the host's
`setup.sh`. Subscribers (notifier, web SSE) verify before acting.
This is the same pattern that ScarGuard's `shared/event_signing.py`
implements; we copy it verbatim.

## Data lifecycle

1. **Capture.** Pi camera → MediaMTX → RTSP → host detector. Host also
   serves a "grab latest frame" endpoint used to attach a snapshot to
   doorbell-press events (which fire faster than detector cooldown).
2. **Inference.** YOLO runs on every Nth frame on the host. Person
   detections above threshold are events.
3. **Persistence.** Events written to `/data/openring.db` (SQLite,
   WAL). Snapshots saved as files under `/data/snapshots/`. Optional
   v0.2 video clips under `/data/clips/` with pre/post-roll.
4. **Notification.** `notifier` consumes `openring:*` channels,
   dispatches to whichever channels the user configured.
5. **Retention.** Same retention engine as ScarGuard — single config
   key (`system.retention_days`) prunes old snapshots, events, clips.
   Events with user feedback are retained indefinitely (the "training
   data" pile).
6. **Egress.** None, except whatever the user-configured notification
   channel does. Notifier dispatches always go from the host, never
   the doorbell — the doorbell only ever talks to one address (its
   host).

## Security model

| Threat | Mitigation |
|---|---|
| Someone on the LAN reads the RTSP stream | Basic auth on MediaMTX, fresh password per deployment |
| Someone on the LAN replays a doorbell-press POST to spam alerts | Bearer token on the device → host endpoint; host rate-limits per device_id |
| Compromised doorbell publishes fake events to spam UI | Doorbell can only POST to two endpoints; host re-publishes onto the bus with HMAC, so a Pi compromise can't directly inject signed events |
| Compromised host | Game over for that user. We can't protect against this; we can ensure footage never went to anyone else, which is the whole point. |
| Network outage | Doorbell queues up to 16 button presses on disk and replays. Host stack runs entirely locally so detection/storage are unaffected by WAN outages. |
| Repo / supply-chain compromise | The `pi-setup.sh` pinned hashes for MediaMTX and the host stack uses pinned image digests; we commit to publishing SBOMs from v0.1.0 onward. |

## What ports are open where

| Host | Port | Bound to | Purpose |
|---|---|---|---|
| Doorbell | 8554/tcp | LAN | RTSP for the host detector |
| Doorbell | 22/tcp | LAN | SSH (your call whether to keep this on after provisioning) |
| Host | 80/tcp | LAN | Caddy → redirects to 443 |
| Host | 443/tcp | LAN (or Tailscale) | Caddy → web service |
| Host | 8554/tcp | localhost | RTSP relay if you want it; otherwise unused |

Nothing on either device should be reachable from the public internet
without your explicit configuration (Tailscale, port forward, etc.).

## What this architecture rules out

- Ring's "cloud-only review" model. There is no relay; the host is
  authoritative.
- True battery operation of the doorbell. See [HARDWARE.md](HARDWARE.md).
- Always-recording with months of cloud retention. You can configure
  arbitrary retention on the host but you're paying for the disk.
- Multi-tenant SaaS. There's no account system because there's no
  account; the web UI uses a single auth realm per host.
