"""Private per-user knowledge: imported chat exports, searched to ground live answers.

Stored under ``data/private/<user_id>/chats.jsonl`` — never shared across users. A session only ever
gets the store of its own ``user_id`` (see SessionManager).
"""

from copilot.knowledge.index import KnowledgeIndex, KnowledgeStore, SearchHit
from copilot.knowledge.importers import Doc, import_bytes, import_path

__all__ = ["Doc", "import_bytes", "import_path", "KnowledgeIndex", "KnowledgeStore", "SearchHit"]
