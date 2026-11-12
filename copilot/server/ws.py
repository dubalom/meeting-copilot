"""WebSocket endpoint ``/ws/session/{session_id}`` — wire protocol from CLAUDE.md §5.6.

client → server
  binary : 1 byte channel (0 = them, 1 = me) + int16 LE PCM @16 kHz (normally 640 samples / 40 ms)
  text   : JSON control: hello | answer | auto | cancel | summary | end | ping
server → client
  text   : JSON events: snapshot | status | transcript | utterance_end | speech_started |
           answer_start | answer_delta | answer_end | summary | usage | error | pong
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from copilot.session import MeetingSession, SessionManager

log = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Framing helpers (shared with the CLI client; unit-tested)
# --------------------------------------------------------------------------- #
def encode_audio_frame(channel: int, pcm: bytes) -> bytes:
    if channel not in (0, 1):
        raise ValueError("channel must be 0 (them) or 1 (me)")
    return bytes((channel,)) + pcm


def decode_audio_frame(data: bytes) -> tuple[int, bytes] | None:
    """Return (channel, pcm) or None for malformed frames (odd length, unknown channel, empty)."""
    if len(data) < 3:
        return None
    channel = data[0]
    pcm = data[1:]
    if channel not in (0, 1) or len(pcm) % 2:
        return None
    return channel, pcm


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@router.websocket("/ws/session/{session_id}")
async def session_socket(ws: WebSocket, session_id: str) -> None:
    manager: SessionManager = ws.app.state.sessions
    auth = ws.app.state.auth
    user_id = auth.resolve_ws_user(ws)
    if user_id is None:
        await ws.close(code=4401, reason="unauthorized")
        return

    await ws.accept()
    session: MeetingSession = manager.get(session_id) or manager.create(session_id, user_id=user_id)
    if session.user_id != user_id:
        await ws.close(code=4403, reason="forbidden")
        return

    queue = session.subscribe()
    writer = asyncio.create_task(_writer(ws, queue), name=f"ws-writer-{session_id}")
    try:
        await ws.send_text(json.dumps(session.snapshot_event()))
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if (data := message.get("bytes")) is not None:
                frame = decode_audio_frame(data)
                if frame is None:
                    continue
                if not session.active:
                    await session.start()  # client sent audio without hello: start with defaults
                await session.feed(frame[0], frame[1])
            elif (text := message.get("text")) is not None:
                try:
                    control: dict[str, Any] = json.loads(text)
                except json.JSONDecodeError:
                    await _safe_send(ws, {"type": "error", "message": "control messages must be JSON"})
                    continue
                await session.handle_control(control)
                if control.get("type") == "end":
                    await _safe_send(ws, {"type": "usage", **session.usage()})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws session %s failed", session_id)
    finally:
        session.unsubscribe(queue)
        writer.cancel()
        try:
            await writer
        except (asyncio.CancelledError, Exception):
            pass
        log.info("client left session %s (subscribers left: %d)", session_id, len(session.subscribers))


async def _writer(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            return


async def _safe_send(ws: WebSocket, event: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:
        pass
