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
from .memory import DOMAINS, KINDS, MemoryStore


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]
    mutates: bool = False
    summarize: Callable[..., str] | None = None  # how a write action is read back
    # For tools that only *sometimes* need confirmation (e.g. memory: replacing an active
    # goal does, saving an ordinary fact doesn't): returns the reason, or "" to run now.
    # The handler then receives confirmed=True when it runs after the user's yes.
    confirm_check: Callable[..., str] | None = None

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


@dataclass
class PendingAction:
    action_id: str
    tool: str
    args: dict
    summary: str
    created_turn: int
    confirmed_arg: bool = False  # pass confirmed=True to the handler


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
        # Only the registry may say an action was confirmed (never the model's arguments).
        args = {k: v for k, v in args.items() if k != "confirmed"}
        reason = ""
        if not tool.mutates and tool.confirm_check is not None:
            try:
                reason = tool.confirm_check(ctx=ctx, **args)
            except (TypeError, ValueError):
                reason = ""  # the handler will report the problem
        if tool.mutates or reason:
            self._next_id += 1
            if reason:
                summary = reason
            else:
                summary = tool.summarize(**args) if tool.summarize else f"{name} {json.dumps(args, ensure_ascii=False)}"
            self.pending = PendingAction(f"A{self._next_id}", name, args, summary, turn, confirmed_arg=bool(reason))
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
    def confirm(self, action_id: str, ctx: ToolContext, turn: int, user_text: str | None = None) -> dict:
        p = self.pending
        if p is None or p.action_id != action_id:
            return {"error": "there is no pending action with that id; propose the action again"}
        if turn != p.created_turn + 1:
            self.pending = None
            return {"error": "confirmation must come in the user's very next reply; propose it again"}
        if user_text is not None and not is_clear_yes(user_text):
            # Not done. Her reply was not an unambiguous yes: keep it pending one more turn.
            p.created_turn = turn
            return {"error": "not confirmed: the user's reply is not a clear yes, so nothing was done. "
                    "Ask her to answer yes or no."}
        self.pending = None
        ctx.log(f"Confirmed: {p.summary}")
        if p.confirmed_arg:
            return self.tools[p.tool].handler(ctx=ctx, confirmed=True, **p.args)
        return self.tools[p.tool].handler(ctx=ctx, **p.args)

    def cancel(self, ctx: ToolContext) -> dict:
        if self.pending:
            ctx.log(f"Cancelled: {self.pending.summary}")
        self.pending = None
        return {"status": "cancelled"}


# ---------------------------------------------------------------------- confirmation words
_YES_WORDS = {
    "si", "sì", "yes", "yeah", "yep", "ok", "okay", "confermo", "conferma", "confermato", "certo",
    "certamente", "esatto", "procedi", "vai", "fallo", "sure", "confirm", "confirmed", "correct",
    "giusto", "perfetto", "assolutamente", "daccordo", "absolutely",
}
_YES_PHRASES = ("va bene", "d'accordo", "d accordo", "go ahead", "do it", "of course", "sounds good")
_NO_WORDS = {
    "no", "non", "not", "nope", "don't", "dont", "annulla", "cancella", "cancel", "aspetta", "wait",
    "stop", "never", "mai", "niente", "nothing",
}


def is_clear_yes(text: str) -> bool:
    """True only for an unambiguous yes ("sì", "confermo", "yes, go ahead"). Anything with a
    negation or hesitation ("sì, ma non adesso", "wait") is not a yes. Enforced in code so a
    misheard or misread reply can never trigger an action."""
    t = (text or "").lower().replace("’", "'")
    words = "".join(c if (c.isalnum() or c == "'") else " " for c in t).split()
    if not words or any(w in _NO_WORDS for w in words):
        return False
    return any(w in _YES_WORDS for w in words) or any(p in t for p in _YES_PHRASES)


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


def _memory_args(kind: str, content: str, subject: str = "", importance: int = 3, domain: str = "general",
                 status: str = "active", slot_key: str = "", data=None, replaces_id: int | None = None,
                 additional: bool = False) -> dict:
    return dict(kind=kind, content=content, subject=subject, importance=importance, domain=domain,
                status=status, slot_key=slot_key, data=data, replaces_id=replaces_id, additional=additional)


def _memory_remember_check(ctx: ToolContext, **args) -> str:
    plan = ctx.memory.plan_remember(**_memory_args(**args))
    return plan.get("needs_confirmation") or ""


def _memory_remember(ctx: ToolContext, confirmed: bool = False, **args) -> dict:
    try:
        result = ctx.memory.remember(**_memory_args(**args), confirmed=confirmed)
    except ValueError as e:
        return {"status": "not_saved", "reason": str(e)}
    if result["status"] == "possible_conflict":
        return {
            **result,
            "instruction": "Nothing saved yet. These active memories look related. If the new one REPLACES "
            "one of them, call memory_remember again with replaces_id (she will be asked to confirm a "
            "goal/decision change). If it is genuinely an ADDITIONAL one, call again with additional=true. "
            "If unsure, ask her.",
        }
    content = args.get("content", "")
    if result.get("superseded"):
        ctx.log(f"Memory updated (#{result['superseded']} kept as history): {content[:50]}")
    elif result["status"] != "unchanged":
        ctx.log(f"Memory {result['status']}: {content[:60]}")
    return result


def _memory_recall(ctx: ToolContext, query: str = "", kind: str = "", limit: int = 8,
                   domain: str = "", include_history: bool = False) -> dict:
    items = ctx.memory.recall(query, kind if kind in KINDS else "", limit,
                              include_history=bool(include_history),
                              domain=domain if domain in DOMAINS else "")
    ctx.log(f"Memory: {len(items)} found" + (f" for '{query[:30]}'" if query else "")
            + (" (incl. history)" if include_history else ""))
    result = {"memories": items}
    if include_history:
        result["note"] = ("Items with status superseded/archived/completed are HISTORY: they are not "
                          "current and must not drive today's priorities.")
    return result


def _memory_set_status_check(ctx: ToolContext, memory_id: int, status: str) -> str:
    return ctx.memory.plan_status(int(memory_id), status).get("needs_confirmation") or ""


def _memory_set_status(ctx: ToolContext, memory_id: int, status: str, confirmed: bool = False) -> dict:
    try:
        result = ctx.memory.set_status(int(memory_id), status, confirmed=confirmed)
    except ValueError as e:
        return {"error": str(e)}
    if result["status"] not in ("unchanged", "needs_confirmation"):
        ctx.log(f"Memory #{memory_id} → {status}")
    return result


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
        "Save one durable, genuinely useful piece of information about her for the long term. "
        "Choose kind precisely: fact = objectively true; preference = her taste or way of working; "
        "goal = an outcome she is committed to; hypothesis = an idea she is considering or testing "
        "(NOT decided); decision = something she has explicitly decided; plus project, person, "
        "commitment, routine, followup, kpi. Never turn a hypothesis into a decision unless she clearly "
        "says she has decided. status: active (default), future (a later target, not a current "
        "priority) or paused. For single-valued things use slot_key so a new value replaces the old one "
        "(history is kept): business.revenue_target.current, business.revenue_target.future, "
        "business.offer, business.icp, business.niche, business.pricing, business.acquisition_channel, "
        "business.delivery_model, or a clear dotted key like personal.home_city or equestrian.horse.<name>. "
        "Changing an active goal or decision, or turning a hypothesis into a decision, returns "
        "needs_confirmation: read it back and ask. Never save small talk, passing remarks, moods, "
        "one-off chatter, things you only inferred, or what is already in the calendar. After saving, "
        "just say 'Annotato.' (or 'Noted.').",
        _obj({
            "kind": {"type": "string", "enum": list(KINDS)},
            "content": {"type": "string", "description": "the information, self-contained, in her language"},
            "subject": {"type": "string", "description": "who/what it is about, e.g. 'Giulia', 'revenue target'"},
            "domain": {"type": "string", "enum": list(DOMAINS)},
            "status": {"type": "string", "enum": ["active", "future", "paused"]},
            "slot_key": {"type": "string"},
            "data": {"type": "object", "description": "exact values, e.g. {\"amount\": 10000, \"currency\": \"EUR\", \"period\": \"month\"}"},
            "importance": {"type": "integer", "description": "2 (useful) to 5 (core goal/priority)"},
            "replaces_id": {"type": "integer", "description": "id of the memory this one replaces"},
            "additional": {"type": "boolean", "description": "true if it is genuinely in addition to similar active ones"},
        }, ["kind", "content"]),
        _memory_remember,
        confirm_check=_memory_remember_check,
    ))
    reg.register(Tool(
        "memory_recall",
        "Search long-term memory by keywords, kind and/or domain. By default returns only current "
        "memories (active, future, paused). Set include_history=true only when she asks about the past "
        "or history is clearly relevant (e.g. 'what was my old target?'); history never sets today's priorities.",
        _obj({
            "query": {"type": "string"}, "kind": {"type": "string"}, "domain": {"type": "string"},
            "include_history": {"type": "boolean"}, "limit": {"type": "integer"},
        }),
        _memory_recall,
    ))
    reg.register(Tool(
        "memory_set_status",
        "Change a memory's status (find its id with memory_recall): active, future, paused, completed "
        "(achieved/done) or archived (no longer relevant, kept as history). Ending an active goal or "
        "decision, or reactivating something archived, asks her to confirm.",
        _obj({
            "memory_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["active", "future", "paused", "completed", "archived"]},
        }, ["memory_id", "status"]),
        _memory_set_status,
        confirm_check=_memory_set_status_check,
    ))
    reg.register(Tool(
        "memory_forget",
        "Permanently delete one long-term memory by id (find the id with memory_recall first). Prefer "
        "memory_set_status archived unless she wants it erased. Requires her confirmation.",
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
