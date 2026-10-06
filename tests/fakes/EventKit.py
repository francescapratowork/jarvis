"""Minimal stand-in for PyObjC's EventKit (tests only).

Mirrors the selectors calendar_helper uses (method names with trailing underscores, error
out-parameters returned as (ok, error) tuples). Repeating events are expanded into
occurrences that share one eventIdentifier; saving an occurrence with EKSpanThisEvent
detaches it, EKSpanFutureEvents changes the series from that occurrence on.
State lives in module-level STATE so tests can inspect it; reset() starts fresh.
"""

import datetime as _dt
import itertools

from Foundation import NSDate

EKEntityTypeEvent, EKEntityTypeReminder = 0, 1
_ids = itertools.count(1)


class _Error:
    def __init__(self, text):
        self.text = text

    def localizedDescription(self):
        return self.text


class _Source:
    def __init__(self, title):
        self._t = title

    def title(self):
        return self._t


class EKCalendar:
    def __init__(self, title, account, kind=1, writable=True, subscribed=False, entity=EKEntityTypeEvent):
        self._id = f"CAL-{next(_ids)}"
        self._title, self._source, self._type = title, _Source(account), kind
        self._writable, self._subscribed, self.entity = writable, subscribed, entity

    def calendarIdentifier(self): return self._id
    def title(self): return self._title
    def source(self): return self._source
    def type(self): return self._type
    def allowsContentModifications(self): return self._writable
    def isImmutable(self): return not self._writable
    def isSubscribed(self): return self._subscribed


class _Item:
    def __init__(self, store):
        self._store, self._cal, self._title, self._notes = store, None, "", None

    def calendar(self): return self._cal
    def setCalendar_(self, c): self._cal = c
    def title(self): return self._title
    def setTitle_(self, t): self._title = t
    def notes(self): return self._notes
    def setNotes_(self, n): self._notes = n


class EKEvent(_Item):
    @classmethod
    def eventWithEventStore_(cls, store):
        return cls(store)

    def __init__(self, store):
        super().__init__(store)
        self._id, self._start, self._end, self._all_day, self._loc = None, None, None, False, None
        self._rule = None          # weekly repetition count (series master only)
        self._occ = None           # occurrence date (for occurrences of a series)
        self._series = None        # the master event of an occurrence

    def eventIdentifier(self): return self._id
    def calendarItemExternalIdentifier(self): return f"EXT-{self._id}"
    def startDate(self): return self._start
    def setStartDate_(self, d): self._start = d
    def endDate(self): return self._end
    def setEndDate_(self, d): self._end = d
    def isAllDay(self): return self._all_day
    def setAllDay_(self, v): self._all_day = bool(v)
    def location(self): return self._loc
    def setLocation_(self, v): self._loc = v
    def hasRecurrenceRules(self): return bool(self._rule or self._series)
    def occurrenceDate(self): return self._occ if self._occ is not None else self._start


class EKAlarm:
    @classmethod
    def alarmWithAbsoluteDate_(cls, d):
        a = cls()
        a.date = d
        return a


class EKReminder(_Item):
    @classmethod
    def reminderWithEventStore_(cls, store):
        return cls(store)

    def __init__(self, store):
        super().__init__(store)
        self._id, self._due, self._done, self._alarms, self._prio = None, None, False, [], 0

    def calendarItemIdentifier(self): return self._id
    def dueDateComponents(self): return self._due
    def setDueDateComponents_(self, c): self._due = c
    def isCompleted(self): return self._done
    def setCompleted_(self, v): self._done = bool(v)
    def priority(self): return self._prio
    def alarms(self): return list(self._alarms)
    def addAlarm_(self, a): self._alarms.append(a)
    def removeAlarm_(self, a): self._alarms.remove(a)


class _State:
    def __init__(self):
        self.calendars, self.events, self.reminders = [], {}, {}
        self.overrides = {}  # (series id, occurrence ts) -> detached occurrence
        self.fail_next_save = None


STATE = _State()


def reset():
    global STATE
    STATE = _State()
    return STATE


class EKEventStore:
    @classmethod
    def authorizationStatusForEntityType_(cls, entity):
        return 3  # full access

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    # calendars
    def calendarsForEntityType_(self, entity):
        return [c for c in STATE.calendars if c.entity == entity]

    def calendarWithIdentifier_(self, cid):
        return next((c for c in STATE.calendars if c.calendarIdentifier() == cid), None)

    def defaultCalendarForNewEvents(self):
        return next((c for c in STATE.calendars if c.entity == EKEntityTypeEvent and getattr(c, "default", False)), None)

    def defaultCalendarForNewReminders(self):
        return next((c for c in STATE.calendars if c.entity == EKEntityTypeReminder and getattr(c, "default", False)), None)

    # events
    def predicateForEventsWithStartDate_endDate_calendars_(self, s, e, cals):
        return (s.timeIntervalSince1970(), e.timeIntervalSince1970())

    def _occurrences(self, master):
        step = 7 * 86400
        for n in range(master._rule):
            occ_ts = master._start.timeIntervalSince1970() + n * step
            key = (master._id, occ_ts)
            if key in STATE.overrides:
                if STATE.overrides[key] is not None:
                    yield STATE.overrides[key]
                continue
            o = EKEvent(self)
            o._id, o._cal, o._title, o._notes, o._loc = master._id, master._cal, master._title, master._notes, master._loc
            o._start = NSDate(occ_ts)
            o._end = NSDate(occ_ts + master._end.timeIntervalSince1970() - master._start.timeIntervalSince1970())
            o._occ, o._series, o._all_day = NSDate(occ_ts), master, master._all_day
            yield o

    def eventsMatchingPredicate_(self, pred):
        s, e = pred
        out = []
        for ev in STATE.events.values():
            for item in (self._occurrences(ev) if ev._rule else [ev]):
                if item._start.timeIntervalSince1970() < e and item._end.timeIntervalSince1970() > s:
                    out.append(item)
        return out

    def eventWithIdentifier_(self, eid):
        ev = STATE.events.get(eid)
        if ev is not None and ev._rule:
            return next(self._occurrences(ev))  # like EventKit: the first occurrence
        return ev

    def calendarItemWithIdentifier_(self, iid):
        return STATE.reminders.get(iid)

    def _fail(self):
        if STATE.fail_next_save:
            text, STATE.fail_next_save = STATE.fail_next_save, None
            return (False, _Error(text))
        return None

    def saveEvent_span_commit_error_(self, ev, span, commit, err):
        failure = self._fail()
        if failure:
            return failure
        if not ev._cal.allowsContentModifications():
            return (False, _Error("calendar is read-only"))
        if ev._series is not None:  # an occurrence
            master = ev._series
            if span == 0:
                STATE.overrides[(master._id, ev._occ.timeIntervalSince1970())] = ev
            else:  # this and following: shift the whole series by the same amount (simplified)
                delta = ev._start.timeIntervalSince1970() - ev._occ.timeIntervalSince1970()
                length = ev._end.timeIntervalSince1970() - ev._start.timeIntervalSince1970()
                master._start = NSDate(master._start.timeIntervalSince1970() + delta)
                master._end = NSDate(master._start.timeIntervalSince1970() + length)
                master._title = ev._title
            return (True, None)
        if ev._id is None:
            ev._id = f"EV-{next(_ids)}"
        STATE.events[ev._id] = ev
        return (True, None)

    def removeEvent_span_commit_error_(self, ev, span, commit, err):
        failure = self._fail()
        if failure:
            return failure
        if ev._series is not None:
            master = ev._series
            if span == 0:
                STATE.overrides[(master._id, ev._occ.timeIntervalSince1970())] = None
            else:
                keep = int((ev._occ.timeIntervalSince1970() - master._start.timeIntervalSince1970()) // (7 * 86400))
                master._rule = keep
            return (True, None)
        STATE.events.pop(ev._id, None)
        return (True, None)

    # reminders
    def predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(self, s, e, cals):
        return "incomplete"

    def fetchRemindersMatchingPredicate_completion_(self, pred, handler):
        handler([r for r in STATE.reminders.values() if not r.isCompleted()])

    def saveReminder_commit_error_(self, r, commit, err):
        failure = self._fail()
        if failure:
            return failure
        if r._id is None:
            r._id = f"REM-{next(_ids)}"
        STATE.reminders[r._id] = r
        return (True, None)

    def removeReminder_commit_error_(self, r, commit, err):
        STATE.reminders.pop(r._id, None)
        return (True, None)


# ---------------------------------------------------------------------- test helpers
def add_calendar(title, account="iCloud", **kw):
    default = kw.pop("default", False)
    c = EKCalendar(title, account, **kw)
    c.default = default
    STATE.calendars.append(c)
    return c


def add_event(cal, title, start, end, weekly=0):
    ev = EKEvent(None)
    ev._id, ev._cal, ev._title = f"EV-{next(_ids)}", cal, title
    ev._start, ev._end = NSDate(start.timestamp()), NSDate(end.timestamp())
    ev._rule = weekly or None
    STATE.events[ev._id] = ev
    return ev
