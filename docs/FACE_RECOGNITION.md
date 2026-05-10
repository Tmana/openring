# OpenRing — Face Recognition (v0.4)

Off by default. Local-only. Embeddings live in the data directory, never
leave the host.

This document is the design contract for the v0.4 milestone. It covers
**what face data we ask the user to provide**, **how that data turns into
"suppress this alert" / "escalate this alert" decisions**, and **what
guarantees we make about the data** once it's enrolled.

The feature ships in three stacked PRs:

- **PR-A — recognizer sidecar.** Schema, embeddings, Redis publish.
  *No UI. No notification changes.* This document and the
  `services/recognizer/` directory.
- **PR-B — enrollment UI.** Upload photos, label faces, manage
  enrolled identities. Web admin pages only — no behavior change for
  alerts. Web saves photo + `known_faces` row, then publishes
  `openring:enrollment` (HMAC-signed) asking the recognizer to embed.
  The recognizer also runs a startup catch-up sweep so a missed
  message during a recognizer restart self-heals on next boot.
- **PR-C — notifier rules.** Suppression and escalation. Read the
  recognition results PR-A wrote and let the user wire them to
  per-face channel sets in `openring.yml`.

Each PR is independently reviewable and the system is functional after
each merge — PR-A produces recognition rows that nothing reads,
PR-B lets you enroll without anything firing, PR-C closes the loop.

---

## 1. What this is for

Two user-visible outcomes, in the order the user asked for them:

1. **Suppression.** When a recognized household member arrives, OpenRing
   stays quiet. No phone buzz, no Discord message — but the event is
   still in the events page with the matched face label, so you can
   review later.
2. **Escalation.** When a specific recognized face arrives — or when an
   *unrecognized* face arrives in a configured "stranger danger" mode —
   OpenRing fires a louder, multi-channel alert. The escalation can
   include high-priority webhooks the user has wired to their own auto-
   call provider (Twilio, SignalWire, a smart-home hub).

Plus an everyday quality-of-life win: notifications can read **"Sarah is
at the front door"** instead of **"Person at the front door"** when a
face is enrolled.

This is not biometrics-as-authentication. We do not unlock anything,
we do not authorize anything, and we do not feed face data to any
external system. A face is just a label that lets the user write better
notification rules.

---

## 2. What face data we ask for

### 2.1 Per enrolled person — the minimum

| Field | Required | Why |
|---|---|---|
| **Display label** | yes | Shows in notifications and the events page (`Sarah`, `Mom`, `mail carrier`). |
| **3–5 reference photos** | yes (≥3) | One photo gives a single embedding, which is brittle to glasses / lighting / haircut. Three to five gives a reliable cluster. Above five returns diminishing accuracy gains. |
| **Notes** (free text) | optional | "wears glasses, has been Mom since the 90s" — for the user, not the matcher. |
| **Enabled** (bool) | yes | Soft-delete. Lets you keep the embeddings (so re-enrolling later is one click) without the recognizer firing matches. |

### 2.2 Reference-photo guidance (surfaced in the enrollment UI)

- **Resolution:** ≥ 256 × 256 pixels covering the face. The face-detector
  upscales smaller crops but accuracy suffers.
- **Variety:** different angles, lighting, days. Five photos taken in
  the same selfie session are weaker than five taken across a week.
- **Single subject:** one face per reference photo. The enrollment
  pipeline rejects photos with multiple detected faces (the user picks
  again), to avoid silently embedding the wrong person.
- **No masks / sunglasses on the references**: those occlusions tank
  embedding quality. Day-of-event matches against masked faces are
  fine — that's what we score against.

### 2.3 What we extract and persist

For each reference photo, the recognizer computes a fixed-size embedding
vector (128-D, `float32`, ~512 bytes raw) and stores it in
`recognizer.db` keyed to the enrolled face. **The reference photo
itself is also stored**, in `/data/face-references/<face-id>/<n>.jpg`,
because:

- The user expects to be able to see "what photos did I upload for
  Mom?" in the enrollment UI.
- Re-embedding becomes possible if we ever upgrade the embedding model.

The user can wipe a face entirely (photos + embeddings) from the
enrollment UI — soft-delete clears matching, hard-delete is a separate
button that asks for confirmation.

### 2.4 What we do **not** ask for and never store

- No date of birth, no relationship metadata (parent / spouse /
  contractor) beyond the free-text notes the user writes.
- No phone numbers, email addresses, or other identifiers tied to the
  enrolled face. Notifications fan out per-face *channel sets* the user
  configures — the channels are the user's, not the face's.
- No demographic guesses (age, gender, ethnicity). The model is
  embedding-only. We do not load a demographic head.
- No outbound network calls. The reference photos and embeddings are
  on the host, full stop. There is no "cloud sync" toggle. If you
  back up the host, you back up your enrollments; if you don't, you
  don't. We don't help you do it for you.

### 2.5 Consent model

OpenRing is installed by one user (the homeowner) for one location
(their porch). The face data the user enrolls is data **about other
people** — the user's family, neighbours, deliveries — not about the
user themselves.

The enrollment UI surfaces this honestly: "You are about to ask
OpenRing to recognize *Sarah*. Sarah should know about this. We
recommend you tell her, and that you delete her photos if she asks
you to." This is a copy-and-conscience problem, not a technical one,
and we don't pretend technology fixes it. It's a doorbell, not a
surveillance system, and it's only as well-mannered as the operator.

For domestic North-American use this is the same posture as a wired
Ring doorbell with the recordings going to the homeowner's NAS, except
the user runs the matcher rather than Amazon. Operators in
jurisdictions with stronger biometric-data rules (Illinois BIPA, EU
GDPR Art. 9) should consult their local rules before enrolling any
face that isn't their own.

---

## 3. Architecture

### 3.1 The recognizer sidecar

```
                 openring:detections (HMAC)
                            │
                            ▼
               ┌──────────────────────────┐
   detector ──▶│   recognizer (sidecar)   │──▶ recognizer.db
                └──────────────┬───────────┘
                               │
                               ▼
               openring:recognition (HMAC)
                               │
                               ▼
                          notifier
                          web (events page rendering)
```

`services/recognizer/` is a new sidecar — same shape as `clipper` and
`notifier`. It:

1. Subscribes to `openring:detections` and verifies HMAC.
2. Filters: only events whose class is in
   `face_recognition.trigger_classes` (default `["person"]`) above
   `face_recognition.min_confidence`.
3. Loads the snapshot from `/data/snapshots/<feedback_token>.jpg`,
   cropped to the bbox + a configurable padding margin.
4. Runs face detection on the crop. Zero faces ⇒ writes a `no_face`
   recognition row and bows out. One or more faces ⇒ continues.
5. Computes embeddings, scores against every enabled enrolled face's
   embedding cluster (cosine similarity / euclidean depending on
   `face_recognition` library; we use the `face_recognition` PyPI
   package's distance for v0.4).
6. Picks the best match if its score ≤ `tolerance` (default 0.6 in
   library terms — lower distance is closer match), else flags as
   `unknown`.
7. Persists to `recognizer.db` and publishes on `openring:recognition`
   with HMAC.

All steps that touch the bbox and snapshot file *path* validate the
`feedback_token` against the same `^[a-zA-Z0-9_-]{8,128}$` regex the
clipper uses. Path-traversal hygiene, not optional.

### 3.2 Why a separate SQLite DB

OpenRing's "single writer per table" rule is the load-bearing reason we
use SQLite at all. Detector owns `detection_events`. Clipper owns
`clips` (in the same `openring.db` because the JOIN was unavoidable).
Web owns the `feedback` UPDATE-only path.

The recognizer's writes are orthogonal to the events page render —
the web service can JOIN at render time, or the notifier can read
recognition rows without ever entering `openring.db`. So the
recognizer gets its own DB at `/data/recognizer.db`:

- `known_faces` — one row per enrolled identity
- `face_embeddings` — many rows per `known_faces.id`
- `recognitions` — one row per processed detection event

Web reads `recognizer.db` read-only via a second `sqlite3.connect` for
the events-page join. No write contention is possible because the
recognizer is the only writer.

### 3.3 Schema (ships in PR-A)

```sql
CREATE TABLE IF NOT EXISTS known_faces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,           -- soft-delete bit
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_known_faces_label_unique
    ON known_faces(LOWER(label));

CREATE TABLE IF NOT EXISTS face_embeddings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    face_id       INTEGER NOT NULL REFERENCES known_faces(id) ON DELETE CASCADE,
    embedding     BLOB NOT NULL,                      -- 128 float32 = 512 bytes
    source_image  TEXT NOT NULL,                      -- relative to /data/face-references/
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_face ON face_embeddings(face_id);

CREATE TABLE IF NOT EXISTS recognitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_token  TEXT NOT NULL UNIQUE,             -- joins to detection_events
    camera_name     TEXT NOT NULL,
    status          TEXT NOT NULL,                    -- matched | unknown | no_face | error
    face_id         INTEGER,                          -- null for non-match
    label           TEXT,                             -- denormalised for fast read
    score           REAL,                             -- distance, lower=closer
    bbox            TEXT,                             -- JSON [x1,y1,x2,y2] of the matched face
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recognitions_token ON recognitions(feedback_token);
CREATE INDEX IF NOT EXISTS idx_recognitions_face  ON recognitions(face_id);
```

`recognitions` rows are written for every processed event including
ones that found no face — that way the web events-page join always
distinguishes "we looked, found nothing" from "we never looked"
(recognizer disabled or service down).

### 3.4 Library choice

For v0.4 we use the [`face_recognition`](https://github.com/ageitgey/face_recognition)
PyPI package. It wraps `dlib`, ships ~99% LFW accuracy, and the API
is two function calls: `face_locations()` and `face_encodings()`. The
embedding is 128-D float32. CPU-only is the supported target.

Trade-offs we accepted:
- Slower than InsightFace ArcFace (`~50ms vs ~20ms` per face on a
  modern x86 CPU, similar on a Pi 5 host).
- Slightly less robust to extreme angles. Fine for the porch use case.
- `dlib` is a chunky compile dependency. The Dockerfile pins a known-
  good `dlib` wheel index to avoid rebuild churn.

We avoided InsightFace for v0.4 because its ONNX-runtime model files
add ~250 MB to the image and its Python binding is more brittle. We
can swap to it in v0.5 if accuracy in the field disappoints — the
embedding column is just bytes, and re-embedding from the stored
reference photos is a one-time job.

---

## 4. The rules language

`openring.yml` grows a `face_recognition:` section. Two parts: the
recognizer's own knobs, and a `rules` block consumed by the notifier
in PR-C.

```yaml
face_recognition:
  enabled: false                     # opt-in. recognizer container is a no-op when off.
  trigger_classes: [person]
  min_confidence: 0.40               # only run on detector events ≥ this score
  bbox_padding_pct: 0.20             # widen the bbox before face-detection
  tolerance: 0.6                     # face_recognition lib distance; lower = stricter
  max_concurrent_workers: 2

  # Per-label rule set. First-match-wins. Two special labels:
  #
  #   "*"        — any matched-known face (fallback when the specific
  #                label has no rule of its own)
  #   "unknown"  — a face was detected but did not match any known face
  #
  # `channels: []`         — suppress all notifications for this label
  #                          (the event still appears on the events page).
  # `channels: [a, b, ...]` — fire these channel names from the
  #                           notifications.channels list.
  # `priority: high|normal` — optional override for ntfy priority and
  #                           the "Subject" formatting in email.
  rules:
    - label: "Sarah"
      channels: []                   # household — suppress
    - label: "delivery-bob"
      channels: [phone-ntfy]         # known but worth a quiet ping
      priority: normal
    - label: "ex-roommate"
      channels: [phone-ntfy, owner-email, panic-webhook]
      priority: high                 # ESCALATE — multi-channel + auto-call hook
    - label: "unknown"
      channels: [phone-ntfy]         # the default for any stranger
    - label: "*"
      channels: [phone-ntfy]         # any-known fallback
```

Three properties that fall out of this design:

- **Suppression is a first-class outcome**, not a switch. Empty list →
  no fanout. The user sees the matched face on the events page; their
  phone never buzzes.
- **Escalation is just a longer channel list.** "Auto-call" isn't a
  built-in OpenRing feature — the user wires a webhook channel
  (`type: webhook`) to their own Twilio Function or Home Assistant
  automation that dials their phone. We stay out of the calling
  business; the user owns that integration and its costs.
- **Rule precedence matches the existing per-camera `notification_rules`
  schema** (first-match-wins, `*` catch-all). Same mental model the user
  already learned in v0.1.

When *both* a per-camera `notification_rules` entry AND a
`face_recognition.rules` entry match the same event, the **face rule
wins** (it's the more specific signal). PR-C documents the precedence
table explicitly.

### 4.1 Rule evaluation in the notifier (PR-C preview)

PR-C touches `services/notifier/src/main.py` to:

1. Subscribe to a new internal channel `openring:recognition` *in
   addition to* `openring:detections`.
2. Buffer recognition results keyed by `feedback_token` for a short
   coalescence window (~2 s) so the recognition row arrives before
   the detection notification dispatches.
3. When dispatching, look up `recognitions[feedback_token]`:
   - if `status == matched` → consult `face_recognition.rules` first,
     fall back to `notification_rules` only if no rule matches.
   - if `status == unknown` and there's an `unknown` rule → use it.
   - if `status == no_face` or `error` → fall straight through to the
     existing `notification_rules` path (this is the only way the
     notifier can degrade gracefully when the recognizer is off).

The buffering is a coalescence-or-bust window: if no recognition
arrives in 2 s, the notifier dispatches via the existing path. We will
*never* hold a doorbell-press notification waiting for a face match —
the press is the higher-priority event.

PR-C ships behind a notifier feature flag (`face_recognition.face_rules_enabled`,
default false) so the operator can roll the recognizer out and watch
its rows for a week before changing notification behavior.

---

## 5. Privacy posture

We document this loudly because biometric data has a higher floor of
care than CCTV footage.

### 5.1 What stays local

- The reference photos and the 128-D embeddings.
- Every `recognitions` row.
- Snapshots and clips of recognized events (same retention rules as
  any other event).

### 5.2 What never goes anywhere

- We do not call any API. The matching runs in the recognizer
  container. There is no "cloud face match" feature flag, even off-by-
  default. See the Charter in `CLAUDE.md`.
- We do not collect telemetry of any kind, so face labels do not
  appear in any aggregate we send. Labels do appear on the *internal*
  Redis bus (`openring:recognition` payloads) and in the recognizer
  container's *DEBUG* log lines — both stay on the host. Operators
  who pipe container logs to a remote log aggregator should be aware
  that DEBUG-level logs include face labels; INFO and above only
  reference the opaque `face_id` integer.
- We do not write face labels into webhook payloads *unless* the user's
  configured channel is the destination — i.e. webhooks dispatched on
  behalf of a matched face carry the label, but we never sneak labels
  into channels the user hasn't wired.

### 5.3 Retention and deletion

- Reference photos and embeddings persist until the operator deletes
  them (soft-delete keeps the data; hard-delete erases it).
- `recognitions` rows are pruned by the existing retention engine
  (`system.retention_days`) the same way `detection_events` are. A
  row pinned to a feedback-labelled event survives indefinitely, same
  rule.
- We do not write face data into `audit_log.db`. Audit log records who
  enrolled a face and when, but never the embedding bytes.

### 5.4 Backup hygiene

If you `tar -czf` your `/data` directory, the recognizer DB and the
reference-photo directory are part of the backup. If you ship that
backup to S3, your enrolled faces ride along. **This is the operator's
choice; we do not add a built-in backup feature.** When PR-B ships the
enrollment UI, the page header carries a one-line reminder:

> "These photos and the embeddings derived from them are stored in
> `/data/`. They will be included in any backup of that directory."

Nothing fancier. The operator is an adult.

---

## 6. Failure modes

| Failure | Behavior |
|---|---|
| `face_recognition.enabled: false` | recognizer container starts and idles — no DB writes, no Redis publishes. |
| Recognizer down, detector up | detection events flow normally. notifier falls through to `notification_rules` for every event. |
| Snapshot file missing | recognition row written with `status=error, error="snapshot missing"`. notifier falls through. |
| Detector publishes faster than recognizer consumes | bounded `ThreadPoolExecutor(max_workers=face_recognition.max_concurrent_workers)`; queued jobs drop when full and a `dropped_for_backpressure` warning logs. We do not block the subscribe loop. |
| `dlib` model file missing on first start | service logs an error, exits, restart-policy retries — same shape as detector's YOLO-weights-missing failure today. |
| Two enrolled faces with the same label | UNIQUE index on `LOWER(label)` rejects the duplicate at enrollment time. |
| User uploads a reference photo with two faces | enrollment UI rejects with "Multiple faces detected; please upload a clearer photo." |
| Matched face below `tolerance` but very close | match wins; we do not do an "ambiguous" middle category in v0.4. |

---

## 7. Out of scope for v0.4

- **Face clustering on unknown faces.** Auto-suggesting "you've seen
  this stranger 3 times this week, want to label them?" is a v0.5+
  idea. Nice for a delivery-driver workflow.
- **Per-camera face rules.** The v0.4 rules are global. A v0.5 PR can
  add `cameras[].face_rules` if anyone wants front-vs-back-door
  differentiation.
- **Liveness detection.** OpenRing isn't auth — a held-up photo of
  Sarah would match. That's intentional; we don't want to be in the
  liveness arms race for a doorbell.
- **Multiple-face-in-frame escalation.** The recognizer scores the
  highest-confidence face and uses its label. If three people walk up
  and one is unknown, the rule for the recognized one wins. Fixing
  this properly needs multi-face semantics in the rules language;
  parked.
- **GPU inference path.** CPU-only for v0.4. The dlib build supports
  CUDA but the wheel doesn't, and the v0.4 user is the home-server
  operator, not someone with an idle 4090.
