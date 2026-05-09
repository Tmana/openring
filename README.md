# OpenRing

An open-source, **local-only** smart doorbell. Person-detection at the door, push notifications to your phone, live view in your browser — all without sending a single byte to anyone else's cloud.

> Status: **pre-alpha.** v0.1 scope is being defined ([ROADMAP.md](ROADMAP.md)). Expect rough edges and design churn.

## Why

Commercial smart doorbells are great products built around a transactional bargain you may not want to make: they upload every clip to a vendor cloud, charge a monthly fee for access to your own footage, and bring the vendor's threat model (data breaches, law-enforcement requests, account lockouts, end-of-life bricking) onto your front porch.

OpenRing keeps the convenience and dumps the cloud:

- Captured video and snapshots live on **your** computer (or NAS, or single-board computer running 24/7) — never anywhere else.
- Person detection runs **locally** on that same host, on a YOLO model you can swap or fine-tune.
- Notifications go to whatever you already use (ntfy, Discord, email, generic webhook). No vendor app required.
- Remote access is your call: Tailscale / WireGuard / your VPN of choice. We don't ship a "convenience tunnel" by default because that *is* the cloud bargain we're avoiding.

## What it actually does (v0.1 target)

1. Press the doorbell button → host fires a notification with a still snapshot of whoever's there.
2. Motion + person detection on the live RTSP stream → second class of notification, with an exclusion-zone editor so the mailman bot doesn't ping you four times a day.
3. Web UI (LAN-only by default) shows a live camera feed, an event log, and per-event labelling so you can train a better model on your own data over time.
4. Everything is one `docker compose up` on the host plus a `setup.sh` on the doorbell.

See [ROADMAP.md](ROADMAP.md) for v0.2+ (video clips, two-way audio, optional on-host face recognition).

## Architecture

A thin doorbell device (Pi Zero 2 W + camera + button) and a brain host (your computer) running a Docker Compose stack.

```
┌─────────────── DOORBELL ────────────────┐         ┌─────────────────── HOST ──────────────────────┐
│  Pi Zero 2 W                            │         │  Docker Compose stack                          │
│  ├─ Camera Module 3 → MediaMTX (RTSP)   │ ─RTSP──▶│  detector  ─Redis pub/sub─▶  notifier ──┐     │
│  ├─ GPIO button     → button-firmware   │ ─HTTPS─▶│  web (FastAPI + Jinja, SSE live feed)   │     │
│  └─ (later) USB mic → audio-relay       │ ◀─WS───▶│  redis · caddy · sqlite (events)        │     │
└─────────────────────────────────────────┘         └────────────────────────────────────┬──────────┘
                                                                                          │
                                                                                          ▼
                                                                                   ntfy / Discord / email
                                                                                   (your existing channel)
```

The architecture intentionally mirrors [sentania-labs/scarguard](https://github.com/sentania-labs/scarguard) — a wildlife-detection project that established this Docker-Compose-on-a-Jetson pattern. We swap in person detection, add doorbell-specific event types (button press, two-way audio later), and reuse the rest.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full picture and [docs/FIRMWARE.md](docs/FIRMWARE.md) for what runs on the door device.

## Hardware

The reference build uses a Pi Zero 2 W. Anything Linux-capable with a camera and a GPIO pin will work. See [docs/HARDWARE.md](docs/HARDWARE.md) for the BOM, wiring, and "can I use my existing chime transformer?" answer.

## Quick start

```bash
# 1. Clone the repo on whatever Linux/macOS box will host the brain
git clone https://github.com/Tmana/openring.git
cd openring

# 2. Generate secrets, seed the config volume, build the images.
#    Idempotent — safe to re-run.
./setup.sh

# 3. Bring the stack up
docker compose up -d

# 4. Grab the one-time bootstrap token from the web service logs:
docker compose logs -f web | grep -A2 'First-run setup'

# 5. Browse to the URL it prints (typically http://localhost/setup?token=...)
#    and create the first admin user.

# 6. From the web UI: Admin → Doorbells → "Pair new device" (5-min window).

# 7. On the Pi, after flashing Pi OS Lite 64-bit:
sudo ./services/doorbell-firmware/pi-setup.sh --host-url http://<your-host>

# 8. Paste the YAML camera snippet pi-setup.sh prints into openring.yml
#    on the host, then:
docker compose restart detector

# 9. Press the button.
```

`setup.sh --help` and `pi-setup.sh --help` document every flag. Both scripts have a `--dry-run` mode if you want to preview what they'd do before letting them touch anything.

For the long-form walkthrough — expected output at every step, troubleshooting, and the small decisions you'll have to make — see [`docs/QUICKSTART.md`](docs/QUICKSTART.md).

## Local-only, in practice

OpenRing **never** makes an outbound connection to a third party that we control. The only outbound traffic is whatever notification channel *you* configure (your ntfy server, Discord webhook, SMTP relay). If you point all of those at services running on your own LAN, OpenRing is completely air-gapped.

What this means for you:

- No "OpenRing cloud" exists, will ever exist, or could be quietly added in a future update without a major rewrite.
- If you want to access the live feed away from home: run Tailscale, WireGuard, or your reverse proxy of choice. We document Tailscale in [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) (TBD) because it's the lowest-friction option, but you're not locked in.
- A breach of OpenRing's repo or maintainers cannot exfiltrate your footage, because there's nowhere for it to go.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports, hardware tested-on confirmations, and feature ideas welcome — open an issue first for anything non-trivial so we can coordinate scope.

## License

[MIT](LICENSE).

## Lineage

OpenRing borrows its core architecture (Docker Compose host stack, Redis pub/sub bus, HMAC-signed event channel, snapshot-and-feedback labelling pipeline) from [ScarGuard](https://github.com/sentania-labs/scarguard). Heavy mechanical reuse, different problem domain.
