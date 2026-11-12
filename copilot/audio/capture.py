"""Audio capture: two independent sources (``them`` = system loopback, ``me`` = microphone),
each delivering 16 kHz mono int16 PCM frames into its own thread-safe queue.

    from copilot.audio.capture import open_sources
    sources = open_sources(cfg)          # {"them": AudioSource, "me": AudioSource}
    for s in sources.values(): s.start()
    frame = sources["them"].get(timeout=1.0)   # np.ndarray[int16], len = sample_rate*frame_ms/1000

Design:
- Devices are opened at their *native* sample rate (we never change system device settings,
  which matters for macOS Multi-Output devices) and resampled to 16 kHz by StreamingResampler.
- Multichannel input is averaged to mono.
- Callbacks run on the PortAudio thread; the work there is a few small numpy ops.
- Queue overflow drops the oldest frames (the pipeline is real-time; stale audio is useless).
- Backends: sounddevice (all platforms) and pyaudiowpatch WASAPI loopback (Windows ``them``).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

import numpy as np

from copilot.audio.devices import AudioDeviceError, SourceSpec, resolve_me, resolve_them
from copilot.audio.dsp import PIPELINE_RATE, Framer, StreamingResampler, rms, to_mono

if TYPE_CHECKING:  # pragma: no cover
    from copilot.config import Config

log = logging.getLogger(__name__)


class AudioSource:
    """One captured channel → queue of int16 frames at the pipeline rate."""

    def __init__(
        self,
        spec: SourceSpec,
        *,
        sample_rate: int = PIPELINE_RATE,
        frame_ms: int = 40,
        queue_seconds: float = 10.0,
    ):
        self.spec = spec
        self.role = spec.role
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.frame_samples = self.sample_rate * self.frame_ms // 1000
        self.queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=max(8, int(queue_seconds * 1000 / self.frame_ms))
        )

        self.level: float = 0.0  # smoothed RMS 0..1 of the most recent audio (for meters)
        self.errors: int = 0  # PortAudio status flags (overflows etc.)
        self.dropped: int = 0  # frames dropped because the consumer was too slow
        self.native_rate: int | None = None
        self.channels: int | None = None

        self._framer = Framer(self.frame_samples)
        self._resampler: StreamingResampler | None = None
        self._lock = threading.Lock()
        self._stream = None  # sounddevice.InputStream | pyaudio stream
        self._pa = None  # pyaudiowpatch.PyAudio instance (Windows loopback only)
        self._running = False

    # ------------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        return self._running

    def describe(self) -> str:
        if self.spec.backend == "wasapi_loopback":
            base = "WASAPI loopback (default output)"
        else:
            base = self.spec.device.name if self.spec.device else "?"
        extra = f" @ {self.native_rate} Hz × {self.channels} ch" if self.native_rate else ""
        return f"{base}{extra} → {self.sample_rate} Hz mono, {self.frame_ms} ms frames"

    def start(self) -> None:
        if self._running:
            return
        if self.spec.backend == "wasapi_loopback":
            self._start_wasapi_loopback()
        else:
            self._start_sounddevice()
        self._running = True
        log.info("audio source %s started: %s", self.role, self.describe())

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            if self.spec.backend == "wasapi_loopback":
                self._stream.stop_stream()
                self._stream.close()
                self._pa.terminate()
            else:
                self._stream.stop()
                self._stream.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            log.exception("error while stopping audio source %s", self.role)
        finally:
            self._stream = None
            self._pa = None

    def get(self, timeout: float | None = None) -> np.ndarray | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_nowait(self) -> np.ndarray | None:
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> int:
        n = 0
        while self.get_nowait() is not None:
            n += 1
        return n

    def __enter__(self) -> "AudioSource":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- internals
    def _prepare(self, native_rate: int, channels: int) -> None:
        self.native_rate = int(native_rate)
        self.channels = int(channels)
        self._resampler = StreamingResampler(self.native_rate, self.sample_rate)

    def _ingest(self, block: np.ndarray) -> None:
        """Called from the audio thread with float32 samples shaped (frames, channels)."""
        mono = to_mono(block)
        cur = rms(mono)
        # fast attack, slow release — reads well on a meter
        self.level = cur if cur > self.level else self.level * 0.8 + cur * 0.2
        with self._lock:
            resampled = self._resampler.process(mono) if self._resampler else mono
            frames = self._framer.push(resampled)
        for frame in frames:
            self._put(frame)

    def _put(self, frame: np.ndarray) -> None:
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(frame)
            except queue.Full:  # pragma: no cover
                self.dropped += 1

    # -- sounddevice backend ----------------------------------------------------
    def _start_sounddevice(self) -> None:
        import sounddevice as sd

        dev = self.spec.device
        assert dev is not None
        channels = max(1, min(2, dev.max_input_channels))
        native_rate = int(round(dev.default_samplerate)) or self.sample_rate
        try:
            sd.check_input_settings(device=dev.index, channels=channels, samplerate=native_rate, dtype="float32")
        except Exception as exc:  # unusual device; fall back to mono / pipeline rate
            log.warning("%s: %s — retrying with 1 channel @ %d Hz", dev.name, exc, self.sample_rate)
            channels, native_rate = 1, self.sample_rate
            try:
                sd.check_input_settings(device=dev.index, channels=channels, samplerate=native_rate, dtype="float32")
            except Exception as exc2:
                raise AudioDeviceError(f"Не удалось открыть «{dev.name}»: {exc2}") from exc2

        self._prepare(native_rate, channels)
        blocksize = int(native_rate * 20 / 1000)  # ~20 ms callbacks

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                self.errors += 1
            try:
                self._ingest(np.array(indata, dtype=np.float32, copy=True))
            except Exception:  # never let an exception escape the audio thread
                self.errors += 1
                log.exception("audio ingest failed (%s)", self.role)

        self._stream = sd.InputStream(
            device=dev.index,
            channels=channels,
            samplerate=native_rate,
            dtype="float32",
            blocksize=blocksize,
            latency="low",
            callback=callback,
        )
        self._stream.start()

    # -- pyaudiowpatch WASAPI loopback (Windows) --------------------------------
    def _start_wasapi_loopback(self) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:  # pragma: no cover - windows only
            raise AudioDeviceError("pyaudiowpatch не установлен: uv sync --extra win") from exc

        self._pa = pyaudio.PyAudio()
        pa = self._pa
        info = None
        if hasattr(pa, "get_default_wasapi_loopback"):
            try:
                info = pa.get_default_wasapi_loopback()
            except Exception:
                info = None
        if info is None:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not speakers.get("isLoopbackDevice"):
                for lb in pa.get_loopback_device_info_generator():
                    if speakers["name"] in lb["name"]:
                        speakers = lb
                        break
                else:
                    raise AudioDeviceError("WASAPI loopback-устройство для вывода по умолчанию не найдено.")
            info = speakers

        channels = max(1, int(info["maxInputChannels"]))
        native_rate = int(info["defaultSampleRate"])
        self._prepare(native_rate, channels)
        self.spec = SourceSpec("them", "wasapi_loopback", None, note=f"loopback of: {info['name']}")

        def callback(in_data, frame_count, time_info, status):  # noqa: ARG001
            if status:
                self.errors += 1
            try:
                pcm = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                self._ingest(pcm.reshape(-1, channels))
            except Exception:
                self.errors += 1
                log.exception("audio ingest failed (%s)", self.role)
            return (None, pyaudio.paContinue)

        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=native_rate,
            frames_per_buffer=int(native_rate * 20 / 1000),
            input=True,
            input_device_index=int(info["index"]),
            stream_callback=callback,
        )
        self._stream.start_stream()


def open_sources(cfg: "Config") -> dict[str, AudioSource]:
    """Resolve devices from config and build (not yet started) sources: them [+ me]."""
    a = cfg.audio
    sources: dict[str, AudioSource] = {}
    them_spec = resolve_them(a.them_device)
    sources["them"] = AudioSource(them_spec, sample_rate=a.sample_rate, frame_ms=a.frame_ms)
    if a.transcribe_me:
        me_spec = resolve_me(a.me_device)
        sources["me"] = AudioSource(me_spec, sample_rate=a.sample_rate, frame_ms=a.frame_ms)
    return sources
