"""Minimal stand-in for PyObjC's Foundation (tests only): just what calendar_helper uses."""

import datetime as _dt

NSUndefinedDateComponent = 9223372036854775807


class NSDate:
    def __init__(self, ts):
        self._ts = float(ts)

    @classmethod
    def dateWithTimeIntervalSince1970_(cls, ts):
        return cls(ts)

    @classmethod
    def dateWithTimeIntervalSinceNow_(cls, s):
        return cls(_dt.datetime.now().timestamp() + s)

    def timeIntervalSince1970(self):
        return self._ts

    def __repr__(self):
        return f"NSDate({_dt.datetime.fromtimestamp(self._ts):%Y-%m-%d %H:%M})"


class NSDateComponents:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self._v = {}
        return self

    def __getattr__(self, name):
        if name.startswith("set") and name.endswith("_"):
            key = name[3].lower() + name[4:-1]
            return lambda value: self._v.__setitem__(key, value)
        if name in ("year", "month", "day", "hour", "minute"):
            return lambda: self._v.get(name, NSUndefinedDateComponent)
        raise AttributeError(name)


class _Calendar:
    def dateFromComponents_(self, c):
        hour = c.hour() if c.hour() != NSUndefinedDateComponent else 0
        minute = c.minute() if c.minute() != NSUndefinedDateComponent else 0
        return NSDate(_dt.datetime(c.year(), c.month(), c.day(), hour, minute).timestamp())


class NSCalendar:
    @staticmethod
    def currentCalendar():
        return _Calendar()


class _RunLoop:
    def runUntilDate_(self, d):
        pass


class NSRunLoop:
    @staticmethod
    def currentRunLoop():
        return _RunLoop()
