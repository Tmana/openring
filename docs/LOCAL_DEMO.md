# OpenRing — Local Demo

Boot the host stack on your dev machine, click through the UI, see
the v0.4 features end-to-end. **No Pi, no porch, no doorbell button
required.** ~15 minutes from clone to login screen.

This is the runbook to use when you want to see the project work
before committing to the full hardware build in
[QUICKSTART.md](QUICKSTART.md).

## Prereqs

- Docker Engine 25+ or Docker Desktop 4.x, with `docker compose` v2.
- ~5 GB free disk for images (web 600 MB, notifier 200 MB, recognizer
  1.5 GB once dlib is built, redis 50 MB, plus volumes).
- ~2 GB free RAM at idle.
- A bash-compatible shell. Git Bash on Windows works for the demo, but
  the `setup.sh` invocation goes through Docker so the host shell
  doesn't matter once Docker is up.

## Two demo tiers

Pick one based on what you want to see.

| Tier | What you get | What it costs |
|---|---|---|
| **A — UI tour** | Dashboard + Events + Config + Faces enrollment (write-only). No detection or face-matching actually fires. | ~3 min build (web + notifier only) |
| **B — v0.4 end-to-end** | Tier A + recognizer running. Enroll a face, watch the recognizer compute embeddings on a real reference photo. Still no detector — face-matching is exercised via a manual Redis publish. | ~15 min build (adds dlib compile in the recognizer image) |

Skip Tier B if you just want to see the UI. The recognizer image is
slow to build because dlib compiles from source.

## Tier A — UI tour (5 min)

```bash
git clone https://github.com/Tmana/openring.git
cd openring
cp docker-compose.override.yml.example docker-compose.override.yml
./setup.sh --no-build
```

`--no-build` lets us pick which services to build below; without it
`setup.sh` builds the full stack including the 3 GB detector image
which we don't need.

The `docker-compose.override.yml.example` step exposes `web` on port
8080 to your host (in production, Caddy fronts web on 80/443 and the
internal port isn't published).  The override is gitignored so you can
edit it freely.

> **Windows-only gotcha**: the `*.sh` and `*.yml` files in this repo
> need LF line endings to run inside Linux containers.  The repo's
> `.gitattributes` enforces this, so a fresh clone is fine.  If you're
> on an older clone and see `exec /app/entrypoint.sh: no such file or
> directory` from one of the containers, your scripts have CRLF; fix
> with `git config core.autocrlf input && git rm --cached -r . && git
> reset --hard`.

Pass `MSYS_NO_PATHCONV=1` in front of any `docker run -v /something:/in-container` command on Git Bash so MSYS doesn't translate `/config` into a Windows path.

Disable user auth for the demo (so you don't need to copy a bootstrap
token from the logs):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v openring-config:/config alpine:3.19 sh -c '
  if ! grep -q "auth:" /config/openring.yml; then
    awk "/^system:/ {print; print \"  auth:\"; print \"    enabled: false\"; next} {print}" \
      /config/openring.yml > /config/openring.yml.new
    mv /config/openring.yml.new /config/openring.yml
  fi
'
```

> **Don't do this on a real deployment.** With auth disabled every
> request gets admin role automatically. Fine for "click around on
> localhost"; not fine for anything reachable from a network.

Build + start the small services:

```bash
docker compose build web notifier
docker compose up -d redis web notifier
```

Wait ~10 seconds for `web` to become healthy:

```bash
until curl -sf http://localhost:8080/health >/dev/null; do sleep 1; done
echo "web is up"
```

Visit **http://localhost:8080** — you should land on the Dashboard.

### Things to click

- **Dashboard** — system status (armed/disarmed), camera list (empty
  for now), deterrent panel.
- **Events** — empty table. The mobile-friendly v0.5 styling kicks in
  if you narrow the browser to <480 px or use Chrome DevTools'
  device emulation (iPhone 12 Pro = 390 × 844).
- **Admin → Config** — the YAML editor. You can see `face_recognition`
  and `clipper` blocks even though those services aren't running.
- **Admin → Faces** — the v0.4 enrollment UI from PR-B. Try uploading
  a photo with a single face. The recognizer isn't running in Tier A
  so the embedding count stays at 0 ("embedding…" indicator) — that's
  the expected behaviour with the recognizer down.
- **Admin → Audit Log** — every config edit + face enrollment is
  recorded here.
- **About** — credits + version info.

### Bring it down

```bash
docker compose down -v --remove-orphans
docker volume rm openring-config openring-data openring-models openring-redis
```

The `-v` removes volumes so a follow-up `setup.sh` starts clean. If
you want to keep your config + enrolled faces between runs, just do
`docker compose down` without `-v`.

## Tier B — v0.4 face-recognition end-to-end (15 min)

Everything from Tier A, plus building and running the recognizer.

After step 1 of Tier A (clone + setup.sh + auth-disable patch):

```bash
docker compose build web notifier recognizer
```

The recognizer build compiles dlib from source. Expect 8–12 minutes
depending on your CPU. The build is cached so subsequent rebuilds
are seconds.

Enable the recognizer in config:

```bash
docker run --rm -v openring-config:/config alpine:3.19 sh -c '
  sed -i "s/^face_recognition:$/face_recognition:\n  enabled: true/" /config/openring.yml || \
  sed -i "s/  enabled: false/  enabled: true/" /config/openring.yml
'
```

Bring up the stack:

```bash
docker compose up -d redis web notifier recognizer
docker compose logs -f recognizer
```

You should see:
```
recognizer ─ OpenRing recognizer starting
recognizer ─ Subscribed to openring:detections + openring:enrollment
```

### Demo: enroll a face

1. Browse to **http://localhost:8080/admin/recognizer**
2. Find a clear, single-subject photo of yourself (or any consenting
   person). 256×256 or larger, JPEG or PNG, ≤10 MB.
3. Type a label (e.g. `demo-face`), select the photo, click **Enroll**.
4. Watch `docker compose logs recognizer` — within a couple seconds
   you should see:
   ```
   recognizer ─ Embedded 1.jpg for face_id=1
   ```
5. The Faces page now shows `1 photo · 1 embedding`. If the embedding
   count stays at zero or the recognizer logs `no face detected in
   reference photo`, the photo didn't pass the single-face check —
   try a different photo.

### Demo: simulate a doorbell-press recognition

Without a real detector running, we manually publish a detection
event onto `openring:detections` and the recognizer treats it as if
the detector had sent it. (HMAC verification is on; we have to sign
the payload.)

```bash
DETECTION_HMAC_KEY=$(grep DETECTION_HMAC_KEY .env | cut -d= -f2)
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(8))')

# Drop a known snapshot in the volume so the recognizer can crop it.
# Use the same photo you enrolled — the recognizer will match itself.
docker run --rm -v openring-data:/data -v "$PWD":/host alpine:3.19 \
  sh -c "cp /host/path/to/your/photo.jpg /data/snapshots/${TOKEN}.jpg"

# Sign + publish the synthetic detection.  Verifying recognition
# message that comes back is left as an exercise — easier to just
# tail the recognizer logs.
docker compose exec -T web python3 <<PY
import hashlib, hmac, json, os, redis
key = bytes.fromhex(os.environ["DETECTION_HMAC_KEY"]) \
      if all(c in "0123456789abcdef" for c in os.environ["DETECTION_HMAC_KEY"]) \
      else __import__("base64").b64decode(os.environ["DETECTION_HMAC_KEY"])
event = {
    "feedback_token": "${TOKEN}",
    "camera_name": "demo-cam",
    "class_name": "person",
    "confidence": 0.9,
    "bbox": [0, 0, 256, 256],
    "timestamp": "2026-05-10T12:00:00+00:00",
}
canonical = json.dumps(event, sort_keys=True, separators=(",",":"), default=str).encode()
event["_sig"] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
r = redis.Redis(host="redis", port=6379, password=os.environ["REDIS_PASSWORD"])
r.publish("openring:detections", json.dumps(event))
print("published", event["feedback_token"])
PY
```

Tail `docker compose logs recognizer` again — you should see:
```
recognizer ─ Matched face on demo-cam (face_id=1, score=0.4xx, token=...)
```

Then check `/admin/recognizer` — you'd see one entry in the
`recognitions` table for the synthetic event (if the web side were
displaying it; in v0.4 it's persisted but not yet rendered on the
events page — that's a v0.5 polish item).

### Bring it down

```bash
docker compose down -v --remove-orphans
docker volume rm openring-config openring-data openring-models openring-redis
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `docker info` hangs forever | Docker Desktop hasn't finished starting. Wait for the tray icon to go solid green. |
| Build fails on `dlib` step | The recognizer image is the only one that needs cmake + a full C++ toolchain. Fix is usually free disk space (~3 GB working buffer) or RAM (the C++ link step wants ~2 GB). |
| `web` container restarts forever | Check `docker compose logs web` for the actual exception. Most common in dev is a stale `openring-data` volume from an earlier version — `docker volume rm openring-data` and re-run setup. |
| `localhost:8080/setup` redirects in a loop | You forgot the `auth.enabled: false` patch. Re-run the patch script in step 1 and `docker compose restart web`. |
| Faces page shows photos but `0 embeddings` permanently | Recognizer container isn't running or `face_recognition.enabled: false`. `docker compose logs recognizer` will tell you which. |
| Recognition step says `no face detected in reference photo` | The photo's face is too small, blurry, or the recognizer's HOG detector misses it. Try a clearer 512×512+ photo. |

## What's NOT in the demo

- **A real doorbell device.** The button + camera + RTSP feed all
  live on the Pi. The demo uses synthetic detection events instead.
  See [QUICKSTART.md](QUICKSTART.md) for the full flow.
- **The detector (YOLO) running.** It's a 3 GB image and the demo
  doesn't need it — face-matching is what we're showing here, and
  the recognizer subscribes to detection events regardless of
  whether they originate from real YOLO inference or our manual
  Redis publish.
- **Two-way audio (v0.3).** Needs the Pi-side audio-relay client to
  pair up, which means real hardware.
- **TLS / Caddy.** The demo skips Caddy and exposes web on port
  8080 directly. Production deployments add `caddy` to the compose
  command and configure `tls.mode: auto` + `tls.domain` in
  `openring.yml`.
