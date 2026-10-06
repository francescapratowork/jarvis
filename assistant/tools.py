"""Tool registry: every capability Jarvis has is one tool.

A tool = name + description + input schema + handler + `mutates` flag.
  mutates=False (read):  runs immediately.
  mutates=True  (write): never runs directly. It becomes a *pending action* that Jarvis
                         must read back to the user; it only runs if the user's very next
                         reply confirms it (enforced here in code, not left to the model).

Adding a capability later = registering one more tool; the conversation engine
(brain.py) does not change.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .calendar import CalendarError, CalendarService
from .memory import KINDS, MemoryStore


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]
    mutates: bool = False
    summarize: Callable[..., str] | None = None  # how a write action is read back

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


@dataclass
class PendingAction:
    action_id: str
    tool: str
    args: dict
    summary: str
    created_turn: int


@dataclass
class ToolContext:
    memory: MemoryStore
    calendar: CalendarService
    open_app: Callable[[str], str]
    log: Callable[[str], None] = lambda text: None
    end_conversation: Callable[[], None] = lambda: None
    state: dict = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self.pending: PendingAction | None = None
        self._next_id = 0

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def definitions(self) -> list[dict]:
        # Deterministic order keeps the prompt prefix stable (prompt caching).
        return [self.tools[n].definition() for n in sorted(self.tools)]

    def new_turn(self, turn: int) -> None:
        """A pending action is only confirmable in the user turn right after it was proposed."""
        if self.pending and turn > self.pending.created_turn + 1:
            self.pending = None

    def execute(self, name: str, args: dict, ctx: ToolContext, turn: int) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name}"}
        if not isinstance(args, dict):
            return {"error": "invalid arguments"}
        if tool.mutates:
            self._next_id += 1
            summary = tool.summarize(**args) if tool.summarize else f"{name} {json.dumps(args, ensure_ascii=False)}"
            self.pending = PendingAction(f"A{self._next_id}", name, args, summary, turn)
            ctx.log(f"Confirm? {summary}")
            return {
                "status": "needs_confirmation",
                "action_id": self.pending.action_id,
                "summary": summary,
                "instruction": "Nothing has been done yet. Read this back to the user in one short "
                "sentence and ask them to confirm. Call confirm_action only if their next reply "
                "clearly says yes.",
            }
        return tool.handler(ctx=ctx, **args)

    # The two built-in tools that resolve a pending write action.
    def confirm(self, action_id: str, ctx: ToolContext, turn: int) -> dict:
        p = self.pending
        if p is None or p.action_id != action_id:
            return {"error": "there is no pending action with that id; propose the action again"}
        if turn != p.created_turn + 1:
            self.pending = None
            return {"error": "confirmation must come in the user's very next reply; propose it again"}
        self.pending = None
        ctx.log(f"Confirmed: {p.summary}")
        return self.tools[p.tool].handler(ctx=ctx, **p.args)

    def cancel(self, ctx: ToolContext) -> dict:
        if self.pending:
            ctx.log(f"Cancelled: {self.pending.summary}")
        self.pending = None
        return {"status": "cancelled"}


# ---------------------------------------------------------------------- helpers
def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def _parse_day(value: str | None) -> date:
    v = (value or "").strip().lower()
    today = date.today()
    if v in ("", "today", "oggi"):
        return today
    if v in ("tomorrow", "domani"):
        return today + timedelta(days=1)
    return date.fromisoformat(v[:10])


def _calendar_error(e: Exception) -> dict:
    return {"error": f"calendar unavailable: {e}"}


# ---------------------------------------------------------------------- tool handlers
def _calendar_list_events(ctx: ToolContext, start_date: str, end_date: str | None = None) -> dict:
    try:
        d0 = _parse_day(start_date)
        d1 = _parse_day(end_date) if end_date else d0
        if d1 < d0:
            d0, d1 = d1, d0
        if (d1 - d0).days > 31:
            return {"error": "range too long; ask for at most 31 days"}
        start = datetime.combine(d0, datetime.min.time())
        end = datetime.combine(d1 + timedelta(days=1), datetime.min.time())
        events = ctx.calendar.events(start, end)
    except (CalendarError, ValueError) as e:
        return _calendar_error(e)
    ctx.log(f"Calendar: {len(events)} events {d0.isoformat()}" + (f"–{d1.isoformat()}" if d1 != d0 else ""))
    return {"from": d0.isoformat(), "to": d1.isoformat(), "events": events}


def _calendar_find_free_time(
    ctx: ToolContext, date: str, start_hour: int = 8, end_hour: int = 20, min_minutes: int = 30
) -> dict:
    try:
        day = _parse_day(date)
        result = ctx.calendar.free_slots(day, int(start_hour), int(end_hour), int(min_minutes))
    except (CalendarError, ValueError) as e:
        return _calendar_error(e)
    ctx.log(f"Calendar: free time {day.isoformat()} ({len(result['free'])} slots)")
    return {"date": day.isoformat(), **result}


def _reminders_list(ctx: ToolContext, due_before: str | None = None) -> dict:
    try:
        items = ctx.calendar.reminders()
    except CalendarError as e:
        return _calendar_error(e)
    if due_before:
        try:
            limit = _parse_day(due_before).isoformat()
            items = [r for r in items if r["due"] and r["due"][:10] <= limit]
        except ValueError:
            pass
    ctx.log(f"Reminders: {len(items)} open")
    return {"reminders": items[:60], "total": len(items)}


def _memory_remember(ctx: ToolContext, kind: str, content: str, subject: str = "", importance: int = 3) -> dict:
    try:
        result = ctx.memory.remember(kind, content, subject, importance)
    except ValueError as e:
        return {"error": str(e)}
    ctx.log(f"Memory {result['status']}: {content[:60]}")
    return result


def _memory_recall(ctx: ToolContext, query: str = "", kind: str = "", limit: int = 8) -> dict:
    items = ctx.memory.recall(query, kind if kind in KINDS else "", limit)
    ctx.log(f"Memory: {len(items)} found" + (f" for '{query[:30]}'" if query else ""))
    return {"memories": items}


def _memory_forget(ctx: ToolContext, memory_id: int) -> dict:
    ok = ctx.memory.forget(int(memory_id))
    return {"status": "deleted" if ok else "not_found", "memory_id": memory_id}


def _memory_forget_summary(memory_id: int, **_) -> str:
    return f"delete memory #{memory_id}"


def _open_app(ctx: ToolContext, app: str) -> dict:
    return {"result": ctx.open_app(app)}


def _end_conversation(ctx: ToolContext) -> dict:
    ctx.end_conversation()
    ctx.log("Conversation ended")
    return {"status": "ended"}


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        "calendar_list_events",
        "List the user's calendar events (all calendars on this Mac) for a day or a range of days. "
        "Dates are YYYY-MM-DD, or 'today'/'tomorrow'. Read-only.",
        _obj({"start_date": {"type": "string"}, "end_date": {"type": "string", "description": "inclusive; omit for a single day"}}, ["start_date"]),
        _calendar_list_events,
    ))
    reg.register(Tool(
        "calendar_find_free_time",
        "Find free time slots on one day between start_hour and end_hour (24h clock), at least "
        "min_minutes long. All-day events don't block time. Read-only.",
        _obj({
            "date": {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'"},
            "start_hour": {"type": "integer"}, "end_hour": {"type": "integer"}, "min_minutes": {"type": "integer"},
        }, ["date"]),
        _calendar_find_free_time,
    ))
    reg.register(Tool(
        "reminders_list",
        "List the user's open (not completed) reminders from all reminder lists, with due dates. "
        "Optionally only those due on or before a date. Read-only.",
        _obj({"due_before": {"type": "string", "description": "YYYY-MM-DD, 'today' or 'tomorrow'"}}),
        _reminders_list,
    ))
    reg.register(Tool(
        "memory_remember",
        "Save one genuinely useful long-term fact about the user (a goal, project, person, "
        "commitment, preference, routine, business context, decision or follow-up). Never save "
        "small talk, one-off chatter or things already in the calendar. After saving, just say "
        "'Annotato.' (or 'Noted.' in English).",
        _obj({
            "kind": {"type": "string", "enum": list(KINDS)},
            "subject": {"type": "string", "description": "who/what it is about, e.g. 'Giulia', 'revenue target'"},
            "content": {"type": "string", "description": "the fact, self-contained, in the user's language"},
            "importance": {"type": "integer", "description": "1 (minor) to 5 (core goal/priority)"},
        }, ["kind", "content"]),
        _memory_remember,
    ))
    reg.register(Tool(
        "memory_recall",
        "Search long-term memory (goals, projects, people, decisions, follow-ups…) by keywords "
        "and/or kind. Use it before answering questions about the user's life, people, plans or business.",
        _obj({"query": {"type": "string"}, "kind": {"type": "string"}, "limit": {"type": "integer"}}),
        _memory_recall,
    ))
    reg.register(Tool(
        "memory_forget",
        "Delete one long-term memory by id (find the id with memory_recall first). Requires the "
        "user's confirmation.",
        _obj({"memory_id": {"type": "integer"}}, ["memory_id"]),
        _memory_forget,
        mutates=True,
        summarize=_memory_forget_summary,
    ))
    reg.register(Tool(
        "open_app",
        "Open (or bring to the front) a Mac application by name, e.g. 'Claude', 'Calendar', "
        "'Spotify', 'Google Chrome'. Only when the user asks for it.",
        _obj({"app": {"type": "string"}}, ["app"]),
        _open_app,
    ))
    reg.register(Tool(
        "end_conversation",
        "End the voice conversation when the user says goodbye or is done (e.g. 'grazie, basta così'). "
        "Say a brief goodbye in the same reply.",
        _obj({}),
        _end_conversation,
    ))
    reg.register(Tool(
        "confirm_action",
        "Execute the pending action the user has just explicitly confirmed (yes / sì / confermo). "
        "Only in the reply right after you asked.",
        _obj({"action_id": {"type": "string"}}, ["action_id"]),
        lambda ctx, action_id: {"error": "handled by registry"},
    ))
    reg.register(Tool(
        "cancel_action",
        "Cancel the pending action (the user said no, or changed the subject).",
        _obj({}),
        lambda ctx: {"error": "handled by registry"},
    ))
    return reg


# ---------------------------------------------------------------------- open apps
APP_ALIASES = {
    "claude": "Claude", "calendar": "Calendar", "calendario": "Calendar", "spotify": "Spotify",
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "cursor": "Cursor",
    "reminders": "Reminders", "promemoria": "Reminders", "notes": "Notes", "note": "Notes",
    "mail": "Mail", "safari": "Safari", "finder": "Finder", "messages": "Messages",
    "messaggi": "Messages", "whatsapp": "WhatsApp", "slack": "Slack", "notion": "Notion",
}


def open_mac_app(name: str) -> str:
    raw = " ".join((name or "").split())
    app = APP_ALIASES.get(raw.lower(), raw)
    if not app or any(c in app for c in "/;&|`$"):
        return "invalid app name"
    found = any(Path(base, f"{app}.app").exists() for base in ("/Applications", "/System/Applications", str(Path.home() / "Applications")))
    try:
        p = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"could not open {app}: {e}"
    if p.returncode != 0:
        return f"{app} is not installed" if not found else f"could not open {app}"
    return f"opened {app}"
