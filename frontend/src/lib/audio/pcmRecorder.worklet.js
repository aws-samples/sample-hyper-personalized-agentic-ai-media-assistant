// AudioWorklet processor: captures mic audio on the audio thread and posts it
// back as 16-bit PCM. The AudioContext is created at 16 kHz, so frames arrive
// already at the rate Nova Sonic expects — we only convert Float32 -> Int16.
class PcmRecorderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;

    const samples = input[0];
    const pcm = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this.port.postMessage({ type: 'audio', data: pcm });
    return true;
  }
}

registerProcessor('pcm-recorder-processor', PcmRecorderProcessor);
