from copilot.transcript import TranscriptBuffer


def test_window_and_eviction():
    b = TranscriptBuffer(max_seconds=100)
    b.add_final("them", "first", ts=1000)
    b.add_final("me", "second", ts=1050)
    b.add_final("them", "third", ts=1120)  # evicts ts=1000 (older than 1120-100)
    assert [u.text for u in b.all()] == ["second", "third"]
    assert [u.text for u in b.window(30, now=1125)] == ["third"]


def test_empty_and_whitespace_ignored_and_ids_increase():
    b = TranscriptBuffer()
    assert b.add_final("them", "   ") is None
    u1 = b.add_final("them", "a  b\n c", ts=1)
    u2 = b.add_final("me", "d", ts=2)
    assert u1.text == "a b c" and u2.id == u1.id + 1


def test_interim_cleared_by_final():
    b = TranscriptBuffer()
    b.set_interim("them", "как ты дума")
    assert b.interim("them") == "как ты дума"
    b.add_final("them", "как ты думаешь?", ts=1)
    assert b.interim("them") == ""


def test_last_question_merges_trailing_them_run():
    b = TranscriptBuffer()
    b.add_final("me", "я закончил с витриной", ts=10)
    b.add_final("them", "Окей, понял.", ts=20)
    b.add_final("them", "А по срокам что", ts=22)
    b.add_final("them", "когда будет готово?", ts=24)
    assert b.last_question() == "А по срокам что когда будет готово?"  # "Окей, понял." is a finished sentence


def test_last_question_respects_gap_and_short_me_backchannel():
    b = TranscriptBuffer()
    b.add_final("them", "Old unrelated topic here", ts=0)
    b.add_final("them", "What is the retention for March?", ts=100)  # gap > 10 s: not merged
    b.add_final("me", "угу", ts=101)  # short backchannel doesn't hide the question
    assert b.last_question() == "What is the retention for March?"
    b.add_final("me", "Retention was around forty percent, I'll double check", ts=105)
    assert b.last_question() is None  # Den already answered


def test_last_question_min_words():
    b = TranscriptBuffer()
    b.add_final("them", "Ok", ts=1)
    assert b.last_question() is None
    b.add_final("them", "Why?", ts=2)
    assert b.last_question() == "Ok Why?"  # "Ok" has no full stop → same phrase
    b.add_final("them", "First question here?", ts=3)
    b.add_final("them", "And the second one?", ts=4)
    assert b.last_question() == "And the second one?"


def test_format_window():
    b = TranscriptBuffer()
    b.add_final("them", "hello there", ts=1_700_000_000)
    line = b.format_window(10**9, now=1_700_000_001)
    assert line.endswith("them: hello there") and line.startswith("[")
