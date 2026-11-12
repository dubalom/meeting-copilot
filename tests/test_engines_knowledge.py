import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from copilot.brain.anthropic_brain import AnthropicBrain
from copilot.config import Config
from copilot.knowledge import KnowledgeStore, import_path
from copilot.knowledge.index import KnowledgeIndex, tokenize
from copilot.knowledge.importers import Doc
from copilot.server.api import create_app
from copilot.session import MeetingSession, SessionManager
from copilot.stt.base import TranscriptEvent
from tests.test_brain import FakeBrain
from tests.test_stt_and_server import FakeTranscriber


# ------------------------------------------------------------------ engines
class _Stream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def g():
            yield "ok"
        return g()

    async def get_final_message(self):
        return type("M", (), {"stop_reason": "end_turn", "usage": type("U", (), {})()})()


class _Client:
    def __init__(self):
        self.calls = []
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        return _Stream()


def test_engine_parameters_per_model_family():
    client = _Client()
    brain = AnthropicBrain(Config(), client=client)

    async def run(engine):
        return "".join([c async for c in brain.answer("q?", "t", engine=engine, knowledge="### chat\nfact")])

    for e in ("fast", "smart", "deep", "max", "nonexistent"):
        asyncio.run(run(e))
    fast, smart, deep, mx, fallback = client.calls
    assert fast["model"] == "claude-haiku-5-5" and fast["thinking"] == {"type": "disabled"} and "extra_body" not in fast
    assert smart["model"] == "claude-sonnet-5-5" and smart["thinking"] == {"type": "between_tools"}
    assert deep["model"] == "claude-opus-5-5" and deep["thinking"] == {"type": "adaptive"}
    assert deep["extra_body"] == {"output_config": {"effort": "medium"}}
    assert mx["model"] == "claude-fable-5-1"
    assert fallback["model"] == "claude-haiku-5-5"  # unknown engine → default engine
    msg = deep["messages"][0]["content"]
    assert "<личные_чаты>" in msg and "Режим «подробно»" in msg
    assert "Режим «подробно»" not in fast["messages"][0]["content"]


def test_session_engine_default_and_per_request():
    async def run():
        brain = FakeBrain()
        s = MeetingSession("e", Config(), FakeTranscriber, transcribe_me=False, brain=brain)
        await s.start()
        await s.handle_control({"type": "engine", "name": "smart"})
        await s.request_answer(question="a")
        await s.wait_answer()
        await s.handle_control({"type": "answer", "question": "b", "engine": "max"})
        await s.wait_answer()
        await s.handle_control({"type": "engine", "name": "bogus"})  # ignored
        return brain, s

    brain, s = asyncio.run(run())
    assert brain.engines == ["smart", "max"] and s.engine == "smart"
    assert [a["engine"] for a in s.answers] == ["smart", "max"]


# ------------------------------------------------------------------ importers
CLAUDE_EXPORT = [{
    "uuid": "c1", "name": "Витрина визитов dm_visits", "created_at": "2026-09-10T10:00:00Z",
    "chat_messages": [
        {"sender": "human", "content": [{"type": "text", "text": "Как дедуплицировать визиты в витрине dm_visits?"}]},
        {"sender": "assistant", "text": "Используй row_number() по visit_id и загрузку по watermark."},
    ],
}]
PROJECTS = [{"uuid": "p1", "name": "Softconstruct", "prompt_template": "Режим встречи: отвечай коротко",
             "docs": [{"filename": "ngr.md", "content": "NGR = GGR - бонусы - налоги"}]}]
CHATGPT = [{"id": "g1", "title": "Retention", "create_time": 1726000000, "mapping": {
    "a": {"message": {"author": {"role": "user"}, "create_time": 1, "content": {"parts": ["What is D7 retention?"]}}},
    "b": {"message": {"author": {"role": "assistant"}, "create_time": 2, "content": {"parts": ["Share of players back on day 7."]}}},
}}]
TELEGRAM = {"chats": {"list": [{"id": 7, "name": "Команда аналитики", "messages": [
    {"type": "message", "from": "Варданян", "date": "2026-10-01T09:00:00", "text": "Den, витрина визитов к пятнице?"},
    {"type": "message", "from": "Den", "date": "2026-10-01T09:01:00",
     "text": [{"type": "plain", "text": "Да, "}, "успею к пятнице"]},
    {"type": "service", "action": "pin_message"},
]}]}}


def test_importers_detect_all_formats(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conversations.json", json.dumps(CLAUDE_EXPORT))
        z.writestr("projects.json", json.dumps(PROJECTS))
        z.writestr("users.json", json.dumps([{"email": "x"}]))  # unknown shape → ignored
    (tmp_path / "claude.zip").write_bytes(buf.getvalue())
    (tmp_path / "chatgpt").mkdir()
    (tmp_path / "chatgpt" / "conversations.json").write_text(json.dumps(CHATGPT))
    (tmp_path / "result.json").write_text(json.dumps(TELEGRAM, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Заметки\nПро ретеншен", encoding="utf-8")

    docs = import_path(tmp_path)
    by = {d.source: d for d in docs}
    assert set(by) == {"claude", "claude-project", "chatgpt", "telegram", "file"}
    assert by["claude"].title == "Витрина визитов dm_visits" and by["claude"].date == "2026-09-10"
    assert "Я: Как дедуплицировать" in by["claude"].text and "Claude: Используй row_number()" in by["claude"].text
    assert "NGR = GGR" in by["claude-project"].text
    assert "Я: What is D7" in by["chatgpt"].text
    assert "Den: Да, успею к пятнице" in by["telegram"].text and "pin_message" not in by["telegram"].text


def test_bm25_search_russian_inflection():
    docs = [Doc("1", "claude", "Витрина визитов", "2026-09-10", "Дедупликация визитов через row_number"),
            Doc("2", "claude", "Отпуск", "2026-08-01", "Билеты в Таиланд и страховка"),
            Doc("3", "claude", "NGR", "", "NGR считается как GGR минус бонусы")]
    idx = KnowledgeIndex(docs)
    hits = idx.search("а витрину по визитам уже доделали?")
    assert [h.chunk.doc_id for h in hits] == ["1"]
    assert idx.search("как считать NGR")[0].chunk.doc_id == "3"
    assert idx.search("погода завтра") == []
    assert tokenize("витрина витрины витрину") == ["витри"] * 3
    assert tokenize("визитам визитов") == ["визит"] * 2
    assert "счита" in tokenize("посчитать") and tokenize("считается") == ["счита"]


def test_long_doc_chunked_and_at_most_two_hits_per_doc():
    text = "\n".join(f"строка {i} про витрину визитов и дубли" for i in range(400))
    idx = KnowledgeIndex([Doc("big", "claude", "Большой чат", "", text)])
    assert len(idx) > 5
    hits = idx.search("витрина дубли", k=3)
    assert len(hits) == 2
    assert all(h.chunk.text.startswith("строка") for h in hits)  # chunks start on a whole line


# ------------------------------------------------------------------ privacy
def _cfg(tmp_path):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    return cfg


def test_store_is_per_user_and_private(tmp_path):
    cfg = _cfg(tmp_path)
    owner = KnowledgeStore(cfg.knowledge.dir, "owner")
    owner.add([Doc("1", "claude", "Витрина визитов", "", "дедупликация визитов через row_number")])
    assert (owner.path.stat().st_mode & 0o777) == 0o600
    mgr = SessionManager(cfg, FakeTranscriber, brain=FakeBrain())
    assert mgr.knowledge_for("owner") is not None
    assert mgr.knowledge_for("alice") is None  # another user never gets Den's store
    assert KnowledgeStore(cfg.knowledge.dir, "../owner").dir != owner.dir  # no path traversal
    s_owner = mgr.create("s1", user_id="owner")
    s_alice = mgr.create("s2", user_id="alice")
    assert s_owner.knowledge is not None and s_alice.knowledge is None


def test_answer_gets_private_context_only_for_owner(tmp_path):
    cfg = _cfg(tmp_path)
    KnowledgeStore(cfg.knowledge.dir, "owner").add(
        [Doc("1", "claude", "Витрина визитов", "2026-09-10", "Витрина визитов: дедупликация через row_number, срок пятница")])

    async def run(user):
        brain = FakeBrain()
        mgr = SessionManager(cfg, FakeTranscriber, brain=brain)
        s = mgr.create(f"s-{user}", user_id=user, transcribe_me=False)
        q = s.subscribe()
        await s.start()
        await s._on_stt_event(TranscriptEvent("them", text="Ден, а витрина по визитам готова?", final=True, speech_final=True))
        await s.request_answer()
        await s.wait_answer()
        start = next(e for e in iter(q.get_nowait, None) if e["type"] == "answer_start")
        return brain.knowledge[0], start["sources"]

    knowledge, sources = asyncio.run(run("owner"))
    assert "row_number" in knowledge and sources[0]["title"] == "Витрина визитов"
    knowledge, sources = asyncio.run(run("alice"))
    assert knowledge is None and sources == []


def test_knowledge_toggle_off(tmp_path):
    cfg = _cfg(tmp_path)
    KnowledgeStore(cfg.knowledge.dir, "owner").add([Doc("1", "claude", "Витрина", "", "витрина визитов готова")])

    async def run():
        brain = FakeBrain()
        s = SessionManager(cfg, FakeTranscriber, brain=brain).create("x", transcribe_me=False)
        await s.start()
        await s.handle_control({"type": "knowledge", "enabled": False})
        await s.request_answer(question="витрина визитов готова?")
        await s.wait_answer()
        return brain.knowledge

    assert asyncio.run(run()) == [None]


def test_api_config_exposes_engines_and_own_knowledge_only(tmp_path):
    cfg = _cfg(tmp_path)
    KnowledgeStore(cfg.knowledge.dir, "owner").add([Doc("1", "claude", "t", "", "текст про витрину")])
    with TestClient(create_app(cfg, FakeTranscriber, brain=FakeBrain())) as client:
        data = client.get("/api/config").json()
    assert [e["id"] for e in data["engines"]] == ["fast", "smart", "deep", "max"]
    assert data["knowledge"]["docs"] == 1


def test_typed_question_searches_only_itself(tmp_path):
    cfg = _cfg(tmp_path)
    KnowledgeStore(cfg.knowledge.dir, "owner").add([
        Doc("1", "claude", "Витрина визитов", "", "витрина визитов дедупликация row_number"),
        Doc("2", "claude", "NGR", "", "NGR считается как GGR минус бонусы и налоги")])
    s = SessionManager(cfg, FakeTranscriber, brain=FakeBrain()).create("t", transcribe_me=False)
    s.buffer.add_final("them", "Ден, а витрина по визитам уже готова?")
    _, typed_sources = s._retrieve("Как посчитать NGR?", typed=True)
    assert [x["title"] for x in typed_sources] == ["NGR"]
