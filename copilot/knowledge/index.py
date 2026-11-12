"""Per-user chat store + a small in-memory BM25 index (no external services, no embeddings).

Tokens are lower-cased words cut to a 5-character prefix — a crude stemmer that works well enough
for Russian inflection ("витрина/витрины/витрину" → "витри", "визитам/визитов" → "визит") and English.
A chunk counts as relevant only when it matches at least two distinct question words (one for one-word
questions): absolute BM25 scores are not comparable across small and large stores. A short RU↔EN bridge
of work terms lets an English question find a Russian chat and back.
"""

from __future__ import annotations

import json
import math
import re
import threading
from array import array
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from copilot.knowledge.importers import Doc, dedupe

_WORD = re.compile(r"\w+", re.UNICODE)
STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему
теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они
тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого
какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть
после над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том
нельзя такой им более всегда конечно всю между это the a an and or of to in on for is are was were be been with as at by it this that
from you your i me my we our they them he she his her do does did not no yes so if then than but can could would should will just
claude какое какие каком какую сколько расскажите расскажи скажите подскажите ваш ваша ваше ваши вашем вашей вами
what which who whom when where why how tell about please there have has had any some
идёт идет идут шёл шел делает делаете делать сделать сделали нужно нужен нужна нужны будут
""".split())

# Russian verbal prefixes: "посчитать"/"считается" should meet on "счита".
_PREFIXES = ("пере", "пред", "под", "при", "про", "раз", "рас", "вы", "до", "за", "на", "от", "по")
_CYR = re.compile(r"^[а-яё]+$")

# A small RU↔EN bridge for work vocabulary: both stems also emit a shared «~concept» term, so a question
# asked in English finds a Russian chat (and back). Stems are the same 5-char prefixes `tokenize` makes.
_BRIDGE = {
    "deadl": "дедла срок", "mart": "витри", "visit": "визит", "segme": "сегме", "clust": "класт",
    "expor": "выгру", "unloa": "выгру", "impor": "загру", "load": "загру", "penal": "штраф",
    "stack": "стек", "exper": "опыт", "optim": "оптим", "migra": "мигра переез", "salar": "зарпл",
    "proje": "проек", "clien": "клиен", "custo": "клиен", "repor": "отчёт отчет", "budge": "бюдже",
    "team": "коман", "manag": "менед", "query": "запро", "table": "табли", "pipel": "пайпл",
    "dashb": "дашбо", "relea": "релиз", "deplo": "депло", "test": "тест", "error": "ошибк", "bug": "баг",
    "meeti": "встре созво", "contr": "догов", "payme": "плате оплат", "price": "цена стоим", "cost": "стоим",
    "licen": "лицен", "hour": "час", "week": "недел", "month": "месяц", "data": "данны", "start": "старт",
    "datab": "базы", "valid": "валид", "dupli": "дубли", "secur": "безоп", "sourc": "источ", "sched": "распи",
    "parti": "парти", "index": "индек", "schem": "схема", "model": "модел", "goal": "цель", "risk": "риск",
}
_CONCEPT: dict[str, str] = {}
for _en, _ru in _BRIDGE.items():
    _group = (_en, *_ru.split())
    _cid = next((_CONCEPT[x] for x in _group if x in _CONCEPT), "~" + _en)  # shared stems join one concept
    for _stem in _group:
        _CONCEPT.setdefault(_stem, _cid)


def _stems(word: str) -> list[str]:
    """Stems of one word: 5-char prefix (+ prefix-less stem for long Russian verbs)."""
    out = [word[:5]]
    if len(word) >= 7 and _CYR.match(word):
        for p in _PREFIXES:
            if word.startswith(p):
                out.append(word[len(p):][:5])  # extra prefix-less stem
                break
    return out


def _word_groups(text: str) -> list[list[str]]:
    """Per word, the terms it contributes. Identifiers like dm_visits give the whole name and its parts."""
    groups = []
    for w in _WORD.findall(text.lower()):
        w = w.strip("_")
        if not w:
            continue
        if w.isdigit():
            if len(w) >= 3:
                groups.append([w])  # ticket numbers, years, amounts
            continue
        if "_" in w:
            terms = [w[:40]]
            for part in w.split("_"):
                if len(part) >= 3 and part not in STOP and not part.isdigit():
                    terms += _stems(part)
            groups.append(terms)
            continue
        if len(w) < 3 or w in STOP:
            continue
        groups.append(_stems(w))
    return groups


def tokenize(text: str) -> list[str]:
    return [t for g in _word_groups(text) for t in g]


def terms(text: str) -> list[str]:
    """Index terms: stems plus cross-language concept terms."""
    out = []
    for g in _word_groups(text):
        out += g
        out += {_CONCEPT[t] for t in g if t in _CONCEPT}
    return out


def query_groups(text: str) -> list[frozenset[str]]:
    """A question as word groups (a group matches if any of its terms does); duplicates removed."""
    seen, out = set(), []
    for g in _word_groups(text):
        grp = frozenset([*g, *(_CONCEPT[t] for t in g if t in _CONCEPT)])
        if grp not in seen:
            seen.add(grp)
            out.append(grp)
    return out


_SENT = re.compile(r"(?<=[.!?;])\s+")  # sentence breaks inside one long message
_DAY = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]$", re.M)  # day markers written by the chat importers


@dataclass
class Chunk:
    doc_id: str
    title: str
    date: str
    source: str
    text: str


@dataclass
class SearchHit:
    chunk: Chunk
    score: float
    groups: tuple = ()  # the query groups, for excerpting
    context: str = ""   # the chunk with its neighbours from the same document


def chunk_doc(doc: Doc, size: int = 500, overlap: int = 100) -> list[Chunk]:
    """Small (~500 chars ≈ 5–8 chat messages) chunks cut at line breaks, so a match means the words met in
    one place; the answer still gets the neighbouring chunks as context. A chunk's date is the last day
    marker at or before it."""
    text = doc.text
    if len(text) <= size:
        days = _DAY.findall(text)
        return [Chunk(doc.id, doc.title, days[-1] if days else doc.date, doc.source, text)]
    marks = [(m.start(), m.group(1)) for m in _DAY.finditer(text)]
    chunks, start, mi, day = [], 0, 0, doc.date
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # prefer to cut at a paragraph / line break
            cut = text.rfind("\n", start + size // 2, end)
            end = cut if cut > 0 else end
        while mi < len(marks) and marks[mi][0] < end:
            day = marks[mi][1]
            mi += 1
        body = text[start:end].strip()
        if body:
            chunks.append(Chunk(doc.id, doc.title, day, doc.source, body))
        if end >= len(text):
            break
        nxt = max(end - overlap, start + 1)
        nl = text.find("\n", nxt, end)  # start the overlap on a whole line
        start = nl + 1 if nl != -1 and nl + 1 < end else nxt
    return chunks


class KnowledgeIndex:
    """BM25 over chunks with an inverted index (fast on tens of thousands of chunks).

    A chunk is relevant when its text matches at least two of the question's words (one for one-word
    questions); a match only in the document title counts as a word but not as the only evidence.
    """

    def __init__(self, docs: list[Doc], k1: float = 1.4, b: float = 0.75):
        self.docs: list[Doc] = []
        self.chunks: list[Chunk] = []
        self._post: dict[str, tuple[array, array]] = {}  # term → (chunk ids, term freqs), compact
        self._len: list[int] = []
        self._total = 0
        self._title_terms: dict[str, frozenset[str]] = {}
        self._lock = threading.Lock()  # extend() from an upload thread vs search() from an answer thread
        self.k1, self.b = k1, b
        self.extend(docs)

    def extend(self, docs: list[Doc]) -> None:
        """Add new documents without re-indexing the old ones (an upload during a meeting stays cheap)."""
        prepared = []
        for d in docs:  # tokenize outside the lock
            cs = chunk_doc(d)
            prepared.append((d, cs, [Counter(terms(c.text)) for c in cs]))
        with self._lock:
            for d, cs, tfs in prepared:
                self.docs.append(d)
                self._title_terms[d.id] = frozenset(terms(d.title))
                for c, tf in zip(cs, tfs):
                    i = len(self.chunks)
                    self.chunks.append(c)
                    n_tok = sum(tf.values())
                    self._len.append(n_tok)
                    self._total += n_tok
                    for t, f in tf.items():
                        p = self._post.get(t)
                        if p is None:
                            p = self._post[t] = (array("I"), array("H"))
                        p[0].append(i)
                        p[1].append(min(f, 65535))

    @property
    def _avg(self) -> float:
        return self._total / len(self._len) if self._len else 0.0

    def _idf_of(self, t: str) -> float:
        p = self._post.get(t)
        if not p:
            return 0.0
        n, df = len(self.chunks), len(p[0])
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def __len__(self) -> int:
        return len(self.chunks)

    def _around(self, ci: int, n: int = 2) -> str:
        """The chunk plus up to `n` chunks on each side from the same document (overlaps removed)."""
        doc = self.chunks[ci].doc_id
        lo, hi = ci, ci
        while lo > 0 and ci - lo < n and self.chunks[lo - 1].doc_id == doc:
            lo -= 1
        while hi + 1 < len(self.chunks) and hi - ci < n and self.chunks[hi + 1].doc_id == doc:
            hi += 1
        out: list[str] = []
        for c in self.chunks[lo:hi + 1]:
            for line in c.text.split("\n"):
                if not out or line not in out[-12:]:  # chunks overlap by a line or two
                    out.append(line)
        return "\n".join(out)

    def _focus(self, text: str, groups) -> float:
        """Best count of question words in ONE line (message); a neighbouring line adds half for words the
        line itself lacks (a question and its answer are often two messages). Rarity breaks ties."""
        lines = [x for ln in text.split("\n") for x in _SENT.split(ln) if x.strip()]  # messages → sentences
        sets = [set(terms(ln)) for ln in lines]
        best = 0.0
        for i, ts in enumerate(sets):
            own = [g for g in groups if g & ts]
            if not own:
                continue
            near = sets[i - 1] if i else set()
            near = near | (sets[i + 1] if i + 1 < len(sets) else set())
            extra = [g for g in groups if not (g & ts) and g & near]
            val = len(own) + 0.5 * len(extra) + 0.01 * sum(max(self._idf_of(t) for t in g) for g in own)
            if val > best:
                best = val
        return best

    def search(self, query: str, k: int = 3, min_terms: int = 2, per_doc: int = 2) -> list[SearchHit]:
        with self._lock:
            return self._search(query, k, min_terms, per_doc)

    def _search(self, query: str, k: int, min_terms: int, per_doc: int) -> list[SearchHit]:
        groups = query_groups(query)
        if not groups or not self.chunks:
            return []
        need = min(min_terms, len(groups))
        body: dict[int, list[float]] = {}  # chunk → best score per group
        for gi, grp in enumerate(groups):
            for t in grp:
                post = self._post.get(t)
                if not post:
                    continue
                idf = self._idf_of(t)
                for ci, f in zip(*post):
                    dl = self._len[ci] or 1
                    s = idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self._avg or 1)))
                    row = body.setdefault(ci, [0.0] * len(groups))
                    if s > row[gi]:
                        row[gi] = s
        ident = [any(("_" in t) or t.isdigit() for t in g) for g in groups]  # dm_visits, REP-14768 …
        scored = []
        for ci, row in body.items():
            matched_body = sum(1 for v in row if v > 0)
            title = self._title_terms.get(self.chunks[ci].doc_id, frozenset())
            title_only = [gi for gi, g in enumerate(groups) if row[gi] == 0 and g & title]
            exact_id = any(row[gi] > 0 and ident[gi] for gi in range(len(groups)))
            if matched_body + len(title_only) < need and not exact_id:
                continue
            s = sum(row) + sum(0.3 * max(self._idf_of(t) for t in groups[gi]) for gi in title_only)
            scored.append((matched_body + len(title_only), round(s, 3), self.chunks[ci].date, ci))
        scored.sort(reverse=True)
        # Re-rank the best candidates by how many question words meet in ONE message/line (± a neighbour):
        # in a long chat, words scattered over 30 unrelated messages are not an answer.
        top = []
        for m, s, d, ci in scored[:300]:
            top.append((self._focus(self.chunks[ci].text, groups), m, s, d, ci))
        top.sort(reverse=True)
        hits, per = [], Counter()
        for _, m, s, _, ci in top:
            c = self.chunks[ci]
            if per[c.doc_id] >= per_doc:  # keep the context diverse
                continue
            per[c.doc_id] += 1
            hits.append(SearchHit(c, round(s, 2), tuple(groups), self._around(ci)))
            if len(hits) >= k:
                break
        return hits


def excerpt(text: str, groups, budget: int) -> str:
    """The part of a chunk around the lines that match the question, within `budget` chars."""
    if len(text) <= budget:
        return text
    lines = text.split("\n")
    if not groups:
        return text[:budget]

    def hits(line):
        ts = set(terms(line))
        return sum(1 for g in groups if g & ts)

    best = max(range(len(lines)), key=lambda i: (hits(lines[i]), -i))
    lo = hi = best
    size = len(lines[best])
    while True:  # grow around the best line, preferring the side with more matches
        grown = False
        for j in sorted((lo - 1, hi + 1), key=lambda j: -hits(lines[j]) if 0 <= j < len(lines) else 1):
            if 0 <= j < len(lines) and size + len(lines[j]) + 1 <= budget:
                size += len(lines[j]) + 1
                lo, hi = min(lo, j), max(hi, j)
                grown = True
        if not grown:
            break
    out = "\n".join(lines[lo:hi + 1])
    return out[:budget] if len(out) > budget else out


class KnowledgeStore:
    """data/private/<user_id>/chats.jsonl — loaded lazily, reloaded when the file changes."""

    def __init__(self, root: Path, user_id: str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user_id) or "_"
        self.dir = Path(root) / safe
        self.path = self.dir / "chats.jsonl"
        self.user_id = user_id
        self._mtime: float | None = None
        self._index: KnowledgeIndex | None = None

    def load_docs(self) -> list[Doc]:
        if not self.path.exists():
            return []
        docs = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    docs.append(Doc(**json.loads(line)))
        return docs

    def add(self, docs: list[Doc]) -> tuple[int, int]:
        """Merge docs into the store; returns (added_or_updated, total)."""
        docs = dedupe(docs)
        current = self._index is not None and self.path.exists() and self.path.stat().st_mtime == self._mtime
        have = {d.id for d in self._index.docs} if current else {d.id for d in self.load_docs()}
        self.dir.mkdir(parents=True, exist_ok=True)
        if not any(d.id in have for d in docs):
            with self.path.open("a", encoding="utf-8") as fh:  # new conversations: append + index only them
                for d in docs:
                    fh.write(d.to_json() + "\n")
            if current:
                self._index.extend(docs)
                self._mtime = self.path.stat().st_mtime
            total = len(have) + len(docs)
        else:  # a re-import replaces conversations: rewrite the file, rebuild the index on next use
            merged = dedupe([*self.load_docs(), *docs])
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for d in merged:
                    fh.write(d.to_json() + "\n")
            tmp.replace(self.path)
            self._index = None
            total = len(merged)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return len(docs), total

    @property
    def index(self) -> KnowledgeIndex:
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if self._index is None or mtime != self._mtime:
            self._index = KnowledgeIndex(self.load_docs())
            self._mtime = mtime
        return self._index

    def remove(self, doc_id: str) -> bool:
        docs = self.load_docs()
        kept = [d for d in docs if d.id != doc_id]
        if len(kept) == len(docs):
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for d in kept:
                fh.write(d.to_json() + "\n")
        tmp.replace(self.path)
        self._index = None
        return True

    def list_docs(self) -> list[dict]:
        return [{"id": d.id, "title": d.title, "source": d.source, "date": d.date, "chars": len(d.text)}
                for d in self.index.docs]

    # ---- profile («Обо мне»): data/private/<user>/profile.md
    @property
    def profile_path(self) -> Path:
        return self.dir / "profile.md"

    def read_profile(self) -> str:
        return self.profile_path.read_text(encoding="utf-8") if self.profile_path.exists() else ""

    def write_profile(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
        try:
            self.profile_path.chmod(0o600)
        except OSError:
            pass

    # ---- «Слова для распознавания»: data/private/<user>/vocab.txt
    @property
    def vocab_path(self) -> Path:
        return self.dir / "vocab.txt"

    def read_vocab(self) -> str:
        return self.vocab_path.read_text(encoding="utf-8") if self.vocab_path.exists() else ""

    def write_vocab(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.vocab_path.write_text(text.strip() + ("\n" if text.strip() else ""), encoding="utf-8")
        try:
            self.vocab_path.chmod(0o600)
        except OSError:
            pass

    def keyterms(self, limit: int = 25) -> list[str]:
        """Most frequent names/acronyms in the saved chats (cached until the store changes)."""
        from copilot.knowledge.keyterms import doc_keyterms

        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime is None:
            return []
        if getattr(self, "_kt_mtime", None) != mtime:
            self._kt = doc_keyterms((d.text for d in self.index.docs), limit=limit, min_count=3)
            self._kt_mtime = mtime
        return self._kt

    def stats(self) -> dict:
        idx = self.index
        by_source = Counter(d.source for d in idx.docs)
        return {"docs": len(idx.docs), "chunks": len(idx), "by_source": dict(by_source)}

    def search(self, query: str, k: int = 3) -> list[SearchHit]:
        return self.index.search(query, k=k)

    def warm(self) -> None:
        """Build the index now (call off the event loop) so the first answer during a meeting is fast."""
        _ = self.index
