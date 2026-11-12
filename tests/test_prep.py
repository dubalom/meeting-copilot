import asyncio
import io
import json
import zipfile

from fastapi.testclient import TestClient

from copilot.brain.prompt import personal_block, system_blocks
from copilot.config import Config
from copilot.knowledge.importers import import_bytes
from copilot.server.api import create_app
from tests.test_brain import FakeBrain
from tests.test_stt_and_server import FakeTranscriber


def _docx(paragraphs):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


TG_HTML = """<html><body><div class="page_header"><div class="text bold">Команда аналитики</div></div>
<div class="message default clearfix" id="message1"><div class="body"><div class="pull_right date details" title="01.10.2026 09:00:00">09:00</div>
<div class="from_name">Варданян</div><div class="text">Den, витрина визитов к пятнице?</div></div></div>
<div class="message default clearfix joined" id="message2"><div class="body"><div class="text">И дашборд &amp; отчёт<br>тоже</div></div></div>
</body></html>"""


def test_import_docx_html_and_telegram_html_zip():
    docs = import_bytes("resume.docx", _docx(["Den — Data Analyst", "GreenPlum &amp; BigQuery"]))
    assert docs[0].title == "resume.docx" and "GreenPlum & BigQuery" in docs[0].text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ChatExport_2026-10-19/messages.html", TG_HTML)
        z.writestr("__MACOSX/._messages.html", "junk")
    docs = import_bytes("chat.zip", buf.getvalue())
    assert len(docs) == 1 and docs[0].title == "Telegram: Команда аналитики" and docs[0].date == "2026-10-01"
    assert "Варданян: Den, витрина визитов к пятнице?" in docs[0].text
    assert "Варданян: И дашборд & отчёт\nтоже" in docs[0].text  # joined message keeps the sender
    assert import_bytes("x.bin", b"\x00\x01") == []


def test_personal_block_in_system_prompt():
    p = personal_block("Я аналитик в OTP", "Созвон с Вартаном про витрину")
    blocks = system_blocks("ctx", personal=p)
    assert len(blocks) == 3 and blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert "О ПОЛЬЗОВАТЕЛЕ" in blocks[2]["text"] and "К ЭТОЙ ВСТРЕЧЕ" in blocks[2]["text"]
    assert personal_block("", "  ") is None


def _app(tmp_path, token=""):
    cfg = Config()
    cfg.knowledge.dir = tmp_path / "private"
    cfg.secrets.app_token = token
    brain = FakeBrain()
    return TestClient(create_app(cfg, FakeTranscriber, brain=brain)), brain


def test_prep_flow_profile_brief_session_and_saved_uploads(tmp_path):
    client, brain = _app(tmp_path)
    with client:
        assert client.put("/api/profile", json={"text": "Я Den, data analyst, GreenPlum"}).json()["ok"]
        assert client.get("/api/profile").json()["text"].startswith("Я Den")
        sid = client.post("/api/sessions", json={"id": "prep-1"}).json()["id"]
        assert sid == "prep-1"
        assert client.put(f"/api/sessions/{sid}/brief", json={"text": "Встреча с Вартаном: статус витрины"}).json()["ok"]

        r = client.post(f"/api/upload?name=notes.md&scope=session&session_id={sid}",
                        content="Витрина визитов: осталась сверка с продом, срок пятница".encode())
        assert r.status_code == 200 and r.json()["added"] == 1
        r = client.post("/api/upload?name=conversations.json&scope=saved",
                        content=json.dumps([{"uuid": "c", "name": "NGR формула", "chat_messages": [
                            {"sender": "human", "text": "как считать NGR"},
                            {"sender": "assistant", "text": "NGR = GGR минус бонусы"}]}]).encode())
        assert r.json()["added"] == 1
        bad = client.post(f"/api/upload?name=x.bin&scope=session&session_id={sid}", content=b"\x00\x01")
        assert bad.status_code == 422

        docs = client.get(f"/api/knowledge/docs?session_id={sid}").json()
        assert [d["title"] for d in docs["session"]] == ["notes.md"]
        assert [d["title"] for d in docs["saved"]] == ["NGR формула"]
        assert client.get("/api/config").json()["knowledge"]["docs"] == 1

        with client.websocket_connect(f"/ws/session/{sid}") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "hello", "channels": ["them"]}))
            ws.send_text(json.dumps({"type": "answer", "question": "Витрина визитов готова?"}))
            from tests.test_stt_and_server import _drain_until

            start, _ = _drain_until(ws, lambda e: e["type"] == "answer_start")
            _drain_until(ws, lambda e: e["type"] == "answer_end")
        assert [s["title"] for s in start["sources"]] == ["notes.md"]
        assert "сверка с продом" in brain.knowledge[-1]
        assert "Я Den, data analyst" in brain.personal[-1] and "статус витрины" in brain.personal[-1]

        sdoc = docs["session"][0]["id"]
        assert client.delete(f"/api/knowledge/docs/{sdoc}?session_id={sid}").json()["ok"]
        assert client.get(f"/api/knowledge/docs?session_id={sid}").json()["session"] == []


def test_prep_is_private_between_users(tmp_path, monkeypatch):
    """Another user (a different token → different user_id) can't touch Den's session or profile."""
    from copilot.server import auth as auth_mod

    client, _ = _app(tmp_path)
    with client:
        client.put("/api/profile", json={"text": "секретный профиль Den"})
        client.post("/api/sessions", json={"id": "den-sess"})
        monkeypatch.setattr(auth_mod.Authenticator, "resolve_user", lambda self, request: "alice")
        assert client.get("/api/profile").json()["text"] == ""
        assert client.put("/api/sessions/den-sess/brief", json={"text": "x"}).status_code == 404
        assert client.post("/api/upload?name=a.md&scope=session&session_id=den-sess", content=b"hi").status_code == 404
        assert client.post("/api/sessions", json={"id": "den-sess"}).status_code == 409
        assert client.get("/api/knowledge/docs?session_id=den-sess").json() == {"saved": [], "session": []}
        assert client.post("/api/sessions", json={"id": "../etc"}).status_code == 400
