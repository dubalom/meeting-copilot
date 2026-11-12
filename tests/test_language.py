import asyncio

from copilot.brain.prompt import answer_user_message
from copilot.config import Config, SttConfig
from copilot.session import MeetingSession
from copilot.stt.deepgram_stt import build_url
from tests.test_brain import FakeBrain
from tests.test_stt_and_server import FakeTranscriber


def test_hello_sets_language_and_switch_reopens_stt():
    async def run():
        FakeTranscriber.instances.clear()
        brain = FakeBrain()
        s = MeetingSession("l", Config(), FakeTranscriber, transcribe_me=False, brain=brain)
        q = s.subscribe()
        await s.handle_control({"type": "hello", "channels": ["them"], "language": "ru", "answer_language": "en"})
        first = FakeTranscriber.instances[-1]
        await s.handle_control({"type": "language", "stt": "en"})
        second = FakeTranscriber.instances[-1]
        await s.handle_control({"type": "language", "stt": "klingon", "answer": "xx"})  # ignored
        await s.request_answer(question="q")
        await s.wait_answer()
        events = [q.get_nowait() for _ in range(q.qsize())]
        return s, first, second, brain, events

    s, first, second, brain, events = asyncio.run(run())
    assert first.language == "ru" and first.stopped
    assert second is not first and second.language == "en" and not second.stopped
    assert s.language == "en" and s.answer_language == "en"
    assert brain.answer_languages == ["en"]
    assert any(e.get("language") == "en" for e in events if e["type"] == "status")


def test_answer_language_note_and_deepgram_url():
    assert "по-русски" in answer_user_message("t", "q?", answer_language="ru")
    assert "English" in answer_user_message("t", "q?", answer_language="en")
    assert "Язык ответа" not in answer_user_message("t", "q?", answer_language="auto")
    assert "language=ru" in build_url(SttConfig().model_copy(update={"language": "ru"}))
