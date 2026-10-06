"""Calendar / Reminders client used by Jarvis's tools (read-only in Phase 2A).

All access goes through `assistant.calendar_helper` in a separate process (see there
for why). JARVIS_CALENDAR_BACKEND = auto | eventkit | applescript.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timedelta

from .config import ROOT

HELPER_TIMEOUT_S = 90


class CalendarError(RuntimeError):
    pass


def _run_helper(*args: str, timeout: float = HELPER_TIMEOUT_S) -> dict:
    try:
        p = subprocess.run(
            [sys.executable, "-m", "assistant.calendar_helper", *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise CalendarError("the calendar did not answer in time") from e
    if p.returncode < 0:
        raise CalendarError(
            f"macOS stopped the calendar helper (signal {-p.returncode}) — usually a missing "
            "privacy permission"
        )
    try:
        data = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except (ValueError, IndexError):
        data = {}
    if p.returncode != 0 or "error" in data:
        raise CalendarError(data.get("error") or p.stderr.strip()[-300:] or "calendar helper failed")
    return data


class CalendarService:
    def __init__(self, backend: str = "auto") -> None:
        self.preference = backend if backend in ("auto", "eventkit", "applescript") else "auto"
        self._backend: str | None = None if self.preference == "auto" else self.preference

    def backend(self) -> str:
        if self._backend is None:
            try:
                status = _run_helper("status", timeout=20)
                ok = status.get("events") == "full_access"
            except CalendarError:
                ok = False
            self._backend = "eventkit" if ok else "applescript"
        return self._backend

    def _with_fallback(self, *args: str) -> dict:
        backend = self.backend()
        try:
            return _run_helper(*args, "--backend", backend)
        except CalendarError:
            if self.preference == "auto" and backend == "eventkit":
                self._backend = "applescript"
                return _run_helper(*args, "--backend", "applescript")
            raise

    # ------------------------------------------------------------------ reads
    def events(self, start: datetime, end: datetime) -> list[dict]:
        data = self._with_fallback(
            "events", "--start", start.isoformat(timespec="minutes"), "--end", end.isoformat(timespec="minutes")
        )
        return data.get("events", [])

    def reminders(self) -> list[dict]:
        return self._with_fallback("reminders").get("reminders", [])

    def free_slots(
        self, day: date, start_hour: int = 8, end_hour: int = 20, min_minutes: int = 30
    ) -> dict:
        window_start = datetime.combine(day, datetime.min.time()).replace(hour=max(0, min(23, start_hour)))
        window_end = datetime.combine(day, datetime.min.time()).replace(hour=max(1, min(23, end_hour)))
        if end_hour >= 24:
            window_end = datetime.combine(day + timedelta(days=1), datetime.min.time())
        events = self.events(window_start, window_end)
        return {
            "free": free_slots(events, window_start, window_end, min_minutes),
            "busy": [e for e in events if not e.get("all_day")],
            "all_day": [e["title"] for e in events if e.get("all_day")],
        }


def free_slots(events: list[dict], window_start: datetime, window_end: datetime, min_minutes: int = 30) -> list[dict]:
    """Gaps of at least `min_minutes` between timed events inside the window.
    All-day events don't block time."""
    busy = []
    for e in events:
        if e.get("all_day"):
            continue
        try:
            s, t = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
        except (KeyError, ValueError):
            continue
        s, t = max(s, window_start), min(t, window_end)
        if t > s:
            busy.append((s, t))
    busy.sort()
    merged: list[list[datetime]] = []
    for s, t in busy:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t)
        else:
            merged.append([s, t])
    free, cursor = [], window_start
    for s, t in merged:
        if (s - cursor).total_seconds() >= min_minutes * 60:
            free.append((cursor, s))
        cursor = max(cursor, t)
    if (window_end - cursor).total_seconds() >= min_minutes * 60:
        free.append((cursor, window_end))
    return [
        {"start": a.isoformat(timespec="minutes"), "end": b.isoformat(timespec="minutes"),
         "minutes": int((b - a).total_seconds() // 60)}
        for a, b in free
    ]


# ---------------------------------------------------------------------- --check-calendar
def check_calendar_report() -> tuple[bool, list[str]]:
    """Run the probe and turn it into plain-language lines. Returns (ok, lines)."""
    lines: list[str] = []
    try:
        report = _run_helper("probe", timeout=180)
    except CalendarError as e:
        return False, [f"The calendar check could not run: {e}"]
    ek = report.get("eventkit", {})
    asr = report.get("applescript", {})
    ek_ok = ek.get("events") == "full_access" and ek.get("reminders") == "full_access"
    if ek_ok:
        lines.append("✅ Direct access (EventKit): WORKING — Jarvis will use this (fast, includes repeating events).")
        lines.append(f"   Calendars found: {len(ek.get('calendars', []))} ({', '.join(ek.get('calendars', [])[:8])})")
        lines.append(f"   Reminder lists found: {len(ek.get('reminder_lists', []))} ({', '.join(ek.get('reminder_lists', [])[:8])})")
        lines.append(f"   Events today: {ek.get('events_today', 0)}")
    elif ek.get("available"):
        lines.append(
            f"⚠️  Direct access (EventKit): calendars = {ek.get('events')}, reminders = {ek.get('reminders')}."
        )
        if "denied" in (ek.get("events"), ek.get("reminders")):
            lines.append(
                "   To enable it: System Settings → Privacy & Security → Calendars (and Reminders) → "
                "turn on Terminal, then quit Terminal (Cmd+Q) and run the check again."
            )
    else:
        lines.append(f"⚠️  Direct access (EventKit) not available: {ek.get('error', 'unknown')}")
    if asr.get("available"):
        lines.append(
            f"✅ Calendar/Reminders apps (AppleScript): WORKING — {asr.get('calendars')} calendars, "
            f"{asr.get('reminder_lists')} reminder lists."
        )
    else:
        lines.append(f"⚠️  Calendar/Reminders apps (AppleScript): not available — {asr.get('error', 'unknown')}")
        if "-1743" in str(asr.get("error", "")):
            lines.append(
                "   To enable it: System Settings → Privacy & Security → Automation → Terminal → "
                "turn on Calendar and Reminders."
            )
    ok = ek_ok or bool(asr.get("available"))
    if ok and not ek_ok:
        lines.append("Jarvis will read your calendar through the Calendar app (works, but slower; repeating events may be incomplete).")
    if not ok:
        lines.append("Jarvis cannot read your calendar yet — follow the step above and run the check again.")
    return ok, lines
