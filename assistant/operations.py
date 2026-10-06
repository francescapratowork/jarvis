"""Operational Calendar + Reminders (Phase 2B · M2).

Every change to the outside world goes through three steps:

  1. plan_*     (when Claude calls a write tool) — resolve and validate everything in code:
                which calendar (routing), which exact event/occurrence, the exact times,
                conflicts. The result is a fixed *plan* plus a one-line summary. Nothing is
                written; the plan becomes the pending action the user must confirm.
  2. confirm    (tools.ToolRegistry) — only the user's very next reply, only a clear yes.
  3. execute    run exactly the stored plan through EventKit and return what EventKit
                actually saved. Each step is recorded in the action log, together with
                how to reverse it ("Annulla l'ultima cosa" — itself confirmed).

Calendar routing: each life area (business, personal, equestrian, growth, general) can be
mapped to one writable calendar. Without a mapping (for the area or for general) Jarvis
asks which calendar to use and remembers the answer (calendar_set_route). Subscribed,
holiday, birthday and read-only calendars are never written to.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any

from .calendar import CalendarError

AREAS = ("business", "personal", "equestrian", "growth", "general")
MAX_BATCH = 12
MAX_EVENT_HOURS = 24
UNDOABLE_TOOLS = (
    "calendar_create_event", "calendar_update_event", "calendar_delete_event", "calendar_create_events_batch",
    "reminders_create", "reminders_update", "reminders_complete",
)

OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    summary TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,            -- proposed | executed | failed | cancelled | expired | replaced
    result TEXT NOT NULL DEFAULT '',
    undo TEXT NOT NULL DEFAULT '',   -- JSON plan that reverses an executed action ('' = not reversible)
    undone_by INTEGER,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_log_status ON action_log(status);
"""


class OpsStore:
    """Settings (calendar routes) and the action log, in the same local SQLite database as
    the memory (separate tables; memories are never touched)."""

    def __init__(self, memory) -> None:
        self.memory = memory
        self.db = memory.db
        with memory._lock:
            self.db.executescript(OPS_SCHEMA)

    # ------------------------------------------------------------------ settings
    def get(self, key: str):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value) -> None:
        with self.memory._tx():
            self.db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def route(self, kind: str, area: str) -> dict | None:
        return self.get(f"{kind}.route.{area}")

    def set_route(self, kind: str, area: str, target: dict) -> None:
        self.set(f"{kind}.route.{area}", {"id": target["id"], "title": target["title"], "account": target.get("account", "")})

    def routes(self, kind: str) -> dict:
        return {a: r for a in AREAS if (r := self.route(kind, a))}

    # ------------------------------------------------------------------ action log
    def log_proposed(self, tool: str, summary: str, plan: dict) -> int:
        now = time.time()
        with self.memory._tx():
            cur = self.db.execute(
                "INSERT INTO action_log (ts, tool, summary, plan, status, updated_at) VALUES (?, ?, ?, ?, 'proposed', ?)",
                (now, tool, summary, json.dumps(plan, ensure_ascii=False, default=str), now),
            )
        return int(cur.lastrowid)

    def log_status(self, log_id: int, status: str, result: dict | None = None, undo: dict | None = None) -> None:
        with self.memory._tx():
            self.db.execute(
                "UPDATE action_log SET status = ?, result = CASE WHEN ? != '' THEN ? ELSE result END, "
                "undo = CASE WHEN ? != '' THEN ? ELSE undo END, updated_at = ? WHERE id = ?",
                (status,
                 *(2 * [json.dumps(result, ensure_ascii=False, default=str) if result is not None else ""]),
                 *(2 * [json.dumps(undo, ensure_ascii=False, default=str) if undo else ""]),
                 time.time(), log_id),
            )

    def mark_undone(self, log_id: int, by_log_id: int) -> None:
        with self.memory._tx():
            self.db.execute("UPDATE action_log SET undone_by = ? WHERE id = ?", (by_log_id, log_id))

    def last_undoable(self) -> dict | None:
        """The most recent executed calendar/reminder change, if it can be reversed and has
        not been reversed yet. Only the very last change is offered (no reaching back past a
        later change that would be left inconsistent)."""
        row = self.db.execute(
            f"SELECT * FROM action_log WHERE status = 'executed' AND tool IN ({','.join('?' * len(UNDOABLE_TOOLS))}) "
            "ORDER BY id DESC LIMIT 1",
            UNDOABLE_TOOLS,
        ).fetchone()
        if row is None or row["undone_by"] is not None or not row["undo"]:
            return None
        return dict(row)

    def recent(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------------- helpers
class PlanError(ValueError):
    pass


def _dt(value: str, field: str) -> datetime:
    v = (value or "").strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(v[:16])
    except ValueError as e:
        raise PlanError(f"{field} must be a local date and time like 2026-10-07T08:00 (got {value!r})") from e


def _day(value: str, field: str) -> date:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError as e:
        raise PlanError(f"{field} must be a date like 2026-10-07 (got {value!r})") from e


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def when_text(start: str, end: str, all_day: bool = False) -> str:
    s, e = datetime.fromisoformat(start[:16]), datetime.fromisoformat(end[:16])
    day = f"{DAYS[s.weekday()]} {s.day:02d}/{s.month:02d}"
    if all_day:
        return f"{day} (all day)"
    if s.date() == e.date():
        return f"{day} {s:%H:%M}–{e:%H:%M}"
    return f"{day} {s:%H:%M} – {DAYS[e.weekday()]} {e.day:02d}/{e.month:02d} {e:%H:%M}"


def _label(c: dict) -> str:
    return f"{c['title']} ({c['account']})" if c.get("account") else c["title"]


def _area(area: str) -> str:
    return area if area in AREAS else "general"


def _times(start: str, end: str = "", duration_minutes: int = 0, all_day: bool = False) -> tuple[str, str]:
    if all_day:
        d0 = _day(start, "start")
        d1 = _day(end, "end") if end else d0
        if d1 < d0:
            raise PlanError("the end date is before the start date")
        return f"{d0.isoformat()}T00:00", f"{d1.isoformat()}T23:59"
    s = _dt(start, "start")
    if end:
        e = _dt(end, "end")
    else:
        e = s + timedelta(minutes=int(duration_minutes or 60))
    if e <= s:
        raise PlanError("the end must be after the start")
    if (e - s) > timedelta(hours=MAX_EVENT_HOURS):
        raise PlanError(f"an event can last at most {MAX_EVENT_HOURS} hours (use all_day for whole days)")
    return _iso(s), _iso(e)


# ---------------------------------------------------------------------- routing
def writable_calendars(ctx) -> tuple[list[dict], list[dict]]:
    cals = ctx.calendar.calendars()
    return [c for c in cals.get("calendars", []) if c.get("writable")], cals.get("calendars", [])


def _match(name: str, candidates: list[dict]) -> list[dict]:
    n = " ".join(name.lower().split())
    exact = [c for c in candidates if _label(c).lower() == n or c["title"].lower() == n]
    return exact or [c for c in candidates if n in _label(c).lower()]


def resolve_calendar(ctx, area: str, calendar: str = "", writable: list | None = None,
                     all_cals: list | None = None) -> dict:
    """Which calendar to write into. Returns the calendar dict, or a dict with "status":
    "needs_calendar_choice" / "error" that is returned to Claude unchanged."""
    if writable is None:
        writable, all_cals = writable_calendars(ctx)
    if calendar:
        found = _match(calendar, writable)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return {"status": "error", "error": f"more than one calendar is called '{calendar}'",
                    "choose_one_of": [_label(c) for c in found]}
        if _match(calendar, all_cals or []):
            return {"status": "error", "error": f"the calendar '{calendar}' is read-only (subscribed, holidays or shared without editing)",
                    "writable_calendars": [_label(c) for c in writable]}
        return {"status": "error", "error": f"no writable calendar called '{calendar}'",
                "writable_calendars": [_label(c) for c in writable]}
    area = _area(area)
    for key in dict.fromkeys((area, "general")):
        route = ctx.actions.route("calendar", key)
        if route:
            match = [c for c in writable if c["id"] == route["id"]]
            if match:
                return match[0]
    return {
        "status": "needs_calendar_choice",
        "area": area,
        "writable_calendars": [_label(c) for c in writable],
        "instruction": f"Nothing has been created. No calendar is set for '{area}' events yet. Ask her which "
        "of these calendars to use (and whether it should be the default for this area or for everything). "
        "Then call calendar_set_route with her choice and propose the event again. Never pick one yourself.",
    }


def resolve_reminder_list(ctx, area: str, list_name: str = "") -> dict:
    lists = [c for c in ctx.calendar.calendars().get("reminder_lists", []) if c.get("writable")]
    if list_name:
        found = _match(list_name, lists)
        if len(found) == 1:
            return found[0]
        return {"status": "error", "error": f"no single writable reminder list called '{list_name}'",
                "reminder_lists": [_label(c) for c in lists]}
    for key in dict.fromkeys((_area(area), "general")):
        route = ctx.actions.route("reminders", key)
        if route:
            match = [c for c in lists if c["id"] == route["id"]]
            if match:
                return match[0]
    default = [c for c in lists if c.get("default")]
    if default:  # the default list she chose in the Reminders app
        return default[0]
    return {"status": "needs_list_choice", "reminder_lists": [_label(c) for c in lists],
            "instruction": "Nothing has been created. Ask her which reminder list to use, then call "
            "calendar_set_route with kind='reminders' and propose it again."}


def conflicts(ctx, start: str, end: str, ignore_id: str = "", ignore_occurrence: str = "") -> list[dict]:
    """Timed events overlapping [start, end) (all-day events don't block time)."""
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    try:
        events = ctx.calendar.events(s, e)
    except CalendarError:
        return []
    out = []
    for ev in events:
        if ev.get("all_day"):
            continue
        if ignore_id and ev.get("id") == ignore_id and (not ignore_occurrence or ev.get("occurrence_start", "") in ("", ignore_occurrence)):
            continue
        es, ee = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
        if es < e and ee > s:
            out.append({"title": ev["title"], "when": when_text(ev["start"], ev["end"]), "calendar": ev.get("calendar", "")})
    return out


def _conflict_text(found: list[dict]) -> str:
    return "" if not found else " — overlaps with " + "; ".join(f"\"{c['title']}\" {c['when']}" for c in found)


# ---------------------------------------------------------------------- planners
def _event_plan(ctx, item: dict, writable, all_cals) -> dict:
    title = " ".join(str(item.get("title", "")).split())
    if not title:
        raise PlanError("the event needs a title")
    all_day = bool(item.get("all_day"))
    start, end = _times(str(item.get("start", "")), str(item.get("end", "") or ""),
                        int(item.get("duration_minutes") or 0), all_day)
    cal = resolve_calendar(ctx, str(item.get("area") or "general"), str(item.get("calendar") or ""), writable, all_cals)
    if "status" in cal:
        return cal
    plan = {"op": "event_create", "calendar_id": cal["id"], "calendar": _label(cal), "title": title,
            "start": start, "end": end, "all_day": all_day,
            "location": str(item.get("location") or ""), "notes": str(item.get("notes") or "")}
    plan["conflicts"] = [] if all_day else conflicts(ctx, start, end)
    plan["past"] = datetime.fromisoformat(start) < datetime.now() - timedelta(minutes=5)
    return plan


def _event_line(p: dict) -> str:
    extra = " (in the past!)" if p.get("past") else ""
    return f"\"{p['title']}\" {when_text(p['start'], p['end'], p['all_day'])} in calendar {p['calendar']}{extra}{_conflict_text(p['conflicts'])}"


def plan_create_event(ctx, title: str, start: str, end: str = "", duration_minutes: int = 0, area: str = "general",
                      calendar: str = "", all_day: bool = False, location: str = "", notes: str = "") -> dict:
    writable, all_cals = writable_calendars(ctx)
    p = _event_plan(ctx, dict(title=title, start=start, end=end, duration_minutes=duration_minutes, area=area,
                              calendar=calendar, all_day=all_day, location=location, notes=notes), writable, all_cals)
    if "status" in p:
        return p
    return {"summary": "add to the calendar: " + _event_line(p), "plan": p,
            "details": {"conflicts": p["conflicts"], "calendar": p["calendar"]}}


def plan_create_events_batch(ctx, events: list) -> dict:
    if not isinstance(events, list) or not events:
        raise PlanError("events must be a non-empty list")
    if len(events) > MAX_BATCH:
        raise PlanError(f"at most {MAX_BATCH} events at once")
    writable, all_cals = writable_calendars(ctx)
    plans = []
    for n, item in enumerate(events, 1):
        if not isinstance(item, dict):
            raise PlanError(f"event {n} is not an object")
        try:
            p = _event_plan(ctx, item, writable, all_cals)
        except PlanError as e:
            raise PlanError(f"event {n}: {e}") from e
        if "status" in p:
            return p
        plans.append(p)
    # Overlaps inside the plan itself.
    for i, a in enumerate(plans):
        for b in plans[i + 1:]:
            if not (a["all_day"] or b["all_day"]) and a["start"] < b["end"] and b["start"] < a["end"]:
                a["conflicts"].append({"title": b["title"], "when": when_text(b["start"], b["end"]), "calendar": b["calendar"]})
    lines = [f"{n}) {_event_line(p)}" for n, p in enumerate(plans, 1)]
    return {"summary": f"add {len(plans)} events to the calendar: " + " | ".join(lines),
            "plan": {"op": "event_batch", "items": plans},
            "details": {"events": [{"title": p["title"], "when": when_text(p["start"], p["end"], p["all_day"]),
                                    "calendar": p["calendar"], "conflicts": p["conflicts"]} for p in plans]}}


def _span(event: dict, apply_to: str) -> str:
    if not event.get("recurring"):
        return "this"
    return "future" if apply_to == "future" else "this"


def _span_text(event: dict, span: str) -> str:
    if not event.get("recurring"):
        return ""
    return " (repeating event: this AND all following occurrences)" if span == "future" else " (repeating event: only this occurrence)"


def plan_update_event(ctx, event_id: str, occurrence_start: str = "", title: str | None = None,
                      start: str | None = None, end: str | None = None, location: str | None = None,
                      notes: str | None = None, apply_to: str = "this") -> dict:
    ev = ctx.calendar.get_event(event_id, occurrence_start or "")
    if not ev.get("writable"):
        return {"status": "error", "error": f"the calendar '{ev.get('calendar')}' does not allow changes"}
    changes: dict[str, Any] = {}
    if title is not None and " ".join(title.split()) and " ".join(title.split()) != ev["title"]:
        changes["title"] = " ".join(title.split())
    if (start or end) and not ev.get("all_day"):
        old_s, old_e = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
        new_s = _dt(start, "start") if start else old_s
        new_e = _dt(end, "end") if end else new_s + (old_e - old_s)  # moving keeps the duration
        new_start, new_end = _times(_iso(new_s), _iso(new_e))
        if new_start != ev["start"]:
            changes["start"] = new_start
        if new_end != ev["end"]:
            changes["end"] = new_end
    elif start or end:
        return {"status": "error", "error": "changing the dates of an all-day event is not supported yet"}
    if location is not None and location != ev["location"]:
        changes["location"] = location
    if notes is not None and notes != ev["notes"]:
        changes["notes"] = notes
    if not changes:
        return {"status": "error", "error": "nothing to change (the event already looks like that)"}
    span = _span(ev, apply_to)
    new_start, new_end = changes.get("start", ev["start"]), changes.get("end", ev["end"])
    found = conflicts(ctx, new_start, new_end, ev["id"], ev.get("occurrence_start", "")) if ("start" in changes or "end" in changes) else []
    what = []
    if "title" in changes:
        what.append(f"rename to \"{changes['title']}\"")
    if "start" in changes or "end" in changes:
        what.append(f"move to {when_text(new_start, new_end)}")
    if "location" in changes:
        what.append("change the location")
    if "notes" in changes:
        what.append("change the notes")
    summary = (f"change \"{ev['title']}\" ({when_text(ev['start'], ev['end'], ev['all_day'])}, {ev['calendar']}): "
               + ", ".join(what) + _span_text(ev, span) + _conflict_text(found))
    plan = {"op": "event_update", "id": ev["id"], "occurrence_start": ev.get("occurrence_start", ""),
            "changes": changes, "span": span, "before": ev}
    return {"summary": summary, "plan": plan, "details": {"conflicts": found}}


def plan_delete_event(ctx, event_id: str, occurrence_start: str = "", apply_to: str = "this") -> dict:
    ev = ctx.calendar.get_event(event_id, occurrence_start or "")
    if not ev.get("writable"):
        return {"status": "error", "error": f"the calendar '{ev.get('calendar')}' does not allow changes"}
    span = _span(ev, apply_to)
    summary = f"DELETE \"{ev['title']}\" {when_text(ev['start'], ev['end'], ev['all_day'])} from {ev['calendar']}" + _span_text(ev, span)
    return {"summary": summary, "plan": {"op": "event_delete", "id": ev["id"],
                                         "occurrence_start": ev.get("occurrence_start", ""), "span": span, "before": ev}}


def _due(due: str) -> str:
    due = (due or "").strip().replace(" ", "T")
    if not due:
        return ""
    if len(due) >= 16:
        return _iso(_dt(due, "due"))
    return _day(due, "due").isoformat()


def _due_text(due: str) -> str:
    if not due:
        return "no due date"
    if len(due) >= 16:
        d = datetime.fromisoformat(due)
        return f"due {DAYS[d.weekday()]} {d.day:02d}/{d.month:02d} at {d:%H:%M} (with an alert)"
    d = date.fromisoformat(due)
    return f"due {DAYS[d.weekday()]} {d.day:02d}/{d.month:02d}"


def plan_create_reminder(ctx, title: str, due: str = "", list: str = "", area: str = "general", notes: str = "") -> dict:  # noqa: A002
    title = " ".join(str(title or "").split())
    if not title:
        raise PlanError("the reminder needs a title")
    due = _due(due)
    target = resolve_reminder_list(ctx, area, list)
    if "status" in target:
        return target
    plan = {"op": "reminder_create", "list_id": target["id"], "list": _label(target), "title": title,
            "due": due, "notes": str(notes or "")}
    return {"summary": f"create the reminder \"{title}\", {_due_text(due)}, in the list {_label(target)}", "plan": plan}


def _find_reminder(ctx, reminder_id: str) -> dict:
    for r in ctx.calendar.reminders():
        if r.get("id") == reminder_id:
            return r
    raise PlanError("reminder not found among the open reminders (use reminders_list for its id)")


def plan_complete_reminder(ctx, reminder_id: str) -> dict:
    r = _find_reminder(ctx, reminder_id)
    return {"summary": f"mark the reminder \"{r['title']}\" as done",
            "plan": {"op": "reminder_update", "id": reminder_id, "changes": {"completed": True}, "before": r}}


def plan_update_reminder(ctx, reminder_id: str, title: str | None = None, due: str | None = None,
                         notes: str | None = None) -> dict:
    r = _find_reminder(ctx, reminder_id)
    changes: dict[str, Any] = {}
    if title is not None and " ".join(title.split()) and " ".join(title.split()) != r["title"]:
        changes["title"] = " ".join(title.split())
    if due is not None and _due(due) != r.get("due", ""):
        changes["due"] = _due(due)
    if notes is not None and notes != r.get("notes", ""):
        changes["notes"] = notes
    if not changes:
        return {"status": "error", "error": "nothing to change"}
    parts = []
    if "title" in changes:
        parts.append(f"rename to \"{changes['title']}\"")
    if "due" in changes:
        parts.append(_due_text(changes["due"]))
    if "notes" in changes:
        parts.append("change the notes")
    return {"summary": f"change the reminder \"{r['title']}\": " + ", ".join(parts),
            "plan": {"op": "reminder_update", "id": reminder_id, "changes": changes, "before": r}}


def plan_undo_last(ctx) -> dict:
    row = ctx.actions.last_undoable()
    if row is None:
        last = ctx.actions.db.execute(
            "SELECT summary, undo, undone_by FROM action_log WHERE status = 'executed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is None:
            return {"status": "error", "error": "there is no executed change to undo"}
        if last["undone_by"] is not None:
            return {"status": "error", "error": f"the last change (\"{last['summary']}\") has already been undone"}
        return {"status": "error", "error": f"the last change (\"{last['summary']}\") cannot be undone automatically"}
    undo = json.loads(row["undo"])
    return {"summary": f"UNDO the last change ({row['summary']}) — " + undo.get("describe", ""),
            "plan": {"op": "undo", "action_id": row["id"], "undo": undo}}


# ---------------------------------------------------------------------- executors
def _event_brief(ev: dict) -> dict:
    return {"title": ev["title"], "when": when_text(ev["start"], ev["end"], ev.get("all_day", False)),
            "start": ev["start"], "end": ev["end"], "calendar": ev.get("calendar", ""), "id": ev.get("id", "")}


def execute(ctx, plan: dict) -> dict:
    """Run a confirmed plan. Returns {"status": "done", ...} with what EventKit actually
    saved (plus "_undo", consumed by the registry), or {"status": "failed", "error": ...}."""
    try:
        return _execute(ctx, plan)
    except (CalendarError, PlanError) as e:
        return {"status": "failed", "error": str(e)}


def _execute(ctx, plan: dict) -> dict:
    op = plan["op"]
    if op == "event_create":
        ev = ctx.calendar.create_event(plan["calendar_id"], plan["title"], plan["start"], plan["end"],
                                       plan["all_day"], plan.get("location", ""), plan.get("notes", ""))
        return {"status": "done", "created": _event_brief(ev),
                "_undo": {"op": "event_delete", "id": ev["id"], "occurrence_start": "", "span": "this",
                          "describe": f"delete \"{ev['title']}\" {when_text(ev['start'], ev['end'], ev.get('all_day', False))}"}}
    if op == "event_batch":
        created, failed = [], []
        for p in plan["items"]:
            try:
                ev = ctx.calendar.create_event(p["calendar_id"], p["title"], p["start"], p["end"], p["all_day"],
                                               p.get("location", ""), p.get("notes", ""))
                created.append(_event_brief(ev))
            except CalendarError as e:
                failed.append({"title": p["title"], "when": when_text(p["start"], p["end"], p["all_day"]), "error": str(e)})
        result = {"status": "done" if not failed else ("partial" if created else "failed"),
                  "created": created, "failed": failed}
        if created:
            result["_undo"] = {"op": "batch_delete", "ids": [c["id"] for c in created],
                               "describe": "delete " + ", ".join(f"\"{c['title']}\" {c['when']}" for c in created)}
        return result
    if op == "event_update":
        res = ctx.calendar.update_event(plan["id"], plan.get("occurrence_start", ""), plan["changes"], plan["span"])
        before, after = res["before"], res["event"]
        restore = {k: before[k] for k in plan["changes"]}
        return {"status": "done", "event": _event_brief(after), "was": _event_brief(before),
                "_undo": {"op": "event_update", "id": after["id"], "occurrence_start": after.get("occurrence_start", "")
                          or plan.get("occurrence_start", ""), "changes": restore, "span": plan["span"],
                          "describe": f"put \"{before['title']}\" back to {when_text(before['start'], before['end'], before.get('all_day', False))}"}}
    if op == "event_delete":
        res = ctx.calendar.delete_event(plan["id"], plan.get("occurrence_start", ""), plan["span"])
        before = res["deleted"]
        result = {"status": "done", "deleted": _event_brief(before), "span": res.get("span", plan["span"])}
        if not before.get("recurring"):
            result["_undo"] = {"op": "event_create", "calendar_id": before["calendar_id"], "calendar": before["calendar"],
                               "title": before["title"], "start": before["start"], "end": before["end"],
                               "all_day": before["all_day"], "location": before.get("location", ""),
                               "notes": before.get("notes", ""),
                               "describe": f"put \"{before['title']}\" back in the calendar ({when_text(before['start'], before['end'], before['all_day'])})"}
        else:
            result["note"] = "deleted occurrences of a repeating event cannot be restored automatically"
        return result
    if op == "reminder_create":
        r = ctx.calendar.create_reminder(plan["list_id"], plan["title"], plan.get("due", ""), plan.get("notes", ""))
        return {"status": "done", "reminder": {k: r.get(k) for k in ("title", "due", "list", "id")},
                "_undo": {"op": "reminder_delete", "id": r["id"], "describe": f"delete the reminder \"{r['title']}\""}}
    if op == "reminder_update":
        res = ctx.calendar.update_reminder(plan["id"], plan["changes"])
        before, after = res["before"], res["reminder"]
        restore = {k: before.get(k, "" if k != "completed" else False) for k in plan["changes"]}
        return {"status": "done", "reminder": {k: after.get(k) for k in ("title", "due", "list", "completed", "id")},
                "_undo": {"op": "reminder_update", "id": after["id"], "changes": restore,
                          "describe": f"restore the reminder \"{before['title']}\""}}
    if op == "reminder_delete":
        res = ctx.calendar.delete_reminder(plan["id"])
        return {"status": "done", "deleted": {k: res["deleted"].get(k) for k in ("title", "due", "list")}}
    if op == "batch_delete":
        deleted, failed = [], []
        for event_id in plan["ids"]:
            try:
                deleted.append(_event_brief(ctx.calendar.delete_event(event_id, "", "this")["deleted"]))
            except CalendarError as e:
                failed.append({"id": event_id, "error": str(e)})
        return {"status": "done" if not failed else ("partial" if deleted else "failed"), "deleted": deleted, "failed": failed}
    if op == "undo":
        original = int(plan["action_id"])
        result = _execute(ctx, plan["undo"])
        result.pop("_undo", None)  # an undo is not itself undone automatically
        if result.get("status") in ("done", "partial"):
            ctx.actions.mark_undone(original, int(ctx.state.get("action_log_id") or 0))
        result["undid"] = plan["undo"].get("describe", "")
        return result
    raise PlanError(f"unknown operation {op}")
