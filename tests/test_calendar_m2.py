"""Phase 2B · M2 tests: operational Calendar + Reminders.

The real calendar_helper code runs against a fake EventKit (tests/fakes), through its own
command-line entry point (JSON in, JSON out), exactly as the subprocess does on the Mac.

Run:  .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
FAKES = Path(__file__).resolve().parent / "fakes"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(FAKES))

import EventKit as FakeEK  # noqa: E402  (the fake)

from assistant import calendar as calmod  # noqa: E402
from assistant import calendar_helper as helper  # noqa: E402
from assistant.calendar import CalendarError, CalendarService  # noqa: E402
from assistant.memory import MemoryStore  # noqa: E402
from assistant.operations import OpsStore  # noqa: E402
from assistant.tools import ToolContext, build_registry  # noqa: E402

TOMORROW = date.today() + timedelta(days=1)


def at(hour: int, minute: int = 0, day: date = TOMORROW) -> datetime:
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def run_helper_in_process(*args, timeout=None, payload=None):
    """Same contract as calendar._run_helper, but runs calendar_helper.main() in-process."""
    out = io.StringIO()
    stdin = io.StringIO(json.dumps(payload) if payload is not None else "")
    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        with contextlib.redirect_stdout(out):
            code = helper.main(list(args))
    finally:
        sys.stdin = old_stdin
    data = json.loads(out.getvalue().strip().splitlines()[-1])
    if code != 0 or "error" in data:
        raise CalendarError(data.get("error") or "calendar helper failed")
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self.state = FakeEK.reset()
        self.icloud = FakeEK.add_calendar("Personale", "iCloud", default=True)
        self.work = FakeEK.add_calendar("Lavoro", "iCloud")
        self.google = FakeEK.add_calendar("Calendar", "francesca@gmail.com")
        self.google2 = FakeEK.add_calendar("Calendar", "work@company.it")
        self.holidays = FakeEK.add_calendar("Festività italiane", "Subscribed", kind=3, writable=False, subscribed=True)
        self.birthdays = FakeEK.add_calendar("Compleanni", "Other", kind=4, writable=False)
        self.rlist = FakeEK.add_calendar("Promemoria", "iCloud", entity=FakeEK.EKEntityTypeReminder, default=True)
        self.rlist2 = FakeEK.add_calendar("Spesa", "iCloud", entity=FakeEK.EKEntityTypeReminder)
        self._orig = calmod._run_helper
        calmod._run_helper = run_helper_in_process
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(Path(self.tmp.name) / "jarvis_memory.db")
        self.actions = OpsStore(self.memory)
        self.cal = CalendarService("eventkit")
        self.logs = []
        self.ctx = ToolContext(memory=self.memory, calendar=self.cal, open_app=lambda a: "", log=self.logs.append,
                               actions=self.actions)
        self.reg = build_registry()
        self.turn = 0

    def tearDown(self):
        calmod._run_helper = self._orig
        self.tmp.cleanup()

    # helpers ------------------------------------------------------------
    def call(self, name, **args):
        self.turn += 1
        return self.reg.execute(name, args, self.ctx, self.turn)

    def yes(self, response, reply="Sì."):
        self.turn += 1
        return self.reg.confirm(response["action_id"], self.ctx, self.turn, user_text=reply)

    def events(self, day=TOMORROW):
        return self.cal.events(at(0, day=day), at(23, 59, day=day))

    def log_rows(self):
        return self.actions.recent(50)


# ---------------------------------------------------------------------- the helper itself
class HelperTest(Base):
    def test_calendars_report_writability(self):
        data = self.cal.calendars()
        by = {(c["title"], c["account"]): c for c in data["calendars"]}
        self.assertTrue(by[("Personale", "iCloud")]["writable"])
        self.assertTrue(by[("Personale", "iCloud")]["default"])
        self.assertFalse(by[("Festività italiane", "Subscribed")]["writable"])
        self.assertFalse(by[("Compleanni", "Other")]["writable"])
        self.assertEqual([c["title"] for c in data["reminder_lists"]], ["Promemoria", "Spesa"])

    def test_events_have_stable_ids(self):
        FakeEK.add_event(self.work, "Call", at(9), at(10))
        ev = self.events()[0]
        self.assertTrue(ev["id"].startswith("EV-"))
        self.assertEqual(ev["external_id"], "EXT-" + ev["id"])
        self.assertEqual(ev["calendar_id"], self.work.calendarIdentifier())
        self.assertTrue(ev["writable"])

    def test_read_only_calendar_is_refused_even_with_a_forged_plan(self):
        with self.assertRaises(CalendarError) as e:
            self.cal.create_event(self.holidays.calendarIdentifier(), "X", iso(at(8)), iso(at(9)))
        self.assertIn("does not allow changes", str(e.exception))
        self.assertEqual(self.state.events, {})

    def test_repeating_event_needs_an_occurrence(self):
        series = FakeEK.add_event(self.icloud, "Palestra", at(19), at(20), weekly=4)
        with self.assertRaises(CalendarError) as e:
            self.cal.get_event(series._id)
        self.assertIn("repeating event", str(e.exception))

    def test_save_failure_is_reported_not_hidden(self):
        self.state.fail_next_save = "The operation couldn't be completed (iCloud offline)"
        with self.assertRaises(CalendarError) as e:
            self.cal.create_event(self.icloud.calendarIdentifier(), "X", iso(at(8)), iso(at(9)))
        self.assertIn("iCloud offline", str(e.exception))

    def test_writes_need_eventkit(self):
        cal = CalendarService("applescript")
        with self.assertRaises(CalendarError) as e:
            cal.create_event(self.icloud.calendarIdentifier(), "X", iso(at(8)), iso(at(9)))
        self.assertIn("check-calendar", str(e.exception))

    def test_subprocess_json_plumbing(self):
        """The real subprocess path: JSON on stdin, one JSON line on stdout."""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(FAKES), str(ROOT)])}
        code = (
            "import EventKit as F, datetime as d, sys\n"
            "F.add_calendar('Personale','iCloud')\n"
            "from assistant import calendar_helper as h\n"
            "sys.exit(h.main(sys.argv[1:]))\n"
        )
        cid = None
        # calendars
        p = subprocess.run([sys.executable, "-c", code, "calendars"], cwd=ROOT, env=env, capture_output=True, text=True)
        cal = json.loads(p.stdout)["calendars"][0]
        cid = cal["id"]
        # create with a JSON payload on stdin (ids restart in a fresh process, so recreate the calendar)
        payload = json.dumps({"calendar_id": cid, "title": "Equitazione", "start": iso(at(8)), "end": iso(at(12))})
        p = subprocess.run([sys.executable, "-c", code, "event-create"], cwd=ROOT, env=env, input=payload,
                           capture_output=True, text=True)
        out = json.loads(p.stdout)
        self.assertEqual(out["event"]["title"], "Equitazione")
        self.assertEqual((out["event"]["start"], out["event"]["end"]), (iso(at(8)), iso(at(12))))


# ---------------------------------------------------------------------- routing
class RoutingTest(Base):
    def test_no_mapping_means_ask_never_guess(self):
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        self.assertEqual(r["status"], "needs_calendar_choice")
        self.assertIn("Personale (iCloud)", r["writable_calendars"])
        self.assertNotIn("Festività italiane (Subscribed)", r["writable_calendars"])
        self.assertNotIn("Compleanni (Other)", r["writable_calendars"])
        self.assertIsNone(self.reg.pending)
        self.assertEqual(self.state.events, {})

    def test_route_is_remembered_and_validated(self):
        r = self.call("calendar_set_route", area="equestrian", calendar="Festività italiane")
        self.assertIn("read-only", r["error"])
        r = self.call("calendar_set_route", area="equestrian", calendar="Calendar")
        self.assertEqual(sorted(r["choose_one_of"]), ["Calendar (francesca@gmail.com)", "Calendar (work@company.it)"])
        r = self.call("calendar_set_route", area="equestrian", calendar="Calendar (francesca@gmail.com)")
        self.assertEqual(r["status"], "saved")
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertIn("Calendar (francesca@gmail.com)", r["summary"])

    def test_general_is_the_fallback(self):
        self.call("calendar_set_route", area="general", calendar="Personale")
        r = self.call("calendar_create_event", title="Unghie", start=iso(at(15)), duration_minutes=120, area="personal")
        self.assertIn("Personale (iCloud)", r["summary"])
        self.assertIn("15:00–17:00", r["summary"])

    def test_named_calendar_overrides_route(self):
        self.call("calendar_set_route", area="general", calendar="Personale")
        r = self.call("calendar_create_event", title="Call", start=iso(at(9)), area="business", calendar="Lavoro")
        self.assertIn("Lavoro (iCloud)", r["summary"])


# ---------------------------------------------------------------------- create / confirm
class CreateTest(Base):
    def setUp(self):
        super().setUp()
        self.call("calendar_set_route", area="equestrian", calendar="Personale")
        self.call("calendar_set_route", area="business", calendar="Lavoro")

    def test_equitazione_tomorrow_8_12(self):
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertIn("08:00–12:00", r["summary"])
        self.assertEqual(self.state.events, {})  # nothing written before the yes
        done = self.yes(r)
        self.assertEqual(done["status"], "done")
        created = done["created"]
        self.assertEqual((created["title"], created["start"], created["end"]), ("Equitazione", iso(at(8)), iso(at(12))))
        stored = self.events()
        self.assertEqual([(e["title"], e["start"], e["end"], e["calendar"]) for e in stored],
                         [("Equitazione", iso(at(8)), iso(at(12)), "Personale")])
        row = self.log_rows()[0]
        self.assertEqual(row["status"], "executed")
        self.assertEqual(row["tool"], "calendar_create_event")
        self.assertIn(created["id"], row["result"])
        self.assertIn("event_delete", row["undo"])
        self.assertTrue(any(line.startswith("Done:") for line in self.logs))

    def test_no_yes_no_event(self):
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        for reply in ("aspetta", "no", "sì ma non domani"):
            res = self.yes(r, reply)
            self.assertIn("error", res)
        self.assertEqual(self.state.events, {})
        self.reg.cancel(self.ctx)
        self.assertEqual(self.log_rows()[0]["status"], "cancelled")

    def test_model_cannot_bypass_or_forge_the_plan(self):
        forged = {"op": "event_create", "calendar_id": self.holidays.calendarIdentifier(), "title": "X",
                  "start": iso(at(8)), "end": iso(at(9)), "all_day": False}
        r = self.call("calendar_create_event", title="X", start=iso(at(8)), plan=forged)
        self.assertIn("error", r)
        r = self.call("calendar_create_event", title="X", start=iso(at(8)), area="business", confirmed=True)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertEqual(self.state.events, {})
        # confirmation only in the very next turn
        self.turn += 1
        late = self.reg.confirm(r["action_id"], self.ctx, self.turn + 1, user_text="sì")
        self.assertIn("error", late)
        self.assertEqual(self.state.events, {})

    def test_conflicts_are_reported(self):
        FakeEK.add_event(self.work, "Call con Luca", at(9), at(10))
        FakeEK.add_event(self.icloud, "Festa", at(0), at(23, 59)).setAllDay_(True)
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        self.assertIn("overlaps with \"Call con Luca\"", r["summary"])
        self.assertNotIn("Festa", r["summary"])  # all-day events don't block time
        self.assertEqual(len(r["details"]["conflicts"]), 1)

    def test_bad_times_are_rejected_before_asking(self):
        r = self.call("calendar_create_event", title="X", start=iso(at(12)), end=iso(at(8)), area="business")
        self.assertIn("after the start", r["error"])
        r = self.call("calendar_create_event", title="X", start="domani alle 8", area="business")
        self.assertIn("start must be", r["error"])
        self.assertIsNone(self.reg.pending)

    def test_eventkit_failure_is_reported_as_failed(self):
        r = self.call("calendar_create_event", title="Equitazione", start=iso(at(8)), end=iso(at(12)), area="equestrian")
        self.state.fail_next_save = "iCloud offline"
        done = self.yes(r)
        self.assertEqual(done["status"], "failed")
        self.assertIn("iCloud offline", done["error"])
        self.assertEqual(self.log_rows()[0]["status"], "failed")
        self.assertFalse(any(line.startswith("Done:") for line in self.logs))


# ---------------------------------------------------------------------- update / delete / recurring
class ChangeTest(Base):
    def setUp(self):
        super().setUp()
        self.gym = FakeEK.add_event(self.icloud, "Palestra", at(16), at(17, 30))

    def test_move_keeps_duration_and_undo_restores(self):
        r = self.call("calendar_update_event", event_id=self.gym._id, start=iso(at(18)))
        self.assertIn("move to", r["summary"])
        self.assertIn("18:00–19:30", r["summary"])
        done = self.yes(r)
        self.assertEqual((done["event"]["start"], done["event"]["end"]), (iso(at(18)), iso(at(19, 30))))
        u = self.call("undo_last_action")
        self.assertIn("UNDO", u["summary"])
        self.assertIn("16:00–17:30", u["summary"])
        res = self.yes(u, "sì, annulla pure")  # "annulla" means cancel → not a clear yes
        self.assertIn("error", res)
        res = self.yes(u, "Confermo")
        self.assertEqual(res["status"], "done")
        ev = [e for e in self.events() if e["title"] == "Palestra"][0]
        self.assertEqual((ev["start"], ev["end"]), (iso(at(16)), iso(at(17, 30))))
        again = self.call("undo_last_action")
        self.assertIn("error", again)

    def test_delete_and_undo_recreates(self):
        r = self.call("calendar_delete_event", event_id=self.gym._id)
        self.assertIn("DELETE \"Palestra\"", r["summary"])
        self.assertEqual(self.yes(r)["status"], "done")
        self.assertEqual(self.events(), [])
        u = self.call("undo_last_action")
        self.yes(u)
        ev = self.events()
        self.assertEqual([(e["title"], e["start"], e["end"], e["calendar"]) for e in ev],
                         [("Palestra", iso(at(16)), iso(at(17, 30)), "Personale")])

    def test_repeating_event_changes_only_one_occurrence(self):
        series = FakeEK.add_event(self.icloud, "Lezione", at(10), at(11), weekly=4)
        r = self.call("calendar_update_event", event_id=series._id, start=iso(at(12)))
        self.assertIn("repeating event", r["error"])
        occurrences = [e for e in self.cal.events(at(0), at(0) + timedelta(days=28)) if e["title"] == "Lezione"]
        second = occurrences[1]
        r = self.call("calendar_update_event", event_id=series._id, occurrence_start=second["occurrence_start"],
                      start=second["start"][:11] + "12:00")
        self.assertIn("only this occurrence", r["summary"])
        self.yes(r)
        after = [e for e in self.cal.events(at(0), at(0) + timedelta(days=28)) if e["title"] == "Lezione"]
        self.assertEqual([e["start"][11:] for e in after], ["10:00", "12:00", "10:00", "10:00"])
        r = self.call("calendar_update_event", event_id=series._id, occurrence_start=after[2]["occurrence_start"],
                      title="Lezione B", apply_to="future")
        self.assertIn("all following occurrences", r["summary"])

    def test_deleting_a_repeating_occurrence_is_not_auto_undoable(self):
        series = FakeEK.add_event(self.icloud, "Lezione", at(10), at(11), weekly=3)
        first = [e for e in self.events() if e["title"] == "Lezione"][0]
        r = self.call("calendar_delete_event", event_id=series._id, occurrence_start=first["occurrence_start"])
        done = self.yes(r)
        self.assertIn("cannot be restored", done["note"])
        self.assertIn("cannot be undone", self.call("undo_last_action")["error"])

    def test_read_only_event_cannot_be_changed(self):
        hol = FakeEK.add_event(self.holidays, "Festa", at(0), at(23, 59))
        r = self.call("calendar_delete_event", event_id=hol._id)
        self.assertIn("does not allow changes", r["error"])


# ---------------------------------------------------------------------- batch plan
class BatchTest(Base):
    def test_day_plan_is_one_confirmed_batch_and_one_undo(self):
        self.call("calendar_set_route", area="general", calendar="Personale")
        self.call("calendar_set_route", area="business", calendar="Lavoro")
        r = self.call("calendar_create_events_batch", events=[
            {"title": "Equitazione", "start": iso(at(8)), "end": iso(at(12)), "area": "equestrian"},
            {"title": "Lavoro commerciale", "start": iso(at(14)), "duration_minutes": 180, "area": "business"},
            {"title": "Studio AI", "start": iso(at(16, 30)), "duration_minutes": 60, "area": "growth"},
        ])
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertIn("add 3 events", r["summary"])
        self.assertIn("overlaps with \"Studio AI\"", r["summary"])  # 14:00–17:00 vs 16:30
        self.assertEqual(self.state.events, {})
        done = self.yes(r)
        self.assertEqual(done["status"], "done")
        self.assertEqual(len(done["created"]), 3)
        self.assertEqual(len(self.events()), 3)
        u = self.call("undo_last_action")
        self.yes(u)
        self.assertEqual(self.events(), [])

    def test_batch_partial_failure_is_reported(self):
        self.call("calendar_set_route", area="general", calendar="Personale")
        r = self.call("calendar_create_events_batch", events=[
            {"title": "A", "start": iso(at(8)), "area": "general"},
            {"title": "B", "start": iso(at(10)), "area": "general"},
        ])
        self.state.fail_next_save = "network error"
        done = self.yes(r)
        self.assertEqual(done["status"], "partial")
        self.assertEqual([c["title"] for c in done["created"]], ["B"])
        self.assertEqual(done["failed"][0]["title"], "A")


# ---------------------------------------------------------------------- reminders
class ReminderTest(Base):
    def test_reminder_with_time_has_an_alert(self):
        r = self.call("reminders_create", title="Chiamare Marco", due=iso(at(17)))
        self.assertIn("Promemoria (iCloud)", r["summary"])  # the Reminders default list
        self.assertIn("at 17:00 (with an alert)", r["summary"])
        self.assertEqual(self.state.reminders, {})
        done = self.yes(r)
        rem = done["reminder"]
        self.assertEqual((rem["title"], rem["due"]), ("Chiamare Marco", iso(at(17))))
        stored = self.state.reminders[rem["id"]]
        self.assertEqual(len(stored.alarms()), 1)
        self.assertEqual(stored.alarms()[0].date.timeIntervalSince1970(), at(17).timestamp())

    def test_date_only_reminder_and_named_list(self):
        r = self.call("reminders_create", title="Prenotare le unghie", due=TOMORROW.isoformat(), list="Spesa")
        self.assertIn("Spesa", r["summary"])
        rem = self.yes(r)["reminder"]
        self.assertEqual(rem["due"], TOMORROW.isoformat())
        self.assertEqual(self.state.reminders[rem["id"]].alarms(), [])

    def test_complete_and_undo(self):
        rem = self.yes(self.call("reminders_create", title="Comprare fieno"))["reminder"]
        listed = self.call("reminders_list")["reminders"]
        self.assertEqual(listed[0]["id"], rem["id"])
        r = self.call("reminders_complete", reminder_id=rem["id"])
        self.assertIn("as done", r["summary"])
        self.yes(r)
        self.assertEqual(self.call("reminders_list")["reminders"], [])
        self.yes(self.call("undo_last_action"))
        self.assertEqual([x["title"] for x in self.call("reminders_list")["reminders"]], ["Comprare fieno"])

    def test_update_due(self):
        rem = self.yes(self.call("reminders_create", title="Chiamare Marco", due=iso(at(17))))["reminder"]
        r = self.call("reminders_update", reminder_id=rem["id"], due=iso(at(18)))
        self.yes(r)
        stored = self.state.reminders[rem["id"]]
        self.assertEqual(len(stored.alarms()), 1)
        self.assertEqual(stored.alarms()[0].date.timeIntervalSince1970(), at(18).timestamp())


# ---------------------------------------------------------------------- action log lifecycle
class LogTest(Base):
    def test_expired_and_replaced_proposals_are_logged(self):
        self.call("calendar_set_route", area="general", calendar="Personale")
        self.call("calendar_create_event", title="A", start=iso(at(8)))
        self.call("calendar_create_event", title="B", start=iso(at(9)))  # replaces A
        statuses = [r["status"] for r in self.log_rows()]
        self.assertEqual(statuses[:2], ["proposed", "replaced"])
        self.turn += 5
        self.reg.new_turn(self.turn, self.ctx)
        self.assertEqual(self.log_rows()[0]["status"], "expired")
        self.assertIsNone(self.reg.pending)


# ---------------------------------------------------------------------- conversation engine
class ConversationTest(Base):
    def test_equitazione_conversation(self):
        from assistant.brain import Brain

        replies, spoken = [], []

        class Stream:
            def __init__(self, kw): self.msg = replies.pop(0)
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self):
                for b in self.msg.content:
                    if b.type == "text":
                        yield NS(type="text", text=b.text)
            def get_final_message(self): return self.msg

        client = NS(messages=NS(stream=lambda **kw: Stream(kw)), beta=NS(messages=NS(stream=lambda **kw: Stream(kw))))
        cfg = NS(llm_model="test", llm_effort="low", llm_fallbacks="none", user_name="Miss Prato")
        brain = Brain(cfg, self.memory, self.reg, self.ctx, client=client)
        msg = lambda stop, *b: NS(content=list(b), stop_reason=stop, usage=None)
        T = lambda t: NS(type="text", text=t)
        U = lambda i, n, a: NS(type="tool_use", id=i, name=n, input=a)
        results = []
        orig = brain._run_tool
        brain._run_tool = lambda n, a: results.append(orig(n, a)) or results[-1]

        args = {"title": "Equitazione", "start": iso(at(8)), "end": iso(at(12)), "area": "equestrian"}
        replies += [msg("tool_use", U("1", "calendar_create_event", args)),
                    msg("end_turn", T("Su quale calendario metto l'equitazione: Personale, Lavoro o Calendar?"))]
        brain.respond("Aggiungimi equitazione domani dalle 8 alle 12.", spoken.append)
        self.assertEqual(results[-1]["status"], "needs_calendar_choice")

        replies += [msg("tool_use", U("2", "calendar_set_route", {"area": "equestrian", "calendar": "Personale"}),
                        U("3", "calendar_create_event", args)),
                    msg("end_turn", T("Le aggiungo Equitazione domani dalle 8 alle 12 nel calendario Personale. Conferma?"))]
        brain.respond("Usa Personale per l'equitazione.", spoken.append)
        pending = self.reg.pending
        self.assertIsNotNone(pending)
        self.assertEqual(self.state.events, {})

        replies += [msg("tool_use", U("4", "confirm_action", {"action_id": pending.action_id})),
                    msg("end_turn", T("Fatto. Equitazione è in calendario dalle 8 alle 12."))]
        brain.respond("Sì.", spoken.append)
        self.assertEqual(results[-1]["status"], "done")
        self.assertEqual([(e["title"], e["start"], e["end"]) for e in self.events()],
                         [("Equitazione", iso(at(8)), iso(at(12)))])
        self.assertEqual(spoken[-1], "Fatto. Equitazione è in calendario dalle 8 alle 12.")


if __name__ == "__main__":
    unittest.main()
