"""Deepgram Nova-3 live transcription over a raw WebSocket.

We talk to ``wss://api.deepgram.com/v1/listen`` directly (the ``websockets`` package) instead of
``deepgram-sdk``: the wire protocol is small and stable, and owning the connection makes
keepalive, reconnect and buffering behaviour explicit.

Protocol (docs: https://developers.deepgram.com/docs/live-streaming-audio):
- auth header ``Authorization: Token <key>``; query params select model/language/format
- client → server: binary PCM frames; JSON ``{"type":"KeepAlive"}`` during silence;
  ``{"type":"CloseStream"}`` to flush and close
- server → client: ``Results`` (interim/final transcripts), ``UtteranceEnd``, ``SpeechStarted``,
  ``Metadata``, ``Error``
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable
from urllib.parse import urlencode

from copilot.config import SttConfig
from copilot.stt.base import Speaker, TranscriptEvent, Transcriber, StatusCallback, EventCallback

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://api.deepgram.com/v1/listen"
FRAME_BYTES_PER_SECOND = 16_000 * 2  # 16 kHz int16 mono


class DeepgramAuthError(RuntimeError):
    """401/403 from Deepgram: wrong or revoked key — retrying is pointless."""


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def build_url(cfg: SttConfig, *, sample_rate: int = 16_000, keyterms: Iterable[str] = (), base: str = DEFAULT_URL) -> str:
    params: list[tuple[str, str]] = [
        ("model", cfg.model),
        ("language", cfg.language),
        ("encoding", "linear16"),
        ("sample_rate", str(sample_rate)),
        ("channels", "1"),
        ("interim_results", "true"),
        ("smart_format", "true"),
        ("punctuate", "true"),
        ("endpointing", str(cfg.endpointing_ms)),
        ("utterance_end_ms", str(cfg.utterance_end_ms)),
        ("vad_events", "true"),
    ]
    seen: set[str] = set()
    for term in keyterms:
        term = term.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            params.append(("keyterm", term))  # repeated param, one per term (never comma-joined)
    return f"{base}?{urlencode(params)}"


def _first_alternative(data: dict) -> dict:
    channel = data.get("channel")
    if not isinstance(channel, dict):  # UtteranceEnd/SpeechStarted carry `channel` as a list of indices
        return {}
    alts = channel.get("alternatives") or []
    return alts[0] if alts and isinstance(alts[0], dict) else {}


def _transcript_of(data: dict) -> str:
    return (_first_alternative(data).get("transcript") or "").strip()


def parse_message(speaker: Speaker, data: dict, *, now: float | None = None, offset: float = 0.0) -> TranscriptEvent | None:
    """Turn one Deepgram JSON message into a TranscriptEvent (or None for noise)."""
    now = time.time() if now is None else now
    kind = data.get("type")
    if kind == "Results":
        alt = _first_alternative(data)
        text = (alt.get("transcript") or "").strip()
        if not text:
            return None
        start = data.get("start")
        words = [w for w in (alt.get("words") or []) if isinstance(w, dict) and w.get("start") is not None]
        word_start = offset + float(words[0]["start"]) if words else None
        word_end = offset + float(words[-1].get("end", words[-1]["start"])) if words else None
        return TranscriptEvent(
            speaker=speaker,
            kind="transcript",
            text=text,
            final=bool(data.get("is_final")),
            speech_final=bool(data.get("speech_final")),
            ts=now,
            start=(offset + float(start)) if start is not None else None,
            duration=float(data["duration"]) if data.get("duration") is not None else None,
            confidence=alt.get("confidence"),
            word_start=word_start,
            word_end=word_end,
        )
    if kind == "UtteranceEnd":
        return TranscriptEvent(speaker=speaker, kind="utterance_end", ts=now, start=offset + float(data.get("last_word_end") or 0.0))
    if kind == "SpeechStarted":
        return TranscriptEvent(speaker=speaker, kind="speech_started", ts=now, start=offset + float(data.get("timestamp") or 0.0))
    if kind == "Error":
        log.error("deepgram error (%s): %s", speaker, data)
    return None


def split_on_pauses(ev: TranscriptEvent, words: list[dict], offset: float, gap: float) -> list[TranscriptEvent]:
    """Split one final segment at inner pauses longer than ``gap`` seconds.

    Deepgram (notably with language=multi) can return two separate phrases as one final segment.
    Each part is rebuilt from ``punctuated_word``; all parts but the last are marked speech_final.
    """
    if not ev.final or len(words) < 2:
        return [ev]
    groups: list[list[dict]] = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        if float(cur["start"]) - float(prev.get("end", prev["start"])) > gap:
            groups.append([])
        groups[-1].append(cur)
    if len(groups) == 1:
        return [ev]
    parts: list[TranscriptEvent] = []
    for i, g in enumerate(groups):
        text = " ".join(str(w.get("punctuated_word") or w.get("word") or "") for w in g).strip()
        last = i == len(groups) - 1
        parts.append(
            TranscriptEvent(
                speaker=ev.speaker, kind="transcript", text=text, final=True,
                speech_final=ev.speech_final if last else True, ts=ev.ts, start=ev.start, duration=ev.duration,
                confidence=ev.confidence, word_start=offset + float(g[0]["start"]),
                word_end=offset + float(g[-1].get("end", g[-1]["start"])),
            )
        )
    return [p for p in parts if p.text]


def backoff_delays(max_seconds: float, first: float = 0.5) -> Iterable[float]:
    d = first
    while True:
        yield d
        d = min(d * 2, max_seconds)


def _http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) or getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


# --------------------------------------------------------------------------- #
# Transcriber
# --------------------------------------------------------------------------- #
class DeepgramTranscriber(Transcriber):
    def __init__(
        self,
        speaker: Speaker,
        on_event: EventCallback,
        on_status: StatusCallback | None = None,
        *,
        api_key: str,
        cfg: SttConfig,
        keyterms: Iterable[str] = (),
        sample_rate: int = 16_000,
        url_base: str = DEFAULT_URL,
    ):
        super().__init__(speaker, on_event, on_status)
        if not api_key:
            raise DeepgramAuthError("DEEPGRAM_API_KEY is empty")
        self._api_key = api_key
        self._cfg = cfg
        self._sample_rate = sample_rate
        self._url = build_url(cfg, sample_rate=sample_rate, keyterms=keyterms, base=url_base)
        frames_per_second = 25  # 40 ms frames
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=int(cfg.buffer_seconds * frames_per_second))
        self._task: asyncio.Task | None = None
        self._closing = False
        self._sent_bytes_total = 0
        self._conn_offset_seconds = 0.0  # audio seconds sent before the current connection (timestamps restart per connection)
        self.dropped_frames = 0
        self.reconnects = 0

    # ---------------------------------------------------------------- public
    async def start(self) -> None:
        if self._task is None:
            self._closing = False
            self._task = asyncio.create_task(self._run(), name=f"deepgram-{self.speaker}")

    async def send(self, pcm: bytes) -> None:
        if self._closing or not pcm:
            return
        self.count_audio(pcm, self._sample_rate)
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()  # drop the oldest frame; real-time audio must not back up
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(pcm)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._closing = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            self._queue.get_nowait()
            self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, timeout=6.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        except Exception:  # pragma: no cover - errors already logged in _run
            pass
        finally:
            self._task = None
            await self.set_status("closed")

    # ------------------------------------------------------------- internals
    async def _run(self) -> None:
        delays = backoff_delays(self._cfg.reconnect_max_seconds)
        first = True
        while not self._closing:
            await self.set_status("connecting" if first else "reconnecting")
            try:
                ws = await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = _http_status(exc)
                if code in (401, 403):
                    await self.set_status("error", f"Deepgram rejected the API key (HTTP {code})")
                    log.error("deepgram auth failed (%s): %s", self.speaker, exc)
                    return
                delay = next(delays)
                log.warning("deepgram connect failed (%s): %s — retry in %.1fs", self.speaker, exc, delay)
                await self.set_status("reconnecting", str(exc))
                await asyncio.sleep(delay)
                continue

            if not first:
                self.reconnects += 1
            first = False
            delays = backoff_delays(self._cfg.reconnect_max_seconds)
            self._conn_offset_seconds = self._sent_bytes_total / FRAME_BYTES_PER_SECOND
            await self.set_status("connected")
            try:
                await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    break
                delay = next(delays)
                log.warning("deepgram connection dropped (%s): %s — reconnect in %.1fs", self.speaker, exc, delay)
                await self.set_status("reconnecting", str(exc))
                await asyncio.sleep(delay)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass
            if self._closing:
                break

    async def _connect(self):
        import websockets

        headers = {"Authorization": f"Token {self._api_key}"}
        kwargs = dict(max_size=2**22, ping_interval=20, ping_timeout=20, open_timeout=10, close_timeout=5)
        try:
            return await websockets.connect(self._url, additional_headers=headers, **kwargs)
        except TypeError:  # websockets < 13 uses extra_headers
            return await websockets.connect(self._url, extra_headers=headers, **kwargs)

    async def _pump(self, ws) -> None:
        """Run sender and receiver until either finishes; the other is cancelled."""
        sender = asyncio.create_task(self._sender(ws), name=f"dg-send-{self.speaker}")
        receiver = asyncio.create_task(self._receiver(ws), name=f"dg-recv-{self.speaker}")
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        if sender in done and receiver in pending and self._closing:
            # CloseStream sent: give the server a moment to flush final results.
            try:
                await asyncio.wait_for(receiver, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            pending.discard(receiver)
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                raise exc

    async def _sender(self, ws) -> None:
        keepalive = self._cfg.keepalive_seconds
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"type": "KeepAlive"}))
                continue
            if item is None:
                await ws.send(json.dumps({"type": "CloseStream"}))
                return
            await ws.send(item)
            self._sent_bytes_total += len(item)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("deepgram: non-JSON message ignored")
                continue
            if not isinstance(data, dict):
                continue
            if log.isEnabledFor(logging.DEBUG):
                log.debug("dg[%s] %s final=%s speech_final=%s start=%s dur=%s last_word_end=%s | %s",
                          self.speaker, data.get("type"), data.get("is_final"), data.get("speech_final"),
                          data.get("start"), data.get("duration"), data.get("last_word_end"),
                          _transcript_of(data)[:60])
            try:
                event = parse_message(self.speaker, data, offset=self._conn_offset_seconds)
                events = [event] if event is not None else []
                if event is not None and event.final:
                    words = [w for w in (_first_alternative(data).get("words") or [])
                             if isinstance(w, dict) and w.get("start") is not None]
                    events = split_on_pauses(event, words, self._conn_offset_seconds, self._cfg.phrase_gap_seconds)
            except Exception:  # one odd message must never kill the connection
                log.exception("deepgram: failed to parse message %r", str(data)[:200])
                continue
            for ev in events:
                await self.emit(ev)
