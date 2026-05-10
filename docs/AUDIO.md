# OpenRing — Two-way audio (v0.3)

Reference design for the audio path from the doorbell's mic to your
browser and from your browser to the doorbell's speaker. v0.3 ships
**half-duplex push-to-talk** — when the operator is talking, the Pi's
mic stops transmitting, and vice versa. Full-duplex with echo
cancellation is out of scope (see "Why half-duplex" below).

This doc is the contract between the three participants
(`services/doorbell-firmware/src/audio_relay.py`,
`services/audio-relay/`, browser code in `services/web/`). Anything
that breaks this contract is a behavioural change requiring a doc
update.

## Three components, two wires

```
┌─ Pi (audio_relay.py) ─┐    WS₁     ┌─ Host audio-relay ─┐    WS₂     ┌─ Browser ─┐
│                       │ ──Bearer──▶│                     │ ──JWT────▶│            │
│  arecord + opusenc    │ ◀────────  │  pairs sessions     │ ◀────────│  Web Audio │
│  opusdec + aplay      │   binary   │  pipes frames       │   binary  │   API      │
└───────────────────────┘   frames   └─────────────────────┘   frames  └────────────┘
```

- **WS₁: Pi ↔ host.** Long-lived. The Pi opens this when its service
  starts and reconnects on close. Auth: device Bearer token from
  `/etc/openring/secrets.env` — same token PR #1 already issues.
- **WS₂: host ↔ browser.** Short-lived (per push-to-talk session).
  Auth: short-lived JWT issued by the web service via
  `POST /api/audio/session` (admin-only). The JWT carries the device
  id the browser wants to talk to and an expiry ≤ 5 minutes.
- **The host audio-relay** is a stateless pairer. It maintains a map
  of `{device_id → connected Pi WS}` and a map of
  `{session_id → connected browser WS}`; on a matched pair it pipes
  frames in both directions. No transcoding. No buffering beyond a
  small jitter pool. No persistence.

## Wire format

Every WebSocket message is **binary**. The first byte is the frame
type. The rest is the payload.

| Type byte | Direction | Payload |
|---|---|---|
| `0x01` | client → host (both legs) | hello: 1 byte session_role (`0x01`=Pi, `0x02`=browser) + JSON metadata |
| `0x02` | bidirectional | opus audio frame (raw, not encapsulated) — 20 ms @ 16 kHz mono |
| `0x03` | bidirectional | state change — 1 byte: `0x01`=I'm-about-to-talk, `0x02`=I'm-done-talking |
| `0x04` | host → client | error: 1 byte error code + utf-8 reason |
| `0x05` | bidirectional | ping/pong keepalive: 8 bytes echoed back |

Why custom binary instead of JSON-wrapped Opus? Opus frames are
already small (~80-160 bytes at 16 kbps), and a single byte of
type-discrimination keeps the per-frame overhead at 1 byte instead
of ~30 for a JSON envelope. Browsers ship `WebSocket` with a binary
mode, the Pi side uses Python's `websockets` library which handles
binary natively.

### Hello frame

```
0x01 0x01 {"version":"0.3","device_id":"front-door"}      # Pi side
0x01 0x02 {"version":"0.3","jwt":"eyJ..."}                 # browser side
```

The host validates the JWT *or* device token at `0x01` time. On
acceptance it replies with another `0x01` carrying its own metadata.
On rejection it replies with `0x04` and closes.

### Audio frame

```
0x02 <opus payload bytes>
```

Opus parameters fixed at:

- 16 kHz sample rate
- 1 channel (mono — voice intelligibility, half the bandwidth of
  stereo, matches the cheap doorbell mics)
- 20 ms frame size (16 kHz × 0.020 s = 320 samples)
- Raw frame, no Ogg container

Bandwidth: ~16 kbps payload + framing = ~20 kbps each direction.
Trivial on a LAN.

### State frame

The half-duplex coordinator. The browser sends `0x03 0x01`
("I'm-about-to-talk") when the operator presses the talk button. The
host forwards it to the Pi, which mutes its mic and unmutes its
speaker. When the operator releases the button, the browser sends
`0x03 0x02` and the Pi flips back. Same pattern in reverse if the
operator is in "listen" mode.

Why explicit signalling rather than activity detection? Activity
detection requires a VAD (voice activity detector) — extra CPU,
extra dependency, and a class of "talked but didn't get heard"
failure modes that's awful to debug. An explicit state frame is
~1 byte and impossible to misread.

### Error codes

| Code | Meaning |
|---|---|
| `0x01` | Auth failure (bad JWT, bad Bearer, expired) |
| `0x02` | Device not connected — no Pi WS for the requested device_id |
| `0x03` | Already paired — another browser already holds a session for this device |
| `0x04` | Protocol violation (unknown type byte, malformed hello, etc.) |
| `0x05` | Internal error (host misbehaved) |

## Auth

### Pi → host (WS₁)

Standard Bearer auth on the upgrade request:

```
GET /audio/device HTTP/1.1
Host: openring.local
Authorization: Bearer <device_token>
Upgrade: websocket
```

The host validates against `auth_module.validate_device_token` (same
helper PR #1 ships). Connection refused with HTTP 401 on bad token.

### Browser → host (WS₂)

The browser asks the web service for an audio session token:

```
POST /api/audio/session HTTP/1.1
Cookie: session=<existing-admin-session>
Content-Type: application/json

{"device_id": "front-door"}
```

Response:

```json
{
  "audio_url": "wss://openring.local/audio/browser?token=<jwt>",
  "expires_at": "2026-05-09T19:32:11Z"
}
```

The JWT carries:
- `iss` — `"openring-web"`
- `aud` — `"openring-audio"`
- `sub` — admin username
- `device_id` — the device the browser wants to talk to
- `exp` — issued time + 5 minutes
- `jti` — random session id, one-time-use

Signed with the same `OPENRING_AUDIO_KEY` (new — generated by
`setup.sh`, distributed via `.env` like `DETECTION_HMAC_KEY`). The
host audio-relay verifies on connect.

JWT instead of session cookie because the browser opens the WS as a
totally fresh connection and we don't want to share session cookies
across a port that doesn't speak HTTP. Also a JWT can be invalidated
by changing the signing key (rotation), and doesn't require touching
auth.db on every connect.

### Why one-shot tokens

The `jti` claim plus the audio-relay's in-memory used-jti set means
each JWT works exactly once. A captured token can't be replayed even
if it hasn't expired yet. Cost: a tiny hashtable on the audio-relay,
cleared at process restart (acceptable — a captured 5-min-old token
that survives a restart is a 5-min window of replay; the operator's
session and password are unchanged). The trade-off is documented; if
operators want stricter, sticky-session memcached/Redis-backed
revocation is a v0.4 follow-up.

## Half-duplex state machine

```
              ┌────────────── IDLE ─────────────┐
              │                                 │
              ▼                                 │
  browser sends 0x03 0x01            Pi sends 0x03 0x01
  (push-to-talk pressed)             (listen toggle on host)
              │                                 │
              ▼                                 ▼
       BROWSER_TALKING                    PI_TALKING
       Pi: mic muted,                     Browser: mic muted,
           speaker open                       speaker open
              │                                 │
       browser releases                Pi sends 0x03 0x02
       (or 30s safety timeout)         (or 30s safety timeout)
              │                                 │
              └──────────────► IDLE ◄───────────┘
```

Either side can request a state transition. The host audio-relay is
the arbiter — if browser and Pi both try to grab the floor in the
same second, whichever message arrives first wins. The loser's
`0x03 0x01` is rejected with a `0x04 0x06` (new code: "floor busy").

Both sides have a 30-second talk-time safety. The host sends
`0x03 0x02` to both ends at 30 seconds regardless of who held the
floor. Prevents a stuck half-duplex from a crashed browser tab from
holding the doorbell hostage.

## Why half-duplex (and not full-duplex like a real doorbell)

Three reasons:

1. **Echo cancellation is hard.** Without it, full-duplex on cheap
   USB DACs leads to feedback howls. WebRTC includes AEC; rolling
   our own on a Pi Zero 2 W class device is a months-long project.
2. **The doorbell use case is fine with half-duplex.** "Hi, I'll be
   right down" → release. "Sure, take care" → release. The two
   parties don't actually need to be talking simultaneously the
   way a phone call does.
3. **Push-to-talk is a clearer UX.** The big honking button removes
   ambiguity about whether the mic is hot. A privacy property: if
   the browser tab is in the background, the mic isn't open.

Full-duplex via WebRTC + ICE over the LAN is a v0.4+ track if there's
demand.

## Audio pipeline on each side

### Pi (audio_relay.py)

**Outgoing (mic → host):**

```
arecord -f S16_LE -r 16000 -c 1 --buffer-size=1024 |
  opusenc --bitrate 16 --raw --raw-rate 16000 --raw-chan 1 - - |
  websocket-write
```

Frames pulled in 320-sample (20 ms) chunks; opusenc emits one Opus
frame per chunk. Each frame becomes one `0x02` WS message.

**Incoming (host → speaker):**

```
websocket-read 0x02 frames |
  opusdec --raw --rate 16000 - - |
  aplay -f S16_LE -r 16000 -c 1
```

Both pipelines are coroutines around `arecord`/`aplay` subprocesses
(same pattern as the segmenter's ffmpeg subprocess). When the half-
duplex state forbids transmit, the mic pipeline still consumes
PCM from `arecord` (so the buffer doesn't fill) but drops it before
encode. When transmit is allowed but the speaker is forbidden,
incoming `0x02` frames are dropped at the WebSocket reader.

### Host audio-relay (services/audio-relay/)

Pure forwarder. No codec touches. Maintains:

- `_pi_sessions: dict[device_id, WebSocketServerProtocol]`
- `_browser_sessions: dict[session_id, (device_id, WebSocketServerProtocol)]`
- `_floor_holder: dict[device_id, "browser"|"pi"|None]`
- `_used_jtis: set[str]` (for one-shot enforcement)

Every received `0x02` frame is sent unchanged to the paired peer
**iff** the floor allows it. Every `0x03` arbitrates and broadcasts.

### Browser (services/web/src/static/audio.js)

Web Audio API for both directions:

**Outgoing (mic → host):**

```javascript
navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, sampleRate: 16000}})
  .then(stream => {
    // Resample to 16kHz mono if the device returned higher
    // Encode 20ms frames with the AudioWorklet's Opus binding
    // (see audio.js for the actual code)
    // Send each frame as 0x02 over the WebSocket
  })
```

**Incoming (host → speaker):**

```javascript
ws.onmessage = ev => {
  if (frameType(ev.data) === 0x02) {
    const opusBytes = ev.data.slice(1);
    decoder.decode(opusBytes);  // emits PCM
    audioContext.write(pcm);
  }
};
```

Opus decode in the browser uses the standard `webcodecs` API
(`AudioDecoder`) — Chrome 94+, Safari 17+, Firefox 130+. Falls back
to a small WASM-bundled libopus for older browsers. Both decoders
ship with similar latency profiles (~5 ms) and the AudioWorklet path
keeps it off the main thread.

## What a successful exchange looks like (operator view)

1. Visitor at the door rings the bell. Browser receives a normal
   doorbell-press notification.
2. Operator opens the events page and clicks **Talk** next to the
   live event.
3. Browser asks `/api/audio/session` for a JWT, opens WS₂.
4. Browser sends `0x03 0x01`, holds the talk button down.
5. Mic frames flow Pi-side → speaker. ~150 ms end-to-end latency on
   a quiet LAN.
6. Operator releases the talk button → `0x03 0x02` → idle.
7. Operator may toggle a **Listen** button to flip the floor the
   other way; same protocol in reverse.
8. Closing the browser tab tears down WS₂. Pi remains connected;
   host removes the browser session and tells the Pi `0x03 0x02`.

## Failure modes

| Failure | Behavior |
|---|---|
| Browser tab closes mid-talk | Host sends `0x03 0x02` to Pi; floor released |
| Pi loses WS₁ | Host returns `0x04 0x02` (device-not-connected) on any browser session targeting it |
| 30s talk safety timeout | Host sends `0x03 0x02` to both ends |
| JWT expired | Host returns `0x04 0x01` and closes WS₂ |
| Browser tries to grab the floor while Pi holds it | Host `0x04 0x06` (floor busy) |
| RTP-style packet loss (UDP-ish) | Not applicable — TCP/WebSocket retransmits; you'll get a hiccup, not a glitch |
| Audio device unplugged on Pi mid-session | Pi exits the recording subprocess, sends `0x03 0x02`, host cascades |

## What we deliberately don't do

- **No call recording.** Audio is forwarded frame-by-frame; the host
  doesn't write anything to disk. This is a Charter call: footage
  the operator deliberately captures (snapshot, clip) is one thing;
  *every porch conversation* is another.
- **No remote-access magic.** Same as the rest of OpenRing: if you
  want the talk button to work from outside your LAN, run Tailscale
  and route through there.
- **No Pi-side echo cancellation.** See "Why half-duplex" above.
- **No SIP/RTP.** WebSockets only. Browsers can't open raw UDP, and
  the audio-relay's stateless pairer is far simpler than an SIP
  proxy.

## File map

```
docs/AUDIO.md                                    this doc
services/audio-relay/
├── Dockerfile
├── entrypoint.sh
├── requirements.txt
├── src/
│   ├── main.py                                  WS server + pairer
│   ├── auth.py                                  JWT verify + token
│   ├── frames.py                                wire-format codec
│   └── floor.py                                 half-duplex arbiter
└── tests/
services/doorbell-firmware/src/audio_relay.py    Pi-side WS client
services/web/src/routes/audio.py                 POST /api/audio/session
services/web/src/static/audio.js                 talk + listen buttons
```

## v0.3 acceptance criteria

The following demos work end-to-end on a real Pi Zero 2 W with a
USB DAC + USB mic:

- [ ] Press the doorbell button, get notification, click **Talk**,
      hold while saying "Hi, I'll be right down" — Pi speaker plays
      it within ~250 ms of release.
- [ ] Toggle **Listen** with the visitor still at the door — voice
      audible in the browser, half-duplex floor blocks accidental
      mic capture.
- [ ] Tear down the browser tab during a talk — Pi recovers within
      1 second.
- [ ] 30s safety timeout fires when the operator's tab is locked
      with the button held.
- [ ] At least two USB DAC + mic combinations confirmed (see
      [HARDWARE.md](HARDWARE.md) v0.3 addendum).
