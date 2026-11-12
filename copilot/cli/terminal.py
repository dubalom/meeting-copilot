"""Terminal client for the meeting-copilot server.

    python -m copilot cli                   # live: BlackHole (them) + mic (me) → transcript
    python -m copilot replay file.wav       # a WAV file as the `them` channel
    python -m copilot cli --url ws://host:8765   # use an already running server

Without ``--url`` an embedded server is started in the same process, so one command is enough.
The client speaks exactly the protocol the browser will use (CLAUDE.md §5.6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from copilot.config import Config
from copilot.server.ws import encode_audio_frame

CHANNEL = {"them": 0, "me": 1}

# ---------------------------------------------------------------- rendering
_TTY = sys.stdout.isatty()
_C = {
    "them": "\033[96m" if _TTY else "",
    "me": "\033[93m" if _TTY else "",
    "dim": "\033[2m" if _TTY else "",
    "err": "\033[91m" if _TTY else "",
    "ok": "\033[92m" if _TTY else "",
    "reset": "\033[0m" if _TTY else "",
}


class Renderer:
    """Final lines scroll; the current interim lives on one rewritable status line."""

    def __init__(self, show_latency: bool = True):
        self.show_latency = show_latency
        self._status_shown = False
        self.finals = 0
        self.first_final_at: float | None = None
        self.streaming = False
        self.answers_done: list[dict] = []
        self.summary: str | None = None
        self.errors = 0

    def _clear(self) -> None:
        if self._status_shown and _TTY:
            sys.stdout.write("\r\033[K")
            self._status_shown = False

    def line(self, text: str) -> None:
        self._clear()
        print(text, flush=True)

    def interim(self, speaker: str, text: str) -> None:
        if not _TTY or self.streaming:
            return
        width = shutil.get_terminal_size((100, 20)).columns - 1
        s = f"{_C['dim']}… {speaker}: {text}{_C['reset']}"
        visible = f"… {speaker}: {text}"
        if len(visible) > width:
            s = f"{_C['dim']}… {speaker}: …{text[-(width - len(speaker) - 6):]}{_C['reset']}"
        self._clear()
        sys.stdout.write(s)
        sys.stdout.flush()
        self._status_shown = True

    def event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "transcript":
            spk = ev.get("speaker", "?")
            if ev.get("final"):
                self.finals += 1
                self.first_final_at = self.first_final_at or time.time()
                ts = datetime.fromtimestamp(ev.get("ts", time.time())).strftime("%H:%M:%S")
                self.line(f"{_C['dim']}[{ts}]{_C['reset']} {_C.get(spk, '')}{spk:>4}{_C['reset']}: {ev.get('text', '')}")
            else:
                self.interim(spk, ev.get("text", ""))
        elif kind == "status":
            parts = []
            if "stt" in ev:
                parts.append("stt " + " ".join(f"{k}={v}" for k, v in ev["stt"].items()))
            if "detail" in ev:
                parts.append(ev["detail"])
            if "auto" in ev:
                parts.append(f"auto={ev['auto']}")
            if ev.get("session") == "ended":
                parts.append("session ended")
            if parts:
                bad = any(x in json.dumps(ev) for x in ("error", "reconnecting"))
                self.line(f"{_C['err'] if bad else _C['dim']}· {' · '.join(parts)}{_C['reset']}")
        elif kind == "snapshot":
            n = len(ev.get("transcript", []))
            self.line(f"{_C['ok']}· session {ev.get('session_id')} · stt {ev.get('stt')} · {n} earlier lines{_C['reset']}")
            for u in ev.get("transcript", []):
                self.event({"type": "transcript", **u, "final": True})
        elif kind == "answer_start":
            q = ev.get("question") or "(последний вопрос не найден — отвечаю по контексту)"
            self.line(f"\n{_C['ok']}▶ {'вопрос' if ev.get('typed') else 'отвечаю на'}: {q}{_C['reset']}")
            self.streaming = True
        elif kind == "answer_delta":
            sys.stdout.write(ev.get("text", ""))
            sys.stdout.flush()
        elif kind == "answer_end":
            self.streaming = False
            u = ev.get("usage", {})
            bits = [ev.get("status", "")]
            if u.get("first_token_ms") is not None:
                bits.append(f"первый токен {u['first_token_ms']} мс")
            if u.get("total_ms") is not None:
                bits.append(f"всего {u['total_ms']} мс")
            if u:
                bits.append(f"in {u.get('input_tokens', 0)} / cache {u.get('cache_read_input_tokens', 0)} / out {u.get('output_tokens', 0)}")
            sys.stdout.write("\n")
            self.line(f"{_C['dim']}· {' · '.join(b for b in bits if b)}{_C['reset']}\n")
            self.answers_done.append(ev)
        elif kind == "summary":
            self.summary = ev.get("markdown", "")
            self.line(f"\n{_C['ok']}━━ Итоги встречи ━━{_C['reset']}\n{self.summary}\n")
        elif kind == "error":
            self.errors += 1
            self.line(f"{_C['err']}! {ev.get('message')}{_C['reset']}")
        elif kind == "usage":
            stt = ev.get("stt_seconds", {})
            self.line(f"{_C['dim']}· usage: stt {stt} s · {ev.get('utterances')} utterances · {ev.get('duration_s')} s{_C['reset']}")


# ------------------------------------------------------------ embedded server
def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def embedded_server(cfg: Config) -> AsyncIterator[str]:
    """Run the FastAPI app inside this event loop; yields the ws base URL."""
    import uvicorn

    from copilot.server.api import create_app

    app = create_app(cfg)
    port = _free_port()  # embedded server never collides with `serve` or another cli run
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="websockets-sansio", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        if task.done():
            task.result()  # raises (e.g. port already in use)
        await asyncio.sleep(0.05)
    else:
        raise RuntimeError("embedded server failed to start")
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)


# ------------------------------------------------------------------- client
AudioPump = Callable[[Callable[[int, bytes], Awaitable[None]]], Awaitable[None]]


async def run_client(
    cfg: Config,
    pump: AudioPump,
    *,
    url: str | None,
    transcribe_me: bool,
    channels: list[str] | None = None,
    session_id: str | None = None,
    linger_s: float = 0.0,
    ask_after: bool = False,
    summary_after: bool = False,
) -> int:
    import websockets

    renderer = Renderer()
    sid = session_id or uuid.uuid4().hex[:12]

    async with contextlib.AsyncExitStack() as stack:
        base = url or await stack.enter_async_context(embedded_server(cfg))
        ws_url = f"{base.rstrip('/')}/ws/session/{sid}"
        if cfg.secrets.app_token:
            ws_url += f"?token={cfg.secrets.app_token}"
        try:
            ws = await stack.enter_async_context(websockets.connect(ws_url, max_size=2**22))
        except Exception as exc:
            print(f"Не удалось подключиться к {base}: {exc}", file=sys.stderr)
            return 2

        hello = {"type": "hello", "client": "cli", "transcribe_me": transcribe_me}
        if channels is not None:
            hello["channels"] = channels
        await ws.send(json.dumps(hello))

        async def reader() -> None:
            async for raw in ws:
                if isinstance(raw, str):
                    renderer.event(json.loads(raw))

        async def send(channel: int, pcm: bytes) -> None:
            await ws.send(encode_audio_frame(channel, pcm))

        reader_task = asyncio.create_task(reader())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError):  # not available on Windows
            import signal

            loop.add_signal_handler(signal.SIGINT, stop.set)
            loop.add_signal_handler(signal.SIGTERM, stop.set)

        async def wait_for(pred, timeout: float) -> None:
            end = loop.time() + timeout
            while not pred() and loop.time() < end:
                await asyncio.sleep(0.05)

        async def pump_then_linger() -> None:
            await pump(send)
            if linger_s:
                await asyncio.sleep(linger_s)  # let the last finals arrive
            if ask_after:
                n = len(renderer.answers_done)
                await ws.send(json.dumps({"type": "answer"}))
                await wait_for(lambda: len(renderer.answers_done) > n, 60)
            if summary_after:
                errs = renderer.errors
                await ws.send(json.dumps({"type": "summary"}))
                await wait_for(lambda: renderer.summary is not None or renderer.errors > errs, 90)

        # keyboard: Enter = answer the last question, text = ask it, /s = summary, /c = cancel
        def on_stdin() -> None:
            line = sys.stdin.readline()
            if line == "":
                with contextlib.suppress(Exception):
                    loop.remove_reader(sys.stdin.fileno())
                return
            cmd = line.strip()
            if cmd in ("/s", "/summary"):
                msg = {"type": "summary"}
            elif cmd in ("/c", "/cancel"):
                msg = {"type": "cancel"}
            elif cmd in ("/r", "/reload"):
                msg = {"type": "reload_context"}
            elif cmd:
                msg = {"type": "answer", "question": cmd}
            else:
                msg = {"type": "answer"}
            asyncio.ensure_future(ws.send(json.dumps(msg)))

        interactive = sys.stdin.isatty() and not ask_after
        if interactive:
            with contextlib.suppress(NotImplementedError, OSError):
                loop.add_reader(sys.stdin.fileno(), on_stdin)
                renderer.line(f"{_C['dim']}Enter — ответ на последний вопрос · текст+Enter — свой вопрос · /s — итоги · /c — отмена{_C['reset']}")

        pump_task = asyncio.create_task(pump_then_linger())
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({pump_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if pump_task.done() and not pump_task.cancelled() and pump_task.exception():
                raise pump_task.exception()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for t in (pump_task, stop_task):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            if interactive:
                with contextlib.suppress(Exception):
                    loop.remove_reader(sys.stdin.fileno())
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "end"}))
                await asyncio.sleep(1.5)  # final flush + usage event
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task
    renderer.line("")
    if ask_after and not renderer.answers_done:
        return 1
    return 0 if renderer.finals else 1


# ------------------------------------------------------------------- pumps
def live_pump(cfg: Config, *, mic_only: bool = False) -> AudioPump:
    """Local capture (BlackHole/loopback + mic) → frames. ``mic_only``: just the microphone as `me`."""
    from copilot.audio.capture import AudioSource, open_sources
    from copilot.audio.devices import resolve_me

    if mic_only:
        sources = {"me": AudioSource(resolve_me(cfg.audio.me_device), frame_ms=cfg.audio.frame_ms)}
    else:
        sources = open_sources(cfg)
    print("Источники:")
    for role, src in sources.items():
        print(f"  {role:<4} ← {src.describe()}")
        if src.spec.note:
            print(f"         {src.spec.note}")
    print("Ctrl+C — стоп.\n")

    async def pump(send: Callable[[int, bytes], Awaitable[None]]) -> None:
        for s in sources.values():
            s.start()
        try:
            while True:
                sent = False
                for role, s in sources.items():
                    while (frame := s.get_nowait()) is not None:
                        await send(CHANNEL[role], frame.tobytes())
                        sent = True
                if not sent:
                    await asyncio.sleep(0.01)
        finally:
            for s in sources.values():
                s.stop()

    return pump


def wav_pump(path: Path, *, channel: str = "them", speed: float = 1.0, realtime: bool = True) -> AudioPump:
    from copilot.audio.wav_source import iter_frames, load_wav_16k

    samples = load_wav_16k(path)
    print(f"Replay: {path.name} · {samples.size / 16000:.1f} s → channel {channel} · speed ×{speed}\n")

    async def pump(send: Callable[[int, bytes], Awaitable[None]]) -> None:
        async for frame in iter_frames(samples, realtime=realtime, speed=speed):
            await send(CHANNEL[channel], frame)

    return pump
