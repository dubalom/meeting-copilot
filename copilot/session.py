"""MeetingSession: the per-meeting core that every client (browser, CLI, replay) talks to.

    audio frames (channel, pcm) ──▶ Transcriber[channel] ──▶ TranscriptBuffer
                                                        └──▶ fan-out JSON events to subscribers

Events follow the wire protocol in CLAUDE.md §5.6, so the WebSocket layer just forwards them.
Answers: control ``answer`` → last question + transcript window → Brain stream → answer_* events.
A new answer cancels the one in flight.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Protocol

from copilot.brain.base import Brain
from copilot.config import Config
from copilot.stt.base import Speaker, Status, TranscriptEvent, Transcriber
from copilot.transcript import TranscriptBuffer

log = logging.getLogger(__name__)

CHANNELS: dict[int, Speaker] = {0: "them", 1: "me"}
CHANNEL_IDS: dict[Speaker, int] = {"them": 0, "me": 1}


class TranscriberFactory(Protocol):
    def __call__(self, speaker: Speaker, on_event, on_status=None) -> Transcriber: ...


class MeetingSession:
    def __init__(
        self,
        session_id: str,
        cfg: Config,
        transcriber_factory: TranscriberFactory,
        *,
        transcribe_me: bool | None = None,
        user_id: str = "owner",
        brain: Brain | None = None,
        brain_error: str | None = None,
        knowledge=None,  # KnowledgeStore of *this* user, or None
        profile_getter=None,  # () -> str: «Обо мне» of *this* user
        vocab_getter=None,  # () -> str: «Слова для распознавания» of *this* user
    ):
        self.id = session_id
        self.user_id = user_id
        self.brain = brain
        self.brain_error = brain_error
        self.knowledge = knowledge
        self.knowledge_enabled = knowledge is not None
        self._profile_getter = profile_getter
        self._vocab_getter = vocab_getter
        self.brief = ""  # «К этой встрече»
        self.session_docs: list = []  # files uploaded «только к этой встрече» (memory only)
        self._keyterms_cache: list[str] = []  # names from those files → STT hints
        self._keyterms_dirty = False
        self._session_index = None
        self.engine = cfg.brain.default_engine
        self.language = cfg.stt.language  # speech language (Deepgram)
        self.answer_language = "auto"  # auto | ru | en
        self.answers: list[dict[str, Any]] = []
        self.tokens: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._answer_task: asyncio.Task | None = None
        self._answer_seq = 0
        self._auto_task: asyncio.Task | None = None
        self._auto_last_id = 0
        self.cfg = cfg
        self._factory = transcriber_factory
        self.transcribe_me = cfg.audio.transcribe_me if transcribe_me is None else transcribe_me
        self.transcribe_them = True
        self.buffer = TranscriptBuffer(max_seconds=900)
        self.transcribers: dict[Speaker, Transcriber] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.last_activity = time.time()
        self.last_client_seen = time.time()
        self.stt_status: dict[str, str] = {"them": "idle", "me": "idle" if self.transcribe_me else "off"}
        # Deepgram emits several is_final segments per utterance; we join them until speech_final/UtteranceEnd.
        self._pending: dict[str, list[str]] = {"them": [], "me": []}
        self._pending_ts: dict[str, float] = {}
        self._pending_word_end: dict[str, float] = {}
        # Our own phrase boundary on top of Deepgram's endpointing (which is lax in `multi` mode):
        # a pause longer than this between two final segments starts a new utterance.
        self.phrase_gap_seconds = cfg.stt.phrase_gap_seconds
        self.brain_status: str = "idle"
        self.auto_enabled: bool = cfg.trigger.auto_default
        self.frames_in: dict[str, int] = {"them": 0, "me": 0}
        self._started = False
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._started = True
            self.stt_status["me"] = "idle" if self.transcribe_me else "off"
            self.stt_status["them"] = "idle" if self.transcribe_them else "off"
            await self._open_transcribers()
            log.info("session %s started (channels: %s, language %s)", self.id,
                     ", ".join(self.transcribers), self.language)

    async def _open_transcribers(self) -> None:
        """Open one transcriber per channel. A failure is shown to the user and leaves the other channel
        working — it must never kill the socket (the client would reconnect into a deaf meeting)."""
        try:  # may scan the saved chats once: never on the event loop
            keyterms = await asyncio.to_thread(lambda: self._keyterms)
        except Exception:
            log.exception("speech hints failed")
            keyterms = []
        for speaker in self._active_speakers():
            try:
                extra = {"extra_keyterms": keyterms} if keyterms else {}
                t = self._factory(speaker, self._on_stt_event, self._on_stt_status, language=self.language, **extra)
                self.transcribers[speaker] = t
                await t.start()
            except Exception as exc:
                log.exception("speech recognition for %s failed to start", speaker)
                self.transcribers.pop(speaker, None)
                self.stt_status[speaker] = "error"
                self._broadcast({"type": "status", "stt": dict(self.stt_status)})
                self._broadcast({"type": "error", "message": f"Распознавание речи ({'собеседники' if speaker == 'them' else 'вы'}) "
                                                            f"не запустилось: {exc}"})

    async def _reopen_transcribers(self) -> None:
        old, self.transcribers = self.transcribers, {}
        await asyncio.gather(*(t.stop() for t in old.values()), return_exceptions=True)
        for speaker in ("them", "me"):
            self._commit(speaker)
        await self._open_transcribers()

    async def set_language(self, stt: str | None = None, answer: str | None = None) -> None:
        """Change languages; a new speech language reopens the STT connections (≈1 s gap)."""
        from copilot.config import ANSWER_LANGUAGES, STT_LANGUAGES

        if answer in {k for k, _ in ANSWER_LANGUAGES}:
            self.answer_language = answer
        if stt in {k for k, _ in STT_LANGUAGES} and stt != self.language:
            self.language = stt
            if self.active:
                await self._reopen_transcribers()
        self._broadcast({"type": "status", "language": self.language, "answer_language": self.answer_language})

    async def stop(self) -> None:
        async with self._lock:
            if not self._started or self.ended_at is not None:
                return
            self.ended_at = time.time()
        await self.cancel_answer()
        if self._auto_task is not None:
            self._auto_task.cancel()
        await asyncio.gather(*(t.stop() for t in self.transcribers.values()), return_exceptions=True)
        for speaker in ("them", "me"):
            self._commit(speaker)  # flush a phrase cut off by the end of the session
        self._broadcast({"type": "status", "session": "ended", "stt": dict(self.stt_status), "brain": self.brain_status})
        log.info("session %s stopped after %.0fs", self.id, self.ended_at - self.started_at)

    @property
    def active(self) -> bool:
        return self._started and self.ended_at is None

    def _active_speakers(self) -> list[Speaker]:
        out: list[Speaker] = []
        if self.transcribe_them:
            out.append("them")
        if self.transcribe_me:
            out.append("me")
        return out or ["them"]

    # -------------------------------------------------------------------- input
    async def feed(self, channel: int | Speaker, pcm: bytes) -> None:
        speaker: Speaker | None = CHANNELS.get(channel) if isinstance(channel, int) else channel  # type: ignore[assignment]
        if speaker is None or not self.active:
            return
        t = self.transcribers.get(speaker)
        if t is None:
            return  # e.g. `me` audio while transcribe_me is off
        self.frames_in[speaker] += 1
        self.last_activity = time.time()
        await t.send(pcm)

    async def handle_control(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "hello":
            if not self._started:
                if isinstance(msg.get("channels"), list):
                    chans = {str(c) for c in msg["channels"]}
                    self.transcribe_them = "them" in chans
                    self.transcribe_me = "me" in chans
                elif "transcribe_me" in msg:
                    self.transcribe_me = bool(msg["transcribe_me"])
            if msg.get("engine") in self.cfg.brain.engines:
                self.engine = msg["engine"]
            if not self._started:
                await self.set_language(msg.get("language"), msg.get("answer_language"))
            if "auto" in msg:
                self.auto_enabled = bool(msg["auto"])
            if "knowledge" in msg:
                self.knowledge_enabled = bool(msg["knowledge"]) and self.knowledge is not None
            await self.start()
            self._broadcast({"type": "status", "stt": dict(self.stt_status), "brain": self.brain_status,
                             "auto": self.auto_enabled, "engine": self.engine, "knowledge": self.knowledge_enabled,
                             "transcribe_me": self.transcribe_me})
        elif kind == "end":
            await self.stop()
        elif kind == "auto":
            self.auto_enabled = bool(msg.get("enabled", True))
            if not self.auto_enabled and self._auto_task is not None:
                self._auto_task.cancel()
            self._broadcast({"type": "status", "auto": self.auto_enabled})
        elif kind == "answer":
            q = msg.get("question")
            eng = msg.get("engine")
            await self.request_answer(question=str(q) if q else None, source="manual",
                                      engine=str(eng) if eng else None)
        elif kind == "language":
            await self.set_language(msg.get("stt"), msg.get("answer"))
        elif kind == "engine":
            name = str(msg.get("name") or "")
            if name in self.cfg.brain.engines:
                self.engine = name
                self._broadcast({"type": "status", "engine": name})
        elif kind == "knowledge":
            self.knowledge_enabled = bool(msg.get("enabled")) and self.knowledge is not None
            self._broadcast({"type": "status", "knowledge": self.knowledge_enabled})
        elif kind == "cancel":
            await self.cancel_answer()
        elif kind == "summary":
            asyncio.create_task(self._summary())
        elif kind == "reload_context":
            if self.brain is not None:
                self.brain.reload_context()
                self._broadcast({"type": "status", "detail": "context reloaded"})
        elif kind == "ping":
            self._broadcast({"type": "pong"})
        else:
            self._broadcast({"type": "error", "message": f"unknown control message: {kind}"})

    # ------------------------------------------------------------------- output
    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.subscribers.add(q)
        self.last_client_seen = time.time()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        self.last_client_seen = time.time()

    def snapshot_event(self) -> dict[str, Any]:
        """State for a (re)connecting client: status + recent transcript."""
        return {
            "type": "snapshot",
            "session_id": self.id,
            "started_at": self.started_at,
            "ended": self.ended_at is not None,
            "stt": dict(self.stt_status),
            "brain": self.brain_status,
            "auto": self.auto_enabled,
            "engine": self.engine,
            "language": self.language,
            "answer_language": self.answer_language,
            "knowledge": self.knowledge_enabled,
            "transcribe_me": self.transcribe_me,
            "transcript": [
                {"speaker": u.speaker, "text": u.text, "ts": u.ts, "final": True, "id": u.id}
                for u in self.buffer.window(self.cfg.brain.window_seconds * 2)
            ],
            # past answers, so a reloaded page shows the whole conversation, not just the next answer
            "answers": [
                {k: a.get(k) for k in ("id", "ts", "question", "text", "status", "source", "engine", "usage", "sources",
                                       "typed", "engine_label")}
                for a in self.answers[-50:] if a.get("status") != "cancelled" and a.get("text")
            ],
        }

    def _broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # a stalled client must not slow the meeting down: drop the oldest event for it
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ------------------------------------------------------------ STT callbacks
    async def _on_stt_event(self, ev: TranscriptEvent) -> None:
        self.last_activity = time.time()
        spk = ev.speaker
        if ev.kind == "transcript":
            pending = self._pending[spk]
            if ev.final:
                prev_end = self._pending_word_end.get(spk)
                if pending and prev_end is not None and ev.word_start is not None \
                        and ev.word_start - prev_end > self.phrase_gap_seconds:
                    self._commit(spk)
                if not pending:
                    self._pending_ts[spk] = ev.ts
                pending.append(ev.text)
                if ev.word_end is not None:
                    self._pending_word_end[spk] = ev.word_end
                if ev.speech_final:
                    self._commit(spk)
                else:
                    self._show_interim(spk, " ".join(pending), ev.ts)
            else:
                self._show_interim(spk, " ".join([*pending, ev.text]), ev.ts)
        elif ev.kind == "utterance_end":
            self._commit(spk)
            self._broadcast({"type": "utterance_end", "speaker": ev.speaker, "ts": ev.ts})
        elif ev.kind == "speech_started":
            self._broadcast({"type": "speech_started", "speaker": ev.speaker, "ts": ev.ts})

    def _show_interim(self, speaker: Speaker, text: str, ts: float) -> None:
        self.buffer.set_interim(speaker, text)
        self._broadcast({"type": "transcript", "speaker": speaker, "text": text, "final": False, "ts": ts})

    def _commit(self, speaker: Speaker) -> None:
        """Turn the pending final segments of `speaker` into one utterance."""
        pending = self._pending[speaker]
        if not pending:
            return
        text = " ".join(pending)
        pending.clear()
        self._pending_word_end.pop(speaker, None)
        u = self.buffer.add_final(speaker, text, ts=self._pending_ts.pop(speaker, time.time()))
        if u is not None:
            self._broadcast({"type": "transcript", "speaker": speaker, "text": u.text, "final": True,
                             "ts": u.ts, "id": u.id})
            self._auto_on_utterance(u)

    async def _on_stt_status(self, speaker: Speaker, status: Status, detail: str) -> None:
        self.stt_status[speaker] = status
        event: dict[str, Any] = {"type": "status", "stt": dict(self.stt_status), "brain": self.brain_status}
        if detail:
            event["detail"] = f"{speaker}: {detail}"
        self._broadcast(event)

    # ------------------------------------------------------------------ answers
    async def request_answer(self, question: str | None = None, source: str = "manual",
                             engine: str | None = None) -> int | None:
        """Start answering. ``question`` None = take the last question from the transcript."""
        if self.brain is None:
            self._broadcast({"type": "error", "message": f"Ответы недоступны: {self.brain_error or 'brain не настроен'}"})
            return None
        await self.cancel_answer()
        typed = question is not None
        if question is None:
            question = self.buffer.last_question()
            if question is None:
                last = self.buffer.last_them_utterances(1)
                question = last[0].text if last else None
        transcript = self.buffer.format_window(self.cfg.brain.window_seconds, include_interim=True)
        engine = engine if engine in self.cfg.brain.engines else self.engine
        self._answer_seq += 1
        aid = self._answer_seq
        self._answer_task = asyncio.create_task(
            self._run_answer(aid, question, transcript, source, typed, engine))
        return aid

    # ------------------------------------------------------- meeting prep
    def personal(self) -> str | None:
        from copilot.brain.prompt import materials_map, personal_block

        profile = ""
        if self._profile_getter is not None:
            try:
                profile = self._profile_getter() or ""
            except Exception:
                log.exception("profile read failed")
        return personal_block(profile, self.brief, materials_map([d.title for d in self.session_docs]))

    def add_session_docs(self, docs: list) -> None:
        """Attach files to this meeting and index them right away (call off the event loop for big files),
        so the first answer during the meeting doesn't wait for indexing."""
        from copilot.knowledge.importers import dedupe
        from copilot.knowledge.index import KnowledgeIndex

        docs = dedupe(docs)
        have = {d.id for d in self.session_docs}
        if self._session_index is not None and not any(d.id in have for d in docs):
            self._session_index.extend(docs)  # only the new file is indexed
            self.session_docs = [*self.session_docs, *docs]
            self._keyterms_dirty = True
            return
        merged = dedupe([*self.session_docs, *docs])  # a re-upload replaces: rebuild once
        index = KnowledgeIndex(merged)
        self.session_docs, self._session_index = merged, index
        self._keyterms_dirty = True

    def remove_session_doc(self, doc_id: str) -> bool:
        before = len(self.session_docs)
        self.session_docs = [d for d in self.session_docs if d.id != doc_id]
        self._session_index = None
        self._keyterms_dirty = True
        return len(self.session_docs) != before

    def _search_all(self, query: str) -> list:
        """Files for this meeting come first, then the saved private store when «Мои чаты» is on.
        Scores of two different indexes aren't comparable, so the lists are interleaved by rank."""
        k = self.cfg.knowledge.top_k
        session_hits, saved_hits = [], []
        if self.session_docs:
            if self._session_index is None:
                from copilot.knowledge.index import KnowledgeIndex

                self._session_index = KnowledgeIndex(self.session_docs)
            session_hits = self._session_index.search(query, k=k)
        if self.knowledge_enabled and self.knowledge is not None:
            saved_hits = self.knowledge.search(query, k=k)
        hits = []
        for i in range(k):
            for lst in (session_hits, saved_hits):
                if i < len(lst):
                    hits.append(lst[i])
        return hits[:k]

    @property
    def _keyterms(self) -> list[str]:
        """Speech-recognition hints, computed when STT (re)opens: the user's own word list first, then names
        from «Обо мне» and the brief, the meeting's files and the saved chats (glossary.md is added by the factory)."""
        from copilot.knowledge.keyterms import doc_keyterms, merge_keyterms, parse_vocab, text_keyterms

        if self._keyterms_dirty:
            self._keyterms_cache = doc_keyterms(d.text for d in self.session_docs)
            self._keyterms_dirty = False
        vocab, profile = "", ""
        try:
            vocab = self._vocab_getter() if self._vocab_getter else ""
            profile = self._profile_getter() if self._profile_getter else ""
        except Exception:
            log.exception("keyterm sources failed")
        # The whole saved-chats base is deliberately NOT a source: it is too broad, and irrelevant hints hurt
        # recognition (measured). This meeting's files, the brief and «Обо мне» are specific to the people talking.
        # most specific first: the user's own list, this meeting's brief and files, then «Обо мне»
        return merge_keyterms(parse_vocab(vocab), text_keyterms(self.brief), self._keyterms_cache,
                              text_keyterms(profile), limit=50)

    async def refresh_speech_hints(self) -> None:
        """New words for recognition (vocabulary edited during a meeting): reopen STT (≈1 s gap)."""
        if self.active and self.transcribers:
            await self._reopen_transcribers()

    def _has_knowledge(self) -> bool:
        return bool(self.session_docs) or (self.knowledge_enabled and self.knowledge is not None)

    async def _retrieve_smart(self, question: str | None, typed: bool) -> tuple[str | None, list[dict]]:
        """Plain search first (fast); if it finds nothing, ask the small model for synonyms (RU/EN) and retry.
        Costs ~0.5–1 s only on misses, and only when there are files/chats to search."""
        knowledge, sources = await asyncio.to_thread(self._retrieve, question, typed)
        if knowledge or not question or not self._has_knowledge() or self.brain is None:
            return knowledge, sources
        try:
            extra = await asyncio.wait_for(self.brain.expand_query(question), timeout=2.0)
        except Exception as exc:  # timeout, API error, fake brain — answer without files
            log.info("query expansion skipped: %s", exc.__class__.__name__)
            return knowledge, sources
        if not extra:
            return knowledge, sources
        return await asyncio.to_thread(self._retrieve, question, typed, extra)

    def _retrieve(self, question: str | None, typed: bool = False, extra: str = "") -> tuple[str | None, list[dict]]:
        """Search this user's private material only: this meeting's files + their own saved store.
        A typed question is searched as is; a meeting question also uses the last `them` lines."""
        if not self.session_docs and not (self.knowledge_enabled and self.knowledge is not None):
            return None, []
        recent = "" if typed else " ".join(u.text for u in self.buffer.last_them_utterances(2))
        query = f"{question or ''} {recent} {extra}".strip()
        if not query:
            return None, []
        try:
            hits = self._search_all(query)
        except Exception:
            log.exception("knowledge search failed")
            return None, []
        from copilot.knowledge.index import excerpt

        parts, sources = [], []
        share = max(600, self.cfg.knowledge.max_chars // max(1, len(hits)))  # fair share per hit
        for h in hits:
            c = h.chunk
            text = excerpt(h.context or c.text, h.groups, share)  # the lines around the match + neighbours
            if not text:
                continue
            parts.append(f"### {c.title} ({c.date or 'без даты'})\n{text}")
            sources.append({"title": c.title, "date": c.date, "source": c.source, "score": h.score})
        return ("\n\n".join(parts) or None), sources

    async def cancel_answer(self) -> None:
        t = self._answer_task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._answer_task = None

    async def wait_answer(self) -> None:
        if self._answer_task is not None:
            try:
                await self._answer_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_answer(self, aid: int, question: str | None, transcript: str, source: str, typed: bool,
                          engine: str) -> None:
        assert self.brain is not None
        started = time.time()
        self._set_brain("thinking")
        knowledge, sources = await self._retrieve_smart(question, typed)
        eng = self.cfg.brain.engines.get(engine)
        self._broadcast({"type": "answer_start", "id": aid, "question": question or "", "source": source,
                         "typed": typed, "ts": started, "engine": engine,
                         "engine_label": eng.label if eng else engine, "sources": sources})
        chunks: list[str] = []
        status = "done"
        try:
            async for delta in self.brain.answer(question, transcript, engine=engine, knowledge=knowledge,
                                                 personal=self.personal(), answer_language=self.answer_language):
                if not chunks:
                    self._set_brain("streaming")
                chunks.append(delta)
                self._broadcast({"type": "answer_delta", "id": aid, "text": delta})
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = "error"
            log.exception("answer %s failed", aid)
            self._broadcast({"type": "error", "message": f"Ошибка ответа: {exc}"})
        finally:
            usage = getattr(self.brain, "last_usage", None)
            u = usage.as_dict() if usage is not None else {}
            if usage is not None and status == "done":
                self.tokens["input"] += usage.input_tokens
                self.tokens["output"] += usage.output_tokens
                self.tokens["cache_read"] += usage.cache_read_input_tokens
                self.tokens["cache_write"] += usage.cache_creation_input_tokens
            text = "".join(chunks)
            eng_cfg = self.cfg.brain.engines.get(engine)
            self.answers.append({"id": aid, "ts": started, "question": question, "text": text,
                                 "status": status, "source": source, "engine": engine, "usage": u,
                                 "sources": sources, "typed": typed,
                                 "engine_label": eng_cfg.label if eng_cfg else engine})
            self._broadcast({"type": "answer_end", "id": aid, "status": status, "text": text, "usage": u})
            self._set_brain("idle")

    async def _summary(self) -> None:
        if self.brain is None:
            self._broadcast({"type": "error", "message": f"Итоги недоступны: {self.brain_error or 'brain не настроен'}"})
            return
        from copilot.transcript import format_utterances

        items = self.buffer.all()
        if not items:
            self._broadcast({"type": "summary", "markdown": "_Транскрипт пуст._"})
            return
        self._set_brain("thinking")
        try:
            md = await self.brain.summarize(format_utterances(items), personal=self.personal())
            self._broadcast({"type": "summary", "markdown": md})
        except Exception as exc:
            log.exception("summary failed")
            self._broadcast({"type": "error", "message": f"Ошибка итогов: {exc}"})
        finally:
            self._set_brain("idle")

    # --------------------------------------------------------------------- auto
    def _auto_on_utterance(self, u) -> None:
        """Debounced: after `them` stops talking, ask the cheap filter whether Den should answer."""
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()  # newer speech supersedes the pending check
        if not self.auto_enabled or self.brain is None or u.speaker != "them":
            return  # Den speaking (`me`) also cancels a pending check: he is already answering
        if u.words < self.cfg.trigger.min_words_auto and not u.text.rstrip().endswith("?"):
            return
        try:
            self._auto_task = asyncio.get_running_loop().create_task(self._auto_check(u.id))
        except RuntimeError:  # no running loop (sync tests)
            pass

    async def _auto_check(self, utterance_id: int) -> None:
        await asyncio.sleep(self.cfg.trigger.debounce_seconds)
        if utterance_id <= self._auto_last_id:
            return
        transcript = self.buffer.format_window(60)
        try:
            answer, question = await self.brain.should_answer(transcript)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("auto filter failed: %s", exc)
            return
        fu = getattr(self.brain, "last_filter_usage", None)
        if fu is not None:
            self.tokens["input"] += fu.input_tokens
            self.tokens["output"] += fu.output_tokens
        self._broadcast({"type": "auto_check", "answer": answer, "question": question})
        if answer:
            self._auto_last_id = utterance_id
            await self.request_answer(question=None, source="auto")

    def _set_brain(self, status: str) -> None:
        if status != self.brain_status:
            self.brain_status = status
            self._broadcast({"type": "status", "brain": status})

    # -------------------------------------------------------------------- usage
    def usage(self) -> dict[str, Any]:
        return {
            "stt_seconds": {spk: round(t.audio_seconds, 1) for spk, t in self.transcribers.items()},
            "frames_in": dict(self.frames_in),
            "utterances": len(self.buffer),
            "answers": len(self.answers),
            "tokens": dict(self.tokens),
            "duration_s": round((self.ended_at or time.time()) - self.started_at, 1),
        }


class SessionManager:
    """Owns live sessions; reaps the ones nobody is connected to (CLAUDE.md §5.6: reconnect keeps the session)."""

    def __init__(self, cfg: Config, transcriber_factory: TranscriberFactory,
                 brain: Brain | None = None, brain_error: str | None = None):
        self.cfg = cfg
        self.factory = transcriber_factory
        self.brain = brain
        self.brain_error = brain_error
        self._stores: dict = {}
        self.sessions: dict[str, MeetingSession] = {}
        self._reaper: asyncio.Task | None = None

    def create(self, session_id: str | None = None, **kwargs) -> MeetingSession:
        sid = session_id or uuid.uuid4().hex[:12]
        if sid in self.sessions:
            return self.sessions[sid]
        kwargs.setdefault("brain", self.brain)
        kwargs.setdefault("brain_error", self.brain_error)
        user = kwargs.get("user_id", "owner")
        kwargs.setdefault("knowledge", self.knowledge_for(user))
        kwargs.setdefault("profile_getter", lambda u=user: self.store_for(u).read_profile())
        kwargs.setdefault("vocab_getter", lambda u=user: self.store_for(u).read_vocab())
        s = MeetingSession(sid, self.cfg, self.factory, **kwargs)
        self.sessions[sid] = s
        return s

    def store_for(self, user_id: str):
        """The private store object of exactly this user (may be empty)."""
        from copilot.knowledge import KnowledgeStore

        store = self._stores.get(user_id)
        if store is None:
            store = self._stores[user_id] = KnowledgeStore(self.cfg.knowledge.dir, user_id)
        return store

    def knowledge_for(self, user_id: str):
        """The saved chats store of exactly this user (None when disabled or empty)."""
        if not self.cfg.knowledge.enabled:
            return None
        store = self.store_for(user_id)
        return store if store.path.exists() else None

    def attach_store(self, user_id: str) -> None:
        """After the first save into a user's store, give it to that user's live sessions."""
        store = self.knowledge_for(user_id)
        for s in self.sessions.values():
            if s.user_id == user_id and s.knowledge is None and store is not None:
                s.knowledge = store
                s.knowledge_enabled = True

    def get(self, session_id: str) -> MeetingSession | None:
        return self.sessions.get(session_id)

    def get_or_create(self, session_id: str) -> MeetingSession:
        return self.sessions.get(session_id) or self.create(session_id)

    async def close(self, session_id: str) -> None:
        s = self.sessions.pop(session_id, None)
        if s is not None:
            await s.stop()

    async def start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="session-reaper")

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        await asyncio.gather(*(s.stop() for s in self.sessions.values()), return_exceptions=True)

    async def _reap_loop(self) -> None:
        grace = self.cfg.server.session_grace_seconds
        while True:
            await asyncio.sleep(15)
            now = time.time()
            for sid, s in list(self.sessions.items()):
                idle = not s.subscribers and (now - s.last_client_seen) > grace
                if idle or (s.ended_at is not None and (now - s.ended_at) > grace):
                    log.info("reaping session %s (idle=%s)", sid, idle)
                    await self.close(sid)
