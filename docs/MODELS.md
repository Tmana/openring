# OpenRing — Detection Models

OpenRing's detector ports directly from ScarGuard, which means
**any [Ultralytics YOLO](https://github.com/ultralytics/ultralytics)
model** works as long as it can detect a `person`. v0.1 ships with
Ultralytics' COCO-pretrained `yolov8n.pt` as the default — small
enough to run on CPU, accurate enough to pick up an adult on a porch
in daylight or under IR floodlight.

This doc covers:

1. The default and how to swap it
2. Where model files live and how the volume layout works
3. Confidence threshold guidance (the v0.1 default is 0.40 for a
   reason)
4. Picking a different variant for a GPU host
5. Fine-tuning on your own data once you've collected feedback

## TL;DR

```yaml
# config/openring.yml
detection:
  model_path: /models/yolov8n.pt   # COCO-trained nano variant — default
  confidence_threshold: 0.40        # default; raise to 0.55-0.65 if you get FPs
  target_classes:
    - person                        # COCO class 0
  cooldown_seconds: 30
  frame_skip: 2
```

If you want to use a different model:

```bash
# Drop the file into the openring-models named volume
docker run --rm -v openring-models:/models -v $PWD:/src alpine \
    cp /src/yolov8s.pt /models/

# Update config
docker run --rm -it -v openring-config:/config alpine vi /config/openring.yml
# change model_path to /models/yolov8s.pt

# Restart the detector
docker compose restart detector
```

…or use the web UI's **Admin → Models** page once you've completed
first-run setup.

## Default model: `yolov8n.pt`

| Property | Value |
|---|---|
| Source | Ultralytics, COCO-pretrained |
| File size | ~6 MB |
| Inference (CPU, 720p frame) | ~80-150 ms on a modern x86; ~250-400 ms on a Pi 5 host |
| Person mAP | ~0.49 (COCO val) |
| Why | Small, fast, no GPU needed, runs comfortably on a NAS or mini-PC alongside other home services |

The detector downloads `yolov8n.pt` lazily on first use if the file
isn't already in the `openring-models` volume — this is Ultralytics'
default behavior. For airgapped installs, fetch it manually:

```bash
# On any internet-connected machine
curl -L https://github.com/ultralytics/assets/releases/latest/download/yolov8n.pt -o yolov8n.pt
sha256sum yolov8n.pt   # confirm against the upstream release page

# Drop into the volume on the host
docker run --rm -v openring-models:/models -v $PWD:/src alpine \
    cp /src/yolov8n.pt /models/
```

## Model storage layout

| Path (inside web/detector containers) | Source | Purpose |
|---|---|---|
| `/models/<file>.pt` | `openring-models` volume | YOLO weights, mounted **read-only** in the detector |
| `/models/<file>.engine` | `openring-models` volume | TensorRT-compiled weights (advanced; see GPU section) |

The `openring-models` volume is shared between web (rw — for the
upload endpoint) and detector (ro — inference only). Anything you
drop into that volume becomes available to the detector on next
restart.

## Confidence threshold — start high, tune down

The v0.1 default is **0.40**, intentionally higher than ScarGuard's
0.25. A doorbell camera framing your porch sees:

- The actual visitor (the signal we want)
- The mailbox / wreath / decorative scarecrow (hopefully filtered out
  by the COCO `person` class — but YOLO does sometimes hallucinate
  people from busy backgrounds)
- The neighbor across the street walking their dog (a real person,
  but not someone you want notifications about)

The neighbor gets handled by **exclusion zones** (see
`docs/EXCLUSION_ZONES.md`, TBD). The hallucinated-person problem gets
handled by raising the threshold:

| Threshold | Behavior |
|---|---|
| 0.25 | ScarGuard's default; chatty for a doorbell |
| **0.40** | **OpenRing v0.1 default** — small false-positive rate |
| 0.55-0.65 | Recommended after a week of data if you still see FPs |
| 0.80+ | Effectively only fires on full-body adult day-shots |

Adjust per-camera if you have e.g. an interior side door at a
different angle than the front door:

```yaml
cameras:
  - name: front-door
    confidence_threshold: 0.55      # higher because porch is busy
    # ... rest of config
  - name: side-door
    # inherits global 0.40 — interior space, less clutter
```

The web UI's events list shows you confidence per detection; the
**Training Data** admin page graphs feedback (correct / false
positive / wrong class) by class so you can see whether you're
trending toward over- or under-tuned.

## GPU hosts: variant ladder

If your host has an NVIDIA GPU and you've enabled the NVIDIA Container
Runtime in your docker setup, the same `.pt` weights run dramatically
faster and you can afford a bigger variant. Pick by inference budget:

| Variant | File size | x86 + RTX 3060 (720p) | Person mAP |
|---|---|---|---|
| `yolov8n.pt` | 6 MB | 8-12 ms | 0.49 |
| `yolov8s.pt` | 22 MB | 12-18 ms | 0.59 |
| `yolov8m.pt` | 50 MB | 25-35 ms | 0.65 |
| `yolov8l.pt` | 84 MB | 40-60 ms | 0.69 |
| `yolov8x.pt` | 130 MB | 70-100 ms | 0.71 |

For a home doorbell at one camera, `yolov8s.pt` is the sweet spot
once you have any hardware acceleration — better recall on partial
visitors (someone half-occluded by a delivery box) without measurable
latency cost.

To use TensorRT-compiled weights (cuts inference 2-3x further on
NVIDIA), follow the [Ultralytics TensorRT
export guide](https://docs.ultralytics.com/integrations/tensorrt/)
to produce `yolov8s.engine`, drop it in the volume, and point
`model_path` at it. The detector handles `.pt` and `.engine`
identically at the API level.

## Fine-tuning on your own data

Once you've labeled some events as **Correct** / **False Positive** /
**Wrong Class** in the web UI, you can export them as a YOLO-format
dataset and fine-tune a person-and-front-door-specific model:

1. Web UI → **Admin → Training Data → Export YOLO dataset** (zip
   download with images, normalized bbox annotations, and
   `data.yaml`).
2. Fine-tune locally:
   ```bash
   pip install ultralytics
   yolo train model=yolov8n.pt data=data.yaml epochs=50 imgsz=640
   ```
   Outputs land in `runs/detect/train/weights/best.pt`.
3. Drop `best.pt` into the `openring-models` volume and update
   `detection.model_path`.
4. Restart the detector.

The same training script lives in `training/train.py` upstream in
ScarGuard; we'll port it into OpenRing if there's demand. For now the
two-line `yolo train` command above is enough.

A reasonable progression for a new install:

| Time on the porch | Model |
|---|---|
| Day 1 | Stock `yolov8n.pt`, threshold 0.40 |
| First weekend (50+ events labelled) | Raise threshold to 0.55 if you're still getting FPs |
| First month (500+ events labelled) | Fine-tune `yolov8n.pt` on your own data; expect a 5-15% drop in FP rate without losing real detections |
| Ongoing | Re-export and re-train every few months as seasons / lighting / your front yard change |

## Per-camera model overrides

Inherited from ScarGuard's `ModelPool`. Each camera can pin a
different model file:

```yaml
cameras:
  - name: front-door
    model_path: /models/best.pt      # your fine-tuned variant
  - name: side-door
    # uses global detection.model_path (yolov8n.pt)
```

Different models share `target_classes` unless overridden per camera
(`detect_classes`). The detector reference-counts loaded models so
two cameras using the same file share one in-memory instance.

## What we don't do

- **No on-Pi inference.** Even a Pi 5 + Hailo accelerator would do
  it, but OpenRing's v0.1 thin-doorbell architecture deliberately
  keeps every classification decision on the host. See
  `docs/ARCHITECTURE.md` for the why.
- **No cloud inference.** OpenRing's charter (`CLAUDE.md` §Charter)
  prohibits any outbound network call to a service we control. There
  is no `api.openring.dev/classify` endpoint and there will not be.
- **No automatic model updates.** The detector pins whatever you set
  `model_path` to. Drop in a new file, restart the detector. We do
  not push weights to your machine.

## Reference: COCO class IDs OpenRing cares about

The default model is COCO-trained, which has 80 classes. The ones
relevant to a front porch:

| ID | Name | Notes |
|---|---|---|
| 0 | `person` | the v0.1 default target |
| 16 | `dog` | useful if you want a "neighbor's dog walked through the camera" filter |
| 17 | `cat` | feline visitors |
| 14 | `bird` | seasonal porch traffic |

Add to `target_classes` to detect them:

```yaml
detection:
  target_classes: [person, dog, cat]
```

Per-class notification routing then lets you ignore raccoons but
get pinged for people:

```yaml
cameras:
  - name: front-door
    notification_rules:
      - class_name: person
        channels: [phone-ntfy, owner-email]
      - class_name: doorbell_press
        channels: [phone-ntfy, owner-email]
      - class_name: dog
        channels: []                # configured but suppressed — log only
      - class_name: "*"
        channels: [phone-ntfy]      # catch-all
```
