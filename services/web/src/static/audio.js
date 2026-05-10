// OpenRing — browser-side audio relay client.
//
// Implements the wire protocol from docs/AUDIO.md:
//   * 0x01 HELLO — sent on connect
//   * 0x02 AUDIO — raw 20ms Opus frame @ 16kHz mono, both directions
//   * 0x03 STATE — take/release floor (half-duplex)
//   * 0x04 ERROR — code + reason from host
//   * 0x05 PING  — echoed back as PONG
//
// Uses WebCodecs for Opus encode/decode (Chrome 94+, Safari 17+,
// Firefox 130+).  Older browsers see a feature-detect failure and the
// Talk/Listen buttons stay disabled with a clear tooltip.
//
// The OpenRingAudio class is the public surface.  Two instances coexist
// fine; OpenRing only ever creates one at a time.

(function () {
    'use strict';

    const FrameType = {
        HELLO: 0x01,
        AUDIO: 0x02,
        STATE: 0x03,
        ERROR: 0x04,
        PING:  0x05,
    };
    const Role = { PI: 0x01, BROWSER: 0x02 };
    const StateOp = { TAKE: 0x01, RELEASE: 0x02 };

    const SAMPLE_RATE = 16000;
    const CHANNELS = 1;
    const FRAME_SAMPLES = 320;          // 20ms @ 16kHz

    function supported() {
        return (
            'AudioEncoder' in window &&
            'AudioDecoder' in window &&
            'AudioData' in window &&
            navigator.mediaDevices &&
            typeof navigator.mediaDevices.getUserMedia === 'function'
        );
    }

    function helloFrame(jwt) {
        const meta = JSON.stringify({ version: '0.3', jwt: jwt });
        const body = new TextEncoder().encode(meta);
        const out = new Uint8Array(2 + body.length);
        out[0] = FrameType.HELLO;
        out[1] = Role.BROWSER;
        out.set(body, 2);
        return out;
    }

    function stateFrame(op) {
        return new Uint8Array([FrameType.STATE, op]);
    }

    function audioFrame(opusBytes) {
        const out = new Uint8Array(1 + opusBytes.byteLength);
        out[0] = FrameType.AUDIO;
        out.set(new Uint8Array(opusBytes), 1);
        return out;
    }

    function decodeFrame(buf) {
        const u8 = new Uint8Array(buf);
        if (u8.length < 1) return null;
        const t = u8[0];
        const body = u8.subarray(1);
        if (t === FrameType.AUDIO)  return { type: 'audio', payload: body };
        if (t === FrameType.STATE)  return { type: 'state', op: body[0] };
        if (t === FrameType.HELLO)  return { type: 'hello' };
        if (t === FrameType.ERROR) {
            const code = body[0];
            const reason = new TextDecoder().decode(body.subarray(1));
            return { type: 'error', code, reason };
        }
        if (t === FrameType.PING)   return { type: 'ping', token: body };
        return null;
    }

    class OpenRingAudio {
        // events: 'paired' | 'connecting' | 'closed' | 'error' (with detail)
        //         'floor-taken' | 'floor-released' (browser side only)
        constructor() {
            this.ws = null;
            this.encoder = null;
            this.decoder = null;
            this.audioCtx = null;
            this.mic = null;
            this.processor = null;     // ScriptProcessorNode for capture
            this.playbackQueue = [];   // Array<AudioBuffer>
            this.playbackTime = 0;
            this.listeners = Object.create(null);
            this.holdsFloor = false;
            this.peerHoldsFloor = false;
        }

        on(name, cb) { (this.listeners[name] ||= []).push(cb); }
        _emit(name, detail) {
            for (const cb of (this.listeners[name] || [])) {
                try { cb(detail); } catch (e) { console.error(e); }
            }
        }

        async connect(audioUrl) {
            if (!supported()) throw new Error('audio not supported in this browser');
            if (this.ws) await this.disconnect();

            this._emit('connecting');
            const ws = new WebSocket(audioUrl);
            ws.binaryType = 'arraybuffer';
            this.ws = ws;
            return new Promise((resolve, reject) => {
                ws.onopen = () => {
                    // The audio_url already carries the JWT in ?token=…;
                    // we still send a HELLO so the host knows we're here.
                    ws.send(helloFrame(''));
                };
                ws.onmessage = ev => {
                    const frame = decodeFrame(ev.data);
                    if (!frame) return;
                    if (frame.type === 'hello') {
                        this._emit('paired');
                        resolve();
                    } else if (frame.type === 'audio') {
                        this._handleIncomingAudio(frame.payload);
                    } else if (frame.type === 'state') {
                        if (frame.op === StateOp.TAKE) {
                            this.peerHoldsFloor = true;
                            this._emit('peer-talking');
                        } else {
                            this.peerHoldsFloor = false;
                            this._emit('peer-released');
                        }
                    } else if (frame.type === 'error') {
                        this._emit('error', { code: frame.code, reason: frame.reason });
                        if (frame.code === 0x01 /* AUTH */) {
                            ws.close();
                            reject(new Error(`audio auth failed: ${frame.reason}`));
                        }
                    } else if (frame.type === 'ping') {
                        // Echo back
                        const out = new Uint8Array(1 + frame.token.length);
                        out[0] = FrameType.PING;
                        out.set(frame.token, 1);
                        ws.send(out);
                    }
                };
                ws.onerror = ev => {
                    this._emit('error', { reason: 'websocket error' });
                    reject(new Error('websocket error'));
                };
                ws.onclose = () => {
                    this._emit('closed');
                    this.ws = null;
                    this._teardownAudio();
                };
            });
        }

        async disconnect() {
            if (this.holdsFloor) await this.releaseFloor();
            if (this.ws) {
                try { this.ws.close(); } catch (e) {}
                this.ws = null;
            }
            this._teardownAudio();
        }

        async takeFloor() {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                throw new Error('not connected');
            }
            this.ws.send(stateFrame(StateOp.TAKE));
            this.holdsFloor = true;
            this._emit('floor-taken');
            await this._startMic();
        }

        async releaseFloor() {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(stateFrame(StateOp.RELEASE));
            }
            this.holdsFloor = false;
            this._emit('floor-released');
            this._stopMic();
        }

        // ── Mic / encode ──────────────────────────────────────────────

        async _startMic() {
            if (this.mic) return;
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: CHANNELS,
                    sampleRate: SAMPLE_RATE,
                    echoCancellation: false,
                    noiseSuppression: false,
                    autoGainControl: true,
                },
            });
            this.mic = stream;

            this.audioCtx ||= new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: SAMPLE_RATE,
            });

            this.encoder = new AudioEncoder({
                output: chunk => {
                    if (!this.holdsFloor || !this.ws ||
                        this.ws.readyState !== WebSocket.OPEN) return;
                    const buf = new ArrayBuffer(chunk.byteLength);
                    chunk.copyTo(buf);
                    this.ws.send(audioFrame(buf));
                },
                error: e => this._emit('error', { reason: `encoder: ${e.message || e}` }),
            });
            await this.encoder.configure({
                codec: 'opus',
                sampleRate: SAMPLE_RATE,
                numberOfChannels: CHANNELS,
                bitrate: 16000,
            });

            const source = this.audioCtx.createMediaStreamSource(stream);
            const processor = this.audioCtx.createScriptProcessor(FRAME_SAMPLES, CHANNELS, CHANNELS);
            this.processor = processor;
            processor.onaudioprocess = ev => {
                if (!this.holdsFloor) return;
                const channel = ev.inputBuffer.getChannelData(0);
                // AudioData wants interleaved Float32 + an explicit
                // timestamp; we use rolling 20ms increments.
                const ts = Math.round(this.audioCtx.currentTime * 1e6);
                const ad = new AudioData({
                    format: 'f32-planar',
                    sampleRate: SAMPLE_RATE,
                    numberOfFrames: channel.length,
                    numberOfChannels: 1,
                    timestamp: ts,
                    data: channel,
                });
                this.encoder.encode(ad);
                ad.close();
            };
            source.connect(processor);
            processor.connect(this.audioCtx.destination);
        }

        _stopMic() {
            if (this.processor) {
                try { this.processor.disconnect(); } catch (e) {}
                this.processor = null;
            }
            if (this.mic) {
                this.mic.getTracks().forEach(t => t.stop());
                this.mic = null;
            }
            if (this.encoder) {
                try { this.encoder.close(); } catch (e) {}
                this.encoder = null;
            }
        }

        // ── Decode / playback ─────────────────────────────────────────

        _ensureDecoder() {
            if (this.decoder) return this.decoder;
            this.audioCtx ||= new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: SAMPLE_RATE,
            });
            this.decoder = new AudioDecoder({
                output: data => this._enqueuePlayback(data),
                error: e => this._emit('error', { reason: `decoder: ${e.message || e}` }),
            });
            this.decoder.configure({
                codec: 'opus',
                sampleRate: SAMPLE_RATE,
                numberOfChannels: CHANNELS,
            });
            return this.decoder;
        }

        _handleIncomingAudio(payloadU8) {
            const dec = this._ensureDecoder();
            const chunk = new EncodedAudioChunk({
                type: 'key',
                timestamp: 0,
                data: payloadU8,
            });
            try { dec.decode(chunk); } catch (e) { /* tolerate malformed frames */ }
        }

        _enqueuePlayback(data) {
            const ctx = this.audioCtx;
            if (!ctx) { data.close(); return; }
            const numFrames = data.numberOfFrames;
            const buf = ctx.createBuffer(CHANNELS, numFrames, SAMPLE_RATE);
            const dest = buf.getChannelData(0);
            data.copyTo(dest, { planeIndex: 0 });
            data.close();

            const src = ctx.createBufferSource();
            src.buffer = buf;
            src.connect(ctx.destination);
            const startAt = Math.max(ctx.currentTime, this.playbackTime);
            src.start(startAt);
            this.playbackTime = startAt + buf.duration;
        }

        _teardownAudio() {
            this._stopMic();
            if (this.decoder) {
                try { this.decoder.close(); } catch (e) {}
                this.decoder = null;
            }
            if (this.audioCtx) {
                try { this.audioCtx.close(); } catch (e) {}
                this.audioCtx = null;
            }
            this.playbackQueue = [];
            this.playbackTime = 0;
            this.holdsFloor = false;
            this.peerHoldsFloor = false;
        }
    }

    // ── Public surface ─────────────────────────────────────────────────

    window.OpenRingAudio = {
        supported: supported,
        Client: OpenRingAudio,

        // Glue helper used by events.html: fetches a session and connects.
        async connectFor(deviceId) {
            const resp = await fetch('/api/audio/session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json',
                           'X-CSRF-Token': getCsrfToken() },
                body: JSON.stringify({ device_id: deviceId }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.error || `HTTP ${resp.status}`);
            }
            const { audio_url } = await resp.json();
            const client = new OpenRingAudio();
            await client.connect(audio_url);
            return client;
        },
    };
})();
