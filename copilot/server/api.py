"""FastAPI application: REST for sessions + the WebSocket router.

    from copilot.server.api import create_app
    app = create_app(cfg)                      # real Deepgram transcribers
    app = create_app(cfg, transcriber_factory) # injected (tests)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from copilot import __version__
from copilot.config import ANSWER_LANGUAGES, STT_LANGUAGES, Config
from copilot.server.auth import Authenticator
from copilot.server.ws import router as ws_router
from copilot.session import SessionManager, TranscriberFactory

log = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    id: str | None = None
    transcribe_me: bool | None = None
    title: str | None = None


class TextBody(BaseModel):
    text: str = ""


MAX_UPLOAD = 150 * 1024 * 1024  # Caddy allows a bit more (deploy/Caddyfile)
_FILE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

# Desktop builds on the public download page: (kind, label, test on the file name)
DOWNLOAD_KINDS = [
    ("mac-arm64", "Mac на чипе Apple (M1–M4)", lambda n: n.endswith(".dmg") and "arm64" in n),
    ("mac-x64", "Mac на Intel", lambda n: n.endswith(".dmg") and "x64" in n),
    ("win", "Windows 10/11 — установщик", lambda n: n.endswith(".exe")),
    ("win-zip", "Windows — без установки (zip)", lambda n: n.endswith("-win.zip")),
]


def list_downloads(folder: Path) -> list[dict[str, Any]]:
    """Newest build of each kind found in ``folder`` (no secrets — installers only)."""
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.iterdir() if p.is_file() and _FILE.match(p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for kind, label, test in DOWNLOAD_KINDS:
        hit = next((p for p in files if test(p.name)), None)
        if hit:
            ver = __import__("re").search(r"(\d+\.\d+\.\d+)", hit.name)
            out.append({"kind": kind, "label": label, "name": hit.name, "size": hit.stat().st_size,
                        "version": ver.group(1) if ver else None, "url": f"/downloads/{hit.name}"})
    return out
_SID = __import__("re").compile(r"^[A-Za-z0-9_-]{4,64}$")


class RedactTokens(logging.Filter):
    """Server logs must never contain the access token (uvicorn logs WebSocket URLs with ?token=…)."""

    _RE = __import__("re").compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "token=" in msg:
            record.msg, record.args = self._RE.sub(r"\1***", msg), None
        return True


def _install_log_redaction() -> None:
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "copilot"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in lg.filters):
            lg.addFilter(RedactTokens())


def create_app(cfg: Config, transcriber_factory: TranscriberFactory | None = None, brain=None) -> FastAPI:
    _install_log_redaction()
    if transcriber_factory is None:
        from copilot.stt import make_transcriber_factory

        transcriber_factory = make_transcriber_factory(cfg)

    brain_error = None
    if brain is None:
        from copilot.brain.anthropic_brain import make_brain
        from copilot.brain.base import BrainUnavailable

        try:
            brain = make_brain(cfg)
        except BrainUnavailable as exc:
            brain_error = str(exc)
            log.warning("answers disabled: %s", exc)

    manager = SessionManager(cfg, transcriber_factory, brain=brain, brain_error=brain_error)
    auth = Authenticator(cfg.secrets.app_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start_reaper()
        log.info("meeting-copilot server v%s on http://%s:%d", __version__, cfg.server.host, cfg.server.port)
        yield
        await manager.shutdown()

    app = FastAPI(title="Meeting Copilot", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.sessions = manager
    app.state.auth = auth
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def current_user(request: Request) -> str:
        return auth.resolve_user(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "sessions": len(manager.sessions)}

    # ---- public: desktop app downloads (installers carry no secrets; the token is pasted in the app)
    @app.get("/api/downloads")
    async def downloads() -> dict[str, Any]:
        return {"builds": list_downloads(cfg.paths.downloads_dir)}

    @app.get("/downloads/{name}")
    async def download_file(name: str) -> FileResponse:
        offered = {b["name"] for b in list_downloads(cfg.paths.downloads_dir)}
        if name not in offered:
            raise HTTPException(404, "no such build")
        return FileResponse(cfg.paths.downloads_dir / name, filename=name,
                            media_type="application/octet-stream")

    @app.get("/api/config")
    async def public_config(user: str = Depends(current_user)) -> dict[str, Any]:
        """What the web client needs to know (no secrets)."""
        import asyncio

        store = manager.knowledge_for(user)  # only this user's private store
        stats = await asyncio.to_thread(store.stats) if store is not None else None  # may build the index
        return {
            "version": __version__,
            "answers": brain is not None,
            "answers_error": brain_error,
            "engines": [{"id": k, "label": e.label, "model": e.model} for k, e in cfg.brain.engines.items()],
            "default_engine": cfg.brain.default_engine,
            "more_engine": cfg.brain.more_engine,
            "auto_default": cfg.trigger.auto_default,
            "language": cfg.stt.language,
            "languages": [{"id": k, "label": v} for k, v in STT_LANGUAGES],
            "answer_languages": [{"id": k, "label": v} for k, v in ANSWER_LANGUAGES],
            "knowledge": stats,
        }

    @app.post("/api/sessions")
    async def create_session(body: CreateSessionRequest | None = None, user: str = Depends(current_user)) -> dict[str, Any]:
        body = body or CreateSessionRequest()
        if body.id is not None:
            if not _SID.match(body.id):
                raise HTTPException(400, "bad session id")
            existing = manager.get(body.id)
            if existing is not None:
                if existing.user_id != user:
                    raise HTTPException(409, "session id taken")
                return {"id": existing.id, "ws_path": f"/ws/session/{existing.id}", "transcribe_me": existing.transcribe_me}
        s = manager.create(body.id, transcribe_me=body.transcribe_me, user_id=user)
        return {"id": s.id, "ws_path": f"/ws/session/{s.id}", "transcribe_me": s.transcribe_me}

    def own_session(session_id: str, user: str):
        s = manager.get(session_id)
        if s is None or s.user_id != user:
            raise HTTPException(404, "session not found")
        return s

    # ---------------------------------------------------------------- meeting prep (all per user)
    @app.get("/api/profile")
    async def get_profile(user: str = Depends(current_user)) -> dict[str, Any]:
        return {"text": manager.store_for(user).read_profile()}

    @app.put("/api/profile")
    async def put_profile(body: TextBody, user: str = Depends(current_user)) -> dict[str, Any]:
        manager.store_for(user).write_profile(body.text[:20000])
        return {"ok": True, "chars": len(body.text)}

    @app.get("/api/vocab")
    async def get_vocab(user: str = Depends(current_user)) -> dict[str, Any]:
        return {"text": manager.store_for(user).read_vocab()}

    @app.put("/api/vocab")
    async def put_vocab(body: TextBody, user: str = Depends(current_user)) -> dict[str, Any]:
        """«Слова для распознавания»: names and terms for speech recognition. Live meetings of this user
        pick them up right away (their speech recognition reconnects, ≈1 s)."""
        from copilot.knowledge.keyterms import parse_vocab

        text = body.text[:5000]
        store = manager.store_for(user)
        changed = text.strip() != store.read_vocab().strip()
        store.write_vocab(text)
        if changed:
            for s in list(manager.sessions.values()):
                if s.user_id == user:
                    await s.refresh_speech_hints()
        return {"ok": True, "terms": len(parse_vocab(text))}

    @app.put("/api/sessions/{session_id}/brief")
    async def put_brief(session_id: str, body: TextBody, user: str = Depends(current_user)) -> dict[str, Any]:
        s = own_session(session_id, user)
        s.brief = body.text[:20000]
        return {"ok": True, "chars": len(s.brief)}

    @app.post("/api/upload")
    async def upload(request: Request, name: str, scope: str = "session", session_id: str | None = None,
                     user: str = Depends(current_user)) -> dict[str, Any]:
        """Raw file body (no multipart). scope=session → only this meeting; scope=saved → the user's base."""
        import asyncio
        import time as _time

        from copilot.knowledge import import_bytes
        from copilot.knowledge.importers import ImportFailed, ImportTooLarge

        if scope not in ("session", "saved"):
            raise HTTPException(400, "scope must be session or saved")
        s = own_session(session_id, user) if scope == "session" else None
        declared = int(request.headers.get("content-length") or 0)
        if declared > MAX_UPLOAD:
            raise HTTPException(413, f"Файл больше {MAX_UPLOAD // 2**20} МБ. Для Telegram выгрузите чат без фото и видео "
                                     "(в окне экспорта снимите галочки с медиа) или по отдельным чатам.")
        buf = bytearray()
        async for part in request.stream():  # stop reading as soon as the limit is crossed
            buf += part
            if len(buf) > MAX_UPLOAD:
                raise HTTPException(413, f"Файл больше {MAX_UPLOAD // 2**20} МБ")
        raw = bytes(buf)
        del buf
        if not raw:
            raise HTTPException(400, "Пустой файл")
        started = _time.time()

        report: dict[str, Any] = {}

        def work():  # parsing + indexing can take seconds: keep it off the event loop (live audio!)
            docs = import_bytes(name, raw, report)
            if not docs:
                return docs
            if s is not None:
                s.add_session_docs(docs)
            else:
                store = manager.store_for(user)
                store.add(docs)
                store.warm()
            return docs

        try:
            docs = await asyncio.to_thread(work)
        except ImportTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except ImportFailed as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # a file shaped in a way no importer expected
            log.exception("upload %s failed", name)
            raise HTTPException(422, f"Не удалось прочитать {name}: {exc.__class__.__name__}") from exc
        if not docs:
            raise HTTPException(422, "Не узнал формат: нужен zip/JSON выгрузки чата (Telegram, claude.ai, ChatGPT), PDF, DOCX, MD или TXT")
        if s is None:
            manager.attach_store(user)
        log.info("upload %s: %d docs, %d chars, %.1f s", scope, len(docs), sum(len(d.text) for d in docs),
                 _time.time() - started)
        return {"scope": scope, "added": len(docs), "titles": [d.title for d in docs][:20],
                "chars": sum(len(d.text) for d in docs), "seconds": round(_time.time() - started, 1),
                "files": report.get("files", 1), "skipped": len(report.get("skipped", [])),
                "skipped_examples": report.get("skipped", [])[:8]}

    @app.get("/api/knowledge/docs")
    async def list_docs(session_id: str | None = None, user: str = Depends(current_user)) -> dict[str, Any]:
        store = manager.knowledge_for(user)
        import asyncio

        out: dict[str, Any] = {"saved": await asyncio.to_thread(store.list_docs) if store is not None else []}
        if session_id:
            s = manager.get(session_id)
            out["session"] = [{"id": d.id, "title": d.title, "source": d.source, "date": d.date, "chars": len(d.text)}
                              for d in s.session_docs] if s is not None and s.user_id == user else []
        return out

    @app.delete("/api/knowledge/docs/{doc_id}")
    async def delete_doc(doc_id: str, session_id: str | None = None, user: str = Depends(current_user)) -> dict[str, Any]:
        if session_id:
            removed = own_session(session_id, user).remove_session_doc(doc_id)
        else:
            store = manager.knowledge_for(user)
            removed = store.remove(doc_id) if store is not None else False
        if not removed:
            raise HTTPException(404, "doc not found")
        return {"ok": True}

    @app.get("/api/sessions")
    async def list_sessions(user: str = Depends(current_user)) -> list[dict[str, Any]]:
        return [
            {"id": s.id, "started_at": s.started_at, "ended": s.ended_at is not None,
             "active": s.active, "utterances": len(s.buffer), "clients": len(s.subscribers)}
            for s in manager.sessions.values()
            if s.user_id == user
        ]

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
        s = manager.get(session_id)
        if s is None or s.user_id != user:
            raise HTTPException(404, "session not found")
        return {**s.snapshot_event(), "usage": s.usage()}

    @app.get("/api/sessions/{session_id}/transcript")
    async def get_transcript(session_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
        s = manager.get(session_id)
        if s is None or s.user_id != user:
            raise HTTPException(404, "session not found")
        return {"id": s.id, "utterances": [{"ts": u.ts, "speaker": u.speaker, "text": u.text, "id": u.id} for u in s.buffer.all()],
                "answers": s.answers}

    @app.delete("/api/sessions/{session_id}")
    async def end_session(session_id: str, user: str = Depends(current_user)) -> dict[str, Any]:
        s = manager.get(session_id)
        if s is None or s.user_id != user:
            raise HTTPException(404, "session not found")
        usage = s.usage()
        await manager.close(session_id)
        return {"id": session_id, "usage": usage}

    app.include_router(ws_router)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.exists():
        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})

        @app.get("/download")
        async def download_page() -> FileResponse:
            return FileResponse(web_dir / "download.html", headers={"Cache-Control": "no-cache"})

        app.mount("/static", StaticFiles(directory=web_dir), name="static")
    return app


def run_server(cfg: Config, *, reload: bool = False, open_browser: bool = False) -> None:
    import uvicorn

    public = cfg.server.host not in ("127.0.0.1", "localhost", "::1")
    if public and not cfg.secrets.app_token:
        raise SystemExit("Сервер слушает внешний адрес, а APP_TOKEN пуст — так любой в интернете получит доступ "
                         "к ответам и к твоим чатам. Задай APP_TOKEN в .env.")
    base = cfg.server.public_url or f"http://{'127.0.0.1' if cfg.server.host in ('0.0.0.0', '') else cfg.server.host}:{cfg.server.port}"
    url = f"{base}/"
    if cfg.secrets.app_token:
        url += f"?token={cfg.secrets.app_token}"
    import sys

    if sys.stdout.isatty() or not public:  # a terminal on your machine: show the link with the token
        print(f"\n  Copilot: {url}\n  (открой в Chrome; ссылка с токеном — не публикуй её)\n", flush=True)
    else:  # Docker/server logs: never print the token (deploy/push.sh shows the link on your Mac)
        print(f"\n  Copilot: {base}/  (ссылку с токеном печатает deploy/push.sh)\n", flush=True)
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    if reload:  # uvicorn needs an import string for reload
        uvicorn.run("copilot.server.api:app_from_env", host=cfg.server.host, port=cfg.server.port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info",
                    ws="websockets-sansio", access_log=cfg.server.access_log, proxy_headers=True,
                    forwarded_allow_ips="*")


def app_from_env() -> FastAPI:  # used by --reload
    from copilot.config import load_config

    return create_app(load_config())
