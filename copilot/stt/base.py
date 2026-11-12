"""Transcriber abstraction: one streaming STT connection per audio channel.

A Transcriber receives 16 kHz mono int16 PCM via ``send()`` and emits ``TranscriptEvent``s
through ``on_event``. Implementations: ``deepgram_stt.DeepgramTranscriber`` (default),
``whisper_stt`` (optional local fallback, later), and ``FakeTranscriber`` in tests.
"""

from __future__ import annotations

import inspect
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

Speaker = Literal["them", "me"]
EventKind = Literal["transcript", "utterance_end", "speech_started"]
Status = Literal["idle", "connecting", "connected", "reconnecting", "closed", "error"]


@dataclass(slots=True)
class TranscriptEvent:
    speaker: Speaker
    kind: EventKind = "transcript"
    text: str = ""
    final: bool = False  # STT marked this segment as final (won't change)
    speech_final: bool = False  # endpoint detected: the utterance is complete
    ts: float = field(default_factory=time.time)  # wall-clock arrival time
    start: float | None = None  # offset in the audio stream, seconds
    duration: float | None = None
    confidence: float | None = None
    word_start: float | None = None  # stream offset of the first word, seconds
    word_end: float | None = None  # stream offset of the last word, seconds


EventCallback = Callable[[TranscriptEvent], Awaitable[None] | None]
StatusCallback = Callable[[Speaker, Status, str], Awaitable[None] | None]


async def _maybe_await(result) -> None:
    if inspect.isawaitable(result):
        await result


class Transcriber(ABC):
    """Lifecycle: ``await start()`` → many ``await send(pcm)`` → ``await stop()``."""

    def __init__(self, speaker: Speaker, on_event: EventCallback, on_status: StatusCallback | None = None):
        self.speaker: Speaker = speaker
        self._on_event = on_event
        self._on_status = on_status
        self.status: Status = "idle"
        self.audio_seconds: float = 0.0  # total audio accepted (for usage metering)

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send(self, pcm: bytes) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def emit(self, event: TranscriptEvent) -> None:
        await _maybe_await(self._on_event(event))

    async def set_status(self, status: Status, detail: str = "") -> None:
        if status == self.status and not detail:
            return
        self.status = status
        if self._on_status is not None:
            await _maybe_await(self._on_status(self.speaker, status, detail))

    def count_audio(self, pcm: bytes, sample_rate: int = 16_000) -> None:
        self.audio_seconds += len(pcm) / 2 / sample_rate
