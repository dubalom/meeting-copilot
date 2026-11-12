"""Big uploads before a meeting: nothing is cut, the right message is found, limits and errors are clear."""

import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from copilot.config import Config
from copilot.knowledge import importers
from copilot.knowledge.importers import Doc, ImportFailed, ImportTooLarge, import_bytes
from copilot.knowledge.index import KnowledgeIndex, KnowledgeStore, chunk_doc, excerpt, query_groups
from copilot.server.api import create_app
from copilot.session import SessionManager
from tests.test_brain import FakeBrain
from tests.test_stt_and_server import FakeTranscriber


def _tg_export(n, plant):
    msgs = [{"id": i, "type": "message", "date": f"2026-{1 + i * 12 // n:02d}-{1 + i % 27:02d}T10:00:00",
             "from": "Ирина", "text": plant.get(i, f"проверь витрину продаж, там {i} строк расходится")}
            for i in range(n)]
    data = {"chats": {"list": [{"id": 1, "name": "DWH команда", "messages": msgs}]}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("DataExport/result.json", json.dumps(data, ensure_ascii=False))
        z.writestr("DataExport/chats/chat_001/photos/photo_1.jpg", b"\xff\xd8" * 1000)  # media: skipped
    return buf.getvalue()


def test_huge_chat_is_not_truncated_and_newest_fact_is_found():
    n = 60_000  # ≈ 3.5 M chars — far beyond the old 200k cut
    docs = import_bytes("tg.zip", _tg_export(n, {n - 5: "Итог: дедлайн по витрине dm_visits переносим на 14 ноября"}))
    assert len(docs) == 1 and len(docs[0].text) > 3_000_000
    assert "[2026-" in docs[0].text  # day markers for dating chunks
    hits = KnowledgeIndex(docs).search("Когда дедлайн по витрине визитов?")
    assert hits and "14 ноября" in excerpt(hits[0].context, hits[0].groups, 1200)
    assert hits[0].chunk.date.startswith("2026-12")


def test_identifier_alone_and_english_question_find_russian_chat():
    docs = [Doc("1", "telegram", "Telegram: DWH", "", "Ирина: дедлайн по витрине dm_visits — 14 ноября\nОлег: ок"),
            Doc("2", "telegram", "Telegram: Отпуск", "", "Олег: билеты в Таиланд")]
    idx = KnowledgeIndex(docs)
    assert idx.search("Когда сдаёте dm_visits?")[0].chunk.doc_id == "1"
    assert idx.search("What is the deadline for the visits data mart?")[0].chunk.doc_id == "1"
    assert idx.search("погода завтра") == []


def test_words_in_one_message_beat_words_scattered_over_a_chunk():
    scattered = "\n".join(["A: выгрузка в ClickHouse закончилась", "B: окно поменяли", "C: Oracle обновили"] * 2)
    focused = "A: привет\nB: для выгрузки в Oracle окно с 02:00 до 04:30\nC: ок"
    idx = KnowledgeIndex([Doc("s", "telegram", "S", "", scattered), Doc("f", "telegram", "F", "", focused)])
    assert idx.search("В какое окно выгрузка в Oracle?")[0].chunk.doc_id == "f"


def test_incremental_extend_matches_full_build():
    a = [Doc(str(i), "claude", f"Чат {i}", "", f"обсуждали витрину {i} и партиционирование") for i in range(50)]
    b = [Doc("x", "claude", "Стек", "", "Целевой стек проекта: Airflow, dbt, ClickHouse")]
    full = KnowledgeIndex(a + b)
    inc = KnowledgeIndex(a)
    inc.extend(b)
    q = "Какой стек на проекте?"
    assert [(h.chunk.doc_id, h.score) for h in inc.search(q)] == [(h.chunk.doc_id, h.score) for h in full.search(q)]


def test_chunks_carry_dates_and_excerpt_centres_on_match():
    text = "[2026-01-05]\n" + "\n".join(f"A: строка {i}" for i in range(80)) + "\n[2026-03-09]\nB: SLA 99.95 для Ромашки\n" + \
        "\n".join(f"A: хвост {i}" for i in range(80))
    chunks = chunk_doc(Doc("d", "telegram", "T", "2026-01-05", text))
    hit = next(c for c in chunks if "99.95" in c.text)
    assert hit.date == "2026-03-09" and chunks[0].date == "2026-01-05"
    ex = excerpt(text, query_groups("SLA Ромашки"), 200)
    assert "99.95" in ex and len(ex) <= 200


def test_zip_limits_media_and_broken_archives(monkeypatch):
    monkeypatch.setattr(importers, "MAX_MEMBER_BYTES", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "а " * 5000)
    with pytest.raises(ImportTooLarge):
        import_bytes("x.zip", buf.getvalue())
    with pytest.raises(ImportFailed):
        import_bytes("broken.zip", b"PK\x03\x04" + b"\x00" * 100)
    # an xlsx is a zip too, but it is a format we don't read — not an error about a broken archive
    assert import_bytes("sheet.xlsx", b"PK\x03\x04" + b"\x00" * 100) == []


def test_upload_endpoint_statuses_and_session_priority(tmp_path, monkeypatch):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    with TestClient(create_app(cfg, FakeTranscriber, brain=FakeBrain())) as c:
        sid = c.post("/api/sessions", json={}).json()["id"]
        up = lambda name, raw, scope="session": c.post("/api/upload", params={"name": name, "scope": scope, "session_id": sid},
                                                       content=raw)
        r = up("tg.zip", _tg_export(2000, {1990: "Прод-кластер Greenplum: 48 сегментов"}))
        assert r.status_code == 200 and r.json()["added"] == 1
        assert up("broken.zip", b"PK\x03\x04" + b"\x01" * 50).status_code == 422
        assert up("a.xlsx", b"PK\x03\x04" + b"\x01" * 50).status_code == 422
        monkeypatch.setattr(importers, "MAX_MEMBER_BYTES", 10)
        r = up("big.zip", _tg_export(50, {}))
        assert r.status_code == 413 and "МБ" in r.json()["detail"]
        assert up("e.txt", b"").status_code == 400
        # saved chats + meeting files: the meeting file comes first
        KnowledgeStore(cfg.knowledge.dir, "owner").add(
            [Doc("old", "claude", "Старый чат", "", "в тестовом кластере Greenplum 8 сегментов")])
        s = c.app.state.sessions.get(sid)
        c.app.state.sessions.attach_store("owner")
        _, sources = s._retrieve("Сколько сегментов в кластере Greenplum?", typed=True)
        assert sources[0]["title"] == "Telegram: DWH команда" and any(x["title"] == "Старый чат" for x in sources)


class ExpandingBrain(FakeBrain):
    def __init__(self):
        super().__init__()
        self.expanded = []

    async def expand_query(self, question):
        self.expanded.append(question)
        return "выйти на работу, дата выхода, нотис, start date"


def test_synonym_fallback_only_when_nothing_found(tmp_path):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    brain = ExpandingBrain()
    s = SessionManager(cfg, FakeTranscriber, brain=brain).create("q", transcribe_me=False)
    s.add_session_docs([Doc("o", "chatgpt", "Оффер", "", "Готов выйти на работу с 1 декабря, нотис 2 недели")])

    async def run(q):
        return await s._retrieve_smart(q, typed=True)

    knowledge, _ = asyncio.run(run("Когда вы сможете приступить к работе?"))
    assert knowledge and "1 декабря" in knowledge and brain.expanded == ["Когда вы сможете приступить к работе?"]
    knowledge, _ = asyncio.run(run("Когда выйти на работу?"))  # found directly → no extra model call
    assert knowledge and len(brain.expanded) == 1


def test_stt_start_failure_is_reported_not_fatal():
    from copilot.session import MeetingSession

    calls = []

    def factory(speaker, on_event, on_status=None, language=None):
        calls.append(speaker)
        if speaker == "them":
            raise RuntimeError("Deepgram 401")
        return FakeTranscriber(speaker, on_event, on_status, language=language)

    async def run():
        s = MeetingSession("f", Config(), factory, transcribe_me=True, brain=FakeBrain())
        q = s.subscribe()
        await s.start()
        assert s.stt_status["them"] == "error" and "me" in s.transcribers
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        await s.stop()
        return s, events

    s, events = asyncio.run(run())
    assert calls == ["them", "me"]  # «me» still opened after «them» failed
    assert any(e.get("type") == "error" and "Deepgram 401" in e["message"] for e in events)


def test_meeting_files_give_speech_recognition_keyterms():
    from copilot.knowledge.keyterms import doc_keyterms

    text = ("Штраф за нарушение SLA. SLA 99.95. Данные в Greenplum и ClickHouse, ClickHouse снова, выгрузка из Greenplum. "
            "The end. Done. Free trial. Data is here. Привет")
    terms = doc_keyterms([text])
    assert set(terms[:3]) == {"SLA", "Greenplum", "ClickHouse"}
    assert not {"The", "Done", "Free", "Data"} & set(terms)  # ordinary words would hurt recognition

    seen = {}

    def factory(speaker, on_event, on_status=None, language=None, extra_keyterms=()):
        seen[speaker] = list(extra_keyterms)
        return FakeTranscriber(speaker, on_event, on_status, language=language)

    async def run():
        from copilot.session import MeetingSession
        s = MeetingSession("k", Config(), factory, transcribe_me=False, brain=FakeBrain())
        await asyncio.to_thread(s.add_session_docs, [Doc("c", "file", "contract.pdf", "", text)])
        await s.start()
        await s.stop()

    asyncio.run(run())
    assert "SLA" in seen["them"]
