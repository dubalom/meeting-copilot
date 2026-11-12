"""Pure-numpy/scipy DSP helpers used by the capture layer.

Nothing here touches PortAudio, so it is importable (and unit-testable) on any machine.

- StreamingResampler: stateful, block-wise resampling of a float32 mono stream to the
  pipeline rate (16 kHz). Anti-alias FIR with carried filter state + linear interpolation
  onto the target grid with carried phase, so consecutive blocks join seamlessly.
  Quality is more than enough for speech-to-text; cost is negligible.
- Framer: accumulates arbitrary-length float blocks and emits fixed-size int16 frames.
- level helpers: RMS / dBFS for the UI meters.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import firwin, lfilter

PIPELINE_RATE = 16_000


class StreamingResampler:
    """Resample a mono float32 stream from ``src_rate`` to ``dst_rate`` block by block."""

    def __init__(self, src_rate: int, dst_rate: int = PIPELINE_RATE, taps: int | None = None):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("rates must be positive")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.identity = self.src_rate == self.dst_rate
        self._step = self.src_rate / self.dst_rate  # input samples per output sample
        # Position (in input-sample units) of the next output sample, relative to the
        # start of the *current* internal buffer. Fractional part carries phase across blocks.
        self._next_pos = 0.0
        # Keep one input sample from the previous block so interpolation can span the seam.
        self._prev = np.zeros(0, dtype=np.float32)

        self._lp_b: np.ndarray | None = None
        self._lp_zi: np.ndarray | None = None
        if not self.identity and self.src_rate > self.dst_rate:
            # Anti-aliasing low-pass before decimation: cutoff a bit below dst Nyquist.
            cutoff = 0.45 * self.dst_rate
            taps = taps or max(31, int(16 * self._step) * 2 + 1)  # ~97 taps for 48k→16k
            self._lp_b = firwin(taps, cutoff, fs=self.src_rate).astype(np.float64)
            self._lp_zi = np.zeros(len(self._lp_b) - 1, dtype=np.float64)

    def process(self, x: np.ndarray) -> np.ndarray:
        """Feed a block of mono float samples; return the resampled block (float32)."""
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.identity:
            return x
        if x.size == 0:
            return x

        if self._lp_b is not None:
            y, self._lp_zi = lfilter(self._lp_b, 1.0, x.astype(np.float64), zi=self._lp_zi)
            x = y.astype(np.float32)

        buf = np.concatenate([self._prev, x]) if self._prev.size else x
        n = buf.size
        # Output positions must satisfy pos <= n-1 so np.interp never extrapolates.
        last = n - 1
        if self._next_pos > last:
            # Not enough new input yet for even one output sample.
            self._prev = buf[-1:].copy()
            self._next_pos -= n - 1
            return np.zeros(0, dtype=np.float32)

        count = int(math.floor((last - self._next_pos) / self._step)) + 1
        positions = self._next_pos + self._step * np.arange(count, dtype=np.float64)
        out = np.interp(positions, np.arange(n, dtype=np.float64), buf.astype(np.float64))

        # Carry: keep the last input sample; shift the next position relative to it.
        new_next = positions[-1] + self._step
        self._prev = buf[-1:].copy()
        self._next_pos = new_next - last
        return out.astype(np.float32)


class Framer:
    """Accumulate float samples (−1..1) and emit fixed-length int16 frames."""

    def __init__(self, frame_samples: int):
        if frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        self.frame_samples = int(frame_samples)
        self._buf = np.zeros(0, dtype=np.float32)

    def push(self, x: np.ndarray) -> list[np.ndarray]:
        """Return zero or more complete int16 frames; leftover samples stay buffered."""
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size:
            self._buf = np.concatenate([self._buf, x]) if self._buf.size else x
        frames: list[np.ndarray] = []
        n = self.frame_samples
        while self._buf.size >= n:
            chunk, self._buf = self._buf[:n], self._buf[n:]
            frames.append(float_to_int16(chunk))
        return frames

    @property
    def pending(self) -> int:
        return int(self._buf.size)


def float_to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


def to_mono(x: np.ndarray) -> np.ndarray:
    """(frames, channels) or (frames,) → (frames,) float32 by averaging channels."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 2:
        return x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    return x.reshape(-1)


def rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x))))


def dbfs(level: float) -> float:
    """Linear RMS (0..1) → dBFS, floored at −90."""
    if level <= 1e-5:
        return -90.0
    return max(-90.0, 20.0 * math.log10(level))
