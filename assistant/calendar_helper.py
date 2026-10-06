"""macOS Calendar / Reminders access, run as a separate process.

    python -m assistant.calendar_helper probe
    python -m assistant.calendar_helper events --start 2026-10-06T00:00 --end 2026-10-07T00:00 [--backend eventkit|applescript]
    python -m assistant.calendar_helper reminders [--backend eventkit|applescript]
    python -m assistant.calendar_helper calendars
    echo '{...}' | python -m assistant.calendar_helper <write command>      (EventKit only)
        event-get, event-create, event-update, event-delete,
        reminder-create, reminder-update, reminder-delete

Write commands read one JSON object on stdin and print what EventKit actually saved
(re-read from the store), so Jarvis only reports what really happened. They refuse to
write into calendars that don't allow changes (subscribed, holidays, birthdays, read-only
shared calendars), and never change a repeating event without knowing which occurrence.

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
from datetime import datetime, timedelta

# EKCalendarType / EKSpan values
CALENDAR_TYPES = {0: "local", 1: "caldav", 2: "exchange", 3: "subscription", 4: "birthday"}
SPAN_THIS, SPAN_FUTURE = 0, 1

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
    out = [_event_dict(e) for e in store.eventsMatchingPredicate_(pred) or []]
    out.sort(key=lambda x: x["start"])
    return out


def _iso(nsdate) -> str:
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970()).isoformat(timespec="minutes")


def _nsdate(dt: datetime):
    _, Foundation = _ek()
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _calendar_writable(cal) -> bool:
    if cal is None:
        return False
    try:
        kind = int(cal.type())
    except Exception:  # noqa: BLE001
        kind = -1
    immutable = bool(cal.isImmutable()) if hasattr(cal, "isImmutable") else False
    subscribed = bool(cal.isSubscribed()) if hasattr(cal, "isSubscribed") else False
    return bool(cal.allowsContentModifications()) and not immutable and not subscribed and kind not in (3, 4)


def _event_dict(e) -> dict:
    cal = e.calendar()
    recurring = bool(e.hasRecurrenceRules()) if hasattr(e, "hasRecurrenceRules") else False
    occurrence = e.occurrenceDate() if hasattr(e, "occurrenceDate") else None
    return {
        "id": str(e.eventIdentifier() or ""),
        "external_id": str(e.calendarItemExternalIdentifier() or "") if hasattr(e, "calendarItemExternalIdentifier") else "",
        "calendar": str(cal.title()) if cal else "",
        "calendar_id": str(cal.calendarIdentifier()) if cal else "",
        "writable": _calendar_writable(cal),
        "title": str(e.title() or ""),
        "start": _iso(e.startDate()),
        "end": _iso(e.endDate()),
        "all_day": bool(e.isAllDay()),
        "location": str(e.location() or ""),
        "notes": str(e.notes() or "")[:300],
        "recurring": recurring,
        "occurrence_start": _iso(occurrence) if (recurring and occurrence is not None) else "",
    }


def _calendar_dict(cal, default_id: str) -> dict:
    source = cal.source()
    return {
        "id": str(cal.calendarIdentifier()),
        "title": str(cal.title()),
        "account": str(source.title()) if source is not None else "",
        "type": CALENDAR_TYPES.get(int(cal.type()), "other"),
        "writable": _calendar_writable(cal),
        "default": str(cal.calendarIdentifier()) == default_id,
    }


def ek_calendars() -> dict:
    """Event calendars and reminder lists, with whether Jarvis may write into each."""
    EventKit, _ = _ek()
    out: dict = {}
    store = ek_store("events")
    default = store.defaultCalendarForNewEvents()
    default_id = str(default.calendarIdentifier()) if default is not None else ""
    out["calendars"] = [_calendar_dict(c, default_id) for c in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)]
    try:
        rstore = ek_store("reminders")
        rdefault = rstore.defaultCalendarForNewReminders()
        rdefault_id = str(rdefault.calendarIdentifier()) if rdefault is not None else ""
        out["reminder_lists"] = [
            _calendar_dict(c, rdefault_id) for c in rstore.calendarsForEntityType_(EventKit.EKEntityTypeReminder)
        ]
    except PermissionError as e:
        out["reminder_lists"] = []
        out["reminders_error"] = str(e)
    return out


# ---------------------------------------------------------------------- EventKit writes
class WriteError(RuntimeError):
    pass


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value)[:16])
    except ValueError as e:
        raise WriteError(f"invalid date/time: {value!r}") from e


def _writable_calendar(store, calendar_id: str):
    cal = store.calendarWithIdentifier_(calendar_id) if calendar_id else None
    if cal is None:
        raise WriteError("calendar not found (it may have been removed)")
    if not _calendar_writable(cal):
        raise WriteError(f"the calendar '{cal.title()}' does not allow changes")
    return cal


def _find_event(store, event_id: str, occurrence_start: str = ""):
    """The exact event (and, for a repeating event, the exact occurrence)."""
    if occurrence_start:
        occ = _parse_dt(occurrence_start)
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            _nsdate(occ - timedelta(days=1)), _nsdate(occ + timedelta(days=1)), None
        )
        for e in store.eventsMatchingPredicate_(pred) or []:
            if str(e.eventIdentifier()) != event_id:
                continue
            occurrence = e.occurrenceDate() if hasattr(e, "occurrenceDate") else None
            for candidate in (occurrence, e.startDate()):
                if candidate is not None and abs(candidate.timeIntervalSince1970() - occ.timestamp()) < 60:
                    return e
        raise WriteError("that occurrence of the event was not found (it may have changed)")
    e = store.eventWithIdentifier_(event_id)
    if e is None:
        raise WriteError("event not found (it may have been deleted or moved)")
    if bool(e.hasRecurrenceRules()):
        raise WriteError("this is a repeating event: say which occurrence (occurrence_start) to change")
    return e


def _check(result, what: str) -> None:
    ok, error = result if isinstance(result, tuple) else (result, None)
    if not ok:
        raise WriteError(f"{what} failed: {error.localizedDescription() if error is not None else 'unknown error'}")


def _apply_event_fields(e, data: dict) -> None:
    if "title" in data:
        e.setTitle_(str(data["title"]))
    if "all_day" in data:
        e.setAllDay_(bool(data["all_day"]))
    if "start" in data:
        e.setStartDate_(_nsdate(_parse_dt(data["start"])))
    if "end" in data:
        e.setEndDate_(_nsdate(_parse_dt(data["end"])))
    if "location" in data:
        e.setLocation_(str(data["location"]) or None)
    if "notes" in data:
        e.setNotes_(str(data["notes"]) or None)
    if e.endDate().timeIntervalSince1970() < e.startDate().timeIntervalSince1970():
        raise WriteError("the end is before the start")


def ek_event_get(data: dict) -> dict:
    store = ek_store("events")
    return {"event": _event_dict(_find_event(store, str(data["id"]), str(data.get("occurrence_start") or "")))}


def ek_event_create(data: dict) -> dict:
    EventKit, _ = _ek()
    store = ek_store("events")
    cal = _writable_calendar(store, str(data.get("calendar_id") or ""))
    e = EventKit.EKEvent.eventWithEventStore_(store)
    e.setCalendar_(cal)
    _apply_event_fields(e, {k: data[k] for k in ("title", "all_day", "start", "end", "location", "notes") if k in data})
    _check(store.saveEvent_span_commit_error_(e, SPAN_THIS, True, None), "saving the event")
    saved = store.eventWithIdentifier_(e.eventIdentifier())
    return {"event": _event_dict(saved if saved is not None else e)}


def ek_event_update(data: dict) -> dict:
    store = ek_store("events")
    e = _find_event(store, str(data["id"]), str(data.get("occurrence_start") or ""))
    if not _calendar_writable(e.calendar()):
        raise WriteError(f"the calendar '{e.calendar().title()}' does not allow changes")
    before = _event_dict(e)
    span = SPAN_FUTURE if data.get("span") == "future" and bool(e.hasRecurrenceRules()) else SPAN_THIS
    _apply_event_fields(e, data.get("changes") or {})
    _check(store.saveEvent_span_commit_error_(e, span, True, None), "saving the change")
    return {"before": before, "event": _event_dict(e)}


def ek_event_delete(data: dict) -> dict:
    store = ek_store("events")
    e = _find_event(store, str(data["id"]), str(data.get("occurrence_start") or ""))
    if not _calendar_writable(e.calendar()):
        raise WriteError(f"the calendar '{e.calendar().title()}' does not allow changes")
    before = _event_dict(e)
    span = SPAN_FUTURE if data.get("span") == "future" and bool(e.hasRecurrenceRules()) else SPAN_THIS
    _check(store.removeEvent_span_commit_error_(e, span, True, None), "deleting the event")
    return {"deleted": before, "span": "future" if span == SPAN_FUTURE else "this"}


def _due_components(due: str):
    _, Foundation = _ek()
    comps = Foundation.NSDateComponents.alloc().init()
    if len(due) >= 16:
        dt = _parse_dt(due)
        comps.setYear_(dt.year); comps.setMonth_(dt.month); comps.setDay_(dt.day)
        comps.setHour_(dt.hour); comps.setMinute_(dt.minute)
        return comps, dt
    try:
        d = datetime.fromisoformat(due[:10])
    except ValueError as e:
        raise WriteError(f"invalid due date: {due!r}") from e
    comps.setYear_(d.year); comps.setMonth_(d.month); comps.setDay_(d.day)
    return comps, None


def _reminder_dict(r) -> dict:
    _, Foundation = _ek()
    due = ""
    comps = r.dueDateComponents()
    if comps is not None:
        d = Foundation.NSCalendar.currentCalendar().dateFromComponents_(comps)
        if d is not None:
            dt = datetime.fromtimestamp(d.timeIntervalSince1970())
            due = dt.isoformat(timespec="minutes") if comps.hour() not in (NS_UNDEFINED, None) else dt.date().isoformat()
    cal = r.calendar()
    return {
        "id": str(r.calendarItemIdentifier()),
        "list": str(cal.title()) if cal else "",
        "list_id": str(cal.calendarIdentifier()) if cal else "",
        "title": str(r.title() or ""),
        "due": due,
        "completed": bool(r.isCompleted()),
        "priority": int(r.priority() or 0),
        "notes": str(r.notes() or "")[:200],
    }


def _set_due(r, due: str | None) -> None:
    EventKit, _ = _ek()
    for alarm in list(r.alarms() or []):
        r.removeAlarm_(alarm)
    if not due:
        r.setDueDateComponents_(None)
        return
    comps, at = _due_components(due)
    r.setDueDateComponents_(comps)
    if at is not None:  # a time was given: notify at that time
        r.addAlarm_(EventKit.EKAlarm.alarmWithAbsoluteDate_(_nsdate(at)))


def _find_reminder(store, reminder_id: str):
    r = store.calendarItemWithIdentifier_(reminder_id)
    if r is None:
        raise WriteError("reminder not found (it may have been deleted)")
    return r


def ek_reminder_create(data: dict) -> dict:
    EventKit, _ = _ek()
    store = ek_store("reminders")
    cal = _writable_calendar(store, str(data.get("list_id") or ""))
    r = EventKit.EKReminder.reminderWithEventStore_(store)
    r.setCalendar_(cal)
    r.setTitle_(str(data["title"]))
    if data.get("notes"):
        r.setNotes_(str(data["notes"]))
    _set_due(r, data.get("due") or None)
    _check(store.saveReminder_commit_error_(r, True, None), "saving the reminder")
    return {"reminder": _reminder_dict(r)}


def ek_reminder_update(data: dict) -> dict:
    store = ek_store("reminders")
    r = _find_reminder(store, str(data["id"]))
    before = _reminder_dict(r)
    changes = data.get("changes") or {}
    if "title" in changes:
        r.setTitle_(str(changes["title"]))
    if "notes" in changes:
        r.setNotes_(str(changes["notes"]) or None)
    if "due" in changes:
        _set_due(r, changes["due"] or None)
    if "completed" in changes:
        r.setCompleted_(bool(changes["completed"]))
    _check(store.saveReminder_commit_error_(r, True, None), "saving the reminder")
    return {"before": before, "reminder": _reminder_dict(r)}


def ek_reminder_delete(data: dict) -> dict:
    store = ek_store("reminders")
    r = _find_reminder(store, str(data["id"]))
    before = _reminder_dict(r)
    _check(store.removeReminder_commit_error_(r, True, None), "deleting the reminder")
    return {"deleted": before}


WRITE_COMMANDS = {
    "event-get": ek_event_get,
    "event-create": ek_event_create,
    "event-update": ek_event_update,
    "event-delete": ek_event_delete,
    "reminder-create": ek_reminder_create,
    "reminder-update": ek_reminder_update,
    "reminder-delete": ek_reminder_delete,
}


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
    out = [_reminder_dict(r) for r in box.get("items", [])]
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
    ap.add_argument("command", choices=["probe", "events", "reminders", "status", "calendars", *WRITE_COMMANDS])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--backend", choices=["eventkit", "applescript"], default="eventkit")
    args = ap.parse_args(argv)
    try:
        if args.command in WRITE_COMMANDS:
            result = WRITE_COMMANDS[args.command](json.loads(sys.stdin.read() or "{}"))
        elif args.command == "calendars":
            result = ek_calendars()
        elif args.command == "probe":
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
