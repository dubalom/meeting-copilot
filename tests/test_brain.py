import asyncio
import json

import numpy as np
from fastapi.testclient import TestClient

from copilot.brain.base import Brain, Usage
from copilot.brain.prompt import answer_user_message, load_context, system_blocks
from copilot.config import Config
from copilot.server.api import create_app
from copilot.server.ws import encode_audio_frame
from copilot.session import MeetingSession
from copilot.stt.base import TranscriptEvent
from tests.test_stt_and_server import FakeTranscriber, _drain_until


class FakeBrain(Brain):
    def __init__(self, chunks=("Суть: ", "да.", " Пункт."), delay=0.0):
        self.chunks = chunks
        self.delay = delay
        self.calls: list[tuple] = []
        self.engines: list = []
        self.knowledge: list = []
        self.personal: list = []
        self.answer_languages: list = []
        self.last_usage = Usage()

    async def answer(self, question, transcript, *, engine=None, knowledge=None, personal=None, answer_language=None):
        self.answer_languages.append(answer_language)
        self.calls.append((question, transcript))
        self.personal.append(personal)
        self.engines.append(engine)
        self.knowledge.append(knowledge)
        self.last_usage = Usage(model="fake", input_tokens=100, output_tokens=5, cache_read_input_tokens=80)
        for c in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield c

    async def summarize(self, transcript, personal=None):
        return "**Тема** — тест"


# ------------------------------------------------------------------ prompt
def test_load_context_order_and_comment_stripping(tmp_path):
    (tmp_path / "snippets.md").write_text("S", encoding="utf-8")
    (tmp_path / "role.md").write_text("<!-- hint -->R", encoding="utf-8")
    (tmp_path / "extra.md").write_text("E", encoding="utf-8")
    (tmp_path / "project.md").write_text("<!-- only a hint -->\n", encoding="utf-8")
    ctx = load_context(tmp_path)
    assert ctx == "### role.md\nR\n\n### snippets.md\nS\n\n### extra.md\nE"


def test_system_blocks_cache_only_on_context():
    blocks = system_blocks("ctx", name="Den")
    assert "Den" in blocks[0]["text"] and "cache_control" not in blocks[0]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert len(system_blocks("  ")) == 1
    msg = answer_user_message("[1] them: hi", "Как дела?")
    assert "Как дела?" in msg and "[1] them: hi" in msg


# ------------------------------------------------------------------ session
def _session(brain):
    return MeetingSession("b", Config(), FakeTranscriber, transcribe_me=False, brain=brain)


def test_answer_uses_last_question_and_streams_events():
    async def run():
        brain = FakeBrain()
        s = _session(brain)
        q = s.subscribe()
        await s.start()
        await s._on_stt_event(TranscriptEvent("them", text="What was retention last month?", final=True, speech_final=True))
        aid = await s.request_answer()
        await s.wait_answer()
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return brain, s, aid, events

    brain, s, aid, events = asyncio.run(run())
    assert brain.calls[0][0] == "What was retention last month?"
    assert "them: What was retention last month?" in brain.calls[0][1]
    kinds = [e["type"] for e in events if e["type"].startswith("answer")]
    assert kinds == ["answer_start", "answer_delta", "answer_delta", "answer_delta", "answer_end"]
    end = [e for e in events if e["type"] == "answer_end"][0]
    assert end["text"] == "Суть: да. Пункт." and end["status"] == "done" and end["id"] == aid
    assert s.tokens == {"input": 100, "output": 5, "cache_read": 80, "cache_write": 0}
    assert [e["brain"] for e in events if e["type"] == "status" and "brain" in e and "stt" not in e] == ["thinking", "streaming", "idle"]


def test_new_answer_cancels_previous():
    async def run():
        s = _session(FakeBrain(chunks=("a",) * 50, delay=0.01))
        await s.start()
        await s.request_answer(question="first")
        await asyncio.sleep(0.05)
        await s.request_answer(question="second")
        await s.wait_answer()
        return s.answers

    answers = asyncio.run(run())
    assert [(a["question"], a["status"]) for a in answers] == [("first", "cancelled"), ("second", "done")]


def test_answer_without_brain_reports_error():
    async def run():
        s = MeetingSession("x", Config(), FakeTranscriber, brain=None, brain_error="ANTHROPIC_API_KEY не задан")
        q = s.subscribe()
        await s.request_answer()
        return q.get_nowait()

    ev = asyncio.run(run())
    assert ev["type"] == "error" and "ANTHROPIC_API_KEY" in ev["message"]


# ------------------------------------------------------------------ over the websocket
def test_ws_answer_and_summary_roundtrip():
    FakeTranscriber.instances.clear()
    brain = FakeBrain()
    pcm = np.zeros(640, dtype=np.int16).tobytes()
    with TestClient(create_app(Config(), FakeTranscriber, brain=brain)) as client:
        with client.websocket_connect("/ws/session/qa") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "hello", "channels": ["them"]}))
            for _ in range(25):
                ws.send_bytes(encode_audio_frame(0, pcm))
            _drain_until(ws, lambda e: e["type"] == "transcript" and e["final"])
            ws.send_text(json.dumps({"type": "answer", "question": "Свой вопрос?"}))
            end, seen = _drain_until(ws, lambda e: e["type"] == "answer_end")
            start = [e for e in seen if e["type"] == "answer_start"][0]
            assert start["typed"] is True and start["question"] == "Свой вопрос?"
            assert end["text"] == "Суть: да. Пункт."
            ws.send_text(json.dumps({"type": "summary"}))
            summ, _ = _drain_until(ws, lambda e: e["type"] == "summary")
            assert summ["markdown"].startswith("**Тема**")
            ws.send_text(json.dumps({"type": "end"}))
            usage, _ = _drain_until(ws, lambda e: e["type"] == "usage")
            assert usage["answers"] == 1 and usage["tokens"]["cache_read"] == 80


def test_anthropic_brain_disables_thinking_and_handles_empty_text():
    from copilot.brain.anthropic_brain import AnthropicBrain

    class FakeStream:
        def __init__(self, texts):
            self.texts = texts

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                for t in self.texts:
                    yield t
            return gen()

        async def get_final_message(self):
            class M:
                stop_reason = "max_tokens"
                usage = type("U", (), {"input_tokens": 10, "output_tokens": 300})()
            return M()

    class FakeClient:
        def __init__(self):
            self.kwargs = None
            self.messages = self

        def stream(self, **kwargs):
            self.kwargs = kwargs
            return FakeStream([])

    client = FakeClient()
    brain = AnthropicBrain(Config(), client=client)

    async def run():
        return "".join([c async for c in brain.answer("q?", "t")])

    text = asyncio.run(run())
    assert client.kwargs["thinking"] == {"type": "disabled"}
    assert "temperature" not in client.kwargs
    assert "пустой ответ" in text and brain.last_usage.output_tokens == 300


def test_parse_filter_reply():
    from copilot.brain.anthropic_brain import parse_filter_reply

    assert parse_filter_reply('{"answer": true, "question": "D7?"}') == (True, "D7?")
    assert parse_filter_reply('Sure: {"answer": false, "question": ""} done') == (False, "")
    assert parse_filter_reply("not json") == (False, "")
    assert parse_filter_reply('{"answer": "yes"}') == (False, "")  # only a real true counts


class AutoBrain(FakeBrain):
    def __init__(self, verdicts):
        super().__init__()
        self.verdicts = list(verdicts)
        self.checked: list[str] = []

    async def should_answer(self, transcript):
        self.checked.append(transcript)
        return self.verdicts.pop(0), "q"


def _auto_session(brain):
    cfg = Config()
    cfg.trigger.debounce_seconds = 0.05
    s = MeetingSession("a", cfg, FakeTranscriber, transcribe_me=True, brain=brain)
    s.auto_enabled = True
    return s


def test_auto_answers_after_debounce_only_when_filter_says_so():
    async def run():
        brain = AutoBrain([False, True])
        s = _auto_session(brain)
        await s.start()
        say = s._on_stt_event
        await say(TranscriptEvent("them", text="Ну вот такая у нас ситуация сейчас.", final=True, speech_final=True))
        await asyncio.sleep(0.15)
        await say(TranscriptEvent("them", text="Den, what was retention last month?", final=True, speech_final=True))
        await asyncio.sleep(0.15)
        await s.wait_answer()
        return brain, s

    brain, s = asyncio.run(run())
    assert len(brain.checked) == 2
    assert [(a["source"], a["status"]) for a in s.answers] == [("auto", "done")]


def test_auto_debounce_and_me_cancel():
    async def run():
        brain = AutoBrain([True, True])
        s = _auto_session(brain)
        await s.start()
        say = s._on_stt_event
        await say(TranscriptEvent("them", text="Den, can you tell us about the pipeline?", final=True, speech_final=True))
        await say(TranscriptEvent("me", text="Yes, sure, so the pipeline is", final=True, speech_final=True))  # Den answers himself
        await asyncio.sleep(0.15)
        await say(TranscriptEvent("them", text="ok", final=True, speech_final=True))  # too short → ignored
        await asyncio.sleep(0.15)
        return brain, s

    brain, s = asyncio.run(run())
    assert brain.checked == [] and s.answers == []
