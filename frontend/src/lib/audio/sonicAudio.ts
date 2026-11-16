/**
 * Browser audio I/O for the Nova Sonic voice path.
 *
 * `SonicRecorder` captures the mic at 16 kHz and emits base64-encoded PCM16
 * frames (what Nova Sonic expects). `SonicPlayer` queues and plays the PCM16
 * audio chunks Sonic streams back, in order.
 *
 * Adapted from the AWS `sample-nova-sonic-websocket-agentcore` audio code.
 */

// Vite resolves this to a served URL for the AudioWorklet module.
import recorderWorkletUrl from './pcmRecorder.worklet.js?url';

const SAMPLE_RATE = 16000;

/** Encode raw bytes to base64 in chunks (avoids call-stack limits on big buffers). */
function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export class SonicRecorder {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  // Incremented on every stop(); start() captures the current value and bails
  // (releasing anything it acquired) if a stop() lands during an await. Without
  // this, navigating away while getUserMedia is still resolving orphans the mic
  // stream — cancel() runs before this.stream is set, so the track never stops
  // and the browser mic indicator stays on forever.
  private gen = 0;

  /** Start capturing; `onFrame` receives base64 PCM16 for each audio frame. */
  async start(onFrame: (base64Pcm: string) => void): Promise<void> {
    // Idempotent: release any existing capture first so repeated start() calls
    // don't orphan mic streams (which would keep the mic indicator on).
    this.stop();
    const gen = this.gen;

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: SAMPLE_RATE,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
    // A stop() (or a newer start()) happened while we were acquiring the mic —
    // abandon this stream immediately so it doesn't linger.
    if (gen !== this.gen) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    this.stream = stream;

    const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await ctx.audioWorklet.addModule(recorderWorkletUrl);
    if (gen !== this.gen) {
      // Stopped during worklet load — tear down what we've acquired.
      stream.getTracks().forEach((t) => t.stop());
      void ctx.close();
      this.stream = null;
      return;
    }
    this.ctx = ctx;

    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-recorder-processor');
    this.node.port.onmessage = (e: MessageEvent) => {
      if (e.data?.type !== 'audio') return;
      const pcm = e.data.data as Int16Array;
      onFrame(bytesToBase64(new Uint8Array(pcm.buffer)));
    };

    this.source.connect(this.node);
    // Connect to destination so the graph runs; the worklet emits no output.
    this.node.connect(this.ctx.destination);
  }

  stop(): void {
    // Invalidate any in-flight start() so it releases the mic it's acquiring.
    this.gen++;
    try {
      this.node?.disconnect();
      this.source?.disconnect();
      this.stream?.getTracks().forEach((t) => t.stop());
      if (this.ctx && this.ctx.state !== 'closed') void this.ctx.close();
    } finally {
      this.node = null;
      this.source = null;
      this.stream = null;
      this.ctx = null;
    }
  }
}

export class SonicPlayer {
  private ctx: AudioContext | null = null;
  private queue: AudioBuffer[] = [];
  private playing = false;
  private src: AudioBufferSourceNode | null = null;
  private sampleRate: number;
  /** Notified when playback starts (true) and when the queue drains (false).
   *  Lets the client suppress mic forwarding while the assistant is speaking. */
  private onPlayingChange?: (playing: boolean) => void;

  constructor(sampleRate = SAMPLE_RATE, onPlayingChange?: (playing: boolean) => void) {
    this.sampleRate = sampleRate;
    this.onPlayingChange = onPlayingChange;
  }

  /** When true, incoming audio is dropped and playback is silenced — the user
   *  muted the assistant's voice (output). Capture/transcripts are unaffected. */
  private muted = false;

  /** Mute/unmute audio OUTPUT. Muting also drops anything currently playing or
   *  queued, so the voice stops immediately (not after the current sentence). */
  setMuted(muted: boolean): void {
    this.muted = muted;
    if (muted) this.clear();
  }

  /** Queue a base64 PCM16 chunk for playback. */
  enqueue(base64Pcm: string, sampleRate = this.sampleRate): void {
    if (this.muted) return; // voice output muted — drop the chunk
    if (!this.ctx || this.ctx.state === 'closed') {
      this.ctx = new AudioContext({ sampleRate });
    }
    const binary = atob(base64Pcm);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

    const pcm = new Int16Array(bytes.buffer);
    const floats = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) {
      floats[i] = pcm[i] / (pcm[i] < 0 ? 0x8000 : 0x7fff);
    }

    const buffer = this.ctx.createBuffer(1, floats.length, sampleRate);
    buffer.getChannelData(0).set(floats);
    this.queue.push(buffer);
    if (!this.playing) this.playNext();
  }

  /** Drop anything queued/playing (barge-in / interruption). */
  clear(): void {
    this.queue = [];
    if (this.src) {
      // Detach onended first so stopping doesn't re-enter playNext().
      this.src.onended = null;
      try {
        this.src.stop();
        this.src.disconnect();
      } catch {
        /* already stopped/ended */
      }
      this.src = null;
    }
    if (this.playing) {
      this.playing = false;
      this.onPlayingChange?.(false);
    }
  }

  close(): void {
    this.clear();
    if (this.ctx && this.ctx.state !== 'closed') void this.ctx.close();
    this.ctx = null;
  }

  private playNext(): void {
    if (!this.ctx || this.ctx.state === 'closed' || this.queue.length === 0) {
      if (this.playing) {
        this.playing = false;
        this.onPlayingChange?.(false);
      }
      return;
    }
    if (!this.playing) {
      this.playing = true;
      this.onPlayingChange?.(true);
    }
    const buffer = this.queue.shift()!;
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    src.onended = () => {
      if (this.src === src) this.src = null;
      this.playNext();
    };
    this.src = src;
    src.start();
  }
}
