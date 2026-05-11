# OpenRing — Camera sources

The detector ingests **RTSP**. Every supported source path either
*is* RTSP natively (IP camera, Pi Zero 2 W) or gets bridged to RTSP
by the `camera-bridge` sidecar (USB webcam, video file).

There is no first-class "USB camera plugged into the host" code path
in the detector itself — that's deliberate. RTSP is the contract the
detector promises; everything else adapts to it.

## Support matrix

| Source | Status | Bridge needed? | Notes |
|---|---|---|---|
| **IP camera** (Reolink, Tapo, Wyze w/ custom FW, Amcrest, Hikvision, Dahua, Axis, etc.) | ✅ Native | No | Just set `rtsp_url` to the camera's published RTSP endpoint. |
| **Pi Zero 2 W** | ✅ Native | No | Run `pi-setup.sh` on the Pi. MediaMTX on the Pi serves at `rtsp://<pi-hostname>:8554/door`. See [QUICKSTART.md](QUICKSTART.md). |
| **USB / wired webcam (Linux host)** | ✅ Bridged | Yes | `camera-bridge` ingests `/dev/videoN` via v4l2 and re-serves as RTSP. |
| **USB webcam (Windows / macOS Docker Desktop)** | ⚠️ Indirect | Yes + extra | Docker Desktop can't see USB devices natively. Either (a) use `usbipd-win` to tunnel the device into the Docker VM, or (b) skip the webcam and use an IP camera or file source. |
| **Video file (dev / demo / unit test)** | ✅ Bridged | Yes | `camera-bridge` loops the file as RTSP. Cheapest way to exercise the full detection → notification flow without hardware. |

## Configuring each path

### Path 1 — IP camera (instant)

Find your camera's RTSP URL (usually documented as "ONVIF stream URL"
or "RTSP path" in the admin panel). Common examples:

| Brand | Typical URL pattern |
|---|---|
| Reolink | `rtsp://user:pass@CAM-IP:554/h264Preview_01_main` |
| Hikvision / Annke | `rtsp://user:pass@CAM-IP:554/Streaming/Channels/101` |
| Dahua / Amcrest | `rtsp://user:pass@CAM-IP:554/cam/realmonitor?channel=1&subtype=0` |
| Axis | `rtsp://user:pass@CAM-IP/axis-media/media.amp` |
| Tapo C100/C200 | `rtsp://user:pass@CAM-IP:554/stream1` |
| Wyze (custom firmware: dafang-hacks / xiaomi-dafang-hacks) | `rtsp://CAM-IP/unicast` |

Then in `openring.yml`:

```yaml
cameras:
  - name: front-door
    source: ipcam
    rtsp_url: rtsp://openring:CHANGE_ME@192.168.1.42:554/h264Preview_01_main
    enabled: true
    resolution: 720
```

Restart the detector to pick up the new camera:

```bash
docker compose restart detector
```

Watch the detector logs for `Connected to <camera>` to confirm.

### Path 2 — Pi Zero 2 W

Build the Pi side per [QUICKSTART.md](QUICKSTART.md) §"Step 3 — Flash
the Pi". `pi-setup.sh` installs MediaMTX and starts it as a systemd
unit serving RTSP at `rtsp://<pi-hostname>:8554/door`.

In `openring.yml`:

```yaml
cameras:
  - name: front-door
    source: pi
    rtsp_url: rtsp://openring:CHANGE_ME@front-door.local:8554/door
    enabled: true
    resolution: 720
```

`source: pi` is treated identically to `source: ipcam` by the
detector — the field is purely informational, so `docker compose
logs` and the Cameras admin page can show the right icon. The
distinction matters for the heartbeat watchdog (v0.2 #16) which only
applies to paired Pi devices, not third-party IP cams.

### Path 3 — USB webcam (Linux host)

> Linux host only. Docker Desktop on Windows / macOS doesn't expose
> host USB to containers without third-party tunneling — see Path 5.

Identify your webcam's V4L2 device:

```bash
v4l2-ctl --list-devices
# Typical output:
# HD Pro Webcam C920 (usb-0000:00:14.0-1):
#         /dev/video0
#         /dev/video1
```

The lowest-numbered `/dev/videoN` for a given camera is usually the
capture device. Pass it to the `camera-bridge`:

```yaml
cameras:
  - name: porch
    source: webcam
    device: /dev/video0
    enabled: true
    resolution: 720
```

The bridge service mounts `/dev/video0` from the host (you'll need to
ensure `docker-compose.yml`'s `camera-bridge` block has the device in
`devices:`, see `docker-compose.override.yml.example`). MediaMTX
inside the bridge wraps it in an RTSP path at
`rtsp://camera-bridge:8554/porch`. The detector connects there.

**Permission gotcha:** the bridge container runs as a non-root user;
`/dev/video0` is usually owned by `root:video` (gid 44 on Debian /
Ubuntu) and not world-readable. Either add `group_add: ["44"]` in the
compose override, or `sudo chmod a+r /dev/video0` on the host (resets
on reboot).

### Path 4 — Video file (dev / demo)

Drop a video into `data/sample-clips/` and point a camera at it:

```yaml
cameras:
  - name: demo
    source: file
    file: /data/sample-clips/walking-past.mp4
    loop: true                     # default true; play once with false
    enabled: true
    resolution: 720
```

The bridge runs `ffmpeg` looping the file into MediaMTX. The detector
sees a normal RTSP source.

This is the fastest way to demo OpenRing on a machine with no camera:
grab any creative-commons CCTV-style clip, drop it in
`data/sample-clips/`, and you'll have real YOLO detections firing in
the events page within a minute.

### Path 5 — Windows / macOS webcam (workaround)

Docker Desktop runs containers in a Linux VM (HyperKit on macOS, WSL2
or Hyper-V on Windows). The host's USB stack isn't proxied into that
VM, so `/dev/videoN` doesn't exist inside the bridge container.

Two options:

1. **Use an IP camera or video file instead.** This is the path most
   Windows/macOS users actually want — IP cameras are $30, work
   over WiFi, and skip the USB-passthrough complexity entirely.
2. **`usbipd-win` (Windows) / VirtualHere (macOS).** Forwards the
   USB device into the Docker VM. Setup is per-device, OS-version
   dependent, and unsupported by the OpenRing maintainers. If you
   really need a local webcam, search "usbipd-win docker webcam"
   for current guidance.

## Mixing sources

You can mix any combination — one Pi at the front door, an IP camera
in the driveway, a file source for testing. Each `cameras[]` entry
is independent. The detector spawns one ingest thread per enabled
camera.

```yaml
cameras:
  - name: front-door
    source: pi
    rtsp_url: rtsp://openring:CHANGE_ME@front-door.local:8554/door

  - name: driveway
    source: ipcam
    rtsp_url: rtsp://admin:CHANGE_ME@192.168.1.50:554/Streaming/Channels/101

  - name: dev-demo
    source: file
    file: /data/sample-clips/walking-past.mp4
    enabled: false                 # turn on when you want to record-and-replay
```

## When the bridge is and isn't running

`camera-bridge` is in the default `docker compose up` set but it's
lightweight (~50 MB image, idle CPU when no `source: webcam | file`
cameras are configured). If every camera in `openring.yml` is
`source: ipcam | pi`, the bridge starts and immediately exits its
MediaMTX server with zero paths configured — harmless.

To stop it after the fact:

```bash
docker compose stop camera-bridge
```

The detector ignores the bridge when no camera has `source: webcam`
or `source: file`, so leaving it up costs only the idle RAM footprint
of an unused MediaMTX process (~10 MB).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Detector logs `Failed to open <rtsp_url>` | Wrong URL path / wrong credentials. Test with `ffprobe rtsp://...` from your laptop first. |
| `Connection refused` from detector to bridge | `camera-bridge` isn't running (`docker compose ps`) or crashed at startup — check `docker compose logs camera-bridge`. |
| Bridge logs `Permission denied: /dev/video0` | The container user doesn't have access. Add `group_add: ["44"]` (or whatever `video` group GID your host uses — check with `getent group video`). |
| Bridge logs `Device or resource busy` | Another app on the host has the webcam open. Close Zoom / OBS / Skype / Chrome's camera permission. |
| Bridge logs `ffmpeg: No such file or directory` for the video path | Wrong path. The path must be the path **inside the container** — usually `/data/sample-clips/X.mp4`, which is a bind-mount of `./data/sample-clips/X.mp4` on the host. |
| Bridge starts but detector sees `404` on the RTSP URL | Bridge couldn't ingest the source. Tail `docker compose logs camera-bridge` for the ffmpeg/v4l2 error. |
