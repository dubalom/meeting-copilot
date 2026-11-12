"""Importers for chat exports → normalized ``Doc`` records.

Supported (auto-detected by JSON shape, plain files or inside a .zip):
- claude.ai export: ``conversations.json`` (list of {name, created_at, chat_messages: [...]})
  and ``projects.json`` (list of {name, description, prompt_template, docs: [{filename, content}]})
- ChatGPT export: ``conversations.json`` (list of {title, create_time, mapping: {...}})
- Telegram Desktop export (JSON): ``result.json`` ({chats: {list: [...]}} or a single chat {name, messages})
- plain ``.md`` / ``.txt`` files, ``.pdf`` (pypdf) and ``.docx`` (one doc per file)
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# Limits that keep a server with 2 GB RAM safe. Nothing is silently cut: exceeding them is an error.
MAX_TOTAL_CHARS = 80_000_000      # text extracted from one upload (≈ a 300 MB Telegram JSON)
MAX_MEMBER_BYTES = 400 * 2**20    # one file inside a zip, uncompressed
MAX_ZIP_BYTES = 1200 * 2**20      # all parsed files inside a zip, uncompressed
# Plain-text formats read as is: notes, code, SQL, configs, data.
TEXT_EXT = (".md", ".txt", ".markdown", ".rst", ".tex", ".org", ".sql", ".py", ".ipynb", ".js", ".ts", ".tsx", ".jsx",
            ".java", ".kt", ".scala", ".go", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".rs", ".swift", ".r",
            ".sh", ".bash", ".zsh", ".ps1", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env.example",
            ".properties", ".xml", ".css", ".scss", ".vue", ".svelte", ".graphql", ".proto", ".dockerfile", ".tf",
            ".log", ".csv", ".tsv")
DATA_EXT = (".csv", ".tsv", ".log", ".json", ".xml")  # data dumps: indexed up to MAX_DATA_CHARS
MAX_DATA_CHARS = 3_000_000
OFFICE_EXT = (".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".rtf")
PARSED = (".json", ".zip", *TEXT_EXT, *OFFICE_EXT)  # everything else (photos, voice, binaries…) is skipped
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "__MACOSX", ".DS_Store",
              "dist", "build", ".next", ".cache", "target"}


class ImportFailed(ValueError):
    """The upload can't be read (broken archive…); the message is shown to the user (HTTP 422)."""


class ImportTooLarge(ImportFailed):
    """The upload is bigger than the server is willing to parse; the message is shown to the user (HTTP 413)."""


@dataclass
class Doc:
    id: str
    source: str  # claude | claude-project | chatgpt | telegram | file
    title: str
    date: str  # ISO date (YYYY-MM-DD) or ""
    text: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _id(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _date(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError):
        return str(value)[:10]


def _clip(text: str) -> str:  # kept for callers; documents are no longer truncated
    return text


def _with_days(items: Iterable[tuple[str, str]]) -> str:
    """Join (iso_date, line) pairs, inserting a «[YYYY-MM-DD]» marker whenever the day changes.
    The index uses the markers to date each chunk, so answers can say when something was said."""
    out, day = [], None
    for d, line in items:
        if d and d != day:
            out.append(f"[{d}]")
            day = d
        out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------- claude.ai
def _claude_message_text(m: dict) -> str:
    parts: list[str] = []
    for c in m.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
    text = "\n".join(parts) or (m.get("text") or "")
    for a in m.get("attachments") or []:
        if isinstance(a, dict) and a.get("extracted_content"):
            text += f"\n[вложение {a.get('file_name', '')}]\n{a['extracted_content'][:4000]}"
    return text.strip()


def parse_claude_conversations(data: list) -> Iterator[Doc]:
    for conv in data:
        msgs = conv.get("chat_messages") or []
        items = []
        for m in msgs:
            text = _claude_message_text(m)
            if text:
                who = "Я" if m.get("sender") == "human" else "Claude"
                items.append((_date(m.get("created_at") or conv.get("created_at")), f"{who}: {text}\n"))
        if not items:
            continue
        title = conv.get("name") or "Без названия"
        yield Doc(_id("claude", conv.get("uuid") or title), "claude", title, _date(conv.get("created_at")),
                  _with_days(items).strip())


def parse_claude_projects(data: list) -> Iterator[Doc]:
    for p in data:
        name = p.get("name") or "Проект"
        chunks = []
        if p.get("description"):
            chunks.append(f"Описание: {p['description']}")
        if p.get("prompt_template"):
            chunks.append(f"Инструкции проекта:\n{p['prompt_template']}")
        for d in p.get("docs") or []:
            if isinstance(d, dict) and d.get("content"):
                chunks.append(f"Файл {d.get('filename', '')}:\n{d['content']}")
        if chunks:
            yield Doc(_id("claude-project", p.get("uuid") or name), "claude-project", f"Проект: {name}",
                      _date(p.get("created_at")), _clip("\n\n".join(chunks)))


# --------------------------------------------------------------------------- ChatGPT
def parse_chatgpt(data: list) -> Iterator[Doc]:
    for conv in data:
        mapping = conv.get("mapping") or {}
        nodes = []
        for node in mapping.values():
            msg = (node or {}).get("message") or {}
            role = (msg.get("author") or {}).get("role")
            parts = (msg.get("content") or {}).get("parts") or []
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if role in ("user", "assistant") and text:
                nodes.append((msg.get("create_time") or 0, "Я" if role == "user" else "ChatGPT", text))
        nodes.sort(key=lambda x: x[0] or 0)
        if not nodes:
            continue
        title = conv.get("title") or "Без названия"
        text = _with_days((_date(ts) if ts else "", f"{w}: {t}\n") for ts, w, t in nodes).strip()
        yield Doc(_id("chatgpt", conv.get("id") or conv.get("conversation_id") or title), "chatgpt", title,
                  _date(conv.get("create_time")), text)


# --------------------------------------------------------------------------- Telegram
def _tg_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(x if isinstance(x, str) else str((x or {}).get("text", "")) for x in value)
    return ""


def _parse_tg_chat(chat: dict) -> Doc | None:
    items = []
    for m in chat.get("messages") or []:
        if m.get("type") != "message":
            continue
        text = _tg_text(m.get("text")).strip()
        if not text:
            continue
        items.append((_date(m.get("date")), f"{m.get('from') or '?'}: {text}"))
    if not items:
        return None
    title = chat.get("name") or "Telegram"
    return Doc(_id("telegram", str(chat.get("id") or title)), "telegram", f"Telegram: {title}", items[0][0],
               _with_days(items))


def parse_telegram(data: dict) -> Iterator[Doc]:
    chats = (data.get("chats") or {}).get("list") if isinstance(data.get("chats"), dict) else None
    for chat in chats if chats is not None else [data]:
        doc = _parse_tg_chat(chat)
        if doc:
            yield doc


# --------------------------------------------------------------------------- dispatch
def parse_json(name: str, data: Any) -> list[Doc]:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        first = data[0]
        if "chat_messages" in first:
            return list(parse_claude_conversations(data))
        if "mapping" in first:
            return list(parse_chatgpt(data))
        if "prompt_template" in first or "docs" in first:
            return list(parse_claude_projects(data))
    if isinstance(data, dict) and ("messages" in data or "chats" in data):
        return list(parse_telegram(data))
    return []


def clean_path(name: str) -> str:
    """A file's path as the user knows it: «Проект/договоры/2025/договор.pdf» (no leading ./, backslashes)."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or name


def skipped_path(name: str) -> bool:
    """Version-control folders, dependencies, build output and OS junk are never knowledge."""
    parts = clean_path(name).split("/")
    return any(p in SKIP_PARTS or p.startswith("._") for p in parts) or parts[-1].startswith(".")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp1251"):  # Russian Windows files are often cp1251
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_file(name: str, raw: bytes) -> list[Doc]:
    lower = name.lower()
    path = clean_path(name)
    text = ""
    if lower.endswith(".json"):
        try:
            data = json.loads(_decode(raw))
        except json.JSONDecodeError:
            return []
        docs = parse_json(name, data)
        if docs:
            return docs  # a chat export
        text = json.dumps(data, ensure_ascii=False, indent=1)  # any other JSON: config, API dump…
    elif lower.endswith(".ipynb"):
        text = _ipynb_text(raw)
    elif lower.endswith(TEXT_EXT):
        text = _decode(raw)
    elif lower.endswith(".pdf"):
        text = _pdf_text(raw)
    elif lower.endswith(".docx"):
        text = _docx_text(raw)
    elif lower.endswith(".xlsx"):
        text = _xlsx_text(raw)
    elif lower.endswith(".pptx"):
        text = _pptx_text(raw)
    elif lower.endswith(".rtf"):
        text = _rtf_text(raw)
    elif lower.endswith((".html", ".htm")):
        tg = _telegram_html(name, raw)
        if tg is not None:
            return [tg]
        text = _html_text(raw)
    text = text.strip()
    if not text:
        return []
    if lower.endswith(DATA_EXT) and len(text) > MAX_DATA_CHARS:
        text = text[:MAX_DATA_CHARS] + f"\n[… файл данных обрезан: показаны первые {MAX_DATA_CHARS // 1_000_000} млн символов]"
    # the folder path is part of the title: «договоры/2025» is searchable and tells Claude where the file lives
    return [Doc(_id("file", path, hashlib.sha1(raw).hexdigest()), "file", path, "", text)]


def _xml_runs(xml: str, tag: str) -> list[str]:
    import html

    return [html.unescape(t) for t in re.findall(rf"<{tag}(?: [^>]*)?>(.*?)</{tag}>", xml, flags=re.S)]


def _xlsx_text(raw: bytes) -> str:
    """Every sheet as tab-separated rows (shared strings resolved). No extra dependency."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return ""
    with z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            shared = ["".join(_xml_runs(si, "t")) for si in re.findall(r"<si>(.*?)</si>", sx, flags=re.S)]
        sheet_names = re.findall(r'<sheet [^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "replace")) \
            if "xl/workbook.xml" in names else []
        out = []
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, sn in enumerate(sheets):
            xml = z.read(sn).decode("utf-8", errors="replace")
            out.append(f"## Лист: {sheet_names[i] if i < len(sheet_names) else i + 1}")
            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, flags=re.S):
                cells = []
                for attrs, inner in re.findall(r"<c([^>]*)>(.*?)</c>", row, flags=re.S):
                    v = re.search(r"<v>(.*?)</v>", inner, flags=re.S)
                    t = re.search(r't="(\w+)"', attrs)
                    if t and t.group(1) == "s" and v:
                        idx = int(v.group(1))
                        cells.append(shared[idx] if idx < len(shared) else "")
                    elif t and t.group(1) == "inlineStr":
                        cells.append("".join(_xml_runs(inner, "t")))
                    else:
                        cells.append(v.group(1) if v else "")
                if any(c.strip() for c in cells):
                    out.append("\t".join(cells))
                if sum(len(x) for x in out) > MAX_DATA_CHARS:
                    out.append("[… таблица обрезана]")
                    return "\n".join(out)
        return "\n".join(out)


def _pptx_text(raw: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return ""
    with z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for i, sn in enumerate(slides, 1):
            xml = z.read(sn).decode("utf-8", errors="replace")
            paras = ["".join(_xml_runs(p, "a:t")) for p in re.findall(r"<a:p>(.*?)</a:p>", xml, flags=re.S)]
            out.append(f"## Слайд {i}\n" + "\n".join(p for p in paras if p.strip()))
        return "\n\n".join(out)


def _ipynb_text(raw: bytes) -> str:
    try:
        nb = json.loads(_decode(raw))
    except json.JSONDecodeError:
        return ""
    out = []
    for cell in nb.get("cells") or []:
        src = cell.get("source") or ""
        src = "".join(src) if isinstance(src, list) else str(src)
        if src.strip():
            out.append(("```\n" + src + "\n```") if cell.get("cell_type") == "code" else src)
    return "\n\n".join(out)


def _rtf_text(raw: bytes) -> str:
    text = _decode(raw)
    text = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", "replace"), text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", text)
    return text


_TAG = re.compile(r"<[^>]+>")


def _unescape(s: str) -> str:
    import html

    return html.unescape(s)


def _html_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    return re.sub(r"\n\s*\n+", "\n\n", _unescape(_TAG.sub("", text)))


def _telegram_html(name: str, raw: bytes) -> Doc | None:
    """Telegram Desktop HTML export (messages.html, messages2.html …)."""
    html_text = raw.decode("utf-8", errors="replace")
    if 'class="message' not in html_text or "from_name" not in html_text:
        return None
    title_m = re.search(r'<div class="text bold">\s*(.*?)\s*</div>', html_text, flags=re.S)
    title = _unescape(_TAG.sub("", title_m.group(1))).strip() if title_m else Path(name).parent.name or "Telegram"
    items, sender, day = [], "?", ""
    for block in re.split(r'<div class="message ', html_text)[1:]:
        dm = re.search(r'title="(\d{2})\.(\d{2})\.(\d{4})', block)
        if dm:
            day = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        fm = re.search(r'<div class="from_name">\s*(.*?)\s*</div>', block, flags=re.S)
        if fm:
            sender = _unescape(_TAG.sub("", fm.group(1))).strip() or sender
        tm = re.search(r'<div class="text">\s*(.*?)\s*</div>', block, flags=re.S)
        if tm:
            text = _unescape(_TAG.sub("", re.sub(r"<br\s*/?>", "\n", tm.group(1)))).strip()
            if text:
                items.append((day, f"{sender}: {text}"))
    if not items:
        return None
    date_m = re.search(r'title="(\d{2})\.(\d{2})\.(\d{4})', html_text)
    date = f"{date_m.group(3)}-{date_m.group(2)}-{date_m.group(1)}" if date_m else ""
    return Doc(_id("telegram-html", title, name), "telegram", f"Telegram: {title}", date, _with_days(items))


def _pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n\n".join(f"[стр. {i}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))
    except Exception:
        return ""


def _docx_text(raw: bytes) -> str:
    """DOCX is a zip of XML: take paragraph texts from word/document.xml (no extra dependency)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile):
        return ""
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        runs = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p, flags=re.S)
        line = "".join(runs)
        line = line.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
        if line.strip():
            paras.append(line)
    return "\n".join(paras)


def iter_files(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                yield from iter_files(p)
    elif path.suffix.lower() == ".zip":
        yield from iter_zip(path.name, path.read_bytes())
    else:
        yield path.name, path.read_bytes()


def iter_zip(name: str, raw: bytes, depth: int = 0, budget: list[int] | None = None) -> Iterator[tuple[str, bytes]]:
    """Members of a (possibly nested) zip; skips folders, macOS junk and media (photos, video, voice).
    Refuses members/archives that would unpack beyond the limits (zip bombs, exports with huge files)."""
    budget = budget if budget is not None else [MAX_ZIP_BYTES]
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ImportFailed(f"Архив {Path(name).name} повреждён или это не zip") from exc
    with z:
        for info in z.infolist():
            fname = info.filename
            if info.is_dir() or "__MACOSX/" in fname or Path(fname).name.startswith("._"):
                continue
            if not fname.lower().endswith(PARSED) or skipped_path(fname):
                continue  # media in Telegram exports, .git, node_modules… — never even unpacked
            if info.file_size > MAX_MEMBER_BYTES:
                raise ImportTooLarge(f"{Path(fname).name}: {info.file_size // 2**20} МБ после распаковки — "
                                     f"больше {MAX_MEMBER_BYTES // 2**20} МБ. Выгрузите отдельный чат или период.")
            budget[0] -= info.file_size
            if budget[0] < 0:
                raise ImportTooLarge(f"Архив распаковывается больше чем в {MAX_ZIP_BYTES // 2**20} МБ текста — "
                                     "выгрузите чаты по отдельности.")
            try:
                data = z.read(info)
            except (zipfile.BadZipFile, EOFError, OSError) as exc:
                raise ImportFailed(f"Не удалось распаковать {Path(fname).name}: архив повреждён") from exc
            if fname.lower().endswith(".zip") and depth < 2:
                inner = clean_path(fname)[:-4]  # «архив.zip» inside a folder → its files live under «…/архив/»
                for sub_name, sub_data in iter_zip(fname, data, depth + 1, budget):
                    yield f"{inner}/{clean_path(sub_name)}", sub_data
            else:
                yield fname, data


def import_bytes(name: str, raw: bytes, report: dict | None = None) -> list[Doc]:
    """Parse one uploaded file (a .zip — with any folders inside — is unpacked, other types parsed directly).
    `report` (optional) receives {"files": n, "skipped": [names of files nothing was read from]}."""
    lower = name.lower()
    rep = report if report is not None else {}
    rep.setdefault("files", 0)
    rep.setdefault("skipped", [])
    if lower.endswith(".zip") or raw[:4] == b"PK\x03\x04" and not lower.endswith((".docx", ".xlsx", ".pptx")):
        docs: list[Doc] = []
        total = 0
        # «Папка/архив.zip» → its files live under «Папка/архив/…»; a zip whose single top folder is already
        # named like the archive keeps that folder instead of doubling it
        root = clean_path(name)[:-4] if lower.endswith(".zip") else ""
        parent, _, leaf = root.rpartition("/")
        for member, data in iter_zip(name, raw):
            rep["files"] += 1
            member_path = clean_path(member)
            if root:
                if member_path.split("/")[0] == leaf:
                    member_path = f"{parent}/{member_path}" if parent else member_path
                else:
                    member_path = f"{root}/{member_path}"
            got = parse_file(member_path, data)
            if not got:
                rep["skipped"].append(member_path)
            docs.extend(got)
            total += sum(len(d.text) for d in got)
            _check_total_chars(total)
        return docs
    rep["files"] += 1
    docs = parse_file(name, raw)
    if not docs:
        rep["skipped"].append(clean_path(name))
    _check_total_chars(sum(len(d.text) for d in docs))
    return docs


def _check_total(docs: list[Doc]) -> list[Doc]:
    _check_total_chars(sum(len(d.text) for d in docs))
    return docs


def _check_total_chars(total: int) -> None:
    if total > MAX_TOTAL_CHARS:
        raise ImportTooLarge(f"В файле {total // 1_000_000} млн символов текста — больше {MAX_TOTAL_CHARS // 1_000_000} млн. "
                             "Загрузите по частям (отдельные чаты или период).")


def import_path(path: str | Path) -> list[Doc]:
    docs: list[Doc] = []
    for name, raw in iter_files(Path(path).expanduser()):
        docs.extend(parse_file(name, raw))
    return docs


def dedupe(docs: Iterable[Doc]) -> list[Doc]:
    seen: dict[str, Doc] = {}
    for d in docs:
        seen[d.id] = d  # later import of the same conversation replaces the earlier one
    return list(seen.values())
