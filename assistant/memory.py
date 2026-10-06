"""Local long-term memory (SQLite, data/jarvis_memory.db).

Long-term memory holds deliberately chosen, durable information. It is separate from the
short-term conversation context (which lives in the Brain and is never stored here
wholesale). Jarvis saves a memory only through the memory tools.

Memory v2 (Phase 2B · M1)
-------------------------
Every memory has:
  kind     what sort of statement it is — a FACT is objective, a PREFERENCE is a taste, a
           GOAL is something she wants to achieve, a HYPOTHESIS is an idea she is considering
           or testing (never a decision), a DECISION is something she has decided; plus
           project, person, commitment, routine, followup, kpi.
  domain   business | growth | equestrian | personal | general
  status   active | future | paused | completed | archived | superseded
  slot_key optional "there is only one current value of this" key, e.g.
           business.revenue_target.current. Saving a new value for a slot SUPERSEDES the old
           one: the old row stays as history (status superseded, valid_until, superseded_by)
           but never enters the working set again.
  data     optional JSON with exact values ({"amount": 10000, "currency": "EUR"}).
  source / source_ref  where it came from (conversation, onboarding:<id>, manual).

Only active (and, clearly labelled, FUTURE) memories reach Claude on each turn — see
working_set(). Archived, completed and superseded memories are history: they are only
returned when history is explicitly requested.

The v1 → v2 migration adds columns in place (ids and content preserved), after copying the
database to data/backups/.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import sqlite3
import threading
import time
from pathlib import Path

KINDS = (
    "fact",
    "preference",
    "goal",
    "hypothesis",
    "decision",
    "project",
    "person",
    "commitment",
    "routine",
    "followup",
    "kpi",
)
DOMAINS = ("business", "growth", "equestrian", "personal", "general")
STATUSES = ("active", "future", "paused", "completed", "archived", "superseded")
# Statuses that are "current" (searchable by default). Everything else is history.
CURRENT_STATUSES = ("active", "future", "paused")
HISTORY_STATUSES = ("completed", "archived", "superseded")
# Changing one of these while it is active is a significant change: it needs confirmation.
GUARDED_KINDS = ("goal", "decision")

SCHEMA_VERSION = 2

SCHEMA_V1 = """
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

# Columns added by v2 (name, definition). Added with ALTER TABLE, so existing rows keep
# their ids and content and get the defaults.
V2_COLUMNS = (
    ("domain", "TEXT NOT NULL DEFAULT 'general'"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("slot_key", "TEXT NOT NULL DEFAULT ''"),
    ("data", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT 'conversation'"),
    ("source_ref", "TEXT NOT NULL DEFAULT ''"),
    ("valid_from", "REAL"),
    ("valid_until", "REAL"),
    ("superseded_by", "INTEGER"),
)

V2_INDEXES = """
CREATE INDEX IF NOT EXISTS memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS memories_slot ON memories(slot_key);
CREATE INDEX IF NOT EXISTS memories_source_ref ON memories(source_ref);
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

# How the working set is put together: (title, filter, limit). Order = priority.
WORKING_SET_SECTIONS = (
    ("CURRENT GOALS", "kind = 'goal' AND status = 'active'", 6),
    ("ACTIVE PROJECTS", "kind = 'project' AND status = 'active'", 6),
    ("DECISIONS (decided)", "kind = 'decision' AND status = 'active'", 8),
    ("HYPOTHESES (being considered/tested — NOT decided)", "kind = 'hypothesis' AND status = 'active'", 8),
    ("KPIs", "kind = 'kpi' AND status = 'active'", 6),
    ("PREFERENCES & ROUTINES", "kind IN ('preference', 'routine') AND status = 'active' AND importance >= 3", 8),
    ("COMMITMENTS & FOLLOW-UPS", "kind IN ('commitment', 'followup') AND status = 'active' AND importance >= 3", 6),
    ("KEY FACTS & PEOPLE", "kind IN ('fact', 'person') AND status = 'active' AND importance >= 3", 10),
    ("FUTURE GOALS (FUTURE — not a current priority; never use for today's prioritization)",
     "status = 'future'", 4),
)

# Canonical slot keys Jarvis should use for single-valued information.
CANONICAL_SLOTS = (
    "business.revenue_target.current",
    "business.revenue_target.future",
    "business.offer",
    "business.icp",
    "business.niche",
    "business.pricing",
    "business.acquisition_channel",
    "business.delivery_model",
)


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _norm_data(data) -> str:
    if data in (None, "", {}):
        return ""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return data
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _slot(value: str) -> str:
    return "".join(c for c in (value or "").strip().lower().replace(" ", "_") if c.isalnum() or c in "._-")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        existed = path.exists() and path.stat().st_size > 0
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.backup_path: Path | None = None
        self.db.executescript(SCHEMA_V1)
        self._migrate(existed)
        try:
            self.db.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:  # SQLite without FTS5: fall back to LIKE search
            self.fts = False

    # ------------------------------------------------------------------ infrastructure
    @contextlib.contextmanager
    def _tx(self):
        """One all-or-nothing transaction (the connection is otherwise in autocommit)."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def schema_version(self) -> int:
        return int(self.db.execute("PRAGMA user_version").fetchone()[0])

    def backup(self, reason: str) -> Path:
        """Consistent copy of the database to data/backups/ (SQLite backup API)."""
        folder = self.path.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{reason}.db"
        n = 1
        while target.exists():
            n += 1
            target = folder / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{reason}-{n}.db"
        dest = sqlite3.connect(str(target))
        try:
            self.db.backup(dest)
        finally:
            dest.close()
        return target

    def _migrate(self, existed: bool) -> None:
        version = self.schema_version()
        if version >= SCHEMA_VERSION:
            return
        has_rows = existed and self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] > 0
        if has_rows:
            self.backup_path = self.backup("before-memory-v2")
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(memories)")}
        with self._tx():
            for name, definition in V2_COLUMNS:
                if name not in columns:
                    self.db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            # v1 had kind 'business' (business context): it is a fact in the business domain.
            self.db.execute("UPDATE memories SET kind = 'fact', domain = 'business' WHERE kind = 'business'")
            self.db.execute(
                f"UPDATE memories SET kind = 'fact' WHERE kind NOT IN ({','.join('?' * len(KINDS))})", KINDS
            )
            self.db.execute("UPDATE memories SET valid_from = created_at WHERE valid_from IS NULL")
            for statement in V2_INDEXES.strip().split(";"):
                if statement.strip():
                    self.db.execute(statement)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------ analysis (no writes)
    def current_for_slot(self, slot_key: str) -> dict | None:
        slot_key = _slot(slot_key)
        if not slot_key:
            return None
        row = self.db.execute(
            f"SELECT * FROM memories WHERE slot_key = ? AND status IN ({','.join('?' * len(CURRENT_STATUSES))}) "
            "ORDER BY (status = 'active') DESC, updated_at DESC LIMIT 1",
            (slot_key, *CURRENT_STATUSES),
        ).fetchone()
        return dict(row) if row else None

    def plan_remember(
        self,
        kind: str,
        content: str,
        subject: str = "",
        importance: int = 3,
        domain: str = "general",
        status: str = "active",
        slot_key: str = "",
        data=None,
        replaces_id: int | None = None,
        additional: bool = False,
    ) -> dict:
        """Decide what saving this memory would do, without writing anything.

        Returns {"action": "insert" | "unchanged" | "merge" | "supersede" | "conflict" | "reject",
                 "target": existing row or None, "needs_confirmation": reason or "", ...}.
        """
        kind = kind if kind in KINDS else "fact"
        domain = domain if domain in DOMAINS else "general"
        status = status if status in ("active", "future", "paused") else "active"
        content = _norm(content)
        subject = _norm(subject)
        slot_key = _slot(slot_key)
        if not content:
            return {"action": "reject", "reason": "empty memory"}
        try:
            importance = max(1, min(5, int(importance or 3)))
        except (TypeError, ValueError):
            importance = 3
        new = {
            "kind": kind, "content": content, "subject": subject, "importance": importance,
            "domain": domain, "status": status, "slot_key": slot_key, "data": _norm_data(data),
        }
        if importance <= 1:
            return {"action": "reject", "new": new, "reason": "too minor for long-term memory"}

        target = None
        if replaces_id:
            target = self.get(int(replaces_id))
            if target is None:
                return {"action": "reject", "new": new, "reason": f"memory #{replaces_id} not found"}
            if target["status"] not in CURRENT_STATUSES:
                return {"action": "reject", "new": new, "reason": f"memory #{replaces_id} is already history ({target['status']})"}
            if not slot_key:
                new["slot_key"] = target["slot_key"]
        elif slot_key:
            target = self.current_for_slot(slot_key)

        if target is not None:
            if self._same(target, new):
                return {"action": "unchanged", "new": new, "target": target, "needs_confirmation": ""}
            return {
                "action": "supersede", "new": new, "target": target,
                "needs_confirmation": self._guard_reason(target, new),
            }

        # No slot: merge near-duplicates (same kind and subject), as in Phase 2A.
        rows = self.db.execute(
            f"SELECT * FROM memories WHERE kind = ? AND lower(subject) = lower(?) "
            f"AND status IN ({','.join('?' * len(CURRENT_STATUSES))})",
            (kind, subject, *CURRENT_STATUSES),
        ).fetchall()
        for row in rows:
            ratio = difflib.SequenceMatcher(None, row["content"].lower(), content.lower()).ratio()
            if ratio >= 0.8:
                if kind in GUARDED_KINDS and row["status"] == "active" and _norm(row["content"]).lower() != content.lower():
                    # A changed active goal/decision is a replacement, never a silent merge.
                    return {"action": "supersede", "new": new, "target": dict(row),
                            "needs_confirmation": self._guard_reason(dict(row), new)}
                return {"action": "merge", "new": new, "target": dict(row), "needs_confirmation": ""}

        # A new ACTIVE goal or decision next to existing active ones on the same subject or
        # in the same domain may be a hidden replacement: make Claude say which it is.
        if kind in ("goal", "decision") and status == "active" and not additional:
            similar = [
                self._public(r) for r in self.db.execute(
                    "SELECT * FROM memories WHERE kind = ? AND status = 'active' AND domain = ?",
                    (kind, domain),
                ).fetchall()
                if (subject and r["subject"].lower() == subject.lower())
                or difflib.SequenceMatcher(None, r["content"].lower(), content.lower()).ratio() >= 0.5
            ]
            if similar:
                return {"action": "conflict", "new": new, "existing": similar}
        return {"action": "insert", "new": new, "target": None, "needs_confirmation": ""}

    @staticmethod
    def _same(row: dict, new: dict) -> bool:
        return (
            _norm(row["content"]).lower() == new["content"].lower()
            and row["kind"] == new["kind"]
            and row["status"] == new["status"]
            and _norm_data(row.get("data")) == new["data"]
        )

    @staticmethod
    def _guard_reason(old: dict, new: dict) -> str:
        """Why replacing `old` with `new` needs the user's confirmation ('' = it doesn't)."""
        if old["kind"] == "hypothesis" and new["kind"] == "decision":
            return f"turn the idea \"{old['content']}\" into a DECISION: \"{new['content']}\""
        if old["status"] == "active" and old["kind"] in GUARDED_KINDS:
            label = "active goal" if old["kind"] == "goal" else "decision"
            return f"replace the {label} \"{old['content']}\" with \"{new['content']}\""
        if new["kind"] == "decision" and old["kind"] != "decision":
            return f"record as a DECISION: \"{new['content']}\" (replacing \"{old['content']}\")"
        return ""

    def plan_status(self, memory_id: int, status: str) -> dict:
        row = self.get(int(memory_id))
        if row is None:
            return {"action": "reject", "reason": f"memory #{memory_id} not found"}
        if status not in STATUSES or status == "superseded":
            return {"action": "reject", "reason": f"invalid status {status}"}
        if row["status"] == status:
            return {"action": "unchanged", "target": row, "needs_confirmation": ""}
        reason = ""
        if row["status"] == "active" and row["kind"] in GUARDED_KINDS:
            reason = f"change the {row['kind']} \"{row['content']}\" from active to {status}"
        elif row["status"] in HISTORY_STATUSES and status == "active":
            reason = f"reactivate the {row['status']} {row['kind']} \"{row['content']}\""
        return {"action": "status", "target": row, "status": status, "needs_confirmation": reason}

    # ------------------------------------------------------------------ writes
    def remember(self, kind: str, content: str, subject: str = "", importance: int = 3, *,
                 domain: str = "general", status: str = "active", slot_key: str = "", data=None,
                 replaces_id: int | None = None, additional: bool = False,
                 source: str = "conversation", source_ref: str = "", confirmed: bool = False) -> dict:
        """Save a memory, applying the replacement rules. A change that needs confirmation
        is refused unless confirmed=True (the tool layer asks the user first)."""
        with self._lock:
            plan = self.plan_remember(kind, content, subject, importance, domain, status, slot_key,
                                      data, replaces_id, additional)
            return self.apply_plan(plan, source=source, source_ref=source_ref, confirmed=confirmed)

    def apply_plan(self, plan: dict, *, source: str = "conversation", source_ref: str = "",
                   confirmed: bool = False) -> dict:
        action = plan["action"]
        if action == "reject":
            raise ValueError(plan["reason"])
        if action == "conflict":
            return {"status": "possible_conflict", "existing": plan["existing"]}
        if plan.get("needs_confirmation") and not confirmed:
            return {"status": "needs_confirmation", "reason": plan["needs_confirmation"]}
        new, target, now = plan["new"], plan.get("target"), time.time()
        with self._tx():
            if action == "unchanged":
                self.db.execute(
                    "UPDATE memories SET importance = MAX(importance, ?), updated_at = ?, "
                    "source_ref = CASE WHEN source_ref = '' THEN ? ELSE source_ref END WHERE id = ?",
                    (new["importance"], now, source_ref, target["id"]),
                )
                return {"id": target["id"], "status": "unchanged"}
            if action == "merge":
                self.db.execute(
                    "UPDATE memories SET content = ?, importance = MAX(importance, ?), updated_at = ?, "
                    "domain = CASE WHEN domain = 'general' THEN ? ELSE domain END, "
                    "data = CASE WHEN ? != '' THEN ? ELSE data END WHERE id = ?",
                    (new["content"], new["importance"], now, new["domain"], new["data"], new["data"], target["id"]),
                )
                return {"id": target["id"], "status": "updated"}
            new_id = self._insert(new, now, source, source_ref)
            if action == "supersede":
                self.db.execute(
                    "UPDATE memories SET status = 'superseded', valid_until = ?, superseded_by = ?, "
                    "updated_at = ? WHERE id = ?",
                    (now, new_id, now, target["id"]),
                )
                return {"id": new_id, "status": "saved", "superseded": target["id"]}
            return {"id": new_id, "status": "saved"}

    def _insert(self, new: dict, now: float, source: str, source_ref: str) -> int:
        cur = self.db.execute(
            "INSERT INTO memories (kind, subject, content, importance, created_at, updated_at, domain, "
            "status, slot_key, data, source, source_ref, valid_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new["kind"], new["subject"], new["content"], new["importance"], now, now, new["domain"],
             new["status"], new["slot_key"], new["data"], source, source_ref, now),
        )
        return int(cur.lastrowid)

    def set_status(self, memory_id: int, status: str, *, confirmed: bool = False) -> dict:
        with self._lock:
            plan = self.plan_status(memory_id, status)
            if plan["action"] == "reject":
                raise ValueError(plan["reason"])
            if plan["action"] == "unchanged":
                return {"id": int(memory_id), "status": "unchanged"}
            if plan["needs_confirmation"] and not confirmed:
                return {"status": "needs_confirmation", "reason": plan["needs_confirmation"]}
            now = time.time()
            ends = status in HISTORY_STATUSES
            with self._tx():
                self.db.execute(
                    "UPDATE memories SET status = ?, updated_at = ?, valid_until = ? WHERE id = ?",
                    (status, now, now if ends else None, int(memory_id)),
                )
            return {"id": int(memory_id), "status": status}

    def forget(self, memory_id: int) -> bool:
        with self._tx():
            cur = self.db.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            return cur.rowcount > 0

    def log_turn(self, role: str, text: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO conversation_log (ts, role, text) VALUES (?, ?, ?)",
                (time.time(), role, text),
            )

    # ------------------------------------------------------------------ reads
    def get(self, memory_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        return dict(row) if row else None

    def recall(self, query: str = "", kind: str = "", limit: int = 8, *,
               include_history: bool = False, domain: str = "") -> list[dict]:
        """Search memories. By default only current ones (active, future, paused); history
        (superseded, archived, completed) only when include_history=True."""
        limit = max(1, min(25, int(limit or 8)))
        statuses = STATUSES if include_history else CURRENT_STATUSES
        where = [f"m.status IN ({','.join('?' * len(statuses))})"]
        params: list = [*statuses]
        if kind:
            where.append("m.kind = ?")
            params.append(kind)
        if domain:
            where.append("m.domain = ?")
            params.append(domain)
        with self._lock:
            terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if len(t) > 1]
            if query.strip() and self.fts and terms:
                match = " OR ".join(f'"{t}"*' for t in terms)
                sql = (
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    f"WHERE memories_fts MATCH ? AND {' AND '.join(where)} "
                    "ORDER BY (m.status = 'active') DESC, bm25(memories_fts), m.importance DESC LIMIT ?"
                )
                rows = self.db.execute(sql, [match, *params, limit]).fetchall()
            elif query.strip() and not self.fts:
                like = f"%{query.strip()}%"
                sql = (
                    f"SELECT m.* FROM memories m WHERE (m.content LIKE ? OR m.subject LIKE ?) AND {' AND '.join(where)} "
                    "ORDER BY (m.status = 'active') DESC, m.importance DESC, m.updated_at DESC LIMIT ?"
                )
                rows = self.db.execute(sql, [like, like, *params, limit]).fetchall()
            elif query.strip():
                rows = []
            else:
                sql = (
                    f"SELECT m.* FROM memories m WHERE {' AND '.join(where)} "
                    "ORDER BY (m.status = 'active') DESC, m.importance DESC, m.updated_at DESC LIMIT ?"
                )
                rows = self.db.execute(sql, [*params, limit]).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.db.execute(
                    f"UPDATE memories SET last_used_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                    [time.time(), *ids],
                )
        return [self._public(r) for r in rows]

    def history(self, slot_key: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memories WHERE slot_key = ? ORDER BY valid_from", (_slot(slot_key),)
        ).fetchall()
        return [self._public(r) for r in rows]

    def working_set(self) -> list[tuple[str, list[dict]]]:
        """What Claude is given on every turn, by section, most important first. Only active
        memories, plus FUTURE goals in their own clearly labelled section. Never history."""
        sections = []
        seen: set[int] = set()
        for title, condition, limit in WORKING_SET_SECTIONS:
            rows = self.db.execute(
                f"SELECT * FROM memories WHERE {condition} ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [self._public(r) for r in rows if r["id"] not in seen]
            seen.update(i["id"] for i in items)
            if items:
                sections.append((title, items))
        return sections

    def profile(self, limit: int = 12) -> list[dict]:
        """Phase 2A interface: the working set as a flat list (active first, FUTURE last)."""
        return [item for _, items in self.working_set() for item in items][:limit]

    def count(self, include_history: bool = True) -> int:
        if include_history:
            return int(self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        return int(self.db.execute(
            f"SELECT COUNT(*) FROM memories WHERE status IN ({','.join('?' * len(CURRENT_STATUSES))})",
            CURRENT_STATUSES,
        ).fetchone()[0])

    def counts_by_status(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")}

    @staticmethod
    def _public(row) -> dict:
        row = dict(row)
        data = row.get("data") or ""
        try:
            data = json.loads(data) if data else None
        except ValueError:
            pass
        out = {
            "id": row["id"],
            "kind": row["kind"],
            "domain": row.get("domain", "general"),
            "status": row.get("status", "active"),
            "subject": row["subject"],
            "content": row["content"],
            "importance": row["importance"],
            "updated": time.strftime("%Y-%m-%d", time.localtime(row["updated_at"])),
        }
        if row.get("slot_key"):
            out["slot_key"] = row["slot_key"]
        if data:
            out["data"] = data
        if row.get("status") == "superseded" and row.get("superseded_by"):
            out["superseded_by"] = row["superseded_by"]
        if row.get("valid_until"):
            out["until"] = time.strftime("%Y-%m-%d", time.localtime(row["valid_until"]))
        return out
