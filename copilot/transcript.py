"""TranscriptBuffer: a time-bounded ring of final utterances plus the latest interim per speaker.

Pure Python, no I/O — the Trigger and Brain read windows from here (CLAUDE.md §5.4).
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal

Speaker = Literal["them", "me"]
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(slots=True)
class Utterance:
    ts: float
    speaker: Speaker
    text: str
    final: bool = True
    id: int = field(default=0)

    @property
    def words(self) -> int:
        return len(_WORD_RE.findall(self.text))

    def format(self) -> str:
        return f"[{datetime.fromtimestamp(self.ts).strftime('%H:%M:%S')}] {self.speaker}: {self.text}"


class TranscriptBuffer:
    def __init__(self, max_seconds: float = 900.0, max_items: int = 5000):
        self.max_seconds = max_seconds
        self._items: deque[Utterance] = deque(maxlen=max_items)
        self._interim: dict[str, str] = {}
        self._next_id = 1

    # ------------------------------------------------------------------ write
    def add_final(self, speaker: Speaker, text: str, ts: float | None = None) -> Utterance | None:
        text = " ".join(text.split())
        if not text:
            return None
        u = Utterance(ts=time.time() if ts is None else ts, speaker=speaker, text=text, final=True, id=self._next_id)
        self._next_id += 1
        self._items.append(u)
        self._interim.pop(speaker, None)
        self._evict()
        return u

    def set_interim(self, speaker: Speaker, text: str) -> None:
        text = " ".join(text.split())
        if text:
            self._interim[speaker] = text
        else:
            self._interim.pop(speaker, None)

    def clear(self) -> None:
        self._items.clear()
        self._interim.clear()

    # ------------------------------------------------------------------- read
    def interim(self, speaker: Speaker) -> str:
        return self._interim.get(speaker, "")

    def all(self) -> list[Utterance]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def window(self, seconds: float, now: float | None = None) -> list[Utterance]:
        now = time.time() if now is None else now
        cutoff = now - seconds
        return [u for u in self._items if u.ts >= cutoff]

    def last_them_utterances(self, n: int) -> list[Utterance]:
        out = [u for u in reversed(self._items) if u.speaker == "them"][:n]
        out.reverse()
        return out

    def last_question(self, *, max_parts: int = 3, max_gap_seconds: float = 10.0, min_words: int = 3) -> str | None:
        """The question to answer: the last ``them`` utterance, extended backwards only over
        utterances that do not end a sentence (a phrase split by a pause: "А по срокам что" +
        "когда будет готово?"). Complete earlier sentences are left out — the Brain sees the whole
        transcript window anyway. Returns None when there is nothing substantial to answer."""
        run: list[Utterance] = []
        for u in reversed(self._items):
            if u.speaker != "them":
                if run:
                    break
                # trailing `me` utterances: the question (if any) came before Den's last reply —
                # keep scanning only if Den's reply was very short (e.g. "м-м", "ага")
                if u.words > 3:
                    break
                continue
            if run and ((run[-1].ts - u.ts) > max_gap_seconds or _ends_sentence(u.text)):
                break
            run.append(u)
            if len(run) >= max_parts:
                break
        if not run:
            return None
        run.reverse()
        text = " ".join(u.text for u in run)
        words = len(_WORD_RE.findall(text))
        if words >= min_words or (words >= 2 and text.rstrip().endswith("?")):
            return text
        return None

    def format_window(self, seconds: float, now: float | None = None, include_interim: bool = False) -> str:
        lines = [u.format() for u in self.window(seconds, now)]
        if include_interim:
            for spk in ("them", "me"):
                if self._interim.get(spk):
                    lines.append(f"[…] {spk}: {self._interim[spk]} (interim)")
        return "\n".join(lines)

    # -------------------------------------------------------------- internals
    def _evict(self) -> None:
        if not self._items:
            return
        cutoff = self._items[-1].ts - self.max_seconds
        while self._items and self._items[0].ts < cutoff:
            self._items.popleft()


def _ends_sentence(text: str) -> bool:
    return text.rstrip().rstrip("»\"')").endswith((".", "?", "!", "…"))


def format_utterances(items: Iterable[Utterance]) -> str:
    return "\n".join(u.format() for u in items)
