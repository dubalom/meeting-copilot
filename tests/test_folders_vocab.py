"""Folders and nested zips, more file types, speech-recognition vocabulary, answers restored on reconnect."""

import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from copilot.brain.prompt import materials_map
from copilot.config import Config
from copilot.knowledge.importers import import_bytes
from copilot.knowledge.index import KnowledgeIndex
from copilot.knowledge.keyterms import merge_keyterms, parse_vocab, text_keyterms
from copilot.server.api import create_app
from copilot.session import MeetingSession
from tests.test_brain import FakeBrain
from tests.test_stt_and_server import FakeTranscriber


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _xlsx(rows):
    shared = sorted({c for r in rows for c in r if isinstance(c, str)})
    idx = {s: i for i, s in enumerate(shared)}
    xml_rows = "".join(
        "<row>" + "".join(f'<c t="s"><v>{idx[c]}</v></c>' if isinstance(c, str) else f"<c><v>{c}</v></c>" for c in r) + "</row>"
        for r in rows)
    return _zip({
        "xl/workbook.xml": '<workbook><sheets><sheet name="Бюджет" sheetId="1"/></sheets></workbook>',
        "xl/sharedStrings.xml": "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        "xl/worksheets/sheet1.xml": f"<worksheet><sheetData>{xml_rows}</sheetData></worksheet>",
    })


def _pptx(slides):
    files = {f"ppt/slides/slide{i}.xml": "<p:sld>" + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in s) + "</p:sld>"
             for i, s in enumerate(slides, 1)}
    return _zip(files)


def project_zip():
    inner = _zip({"notes.md": "Ретро: релиз витрины перенесли на 2 декабря"})
    return _zip({
        "Проект Альфа/sql/dm_visits.sql": "-- дедупликация визитов\nselect *, row_number() over (partition by visit_id) from stg.visits;",
        "Проект Альфа/договоры/2025/договор.txt": "Штраф за простой 0,5% в час".encode("cp1251"),
        "Проект Альфа/финансы/budget.xlsx": _xlsx([["Статья", "Сумма"], ["Лицензии Greenplum", 4200000]]),
        "Проект Альфа/презентации/kickoff.pptx": _pptx([["Цели проекта", "Запуск витрины к Q1"], ["Риски: нет доступа к DataSpot"]]),
        "Проект Альфа/analysis.ipynb": json.dumps({"cells": [{"cell_type": "code", "source": ["df.groupby('day7').mean()"]},
                                                            {"cell_type": "markdown", "source": "Retention D7 = 31%"}]}),
        "Проект Альфа/data/players.csv": "player_id,ggr\n1,100\n2,250\n",
        "Проект Альфа/config/airflow.json": json.dumps({"dag": "dm_visits_daily", "schedule": "0 3 * * *"}),
        "Проект Альфа/архив.zip": inner,
        "Проект Альфа/.git/config": "[core]",
        "Проект Альфа/node_modules/x/index.js": "junk",
        "Проект Альфа/фото.jpg": b"\xff\xd8\xff",
        "Проект Альфа/empty.txt": "",
    })


def test_folder_zip_keeps_paths_reads_many_formats_and_reports_skips():
    report = {}
    docs = import_bytes("Проект Альфа.zip", project_zip(), report)
    by = {d.title: d.text for d in docs}
    assert "Проект Альфа/sql/dm_visits.sql" in by and "row_number" in by["Проект Альфа/sql/dm_visits.sql"]
    assert "0,5% в час" in by["Проект Альфа/договоры/2025/договор.txt"]  # cp1251 decoded
    assert "Лицензии Greenplum\t4200000" in by["Проект Альфа/финансы/budget.xlsx"]
    assert "Риски: нет доступа к DataSpot" in by["Проект Альфа/презентации/kickoff.pptx"]
    assert "Retention D7 = 31%" in by["Проект Альфа/analysis.ipynb"]
    assert "dm_visits_daily" in by["Проект Альфа/config/airflow.json"]
    assert "2 декабря" in by["Проект Альфа/архив/notes.md"]  # nested zip lives under its own folder
    assert not any(".git" in t or "node_modules" in t or t.endswith(".jpg") for t in by)
    assert report["skipped"] == ["Проект Альфа/empty.txt"]  # media/.git never even unpacked; empty file reported
    # a zip without a top folder gets the archive name as its folder
    docs = import_bytes("материалы.zip", _zip({"a.md": "текст про витрину"}))
    assert docs[0].title == "материалы/a.md"
    # a zip that sits inside an uploaded folder keeps the folder path
    docs = import_bytes("Проект/старое/архив.zip", _zip({"b.md": "старый план"}))
    assert docs[0].title == "Проект/старое/архив/b.md"


def test_search_finds_files_by_folder_and_content():
    idx = KnowledgeIndex(import_bytes("Проект Альфа.zip", project_zip()))
    assert idx.search("Какой штраф за простой по договору?")[0].chunk.title.endswith("договор.txt")
    assert idx.search("Какие риски в презентации проекта?")[0].chunk.title.endswith("kickoff.pptx")
    assert idx.search("как дедуплицировать визиты row_number")[0].chunk.title.endswith("dm_visits.sql")


def test_materials_map_groups_by_folder():
    m = materials_map(["Проект/sql/a.sql", "Проект/sql/b.sql", "Проект/договор.pdf", "Telegram: DWH"])
    assert m.startswith("4 файлов") and "- Проект/sql/: a.sql, b.sql" in m and "Telegram: DWH" in m
    assert materials_map([]) is None
    assert "показаны 80" in materials_map([f"папка/f{i}.md" for i in range(120)])


def test_vocabulary_parsing_and_merge():
    assert parse_vocab("Вардан, Greenplum\n dm_visits ; ретеншен\nGreenplum") == ["Вардан", "Greenplum", "dm_visits", "ретеншен"]
    terms = text_keyterms("Я работаю с Greenplum и BigQuery. Мой руководитель Павел Козловский, а заказчик Вардан.")
    assert {"Greenplum", "BigQuery", "Павел", "Козловский", "Вардан"} <= set(terms) and "Мой" not in terms
    assert merge_keyterms(["SLA", "dbt"], ["sla", "Airflow"], limit=3) == ["SLA", "dbt", "Airflow"]


def test_vocab_api_feeds_speech_recognition_and_reopens_live_meeting(tmp_path):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    seen = []

    def factory(speaker, on_event, on_status=None, language=None, extra_keyterms=()):
        seen.append(list(extra_keyterms))
        return FakeTranscriber(speaker, on_event, on_status, language=language)

    with TestClient(create_app(cfg, factory, brain=FakeBrain())) as c:
        assert c.get("/api/vocab").json()["text"] == ""
        c.put("/api/profile", json={"text": "Работаю с Greenplum, руководитель Павел Козловский"})
        r = c.put("/api/vocab", json={"text": "Вардан, dm_visits"})
        assert r.json()["terms"] == 2
        with c.websocket_connect("/ws/session/vocab1") as ws:
            ws.send_json({"type": "hello", "channels": ["them"]})
            ws.receive_json()
            for _ in range(20):
                if seen:
                    break
                ws.send_json({"type": "ping"})
                ws.receive_json()
            assert seen[0][:2] == ["Вардан", "dm_visits"] and "Greenplum" in seen[0] and "Козловский" in seen[0]
            n = len(seen)
            c.put("/api/vocab", json={"text": "Вардан, dm_visits, ретеншен"})  # edited during the meeting
            assert len(seen) == n + 1 and "ретеншен" in seen[-1]
            c.put("/api/vocab", json={"text": "Вардан, dm_visits, ретеншен"})  # unchanged → no reconnect
            assert len(seen) == n + 1


def test_reconnect_snapshot_carries_past_answers():
    async def run():
        s = MeetingSession("h", Config(), FakeTranscriber, transcribe_me=False, brain=FakeBrain())
        await s.start()
        await s.request_answer("Первый вопрос?")
        await s.wait_answer()
        await s.request_answer("Второй вопрос?")
        await s.wait_answer()
        snap = s.snapshot_event()
        await s.stop()
        return snap

    snap = asyncio.run(run())
    assert [a["question"] for a in snap["answers"]] == ["Первый вопрос?", "Второй вопрос?"]
    assert snap["answers"][0]["text"] and snap["answers"][0]["engine_label"]


def test_upload_reports_files_and_skips(tmp_path):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    with TestClient(create_app(cfg, FakeTranscriber, brain=FakeBrain())) as c:
        sid = c.post("/api/sessions", json={}).json()["id"]
        r = c.post("/api/upload", params={"name": "Проект Альфа.zip", "scope": "session", "session_id": sid},
                   content=project_zip()).json()
        assert r["added"] >= 8 and r["skipped"] == 1 and r["files"] >= 9
        # a file from a folder upload keeps its path as the title
        r = c.post("/api/upload", params={"name": "Папка/вложенная/план.md", "scope": "session", "session_id": sid},
                   content="План запуска".encode()).json()
        assert r["titles"] == ["Папка/вложенная/план.md"]
        s = c.app.state.sessions.get(sid)
        assert "МАТЕРИАЛЫ К ВСТРЕЧЕ" in s.personal() and "Папка/вложенная/: план.md" in s.personal()


def test_tests_never_see_real_private_data(tmp_path):
    assert Config().knowledge.dir == tmp_path / "private"


def test_keyterm_budget_depends_on_language_mode():
    from copilot.stt import KEYTERMS_MULTI, _budget

    many = [f"Term{i}" for i in range(40)]
    assert len(_budget(many, "multi")) == KEYTERMS_MULTI == 10
    assert len(_budget(many, "ru")) == 40
    assert _budget(["SLA", "sla", "GGR"], "multi") == ["SLA", "GGR"]
