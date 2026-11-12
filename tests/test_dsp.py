import numpy as np
import pytest

from copilot.audio.dsp import Framer, StreamingResampler, dbfs, float_to_int16, rms, to_mono


def _sine(freq: float, rate: int, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _peak_freq(x: np.ndarray, rate: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.fft.rfftfreq(x.size, 1 / rate)[np.argmax(spec)])


@pytest.mark.parametrize("src_rate", [48_000, 44_100, 32_000, 96_000])
def test_resampler_preserves_tone_and_length(src_rate):
    x = _sine(1000.0, src_rate, 2.0)
    rs = StreamingResampler(src_rate, 16_000)
    y = rs.process(x)
    assert abs(y.size - 32_000) <= 2
    assert abs(_peak_freq(y[4000:], 16_000) - 1000.0) < 10
    # amplitude roughly preserved (within the FIR passband)
    assert 0.4 < np.abs(y[4000:]).max() < 0.6


@pytest.mark.parametrize("src_rate,block", [(48_000, 960), (44_100, 441), (44_100, 1000), (48_000, 333)])
def test_resampler_blockwise_equals_oneshot(src_rate, block):
    """Feeding the stream in arbitrary blocks must equal feeding it at once (seamless joins)."""
    x = _sine(440.0, src_rate, 1.0) + 0.1 * _sine(3000.0, src_rate, 1.0)
    whole = StreamingResampler(src_rate, 16_000).process(x)
    rs = StreamingResampler(src_rate, 16_000)
    parts = [rs.process(x[i : i + block]) for i in range(0, x.size, block)]
    chunked = np.concatenate(parts)
    n = min(whole.size, chunked.size)
    assert abs(whole.size - chunked.size) <= 1
    np.testing.assert_allclose(chunked[:n], whole[:n], atol=1e-5)


def test_resampler_identity_and_empty():
    rs = StreamingResampler(16_000, 16_000)
    x = np.ones(100, dtype=np.float32)
    assert rs.process(x) is x or np.array_equal(rs.process(x), x)
    assert StreamingResampler(48_000, 16_000).process(np.zeros(0)).size == 0


def test_resampler_tiny_blocks_do_not_lose_samples():
    rs = StreamingResampler(48_000, 16_000)
    total = sum(rs.process(np.zeros(1, dtype=np.float32)).size for _ in range(4800))
    assert abs(total - 1600) <= 1


def test_framer_exact_frames_and_remainder():
    fr = Framer(640)
    assert fr.push(np.zeros(639)) == []
    frames = fr.push(np.zeros(641))
    assert len(frames) == 2
    assert all(f.dtype == np.int16 and f.size == 640 for f in frames)
    assert fr.pending == 0
    frames = fr.push(np.ones(1300) * 0.5)
    assert len(frames) == 2 and fr.pending == 20
    assert frames[0][0] == int(0.5 * 32767)


def test_float_to_int16_clips():
    out = float_to_int16(np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float32))
    assert out.tolist() == [-32767, -32767, 0, 32767, 32767]


def test_to_mono_and_levels():
    stereo = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
    np.testing.assert_allclose(to_mono(stereo), [0.0, 0.5])
    assert rms(np.zeros(10)) == 0.0
    assert dbfs(0.0) == -90.0
    assert abs(dbfs(1.0)) < 1e-9
    assert -7 < dbfs(0.5) < -5
