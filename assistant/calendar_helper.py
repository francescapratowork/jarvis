"""macOS Calendar / Reminders access, run as a separate process.

    python -m assistant.calendar_helper probe
    python -m assistant.calendar_helper events --start 2026-10-06T00:00 --end 2026-10-07T00:00 [--backend eventkit|applescript]
    python -m assistant.calendar_helper reminders [--backend eventkit|applescript]

Prints one JSON object on stdout. It runs in its own process because macOS privacy
protection may terminate a process that touches calendar data without permission —
that must never take Jarvis down with it.

Two backends:
  eventkit     Apple's EventKit framework (fast, handles repeating events). Needs the
               "Calendars"/"Reminders" privacy permission for the app running Jarvis.
  applescript  Asks the Calendar and Reminders apps via AppleScript (slower, needs the
               "Automation" permission). Repeating events may only show their first date.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime

# EKAuthorizationStatus values
STATUS_NAMES = {0: "not_determined", 1: "restricted", 2: "denied", 3: "full_access", 4: "write_only"}
NS_UNDEFINED = 9223372036854775807  # NSDateComponentUndefined


# ---------------------------------------------------------------------- EventKit
def _ek():
    import EventKit  # pyobjc-framework-EventKit
    import Foundation

    return EventKit, Foundation


def _spin_until(done: threading.Event, timeout: float) -> None:
    """Completion handlers may need a running run loop on this thread."""
    _, Foundation = _ek()
    end = time.time() + timeout
    while not done.is_set() and time.time() < end:
        Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
            Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.1)
        )


def ek_status(kind: str) -> str:
    EventKit, _ = _ek()
    entity = EventKit.EKEntityTypeEvent if kind == "events" else EventKit.EKEntityTypeReminder
    return STATUS_NAMES.get(int(EventKit.EKEventStore.authorizationStatusForEntityType_(entity)), "unknown")


def ek_request(store, kind: str, timeout: float = 120.0) -> dict:
    EventKit, _ = _ek()
    done = threading.Event()
    result: dict = {}

    def handler(granted, error):
        result["granted"] = bool(granted)
        result["error"] = str(error) if error else None
        done.set()

    if kind == "events" and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    elif kind == "reminders" and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
        store.requestFullAccessToRemindersWithCompletion_(handler)
    else:
        entity = EventKit.EKEntityTypeEvent if kind == "events" else EventKit.EKEntityTypeReminder
        store.requestAccessToEntityType_completion_(entity, handler)
    _spin_until(done, timeout)
    if not done.is_set():
        result = {"granted": False, "error": "no answer to the permission request"}
    return result


def ek_store(kind: str, request: bool = True):
    EventKit, _ = _ek()
    store = EventKit.EKEventStore.alloc().init()
    status = ek_status(kind)
    if status == "not_determined" and request:
        ek_request(store, kind)
        store = EventKit.EKEventStore.alloc().init()  # fresh store after granting
        status = ek_status(kind)
    if status != "full_access":
        raise PermissionError(f"EventKit {kind} access: {status}")
    return store


def ek_events(start: datetime, end: datetime) -> list[dict]:
    EventKit, Foundation = _ek()
    store = ek_store("events")
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(
        Foundation.NSDate.dateWithTimeIntervalSince1970_(start.timestamp()),
        Foundation.NSDate.dateWithTimeIntervalSince1970_(end.timestamp()),
        None,
    )
    out = []
    for e in store.eventsMatchingPredicate_(pred) or []:
        out.append(
            {
                "calendar": str(e.calendar().title()) if e.calendar() else "",
                "title": str(e.title() or ""),
                "start": datetime.fromtimestamp(e.startDate().timeIntervalSince1970()).isoformat(timespec="minutes"),
                "end": datetime.fromtimestamp(e.endDate().timeIntervalSince1970()).isoformat(timespec="minutes"),
                "all_day": bool(e.isAllDay()),
                "location": str(e.location() or ""),
            }
        )
    out.sort(key=lambda x: x["start"])
    return out


def ek_reminders() -> list[dict]:
    _, Foundation = _ek()
    store = ek_store("reminders")
    pred = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, None)
    done = threading.Event()
    box: dict = {}

    def handler(reminders):
        box["items"] = list(reminders or [])
        done.set()

    store.fetchRemindersMatchingPredicate_completion_(pred, handler)
    _spin_until(done, 30)
    cal = Foundation.NSCalendar.currentCalendar()
    out = []
    for r in box.get("items", []):
        due = ""
        comps = r.dueDateComponents()
        if comps is not None:
            d = cal.dateFromComponents_(comps)
            if d is not None:
                dt = datetime.fromtimestamp(d.timeIntervalSince1970())
                has_time = comps.hour() not in (NS_UNDEFINED, None)
                due = dt.isoformat(timespec="minutes") if has_time else dt.date().isoformat()
        out.append(
            {
                "list": str(r.calendar().title()) if r.calendar() else "",
                "title": str(r.title() or ""),
                "due": due,
                "priority": int(r.priority() or 0),
                "notes": str(r.notes() or "")[:200],
            }
        )
    out.sort(key=lambda x: (x["due"] == "", x["due"]))
    return out


# ---------------------------------------------------------------------- AppleScript
# Note: AppleScript reserves short words such as st/nd/rd/th — keep names descriptive.
_AS_HELPERS = """
on makeDate(yearValue, monthValue, dayValue, hourValue, minuteValue)
  set resultDate to current date
  set day of resultDate to 1
  set year of resultDate to yearValue
  set month of resultDate to monthValue
  set day of resultDate to dayValue
  set time of resultDate to (hourValue * 3600 + minuteValue * 60)
  return resultDate
end makeDate

on twoDigits(numberValue)
  return text -2 thru -1 of ("0" & (numberValue as text))
end twoDigits

on isoText(dateValue)
  set secondsOfDay to time of dateValue
  return ((year of dateValue) as text) & "-" & my twoDigits((month of dateValue) as integer) & "-" & my twoDigits(day of dateValue) & "T" & my twoDigits(secondsOfDay div 3600) & ":" & my twoDigits((secondsOfDay mod 3600) div 60)
end isoText
"""


def _osascript(script: str, timeout: float) -> str:
    p = subprocess.run(
        ["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "osascript failed")
    return p.stdout


def _as_date(name: str, dt: datetime) -> str:
    return f"set {name} to my makeDate({dt.year}, {dt.month}, {dt.day}, {dt.hour}, {dt.minute})"


def as_events(start: datetime, end: datetime) -> list[dict]:
    script = _AS_HELPERS + f"""
{_as_date("rangeStart", start)}
{_as_date("rangeEnd", end)}
set outText to ""
tell application "Calendar"
  repeat with calendarItem in calendars
    set calendarName to name of calendarItem
    try
      set eventList to (every event of calendarItem whose start date < rangeEnd and end date > rangeStart)
      repeat with eventItem in eventList
        set outText to outText & calendarName & tab & (summary of eventItem) & tab & my isoText(start date of eventItem) & tab & my isoText(end date of eventItem) & tab & ((allday event of eventItem) as text) & linefeed
      end repeat
    end try
  end repeat
end tell
return outText
"""
    out = []
    for line in _osascript(script, timeout=60).splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        out.append(
            {
                "calendar": parts[0],
                "title": parts[1],
                "start": parts[2][:16],
                "end": parts[3][:16],
                "all_day": parts[4].strip() == "true",
                "location": "",
            }
        )
    out.sort(key=lambda x: x["start"])
    return out


def as_reminders() -> list[dict]:
    script = _AS_HELPERS + """
set outText to ""
tell application "Reminders"
  repeat with listItem in lists
    set listName to name of listItem
    set reminderList to (reminders of listItem whose completed is false)
    repeat with reminderItem in reminderList
      set dueText to ""
      try
        set dueValue to due date of reminderItem
        if dueValue is not missing value then set dueText to my isoText(dueValue)
      end try
      set outText to outText & listName & tab & (name of reminderItem) & tab & dueText & linefeed
    end repeat
  end repeat
end tell
return outText
"""
    out = []
    for line in _osascript(script, timeout=60).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        out.append({"list": parts[0], "title": parts[1], "due": parts[2][:16], "priority": 0, "notes": ""})
    out.sort(key=lambda x: (x["due"] == "", x["due"]))
    return out


def as_probe() -> dict:
    script = """
tell application "Calendar" to set calendarCount to count of calendars
tell application "Reminders" to set listCount to count of lists
return (calendarCount as text) & tab & (listCount as text)
"""
    calendars, lists = _osascript(script, timeout=60).strip().split("\t")
    return {"calendars": int(calendars), "reminder_lists": int(lists)}


# ---------------------------------------------------------------------- commands
def probe() -> dict:
    """Check both backends (may show macOS permission prompts)."""
    report: dict = {}
    try:
        EventKit, _ = _ek()
        ek: dict = {"available": True}
        for kind in ("events", "reminders"):
            try:
                store = ek_store(kind)
                ek[kind] = "full_access"
                if kind == "events":
                    entity = EventKit.EKEntityTypeEvent
                    ek["calendars"] = [str(c.title()) for c in store.calendarsForEntityType_(entity)]
                else:
                    entity = EventKit.EKEntityTypeReminder
                    ek["reminder_lists"] = [str(c.title()) for c in store.calendarsForEntityType_(entity)]
            except PermissionError:
                ek[kind] = ek_status(kind)
        if ek.get("events") == "full_access":
            now = datetime.now()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            ek["events_today"] = len(ek_events(day_start, day_start.replace(hour=23, minute=59)))
        report["eventkit"] = ek
    except ImportError:
        report["eventkit"] = {"available": False, "error": "pyobjc-framework-EventKit not installed"}
    except Exception as e:  # noqa: BLE001
        report["eventkit"] = {"available": False, "error": f"{type(e).__name__}: {e}"}
    if sys.platform == "darwin":
        try:
            report["applescript"] = {"available": True, **as_probe()}
        except Exception as e:  # noqa: BLE001
            report["applescript"] = {"available": False, "error": str(e)[:300]}
    else:
        report["applescript"] = {"available": False, "error": "not macOS"}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["probe", "events", "reminders", "status"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--backend", choices=["eventkit", "applescript"], default="eventkit")
    args = ap.parse_args(argv)
    try:
        if args.command == "probe":
            result = probe()
        elif args.command == "status":
            result = {"events": ek_status("events"), "reminders": ek_status("reminders")}
        elif args.command == "events":
            start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
            fn = ek_events if args.backend == "eventkit" else as_events
            result = {"events": fn(start, end), "backend": args.backend}
        else:
            fn = ek_reminders if args.backend == "eventkit" else as_reminders
            result = {"reminders": fn(), "backend": args.backend}
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"[:500]}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
