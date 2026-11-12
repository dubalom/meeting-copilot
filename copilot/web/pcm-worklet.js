// AudioWorklet: any input rate → 16 kHz mono int16 frames of 640 samples (40 ms).
// Downsampling = windowed-sinc low-pass (cut-off 7 kHz) + interpolation at the output instants.
// A plain average of every 3 samples (the old way) lets 8–16 kHz content fold back into the speech band
// as noise, which hurts recognition; this filter removes it before decimation.
const TARGET = 16000;
const FRAME = 640;
const TAPS = 95;          // odd; ≈1 ms delay at 48 kHz
const CUTOFF = 7000;      // Hz, below the 8 kHz Nyquist of the 16 kHz output

class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET; // input samples per output sample (sampleRate is a worklet global)
    this.out = new Int16Array(FRAME);
    this.n = 0;
    this.sq = 0;
    if (this.ratio > 1) {
      const h = new Float32Array(TAPS);
      const mid = (TAPS - 1) / 2;
      const fc = CUTOFF / sampleRate;
      let sum = 0;
      for (let k = 0; k < TAPS; k++) {
        const t = k - mid;
        const sinc = t === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * t) / (Math.PI * t);
        const w = 0.42 - 0.5 * Math.cos((2 * Math.PI * k) / (TAPS - 1)) + 0.08 * Math.cos((4 * Math.PI * k) / (TAPS - 1));
        h[k] = sinc * w; // Blackman window
        sum += h[k];
      }
      for (let k = 0; k < TAPS; k++) h[k] /= sum;
      this.h = h;
      this.L = TAPS + 1;
      this.ring = new Float32Array(this.L);
      this.w = 0;      // next write position in the ring
      this.m = -1;     // index of the newest input sample
      this.next = 0;   // input-sample position of the next output sample (fractional)
    }
  }

  // causal FIR output at input index i (i ∈ {m-1, m})
  fir(i) {
    const { h, ring, L } = this;
    let acc = 0;
    let j = (this.w - 1 - (this.m - i) + L) % L;
    for (let k = 0; k < TAPS; k++) {
      acc += h[k] * ring[j];
      j = j === 0 ? L - 1 : j - 1;
    }
    return acc;
  }

  push(v) {
    const s = Math.max(-1, Math.min(1, v));
    this.out[this.n++] = s < 0 ? s * 32768 : s * 32767;
    this.sq += s * s;
    if (this.n === FRAME) {
      const rms = Math.sqrt(this.sq / FRAME);
      const buf = this.out.buffer;
      this.port.postMessage({ pcm: buf, rms }, [buf]);
      this.out = new Int16Array(FRAME);
      this.n = 0;
      this.sq = 0;
    }
  }

  process(inputs) {
    const chans = inputs[0];
    if (!chans || chans.length === 0) return true;
    const len = chans[0].length;
    const k = chans.length;
    for (let i = 0; i < len; i++) {
      let v = 0;
      for (let c = 0; c < k; c++) v += chans[c][i];
      v /= k;
      if (this.ratio <= 1) { this.push(v); continue; }
      this.ring[this.w] = v;
      this.w = (this.w + 1) % this.L;
      this.m++;
      while (this.next + 1 <= this.m) {
        const i0 = Math.floor(this.next);
        const frac = this.next - i0;
        const y0 = this.fir(i0);
        this.push(frac > 0 ? y0 + frac * (this.fir(i0 + 1) - y0) : y0);
        this.next += this.ratio;
      }
    }
    return true;
  }
}

registerProcessor('pcm-capture', PcmCapture);
