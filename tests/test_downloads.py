import os
import time

from fastapi.testclient import TestClient

from copilot.config import load_config
from copilot.server.api import create_app, list_downloads


def _cfg(tmp_path, token=""):
    cfg = load_config()
    cfg.paths.downloads_dir = tmp_path
    cfg.secrets.app_token = token
    return cfg


def test_list_picks_newest_of_each_kind(tmp_path):
    old = tmp_path / "MeetingCopilot-0.1.0-arm64.dmg"
    old.write_bytes(b"a")
    os.utime(old, (time.time() - 100, time.time() - 100))
    (tmp_path / "MeetingCopilot-0.2.0-arm64.dmg").write_bytes(b"bb")
    (tmp_path / "MeetingCopilot-Setup-0.2.0.exe").write_bytes(b"ccc")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "notes.txt").write_text("x")
    builds = list_downloads(tmp_path)
    assert [b["kind"] for b in builds] == ["mac-arm64", "win"]
    assert builds[0]["name"] == "MeetingCopilot-0.2.0-arm64.dmg" and builds[0]["version"] == "0.2.0"
    assert list_downloads(tmp_path / "missing") == []


def test_downloads_are_public_but_only_offered_files(tmp_path):
    (tmp_path / "MeetingCopilot-0.1.0-x64.dmg").write_bytes(b"dmg")
    (tmp_path / "secret.txt").write_text("nope")
    app = create_app(_cfg(tmp_path, token="t0ken"), transcriber_factory=lambda *a, **k: None, brain=object())
    c = TestClient(app)
    assert c.get("/api/config").status_code == 401          # the app itself still needs the token
    r = c.get("/api/downloads")
    assert r.status_code == 200 and r.json()["builds"][0]["kind"] == "mac-x64"
    f = c.get("/downloads/MeetingCopilot-0.1.0-x64.dmg")
    assert f.status_code == 200 and f.content == b"dmg" and "attachment" in f.headers["content-disposition"]
    assert c.get("/downloads/secret.txt").status_code == 404
    assert c.get("/downloads/..%2Fsecret.txt").status_code == 404
    assert c.get("/download").status_code == 200


def test_tokens_are_redacted_from_server_logs():
    import logging

    from copilot.server.api import RedactTokens

    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                            '%s - "WebSocket %s" [accepted]', ("127.0.0.1:5", "/ws/session/abc?token=SeCrEt123&x=1"), None)
    RedactTokens().filter(rec)
    assert "SeCrEt123" not in rec.getMessage() and "token=***" in rec.getMessage()
