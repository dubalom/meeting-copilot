"""Speech-recognition hints from the files attached to a meeting.

Names of systems and acronyms (SLA, Greenplum, ClickHouse, dbt…) are what speech recognition gets wrong most
(«SLA» → «слова»), and they are exactly what a pre-meeting upload contains. The most frequent ones are passed
to Deepgram as key terms when the meeting's speech recognition starts.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# Only high-precision patterns. Measured: ordinary English words as key terms («Data», «Free», «Done»…) make
# Deepgram's mixed-language mode drop Russian speech entirely — a key term must be something rare and specific.
_ACRONYM = re.compile(r"(?<![\w.@/])([A-Z][A-Z0-9]{1,6})(?![\w@/])")                 # SLA, GGR, B2C, D30
_MIXED = re.compile(r"(?<![\w.@/])((?:[a-z]+[A-Z]|[A-Z][a-z]+[A-Z])[A-Za-z0-9]*)(?![\w@/])")  # BigQuery, iGaming, ClickHouse
_IDENT = re.compile(r"(?<![\w.@/])([a-z][a-z0-9]*_[a-z0-9_]+)(?![\w@/])")              # dm_visits, report_dt
_LATIN_IN_RU = re.compile(r"[а-яёА-ЯЁ]+[,:]?\s+([A-Z][a-z]{2,20})(?![\w@/])")           # «в Greenplum», «через Airflow»
_NOISE = {"OK", "ID", "AM", "PM", "TV", "OS", "UI", "UX", "PS", "CC", "TO", "IT", "OR", "IN", "ON", "AT", "IS", "NO",
          "HTTP", "HTTPS", "WWW", "HTML", "JSON", "PDF", "URL", "API"}


def _candidates(text: str):
    for rx in (_ACRONYM, _MIXED, _IDENT, _LATIN_IN_RU):
        for w in rx.findall(text):
            if w not in _NOISE:
                yield w


def doc_keyterms(texts: Iterable[str], limit: int = 20, min_count: int = 2, sample_chars: int = 3_000_000) -> list[str]:
    """Most frequent acronyms, mixed-case product names, identifiers and Latin names used inside Russian text
    (keeps the spelling seen most often)."""
    counts: Counter = Counter()
    spelling: dict[str, Counter] = {}
    budget = sample_chars
    for text in texts:
        if budget <= 0:
            break
        part = text[:budget]
        budget -= len(part)
        for w in _candidates(part):
            key = w.lower()
            counts[key] += 1
            spelling.setdefault(key, Counter())[w] += 1
    out = []
    for key, n in counts.most_common():
        if n < min_count or len(out) >= limit:
            break
        out.append(spelling[key].most_common(1)[0][0])
    return out


_CYR_NAME = re.compile(r"(?<=[^.!?\n]\s)([А-ЯЁ][а-яё]{2,20}(?:-[А-ЯЁ][а-яё]+)?)")  # capitalised mid-sentence


def text_keyterms(text: str, limit: int = 20) -> list[str]:
    """Terms from text the user wrote (profile, brief): Latin names/acronyms and Russian proper names, each
    counted even if it appears once."""
    out = doc_keyterms([text], limit=limit, min_count=1)
    seen = {t.lower() for t in out}
    for w in _CYR_NAME.findall(text or ""):
        if w.lower() not in seen and len(out) < limit:
            seen.add(w.lower())
            out.append(w)
    return out


def parse_vocab(text: str, limit: int = 60) -> list[str]:
    """The user's own list «Слова для распознавания»: one per line or comma-separated."""
    out, seen = [], set()
    for part in re.split(r"[\n,;]+", text or ""):
        term = part.strip().strip("•-* ")
        if 1 < len(term) <= 50 and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
        if len(out) >= limit:
            break
    return out


def merge_keyterms(*lists: Iterable[str], limit: int = 40) -> list[str]:
    """Earlier lists win (the user's own words first); case-insensitive de-duplication."""
    out, seen = [], set()
    for lst in lists:
        for t in lst:
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
                if len(out) >= limit:
                    return out
    return out
