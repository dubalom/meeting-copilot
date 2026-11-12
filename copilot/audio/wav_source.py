"""Replay a WAV file as a live audio channel (tests and reproducible runs without a meeting).

Any WAV (int16/int32/float, any rate, mono/stereo) is converted to 16 kHz mono int16 and
yielded in fixed frames, optionally paced in real time.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from copilot.audio.dsp import PIPELINE_RATE, StreamingResampler, float_to_int16, to_mono


def load_wav_16k(path: str | Path) -> np.ndarray:
    """Read a WAV and return 16 kHz mono int16 samples."""
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width}")
    x = to_mono(x.reshape(-1, channels))
    if rate != PIPELINE_RATE:
        x = StreamingResampler(rate, PIPELINE_RATE).process(x)
    return float_to_int16(x)


async def iter_frames(
    samples: np.ndarray,
    *,
    frame_ms: int = 40,
    realtime: bool = True,
    speed: float = 1.0,
    tail_silence_s: float = 1.5,
) -> AsyncIterator[bytes]:
    """Yield int16 PCM frames; with ``realtime`` paced at ``speed`` × real time.

    A short tail of silence is appended so the STT endpointing fires on the last phrase.
    """
    n = PIPELINE_RATE * frame_ms // 1000
    tail = np.zeros(int(PIPELINE_RATE * tail_silence_s), dtype=np.int16)
    data = np.concatenate([samples.astype(np.int16), tail])
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    period = frame_ms / 1000 / max(speed, 1e-3)
    for i, start in enumerate(range(0, data.size, n)):
        chunk = data[start : start + n]
        if chunk.size < n:
            chunk = np.pad(chunk, (0, n - chunk.size))
        yield chunk.tobytes()
        if realtime:
            delay = t0 + (i + 1) * period - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
