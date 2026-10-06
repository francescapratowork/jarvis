"""Local long-term memory (SQLite, data/jarvis_memory.db).

Long-term memory holds deliberately chosen facts (goals, projects, people, commitments,
preferences, routines, business context, decisions, follow-ups). It is separate from the
short-term conversation context, which lives in the Brain and is never stored here
wholesale. Jarvis saves a memory only through the `memory_remember` tool.
"""

from __future__ import annotations

import difflib
import sqlite3
import threading
import time
from pathlib import Path

KINDS = (
    "goal",
    "project",
    "person",
    "commitment",
    "preference",
    "routine",
    "business",
    "decision",
    "followup",
    "fact",
)
# Kinds always given to Claude as background ("profile"), most important first.
PROFILE_KINDS = ("goal", "preference", "routine", "business", "project", "commitment")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind);
CREATE TABLE IF NOT EXISTS conversation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    subject, content, kind, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, subject, content, kind)
    VALUES (new.id, new.subject, new.content, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content, kind)
    VALUES ('delete', old.id, old.subject, old.content, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content, kind)
    VALUES ('delete', old.id, old.subject, old.content, old.kind);
    INSERT INTO memories_fts(rowid, subject, content, kind)
    VALUES (new.id, new.subject, new.content, new.kind);
END;
"""


class MemoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:  # SQLite without FTS5: fall back to LIKE search
            self.fts = False
        self.db.commit()

    # ------------------------------------------------------------------ writes
    def remember(self, kind: str, content: str, subject: str = "", importance: int = 3) -> dict:
        kind = kind if kind in KINDS else "fact"
        content = " ".join(content.split())
        subject = " ".join((subject or "").split())
        importance = max(1, min(5, int(importance or 3)))
        if not content:
            raise ValueError("empty memory")
        now = time.time()
        with self._lock:
            # Near-duplicate of an existing memory of the same kind/subject: update it.
            rows = self.db.execute(
                "SELECT id, content FROM memories WHERE kind = ? AND lower(subject) = lower(?)",
                (kind, subject),
            ).fetchall()
            for row in rows:
                ratio = difflib.SequenceMatcher(None, row["content"].lower(), content.lower()).ratio()
                if ratio >= 0.8:
                    self.db.execute(
                        "UPDATE memories SET content = ?, importance = MAX(importance, ?), "
                        "updated_at = ? WHERE id = ?",
                        (content, importance, now, row["id"]),
                    )
                    self.db.commit()
                    return {"id": row["id"], "status": "updated"}
            cur = self.db.execute(
                "INSERT INTO memories (kind, subject, content, importance, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, subject, content, importance, now, now),
            )
            self.db.commit()
            return {"id": cur.lastrowid, "status": "saved"}

    def forget(self, memory_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            self.db.commit()
            return cur.rowcount > 0

    def log_turn(self, role: str, text: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO conversation_log (ts, role, text) VALUES (?, ?, ?)",
                (time.time(), role, text),
            )
            self.db.commit()

    # ------------------------------------------------------------------ reads
    def get(self, memory_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        return dict(row) if row else None

    def recall(self, query: str = "", kind: str = "", limit: int = 8) -> list[dict]:
        limit = max(1, min(25, int(limit or 8)))
        params: list = []
        with self._lock:
            if query.strip() and self.fts:
                terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 1]
                if terms:
                    match = " OR ".join(f'"{t}"*' for t in terms)
                    sql = (
                        "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                        "WHERE memories_fts MATCH ?"
                    )
                    params = [match]
                    if kind:
                        sql += " AND m.kind = ?"
                        params.append(kind)
                    sql += " ORDER BY bm25(memories_fts), m.importance DESC LIMIT ?"
                    params.append(limit)
                    rows = self.db.execute(sql, params).fetchall()
                else:
                    rows = []
            elif query.strip():
                like = f"%{query.strip()}%"
                sql = "SELECT * FROM memories WHERE (content LIKE ? OR subject LIKE ?)"
                params = [like, like]
                if kind:
                    sql += " AND kind = ?"
                    params.append(kind)
                sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
                params.append(limit)
                rows = self.db.execute(sql, params).fetchall()
            else:
                sql = "SELECT * FROM memories"
                if kind:
                    sql += " WHERE kind = ?"
                    params.append(kind)
                sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
                params.append(limit)
                rows = self.db.execute(sql, params).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.db.execute(
                    f"UPDATE memories SET last_used_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                    [time.time(), *ids],
                )
                self.db.commit()
        return [self._public(r) for r in rows]

    def profile(self, limit: int = 12) -> list[dict]:
        """The most important background facts, given to Claude on every turn."""
        rows = self.db.execute(
            f"SELECT * FROM memories WHERE kind IN ({','.join('?' * len(PROFILE_KINDS))}) "
            "AND importance >= 3 ORDER BY importance DESC, updated_at DESC LIMIT ?",
            [*PROFILE_KINDS, limit],
        ).fetchall()
        return [self._public(r) for r in rows]

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "subject": row["subject"],
            "content": row["content"],
            "importance": row["importance"],
            "updated": time.strftime("%Y-%m-%d", time.localtime(row["updated_at"])),
        }
