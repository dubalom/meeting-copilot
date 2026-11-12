// Browser audio capture: MediaStream → AudioWorklet → 16 kHz int16 frames per channel.

const CLEAN = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };

export class AudioCapture {
  constructor({ onFrame, onLevel }) {
    this.onFrame = onFrame; // (channel: 'them'|'me', ArrayBuffer) => void
    this.onLevel = onLevel; // (channel, rms 0..1) => void
    this.ctx = null;
    this.nodes = {};
  }

  async init() {
    if (this.ctx) return;
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule('/static/pcm-worklet.js');
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0; // keep worklets pulled by the graph without playing anything
    this.sink.connect(this.ctx.destination);
  }

  async attach(channel, stream) {
    await this.init();
    await this.ctx.resume();
    this.detach(channel);
    const tracks = stream.getAudioTracks();
    if (!tracks.length) throw new Error('no audio track');
    const src = this.ctx.createMediaStreamSource(new MediaStream(tracks));
    const node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    node.port.onmessage = (e) => {
      this.onFrame(channel, e.data.pcm);
      this.onLevel(channel, e.data.rms);
    };
    src.connect(node);
    node.connect(this.sink);
    this.nodes[channel] = { src, node, tracks };
  }

  detach(channel) {
    const n = this.nodes[channel];
    if (!n) return;
    try { n.src.disconnect(); n.node.disconnect(); } catch (_) { /* already gone */ }
    n.node.port.onmessage = null;
    delete this.nodes[channel];
  }

  stop() {
    for (const ch of Object.keys(this.nodes)) {
      this.nodes[ch].tracks.forEach((t) => t.stop());
      this.detach(ch);
    }
  }
}

// Screen/tab share. Video is for the preview; audio (if the user ticked "share audio") is `them`.
export async function getScreen() {
  return navigator.mediaDevices.getDisplayMedia({
    video: { frameRate: 15 },
    audio: { ...CLEAN, suppressLocalAudioPlayback: false },
    systemAudio: 'include',
    selfBrowserSurface: 'exclude',
    surfaceSwitching: 'include',
  });
}

export async function getInput(deviceId, { clean = false } = {}) {
  const audio = deviceId ? { deviceId: { exact: deviceId } } : {};
  Object.assign(audio, clean ? CLEAN : { echoCancellation: true, noiseSuppression: true, autoGainControl: true });
  return navigator.mediaDevices.getUserMedia({ audio, video: false });
}

export async function listInputs() {
  let devices = await navigator.mediaDevices.enumerateDevices();
  let inputs = devices.filter((d) => d.kind === 'audioinput');
  if (inputs.length && !inputs[0].label) {
    // labels are hidden until mic permission is granted once
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    devices = await navigator.mediaDevices.enumerateDevices();
    inputs = devices.filter((d) => d.kind === 'audioinput');
  }
  return inputs.map((d) => ({ id: d.deviceId, label: d.label || 'Микрофон' }));
}

export function isLoopbackLabel(label) {
  return /blackhole|loopback|soundflower|stereo mix|cable output/i.test(label || '');
}
