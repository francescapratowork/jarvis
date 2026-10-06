"""One-time (repeatable) memory onboarding from a profile file.

    ./start_jarvis.sh --import-profile                 dry run: shows what would change
    ./start_jarvis.sh --import-profile --apply         writes it (after a database backup)
    ./start_jarvis.sh --import-profile path/to/file.toml [--apply]

The default file is data/onboarding.toml (the data/ folder is never uploaded to GitHub and
update_jarvis.sh never touches it). Format: see onboarding.example.toml.

Every item has a stable `id`, stored with the memory as source_ref "onboarding:<id>":
  ADD        new item → a new memory
  UNCHANGED  already imported (or already known with the same content) → nothing written
  UPDATE     the item was imported before and changed in the file. A changed content/kind/
             data creates a new version (the previous one stays as history); a changed
             status/importance/subject/domain is updated in place
  SUPERSEDE  the item's slot_key is currently held by a different memory (e.g. one saved in
             conversation), or the item lists it in `replaces = [id, ...]` → the new item
             replaces it; the old one stays as history
The dry run also warns about current memories that look similar to a new goal/decision/
hypothesis/project ("possible overlap"), so they can be listed in `replaces` if intended.
Running the import again therefore never creates duplicates, and items removed from the
file are never deleted (set status = "archived" to retire one). Only the given file is read;
.env and API keys are never read or printed.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .memory import (
    CURRENT_STATUSES, DOMAINS, KINDS, STATUSES, MemoryStore, _norm, _norm_data, _slot,
)

ITEM_FIELDS = {"id", "kind", "content", "subject", "domain", "status", "importance", "slot_key", "data", "replaces"}
OVERLAP_KINDS = ("goal", "decision", "hypothesis", "project")
FILE_STATUSES = tuple(s for s in STATUSES if s != "superseded")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


class OnboardingError(ValueError):
    pass


@dataclass
class Change:
    action: str  # ADD | UNCHANGED | UPDATE | SUPERSEDE
    item: dict
    target: dict | None = None
    details: list[str] = field(default_factory=list)
    targets: list[dict] = field(default_factory=list)  # SUPERSEDE: every memory replaced
    overlaps: list[dict] = field(default_factory=list)  # similar current memories (warning only)
    new_version: bool = False  # UPDATE that creates a new version (content/kind/data changed)


def load_profile(path: Path) -> list[dict]:
    try:
        import tomllib
    except ImportError as e:  # Python < 3.11
        raise OnboardingError("reading the profile needs Python 3.11 or newer") from e
    if not path.is_file():
        raise OnboardingError(f"profile file not found: {path}")
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise OnboardingError(f"the profile file is not valid TOML: {e}") from e
    unknown_top = set(doc) - {"item", "meta"}
    if unknown_top:
        raise OnboardingError(f"unknown section(s) in the file: {', '.join(sorted(unknown_top))} (use [[item]])")
    raw = doc.get("item", [])
    if not isinstance(raw, list) or not raw:
        raise OnboardingError("the file has no [[item]] entries")
    return validate(raw)


def validate(raw: list) -> list[dict]:
    errors, items, ids, slots = [], [], set(), {}
    for n, entry in enumerate(raw, 1):
        where = f"item {n}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not a table")
            continue
        item_id = str(entry.get("id", "")).strip()
        where = f"item {n} ({item_id or 'no id'})"
        unknown = set(entry) - ITEM_FIELDS
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        if not _ID_RE.match(item_id):
            errors.append(f"{where}: id must be lowercase letters, digits, '-', '_' or '.'")
        elif item_id in ids:
            errors.append(f"{where}: duplicate id")
        ids.add(item_id)
        kind = str(entry.get("kind", "")).strip()
        if kind not in KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(KINDS)}")
        content = _norm(str(entry.get("content", "")))
        if not content:
            errors.append(f"{where}: content is empty")
        domain = str(entry.get("domain", "general")).strip()
        if domain not in DOMAINS:
            errors.append(f"{where}: domain must be one of {', '.join(DOMAINS)}")
        status = str(entry.get("status", "active")).strip()
        if status not in FILE_STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(FILE_STATUSES)}")
        importance = entry.get("importance", 3)
        if not isinstance(importance, int) or not 1 <= importance <= 5:
            errors.append(f"{where}: importance must be a whole number from 1 to 5")
            importance = 3
        data = entry.get("data")
        if data is not None and not isinstance(data, dict):
            errors.append(f"{where}: data must be a table, e.g. data = {{ amount = 10000 }}")
        replaces = entry.get("replaces", [])
        if not isinstance(replaces, list) or not all(isinstance(x, int) and x > 0 for x in replaces):
            errors.append(f"{where}: replaces must be a list of memory numbers, e.g. replaces = [3, 7]")
            replaces = []
        slot_key = _slot(str(entry.get("slot_key", "")))
        if slot_key and status in CURRENT_STATUSES:
            if slot_key in slots:
                errors.append(f"{where}: slot_key {slot_key} is also used by {slots[slot_key]} (only one current item per slot)")
            slots[slot_key] = item_id
        items.append({
            "id": item_id, "kind": kind, "content": content, "subject": _norm(str(entry.get("subject", ""))),
            "domain": domain, "status": status, "importance": importance, "slot_key": slot_key,
            "data": _norm_data(data), "replaces": replaces,
        })
    if errors:
        raise OnboardingError("the profile file has problems (nothing was written):\n  - " + "\n  - ".join(errors))
    return items


def _ref(item: dict) -> str:
    return f"onboarding:{item['id']}"


def _current_version(store: MemoryStore, ref: str) -> dict | None:
    row = store.db.execute(
        "SELECT * FROM memories WHERE source_ref = ? AND status != 'superseded' ORDER BY id DESC LIMIT 1", (ref,)
    ).fetchone()
    return dict(row) if row else None


def plan(store: MemoryStore, items: list[dict]) -> list[Change]:
    changes = []
    for item in items:
        existing = _current_version(store, _ref(item))
        if existing is not None:
            diffs = _diff(existing, item)
            if not diffs:
                changes.append(Change("UNCHANGED", item, existing))
            else:
                new_version = any(d.split(":")[0] in ("content", "kind", "data") for d in diffs)
                changes.append(Change("UPDATE", item, existing, details=diffs, new_version=new_version))
            continue
        holder = store.current_for_slot(item["slot_key"]) if item["slot_key"] and item["status"] in CURRENT_STATUSES else None
        if holder is not None and not _diff(holder, item, compare_meta=False) and not item["replaces"]:
            changes.append(Change("UNCHANGED", item, holder, details=["already known"]))
            continue
        targets = [holder] if holder is not None else []
        for memory_id in item["replaces"]:
            row = store.get(memory_id)
            if row is None:
                raise OnboardingError(f"item {item['id']}: replaces memory #{memory_id}, which does not exist")
            if row["status"] in CURRENT_STATUSES and all(t["id"] != row["id"] for t in targets):
                targets.append(row)
        if targets:
            c = Change("SUPERSEDE", item, targets[0], targets=targets)
        else:
            twin = _near_duplicate(store, item)
            if twin is not None:
                changes.append(Change("UNCHANGED", item, twin, details=["already known"]))
                continue
            c = Change("ADD", item)
        c.overlaps = _overlaps(store, item, {t["id"] for t in targets})
        changes.append(c)
    replaced = {t["id"] for c in changes for t in c.targets}
    for c in changes:  # a memory another item already replaces is not an overlap
        c.overlaps = [o for o in c.overlaps if o["id"] not in replaced]
    return changes


def _overlaps(store: MemoryStore, item: dict, exclude: set[int]) -> list[dict]:
    """Current memories (not from onboarding) that look like the same goal/decision/idea."""
    if item["kind"] not in OVERLAP_KINDS or item["status"] not in CURRENT_STATUSES:
        return []
    related = {"goal": ("goal",), "decision": ("decision", "hypothesis"),
               "hypothesis": ("decision", "hypothesis"), "project": ("project",)}[item["kind"]]
    rows = store.db.execute(
        f"SELECT * FROM memories WHERE kind IN ({','.join('?' * len(related))}) "
        f"AND status IN ({','.join('?' * len(CURRENT_STATUSES))}) AND source_ref NOT LIKE 'onboarding:%' "
        "AND domain IN (?, 'general')",
        (*related, *CURRENT_STATUSES, item["domain"]),
    ).fetchall()
    out = []
    for row in rows:
        if row["id"] in exclude:
            continue
        same_subject = item["subject"] and row["subject"].lower() == item["subject"].lower()
        ratio = difflib.SequenceMatcher(None, _norm(row["content"]).lower(), item["content"].lower()).ratio()
        # Goals are few and central: any other current goal in the same area is worth a look.
        if same_subject or ratio >= 0.45 or (row["kind"] == item["kind"] == "goal" and row["status"] == item["status"]):
            out.append(dict(row))
    return out


def _diff(row: dict, item: dict, compare_meta: bool = True) -> list[str]:
    diffs = []
    if _norm(row["content"]) != item["content"]:
        diffs.append(f"content: \"{_short(row['content'])}\" → \"{_short(item['content'])}\"")
    if row["kind"] != item["kind"]:
        diffs.append(f"kind: {row['kind']} → {item['kind']}")
    if _norm_data(row.get("data")) != item["data"]:
        diffs.append(f"data: {row.get('data') or '{}'} → {item['data'] or '{}'}")
    if row["status"] != item["status"]:
        diffs.append(f"status: {row['status']} → {item['status']}")
    if compare_meta:
        for name in ("importance", "subject", "domain", "slot_key"):
            if (row.get(name) or "") != (item[name] or ""):
                diffs.append(f"{name}: {row.get(name) or '—'} → {item[name] or '—'}")
    return diffs


def _near_duplicate(store: MemoryStore, item: dict) -> dict | None:
    rows = store.db.execute(
        f"SELECT * FROM memories WHERE kind = ? AND status = ? AND source_ref NOT LIKE 'onboarding:%'",
        (item["kind"], item["status"]),
    ).fetchall()
    for row in rows:
        if difflib.SequenceMatcher(None, _norm(row["content"]).lower(), item["content"].lower()).ratio() >= 0.9:
            return dict(row)
    return None


def apply(store: MemoryStore, changes: list[Change]) -> Path | None:
    """Write the planned changes in one transaction, after a backup."""
    if not any(c.action != "UNCHANGED" or c.target and not c.target.get("source_ref") for c in changes):
        return None
    backup = store.backup("before-onboarding")
    now = time.time()
    with store._tx():
        for c in changes:
            item, ref = c.item, _ref(c.item)
            if c.action == "UNCHANGED":
                if c.target and not c.target.get("source_ref"):
                    store.db.execute("UPDATE memories SET source_ref = ? WHERE id = ?", (ref, c.target["id"]))
            elif c.action == "ADD":
                store._insert(item, now, "onboarding", ref)
                if item["status"] not in CURRENT_STATUSES:
                    store.db.execute("UPDATE memories SET valid_until = ? WHERE source_ref = ?", (now, ref))
            elif c.action == "SUPERSEDE" or (c.action == "UPDATE" and c.new_version):
                new_id = store._insert(item, now, "onboarding", ref)
                for old in c.targets or [c.target]:
                    store.db.execute(
                        "UPDATE memories SET status = 'superseded', valid_until = ?, superseded_by = ?, "
                        "updated_at = ? WHERE id = ?",
                        (now, new_id, now, old["id"]),
                    )
            elif c.action == "UPDATE":
                ends = item["status"] not in CURRENT_STATUSES
                store.db.execute(
                    "UPDATE memories SET status = ?, importance = ?, subject = ?, domain = ?, slot_key = ?, "
                    "updated_at = ?, valid_until = ? WHERE id = ?",
                    (item["status"], item["importance"], item["subject"], item["domain"], item["slot_key"],
                     now, now if ends else None, c.target["id"]),
                )
    return backup


def _short(text: str, n: int = 70) -> str:
    text = _norm(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def report(changes: list[Change], store: MemoryStore, path: Path, applied: bool,
           backup: Path | None = None) -> list[str]:
    counts = store.counts_by_status()
    current = sum(v for k, v in counts.items() if k in CURRENT_STATUSES)
    lines = [
        "Jarvis memory onboarding — " + ("APPLIED" if applied else "DRY RUN (nothing has been written)"),
        f"Profile file: {path}  ({len(changes)} items)",
        f"Memory database: {store.path}  ({sum(counts.values())} memories: {current} current, "
        f"{sum(counts.values()) - current} history)",
        "",
    ]
    labels = {"SUPERSEDE": "SUPERSEDE/REPLACE"}
    for c in changes:
        i = c.item
        tag = f"{i['kind']} · {i['domain']} · {i['status'].upper()}"
        lines.append(f"  {labels.get(c.action, c.action):<17} {i['id']:<28} {tag}")
        lines.append(f"  {'':<17} \"{_short(i['content'])}\"")
        if c.action == "UPDATE":
            for d in c.details:
                lines.append(f"  {'':<17} {d}")
            if c.new_version:
                lines.append(f"  {'':<17} (the previous version #{c.target['id']} is kept as history)")
        elif c.action == "SUPERSEDE":
            for t in c.targets:
                lines.append(f"  {'':<17} replaces #{t['id']} ({t['kind']}, from {t.get('source') or 'conversation'}): "
                             f"\"{_short(t['content'])}\" → kept as history")
        elif c.action == "UNCHANGED" and c.details:
            lines.append(f"  {'':<17} already in memory as #{c.target['id']}")
        for o in c.overlaps:
            lines.append(f"  {'':<17} ! possible overlap with current #{o['id']} ({o['kind']}): \"{_short(o['content'])}\"")
            lines.append(f"  {'':<17}   if this item replaces it, add  replaces = [{o['id']}]  to the item")
    overlaps = sum(len(c.overlaps) for c in changes)
    summary = {a: sum(1 for c in changes if c.action == a) for a in ("ADD", "UNCHANGED", "UPDATE", "SUPERSEDE")}
    lines += ["", "Summary: " + " · ".join(f"{n} {labels.get(a, a)}" for a, n in summary.items())
              + (f" · {overlaps} possible overlap(s) to review" if overlaps else "")]
    in_file = {_ref(c.item) for c in changes}
    others = store.db.execute(
        "SELECT COUNT(DISTINCT source_ref) FROM memories WHERE source_ref LIKE 'onboarding:%' AND status != 'superseded' "
        f"AND source_ref NOT IN ({','.join('?' * len(in_file))})",
        list(in_file),
    ).fetchone()[0]
    if others:
        lines.append(f"Not in this file (kept untouched): {others} earlier onboarding item(s).")
    if applied:
        lines.append(f"Backup made before writing: {backup}" if backup else "Nothing needed writing.")
    elif any(c.action != "UNCHANGED" for c in changes):
        lines.append("Nothing has been written. To write these changes run the same command with --apply.")
    else:
        lines.append("Memory already matches the file — nothing to do.")
    return lines


def run(store: MemoryStore, path: Path, do_apply: bool) -> list[str]:
    items = load_profile(path)
    with store._lock:
        changes = plan(store, items)
        backup = apply(store, changes) if do_apply else None
    return report(changes, store, path, do_apply, backup)


def summary_json(changes: list[Change]) -> str:  # for tests/diagnostics
    return json.dumps([{"action": c.action, "id": c.item["id"]} for c in changes])
