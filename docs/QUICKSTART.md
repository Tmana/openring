# OpenRing — Quickstart

Zero to working doorbell in roughly 30 minutes, assuming the hardware
is on hand. The README has the same flow in 9 lines; this doc
elaborates with expected output, common errors, and the small
decisions you'll have to make along the way.

## Before you start

You need:

- A **Linux or macOS box** to host the brain. Realistic minimum:
  4 GB RAM, 8 GB disk, a CPU from this decade. A NAS, a mini-PC,
  or a beefier Raspberry Pi 5 all qualify. Windows works via WSL2
  with Docker Desktop, but isn't tested upstream.
- **Docker Engine 25+ or Docker Desktop 4.x**, plus the
  `docker compose` v2 plugin (the `docker compose` command, not
  `docker-compose`).
- A **doorbell device** — see [HARDWARE.md](HARDWARE.md). The
  reference build is a Pi Zero 2 W + Camera Module 3 Wide, ~$84
  total.
- **Network reachability** — the Pi must be able to reach the host
  on the same LAN. Static DHCP reservation strongly recommended.
- A **notification destination** you already use (an [ntfy](https://ntfy.sh)
  topic, a Discord webhook, an SMTP relay, or a generic webhook URL).

You don't need:

- A GPU. The default model runs on CPU.
- A domain name (LAN-only mode is the v0.1 default).
- Anyone else's account, app, or cloud service.

## Step 1 — Set up the host (5 minutes)

```bash
git clone https://github.com/Tmana/openring.git
cd openring
./setup.sh
```

`setup.sh` is interactive only at one point — it'll print "Setup
complete" with an indented list of follow-up steps. The first run
takes the longest because it builds all the Docker images
(~5-10 minutes the first time, seconds on re-runs).

**Expected output highlights:**

```
==> Checking prerequisites
  ✓ docker 28.x.y
  ✓ docker compose 2.x.y
==> Provisioning .env
  ✓ wrote /<path>/openring/.env (chmod 600)
==> Seeding openring-config volume
  ✓ seeded openring-config/config/openring.yml from openring.example.yml
==> Building local images (this may take several minutes the first time)
  ...
  ✓ images built
```

**If you see this:**

| Message | Fix |
|---|---|
| `docker not on PATH` | Install Docker Engine or Docker Desktop, ensure your user is in the `docker` group. |
| `docker compose plugin missing — need v2` | You have the legacy `docker-compose` Python package. Install the v2 plugin (Compose plugin in Docker Desktop, or `apt install docker-compose-plugin`). |
| `setup.sh failed; tail of /tmp/openring-setup.log` (in the smoke test) | Read the log — usually a docker daemon permissions issue or a network failure pulling base images. |

## Step 2 — Bring the stack up (1 minute)

```bash
docker compose up -d
```

This starts redis, caddy, detector, web, and notifier in the
background. First boot takes ~30 seconds while the web service
initializes its SQLite DBs. Confirm with:

```bash
docker compose ps
```

You should see all five services in `running` state with healthy
healthchecks (the `STATUS` column will show `(healthy)` for redis,
web, notifier; detector takes longer to flip green because it's
loading the YOLO model lazily on first detection).

## Step 3 — Grab the bootstrap token (1 minute)

The web service generates a one-time token at first boot and prints
it to stdout. Find it with:

```bash
docker compose logs web | grep -A2 'First-run setup'
```

You'll see something like:

```
═══════════════════════════════════════════════════════════
  First-run setup — complete within 24 hours:
    Browse to: /setup?token=oQqB2VftAkqx9JJeivYYsFIIaEHvr...
  Token also stored at /data/bootstrap_token (chmod 600).
═══════════════════════════════════════════════════════════
```

Copy that path. The token is good for 24 hours; if you miss it,
delete the auth.db (`docker compose exec web rm /data/auth.db`)
and restart web — it'll issue a fresh one.

## Step 4 — Create the first admin user (1 minute)

Open `http://<your-host>/setup?token=<token>` in a browser. Replace
`<your-host>` with whatever the host is reachable as on your LAN —
typically `localhost` if you're on the host itself, or a hostname
like `openring.local` if you've set one up. The TLS-off default
means HTTP, not HTTPS.

You'll see a form asking for:

- **Username** — gets the admin role.
- **Password** — minimum 12 characters; a top-1000 common password
  list is enforced server-side.

Click "Create admin". You'll be redirected to the dashboard.

**Note:** the v0.1 default has TLS off (`tls.mode: "off"` in
`openring.yml`) — fine for LAN deployments. To use Let's Encrypt
or your own certs, see the `tls:` section in
`config/openring.example.yml`. Doing this requires a real domain
pointed at the host.

## Step 5 — Configure cameras and notifications (5-10 minutes)

The seeded `openring.yml` has placeholder values you'll replace.

Easiest way to edit: the web UI's **Admin → Config** page. It has
form-based editors for cameras, notification channels, exclusion
zones, and detection thresholds. Save → the detector and notifier
hot-reload.

If you'd rather edit YAML directly:

```bash
docker run --rm -it -v openring-config:/config alpine sh
# inside the container:
vi /config/openring.yml
```

Minimum to change:

1. **`notifications.channels[].topic`** for ntfy — replace
   `openring-CHANGE_ME` with a long random string. The ntfy
   server treats topic names as auth (anyone with the URL can
   publish), so use ~32 random chars.
2. **`notifications.channels[].enabled: true`** on whichever
   channel you'll actually use.
3. **`system.timezone`** — the seeded value is
   `America/Los_Angeles`. Pick yours from the standard tzdata list.

You don't need to define cameras yet — that happens in step 7
when you pair the doorbell.

## Step 6 — Flash the Pi (5 minutes)

On any other machine (your laptop is fine):

1. Download the **Raspberry Pi Imager** from
   https://www.raspberrypi.com/software/
2. Insert your microSD card.
3. Choose:
   - **Raspberry Pi OS Lite (64-bit)** — under "Other" if not in the
     featured list. **NOT** the desktop variant; OpenRing doesn't
     need a GUI on the Pi.
   - Storage: your SD card.
4. Before clicking Write, **set the OS customization**:
   - Hostname: `openring-doorbell` (or whatever — must be unique
     on your LAN).
   - Username/password — pick a real password, you'll SSH into
     this Pi for diagnostics.
   - Wi-Fi SSID + password.
   - Locale + timezone.
   - **Enable SSH** with the password you set.
5. Write. Eject. Insert into the Pi.

Power on the Pi. Within ~60 seconds it should appear on your
network. If you set the hostname in step 4, you can reach it as
`openring-doorbell.local` (mDNS).

Test that it's reachable:

```bash
ssh <username>@openring-doorbell.local
```

## Step 7 — Open the pairing window (10 seconds)

Back in the OpenRing web UI on the host:

1. Go to **Admin → Doorbells**.
2. Click **Pair new device**.
3. A 5-minute countdown starts. The page tells you what URL
   to point `pi-setup.sh` at — usually
   `http://<host-hostname>` or the IP.

You have 5 minutes from this click. If it expires, click again.

## Step 8 — Run pi-setup.sh on the Pi (3 minutes)

SSH into the Pi if you haven't:

```bash
ssh <username>@openring-doorbell.local
```

Clone OpenRing on the Pi (just for the firmware tree — we don't
need the full host stack on the device):

```bash
git clone https://github.com/Tmana/openring.git
cd openring
sudo ./services/doorbell-firmware/pi-setup.sh \
    --host-url http://<your-host>
```

The script prints colored progress through 9 steps. End-state
output looks like:

```
==> Pairing with host: http://openring.local
  auto-generated 32-char RTSP password
  ✓ paired successfully (device_id=front-door)
==> Writing /etc/openring/secrets.env
  ✓ /etc/openring/secrets.env written (root:openring 0640)
==> Rendering /etc/openring/mediamtx.yml
  ✓ mediamtx.yml rendered
==> Installing + enabling systemd units
  ✓ openring-mediamtx.service active
  ✓ openring-button.service active
  ✓ openring-heartbeat.service active

Pairing complete.

Add this camera entry to openring.yml on the host (under cameras:):

  - name: front-door
    rtsp_url: "rtsp://openring:<random-pass>@openring-doorbell.local:8554/door"
    enabled: true
    resolution: 720
    notification_rules:
      - class_name: doorbell_press
        channels: [phone-ntfy]
      - class_name: person
        channels: [phone-ntfy]

Then docker compose restart detector on the host so it picks up the new camera.
```

**Copy that camera block** — you'll paste it into the host's
`openring.yml` next.

## Step 9 — Tell the host about the new camera (2 minutes)

Back on the host, edit `openring.yml`. Either via the web UI's
Config page (Cameras tab → Add) or via shell:

```bash
docker run --rm -it -v openring-config:/config alpine sh -c "vi /config/openring.yml"
```

Paste the camera block under the `cameras:` list. Replace any
placeholder camera that was there from the example (`pond-north`
etc.).

Then restart the detector so it picks up the new RTSP source:

```bash
docker compose restart detector
```

Check that the detector connected:

```bash
docker compose logs detector --tail 30
```

You're looking for something like
`Camera 'front-door' opened RTSP stream successfully`. If you see
RTSP timeouts, double-check the URL the Pi printed (the password
in particular — it's long and easy to truncate).

## Step 10 — Press the button (instant)

Press the doorbell button on the Pi.

Within ~2 seconds you should:

1. See a notification arrive on your configured channel (ntfy push,
   Discord message, email, etc.) with a snapshot attached.
2. Watch the web UI's **Events** page show a new
   **Doorbell Press** row at the top.

If the notification doesn't arrive but the event row does — your
notifier is fine, the channel config is wrong. Re-check
`notifications.channels[].topic`/`webhook_url`/`smtp_*`.

If neither happens — see the troubleshooting section below.

## Step 11 (optional) — Tighten things up

Now that it works, things you'll want to tune over the first week:

- **Confidence threshold** — if you get false positives (YOLO
  thinks a wreath is a person), bump
  `detection.confidence_threshold` from 0.40 to 0.55. See
  [MODELS.md](MODELS.md) for the full ladder.
- **Exclusion zones** — drag a rectangle over the public sidewalk
  via the web UI's Cameras → Exclusion zones canvas, so neighbors
  walking by don't trigger the camera. The seeded config has a
  ready-made example.
- **Notification rules** — you probably want different routing for
  `doorbell_press` (button physically pressed) vs `person`
  (someone on camera but no press). The example config shows
  the pattern.
- **Remote access** — for "see your front door from outside the
  house" without exposing OpenRing to the public internet, install
  Tailscale on the host, add the device you want to allow, and
  browse to your host's Tailscale hostname. Documented separately
  (TBD `docs/REMOTE_ACCESS.md`).

## Troubleshooting

### `setup.sh` says docker is missing

Install Docker Engine:
- **Debian/Ubuntu:** `curl -fsSL https://get.docker.com | sh`
- **macOS:** install Docker Desktop from docker.com
- **Other:** see https://docs.docker.com/engine/install/

After install, log out / log back in (or `newgrp docker`) so your
user picks up the `docker` group.

### `docker compose up` fails with `port already allocated`

Something else is on port 80 or 443 on your host. Either stop it,
or pick different ports:

```bash
echo "HTTP_PORT=8080" >> .env
echo "HTTPS_PORT=8443" >> .env
docker compose up -d
```

Then browse to `http://localhost:8080`.

### Bootstrap token never appears in logs

It only prints if there are no users yet. If you've already
created an admin and lost the password, recover by:

```bash
docker compose stop web
docker compose run --rm --user root web sh -c "rm /data/auth.db && touch /data/.bootstrap-trigger"
docker compose start web
docker compose logs -f web | grep -A2 'First-run setup'
```

### Pi-setup.sh: "host rejected pairing — open the pairing window"

The 5-minute window expired or was never opened. Click "Pair new
device" again in the web UI, then re-run the script on the Pi.

### Pi-setup.sh: `SHA256 mismatch for mediamtx_v...tar.gz`

Either the upstream MediaMTX release was tampered with (unlikely)
or `services/doorbell-firmware/mediamtx.sha256` is stale. Cross-check
the hash against the release page on
github.com/bluenviron/mediamtx; if your repo's checksum file
matches the upstream and the install fails, file an issue.

For development you can pass `--skip-hash` to bypass — but don't
do this in production.

### Detector never connects to the Pi's RTSP

Diagnostic checklist:

```bash
# On the Pi:
sudo systemctl status openring-mediamtx     # should be active (running)
journalctl -u openring-mediamtx --tail 30   # look for libcamera or ffmpeg errors

# On the host:
docker compose logs detector --tail 50      # should show RTSP connection attempts
```

Common causes:
- Wrong RTSP password in the host's `openring.yml` — re-run
  pi-setup.sh during a fresh pairing window to rotate.
- Pi camera ribbon cable not seated properly — `libcamera-vid`
  prints a clear error.
- Network unreachable — try `ping openring-doorbell.local` from
  the host.

### Button press never reaches the host

Diagnostic:

```bash
# On the Pi:
journalctl -u openring-button --tail 30
```

You should see a "Button pressed at <timestamp>" line per press.

If yes but the host never sees it, check
`/etc/openring/secrets.env` on the Pi for the right `HOST_BASE_URL`
and `DEVICE_TOKEN`. Re-pairing rotates both.

If no log line per press, the GPIO wiring might be wrong. Confirm
your button is wired between GPIO 17 and GND, with `pull_up=True`
in the firmware (the default).

### Notifier sends to ntfy but nothing arrives on my phone

The `topic` in `openring.yml` must match exactly what your phone
is subscribed to. Topic names are case-sensitive and have no
secrets — anyone who knows the topic can publish to it, so use a
long random string.

## What "good" looks like

After 24 hours of normal use, your dashboard should show:

- Several `doorbell_press` events from button pushes
- Possibly several `person` detections from delivery drivers and
  visitors who didn't press the button
- Some false positives — YOLO sees people in unusual places.
  Click **False Positive** on those events to mark them; after
  ~50-100 labels, the confidence threshold tuning advice in
  [MODELS.md](MODELS.md) becomes applicable, and after ~500 you
  can fine-tune a person-and-front-door-specific model.

## Where to go next

- [HARDWARE.md](HARDWARE.md) — full BOM, power options, lens
  choices for non-reference builds
- [FIRMWARE.md](FIRMWARE.md) — what runs on the Pi, why, and how
  to extend it
- [MODELS.md](MODELS.md) — model swap and fine-tuning workflow
- [ARCHITECTURE.md](ARCHITECTURE.md) — service map and the
  architectural decisions behind it
- [ROADMAP.md](../ROADMAP.md) — what's planned for v0.2+ (video
  clips, two-way audio, face recognition, mobile-friendly UI)
- [CLAUDE.md](../CLAUDE.md) — for AI agents working on the repo:
  the project Charter (no cloud, no telemetry, no auto-update —
  ever) and the design decisions that aren't up for negotiation
