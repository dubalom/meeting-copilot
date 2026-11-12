import asyncio
import json
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
from fastapi.testclient import TestClient

from copilot.config import Config, SttConfig
from copilot.server.api import create_app
from copilot.server.ws import decode_audio_frame, encode_audio_frame
from copilot.stt.base import Transcriber, TranscriptEvent
from copilot.stt.deepgram_stt import backoff_delays, build_url, parse_message


# ----------------------------------------------------------------- deepgram helpers
def test_build_url_params_and_repeated_keyterms():
    url = build_url(SttConfig(), keyterms=["GGR", "ggr", "Data Vault", " "])
    q = parse_qs(urlparse(url).query)
    assert q["model"] == ["nova-3"] and q["language"] == ["multi"]
    assert q["encoding"] == ["linear16"] and q["sample_rate"] == ["16000"]
    assert q["interim_results"] == ["true"] and q["vad_events"] == ["true"]
    assert q["keyterm"] == ["GGR", "Data Vault"]  # deduped, one param per term


def test_parse_results_final_and_interim():
    msg = {
        "type": "Results", "is_final": True, "speech_final": True, "start": 1.5, "duration": 2.0,
        "channel": {"alternatives": [{"transcript": " Как дела? ", "confidence": 0.93}]},
    }
    ev = parse_message("them", msg, now=123.0, offset=10.0)
    assert ev.text == "Как дела?" and ev.final and ev.speech_final
    assert ev.start == 11.5 and ev.ts == 123.0 and ev.confidence == 0.93
    msg["is_final"] = False
    assert parse_message("me", msg).final is False


def test_parse_ignores_empty_and_other_types():
    empty = {"type": "Results", "channel": {"alternatives": [{"transcript": ""}]}}
    assert parse_message("them", empty) is None
    assert parse_message("them", {"type": "Metadata"}) is None
    assert parse_message("them", {"type": "UtteranceEnd", "last_word_end": 3.2}).kind == "utterance_end"
    assert parse_message("them", {"type": "SpeechStarted", "timestamp": 1.0}).kind == "speech_started"


def test_backoff():
    it = backoff_delays(4.0)
    assert [next(it) for _ in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]


def test_audio_framing_roundtrip():
    pcm = np.arange(640, dtype=np.int16).tobytes()
    assert decode_audio_frame(encode_audio_frame(1, pcm)) == (1, pcm)
    assert decode_audio_frame(b"\x02" + pcm) is None  # bad channel
    assert decode_audio_frame(b"\x00\x01\x02\x03") is None  # odd pcm length
    assert decode_audio_frame(b"\x00\x01") is None  # too short
    with pytest.raises(ValueError):
        encode_audio_frame(5, pcm)


def test_glossary_terms(tmp_path):
    cfg = Config()
    cfg.paths.context_dir = tmp_path
    (tmp_path / "glossary.md").write_text(
        "# G\n<!-- comment -->\n- GGR — gross gaming revenue\n- `dm_visits`: витрина\n* Data Vault - методология\nтекст\n",
        encoding="utf-8",
    )
    assert cfg.glossary_terms() == ["GGR", "dm_visits", "Data Vault"]


# ----------------------------------------------------------------- server + ws (fake STT)
class FakeTranscriber(Transcriber):
    """Emits an interim after the first frame and a final every 25 frames (≈1 s of audio)."""

    instances: list["FakeTranscriber"] = []

    def __init__(self, speaker, on_event, on_status=None, language=None, **_):
        super().__init__(speaker, on_event, on_status)
        self.language = language
        self.frames = 0
        self.stopped = False
        FakeTranscriber.instances.append(self)

    async def start(self):
        await self.set_status("connected")

    async def send(self, pcm):
        self.count_audio(pcm)
        self.frames += 1
        if self.frames == 1:
            await self.emit(TranscriptEvent(self.speaker, text="при", final=False))
        if self.frames % 25 == 0:
            await self.emit(TranscriptEvent(self.speaker, text=f"{self.speaker} phrase", final=True))
            await self.emit(TranscriptEvent(self.speaker, text=f"{self.frames // 25}", final=True, speech_final=True))

    async def stop(self):
        self.stopped = True
        await self.set_status("closed")


def _client(token=""):
    FakeTranscriber.instances.clear()
    cfg = Config()
    cfg.secrets.app_token = token
    return TestClient(create_app(cfg, FakeTranscriber))


def _drain_until(ws, predicate, limit=200):
    seen = []
    for _ in range(limit):
        ev = json.loads(ws.receive_text())
        seen.append(ev)
        if predicate(ev):
            return ev, seen
    raise AssertionError(f"event not received; got {seen[-5:]}")


def test_ws_session_flow_two_channels():
    pcm = np.zeros(640, dtype=np.int16).tobytes()
    with _client() as client:
        with client.websocket_connect("/ws/session/abc") as ws:
            snap = json.loads(ws.receive_text())
            assert snap["type"] == "snapshot" and snap["session_id"] == "abc"
            ws.send_text(json.dumps({"type": "hello", "client": "test", "transcribe_me": True}))
            for _ in range(25):
                ws.send_bytes(encode_audio_frame(0, pcm))
                ws.send_bytes(encode_audio_frame(1, pcm))
            ev, seen = _drain_until(ws, lambda e: e["type"] == "transcript" and e["final"] and e["speaker"] == "me")
            finals = [e for e in seen if e["type"] == "transcript" and e["final"]]
            assert {e["speaker"] for e in finals} == {"them", "me"}
            assert any(e["type"] == "transcript" and not e["final"] for e in seen)
            ws.send_text(json.dumps({"type": "end"}))
            usage, _ = _drain_until(ws, lambda e: e["type"] == "usage")
            assert usage["stt_seconds"]["them"] == pytest.approx(1.0)
            assert usage["utterances"] == 2
        assert all(t.stopped for t in FakeTranscriber.instances)


def test_ws_transcribe_me_off_ignores_mic_frames():
    pcm = np.zeros(640, dtype=np.int16).tobytes()
    with _client() as client, client.websocket_connect("/ws/session/s2") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "hello", "transcribe_me": False}))
        for _ in range(25):
            ws.send_bytes(encode_audio_frame(1, pcm))
            ws.send_bytes(encode_audio_frame(0, pcm))
        ev, _ = _drain_until(ws, lambda e: e["type"] == "transcript" and e["final"])
        assert ev["speaker"] == "them"
        assert [t.speaker for t in FakeTranscriber.instances] == ["them"]


def test_reconnect_gets_snapshot_with_history():
    pcm = np.zeros(640, dtype=np.int16).tobytes()
    with _client() as client:
        with client.websocket_connect("/ws/session/keep") as ws:
            ws.receive_text()
            ws.send_text(json.dumps({"type": "hello", "transcribe_me": False}))
            for _ in range(25):
                ws.send_bytes(encode_audio_frame(0, pcm))
            _drain_until(ws, lambda e: e["type"] == "transcript" and e["final"])
        with client.websocket_connect("/ws/session/keep") as ws2:  # same id after a drop
            snap = json.loads(ws2.receive_text())
            assert [u["text"] for u in snap["transcript"]] == ["them phrase 1"]


def test_auth_token_required():
    with _client(token="s3cret") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/sessions").status_code == 401
        assert client.get("/api/sessions", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/session/x") as ws:
                ws.receive_text()
        with client.websocket_connect("/ws/session/x?token=s3cret") as ws:
            assert json.loads(ws.receive_text())["type"] == "snapshot"


def test_rest_create_and_end_session():
    with _client() as client:
        r = client.post("/api/sessions", json={"transcribe_me": False}).json()
        assert r["ws_path"] == f"/ws/session/{r['id']}" and r["transcribe_me"] is False
        assert [s["id"] for s in client.get("/api/sessions").json()] == [r["id"]]
        assert client.delete(f"/api/sessions/{r['id']}").status_code == 200
        assert client.get(f"/api/sessions/{r['id']}").status_code == 404


# ----------------------------------------------------------------- wav source
def test_wav_roundtrip(tmp_path):
    import wave

    from copilot.audio.wav_source import iter_frames, load_wav_16k

    rate = 44_100
    t = np.arange(rate) / rate
    stereo = (np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1) * 12000).astype("<i2")
    p = tmp_path / "x.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(stereo.tobytes())
    x = load_wav_16k(p)
    assert x.dtype == np.int16 and abs(x.size - 16000) <= 2

    async def collect():
        return [f async for f in iter_frames(x, realtime=False, tail_silence_s=0.0)]

    frames = asyncio.run(collect())
    assert all(len(f) == 1280 for f in frames) and len(frames) == 25


def test_session_merges_final_segments_until_speech_final():
    from copilot.session import MeetingSession

    async def scenario():
        s = MeetingSession("m", Config(), FakeTranscriber, transcribe_me=False)
        q = s.subscribe()
        await s.start()
        emit = s._on_stt_event
        await emit(TranscriptEvent("them", text="And can you share the SQL", final=True))
        await emit(TranscriptEvent("them", text="for the NGR", final=False))
        await emit(TranscriptEvent("them", text="for the NGR calculation after", final=True))
        await emit(TranscriptEvent("them", text="the call?", final=True, speech_final=True))
        await emit(TranscriptEvent("them", text="Dangling tail", final=True))
        await emit(TranscriptEvent("them", kind="utterance_end"))
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return s, events

    s, events = asyncio.run(scenario())
    finals = [e["text"] for e in events if e.get("type") == "transcript" and e["final"]]
    assert finals == ["And can you share the SQL for the NGR calculation after the call?", "Dangling tail"]
    interims = [e["text"] for e in events if e.get("type") == "transcript" and not e["final"]]
    assert "And can you share the SQL for the NGR" in interims
    assert [u.text for u in s.buffer.all()] == finals


def test_parse_tolerates_list_channel():
    assert parse_message("them", {"type": "SpeechStarted", "channel": [0], "timestamp": 0.5}).kind == "speech_started"
    assert parse_message("them", {"type": "Results", "channel": [0]}) is None


def test_parse_word_timings_and_phrase_gap_split():
    from copilot.session import MeetingSession

    msg = {"type": "Results", "is_final": True, "start": 0.0, "duration": 3.0,
           "channel": {"alternatives": [{"transcript": "a b", "words": [{"start": 0.5, "end": 0.8}, {"start": 1.0, "end": 1.3}]}]}}
    ev = parse_message("them", msg, offset=10.0)
    assert (ev.word_start, ev.word_end) == (10.5, 11.3)

    async def scenario():
        s = MeetingSession("g", Config(), FakeTranscriber, transcribe_me=False)
        await s.start()
        emit = s._on_stt_event
        await emit(TranscriptEvent("them", text="Привет, коллеги.", final=True, word_start=0.2, word_end=1.5))
        await emit(TranscriptEvent("them", text="Quick question for Den:", final=True, word_start=2.9, word_end=4.0))  # 1.4 s gap
        await emit(TranscriptEvent("them", text="what was retention?", final=True, word_start=4.2, word_end=5.0, speech_final=True))
        return [u.text for u in s.buffer.all()]

    assert asyncio.run(scenario()) == ["Привет, коллеги.", "Quick question for Den: what was retention?"]


def test_split_on_pauses_inside_one_segment():
    from copilot.stt.deepgram_stt import split_on_pauses

    words = [
        {"start": 0.1, "end": 0.5, "punctuated_word": "Привет,"},
        {"start": 0.6, "end": 1.0, "punctuated_word": "коллеги."},
        {"start": 2.5, "end": 2.8, "punctuated_word": "Quick"},
        {"start": 2.9, "end": 3.3, "punctuated_word": "question?"},
    ]
    ev = TranscriptEvent("them", text="Привет, коллеги. Quick question?", final=True, speech_final=False)
    parts = split_on_pauses(ev, words, offset=100.0, gap=0.9)
    assert [p.text for p in parts] == ["Привет, коллеги.", "Quick question?"]
    assert [p.speech_final for p in parts] == [True, False]
    assert parts[1].word_start == 102.5
    assert split_on_pauses(ev, words[:2], 0.0, 0.9) == [ev]


def test_hello_channels_mic_only():
    pcm = np.zeros(640, dtype=np.int16).tobytes()
    with _client() as client, client.websocket_connect("/ws/session/mic") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "hello", "channels": ["me"]}))
        status, _ = _drain_until(ws, lambda e: e["type"] == "status" and "stt" in e)
        assert status["stt"]["them"] == "off"
        for _ in range(25):
            ws.send_bytes(encode_audio_frame(1, pcm))
        ev, _ = _drain_until(ws, lambda e: e["type"] == "transcript" and e["final"])
        assert ev["speaker"] == "me"
        assert [t.speaker for t in FakeTranscriber.instances] == ["me"]
