"""Phase 2B · M1 tests: memory v2 migration, replacement rules, working set, onboarding.

Run:  .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant import onboarding  # noqa: E402
from assistant.memory import MemoryStore  # noqa: E402
from assistant.persona import context_note  # noqa: E402
from assistant.tools import ToolContext, build_registry, is_clear_yes  # noqa: E402

# The exact Phase 2A (v1) schema, to build a database as 2026-10-06.15 left it.
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 3, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, last_used_at REAL
);
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind);
CREATE TABLE IF NOT EXISTS conversation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    subject, content, kind, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, subject, content, kind) VALUES (new.id, new.subject, new.content, new.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content, kind)
    VALUES ('delete', old.id, old.subject, old.content, old.kind);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, subject, content, kind)
    VALUES ('delete', old.id, old.subject, old.content, old.kind);
    INSERT INTO memories_fts(rowid, subject, content, kind) VALUES (new.id, new.subject, new.content, new.kind);
END;
"""

V1_ROWS = [
    ("goal", "revenue target", "Arrivare a 10.000 euro al mese con il business di automazione AI.", 5),
    ("business", "offer", "Vende automazioni AI alle aziende B2B.", 4),
    ("preference", "", "Preferisce le call la mattina.", 3),
    ("followup", "Marco", "Richiamare Marco per la proposta.", 3),
    ("person", "Giulia", "Giulia è la sua istruttrice di equitazione.", 4),
]


def make_v1_db(path: Path) -> list[tuple]:
    db = sqlite3.connect(str(path))
    db.executescript(V1_SCHEMA)
    t = time.time() - 3600
    for kind, subject, content, importance in V1_ROWS:
        db.execute(
            "INSERT INTO memories (kind, subject, content, importance, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (kind, subject, content, importance, t, t),
        )
    db.execute("INSERT INTO conversation_log (ts, role, text) VALUES (?, 'user', 'ciao Jarvis')", (t,))
    db.commit()
    rows = db.execute("SELECT id, kind, subject, content, importance, created_at FROM memories ORDER BY id").fetchall()
    db.close()
    return rows


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "jarvis_memory.db"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self) -> MemoryStore:
        return MemoryStore(self.db_path)

    def tools(self, store):
        reg = build_registry()
        ctx = ToolContext(memory=store, calendar=None, open_app=lambda a: "")
        return reg, ctx

    def goal_texts(self, store, title_prefix="CURRENT GOALS"):
        for title, items in store.working_set():
            if title.startswith(title_prefix):
                return [i["content"] for i in items]
        return []


# ---------------------------------------------------------------------- migration
class MigrationTest(Base):
    def test_v1_database_is_upgraded_without_losing_anything(self):
        before = make_v1_db(self.db_path)
        s = self.store()
        self.assertEqual(s.schema_version(), 2)
        self.assertIsNotNone(s.backup_path)
        self.assertTrue(s.backup_path.exists())
        # The backup is the untouched v1 database.
        b = sqlite3.connect(str(s.backup_path))
        self.assertEqual(b.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(len(b.execute("SELECT * FROM memories").fetchall()), len(V1_ROWS))
        b.close()
        # Every memory kept: same id, content, subject, importance, creation time.
        after = s.db.execute("SELECT id, kind, subject, content, importance, created_at, domain, status FROM memories ORDER BY id").fetchall()
        self.assertEqual(len(after), len(before))
        for old, new in zip(before, after):
            self.assertEqual((old[0], old[2], old[3], old[4], old[5]), (new[0], new[2], new[3], new[4], new[5]))
            self.assertEqual(new["status"], "active")
        business = [r for r in after if r["subject"] == "offer"][0]
        self.assertEqual((business["kind"], business["domain"]), ("fact", "business"))
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM conversation_log").fetchone()[0], 1)
        # Search still works on migrated rows, and the migrated goal is in the working set.
        self.assertTrue(any("Marco" in m["content"] for m in s.recall("Marco")))
        self.assertIn(V1_ROWS[0][2], self.goal_texts(s))

    def test_migration_runs_once(self):
        make_v1_db(self.db_path)
        first = self.store()
        first.db.close()
        again = self.store()
        self.assertIsNone(again.backup_path)
        self.assertEqual(again.count(), len(V1_ROWS))
        self.assertEqual(len(list((self.dir / "backups").iterdir())), 1)

    def test_new_database_needs_no_backup(self):
        s = self.store()
        self.assertEqual(s.schema_version(), 2)
        self.assertIsNone(s.backup_path)


# ---------------------------------------------------------------------- contradictions / replacement
class ReplacementTest(Base):
    def seed_targets(self, s):
        s.remember("goal", "Reach €10,000/month from the B2B AI automation business.", "revenue target", 5,
                   domain="business", slot_key="business.revenue_target.current",
                   data={"amount": 10000, "currency": "EUR", "period": "month"})
        s.remember("goal", "Scale to €50,000/month.", "revenue target", 4, domain="business", status="future",
                   slot_key="business.revenue_target.future", data={"amount": 50000, "currency": "EUR", "period": "month"})

    def test_active_10k_plus_future_50k_current_target_is_10k(self):
        s = self.store()
        self.seed_targets(s)
        self.assertEqual(s.current_for_slot("business.revenue_target.current")["data"],
                         '{"amount": 10000, "currency": "EUR", "period": "month"}')
        current = self.goal_texts(s)
        self.assertEqual(current, ["Reach €10,000/month from the B2B AI automation business."])
        future = self.goal_texts(s, "FUTURE GOALS")
        self.assertEqual(future, ["Scale to €50,000/month."])
        note = context_note("now", s.working_set(), "Miss Prato")
        cur_part, fut_part = note.split("FUTURE GOALS")
        self.assertIn("10,000", cur_part)
        self.assertNotIn("50,000", cur_part)
        self.assertIn("FUTURE — not a current priority", "FUTURE GOALS" + fut_part)
        self.assertIn("50,000", fut_part)
        self.assertIn("FUTURE]", fut_part)  # the item itself is tagged FUTURE

    def test_new_target_needs_confirmation_then_supersedes(self):
        s = self.store()
        self.seed_targets(s)
        reg, ctx = self.tools(s)
        old = s.current_for_slot("business.revenue_target.current")
        args = {"kind": "goal", "content": "Reach €20,000/month.", "subject": "revenue target", "domain": "business",
                "slot_key": "business.revenue_target.current", "importance": 5,
                "data": {"amount": 20000, "currency": "EUR", "period": "month"}}
        r = reg.execute("memory_remember", args, ctx, turn=1)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertIn("10,000", r["summary"])
        # Nothing changed before the confirmation.
        self.assertEqual(s.current_for_slot("business.revenue_target.current")["id"], old["id"])
        self.assertEqual(self.goal_texts(s), [old["content"]])

        done = reg.confirm(r["action_id"], ctx, turn=2, user_text="Sì, confermo.")
        self.assertEqual(done["status"], "saved")
        old_now = s.get(old["id"])
        self.assertEqual(old_now["status"], "superseded")
        self.assertEqual(old_now["superseded_by"], done["id"])
        self.assertIsNotNone(old_now["valid_until"])
        self.assertEqual(s.current_for_slot("business.revenue_target.current")["content"], "Reach €20,000/month.")
        self.assertEqual(self.goal_texts(s), ["Reach €20,000/month."])
        self.assertNotIn("10,000", context_note("now", s.working_set(), "Miss Prato"))
        # History: not in normal recall, available when explicitly requested.
        self.assertFalse(any("10,000" in m["content"] for m in s.recall("month")))
        hist = s.recall("month", include_history=True)
        self.assertTrue(any("10,000" in m["content"] and m["status"] == "superseded" for m in hist))
        self.assertEqual([h["status"] for h in s.history("business.revenue_target.current")], ["superseded", "active"])
        # The FUTURE target is untouched.
        self.assertEqual(self.goal_texts(s, "FUTURE GOALS"), ["Scale to €50,000/month."])

    def test_unclear_reply_does_not_confirm(self):
        s = self.store()
        self.seed_targets(s)
        reg, ctx = self.tools(s)
        r = reg.execute("memory_remember", {"kind": "goal", "content": "Reach €20,000/month.", "domain": "business",
                                            "slot_key": "business.revenue_target.current"}, ctx, turn=1)
        for reply in ("Aspetta, no.", "Sì, ma non adesso", "forse", "No."):
            res = reg.confirm(r["action_id"], ctx, turn=reg.pending.created_turn + 1, user_text=reply)
            self.assertIn("error", res, reply)
        self.assertIn("10,000", self.goal_texts(s)[0])
        reg.cancel(ctx)
        self.assertIsNone(reg.pending)
        self.assertIn("10,000", self.goal_texts(s)[0])

    def test_confirmation_cannot_be_skipped(self):
        s = self.store()
        self.seed_targets(s)
        reg, ctx = self.tools(s)
        args = {"kind": "goal", "content": "Reach €20,000/month.", "domain": "business",
                "slot_key": "business.revenue_target.current", "confirmed": True}
        r = reg.execute("memory_remember", args, ctx, turn=1)
        self.assertEqual(r["status"], "needs_confirmation")  # the model can't pre-confirm
        late = reg.confirm(r["action_id"], ctx, turn=3, user_text="sì")
        self.assertIn("error", late)  # confirmation must be the very next reply
        self.assertIn("10,000", self.goal_texts(s)[0])

    def test_similar_goal_without_slot_is_flagged_not_duplicated(self):
        s = self.store()
        self.seed_targets(s)
        reg, ctx = self.tools(s)
        r = reg.execute("memory_remember", {"kind": "goal", "content": "Reach €20,000/month from the B2B AI automation business.",
                                            "subject": "revenue target", "domain": "business"}, ctx, turn=1)
        self.assertIn(r["status"], ("needs_confirmation", "possible_conflict"))
        self.assertEqual(len(self.goal_texts(s)), 1)

    def test_archived_business_never_appears_as_current_project(self):
        s = self.store()
        old = s.remember("project", "Old e-commerce business (Tasaradar).", "Tasaradar", 4, domain="business")
        s.remember("project", "B2B AI automation business.", "AI automation", 5, domain="business")
        reg, ctx = self.tools(s)
        r = reg.execute("memory_set_status", {"memory_id": old["id"], "status": "archived"}, ctx, turn=1)
        self.assertEqual(r["status"], "archived")  # archiving a project is not a goal change
        projects = [i["content"] for t, items in s.working_set() if t.startswith("ACTIVE PROJECTS") for i in items]
        self.assertEqual(projects, ["B2B AI automation business."])
        self.assertNotIn("Tasaradar", context_note("now", s.working_set(), "Miss Prato"))
        self.assertFalse(any("Tasaradar" in m["content"] for m in s.recall("Tasaradar")))
        self.assertTrue(any(m["status"] == "archived" for m in s.recall("Tasaradar", include_history=True)))
        # Reactivating it is explicit and confirmed.
        r = reg.execute("memory_set_status", {"memory_id": old["id"], "status": "active"}, ctx, turn=2)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertEqual(s.get(old["id"])["status"], "archived")

    def test_hypothesis_icp_is_not_a_decision(self):
        s = self.store()
        reg, ctx = self.tools(s)
        r = reg.execute("memory_remember", {"kind": "hypothesis", "content": "ICP to test: Italian logistics SMEs, 20–200 employees.",
                                            "domain": "business", "slot_key": "business.icp", "importance": 4}, ctx, turn=1)
        self.assertEqual(r["status"], "saved")
        sections = dict(s.working_set())
        hyp_title = [t for t in sections if t.startswith("HYPOTHESES")][0]
        self.assertIn("NOT decided", hyp_title)
        self.assertFalse(any(t.startswith("DECISIONS") for t in sections))
        note = context_note("now", s.working_set(), "Miss Prato")
        self.assertIn("hypothesis · business] ICP to test", note)
        # A new idea for the same slot simply replaces the old idea (no decision involved).
        r = reg.execute("memory_remember", {"kind": "hypothesis", "content": "ICP to test: Italian manufacturing SMEs.",
                                            "domain": "business", "slot_key": "business.icp"}, ctx, turn=2)
        self.assertEqual(r["status"], "saved")
        # Turning it into a decision needs confirmation; until then it stays a hypothesis.
        r = reg.execute("memory_remember", {"kind": "decision", "content": "ICP: Italian manufacturing SMEs.",
                                            "domain": "business", "slot_key": "business.icp"}, ctx, turn=3)
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertIn("DECISION", r["summary"])
        self.assertEqual(s.current_for_slot("business.icp")["kind"], "hypothesis")
        reg.confirm(r["action_id"], ctx, turn=4, user_text="yes, I've decided")
        self.assertEqual(s.current_for_slot("business.icp")["kind"], "decision")
        self.assertTrue(any(t.startswith("DECISIONS") for t, _ in s.working_set()))

    def test_ordinary_fact_updates_automatically(self):
        s = self.store()
        reg, ctx = self.tools(s)
        a = reg.execute("memory_remember", {"kind": "fact", "content": "Lives in Milan.", "domain": "personal",
                                            "slot_key": "personal.home_city", "importance": 4}, ctx, turn=1)
        b = reg.execute("memory_remember", {"kind": "fact", "content": "Lives in Rome.", "domain": "personal",
                                            "slot_key": "personal.home_city", "importance": 4}, ctx, turn=2)
        self.assertEqual(b["status"], "saved")
        self.assertEqual(b["superseded"], a["id"])
        self.assertIsNone(reg.pending)

    def test_conservative_saving(self):
        s = self.store()
        reg, ctx = self.tools(s)
        r = reg.execute("memory_remember", {"kind": "fact", "content": "She is a bit tired today.", "importance": 1}, ctx, turn=1)
        self.assertEqual(r["status"], "not_saved")
        self.assertEqual(s.count(), 0)
        # Near-duplicates merge instead of piling up (Phase 2A behaviour kept).
        reg.execute("memory_remember", {"kind": "preference", "content": "Prefers calls in the morning."}, ctx, turn=2)
        r = reg.execute("memory_remember", {"kind": "preference", "content": "Prefers calls in the morning!"}, ctx, turn=3)
        self.assertEqual(r["status"], "updated")
        self.assertEqual(s.count(), 1)


# ---------------------------------------------------------------------- onboarding
PROFILE = """
[[item]]
id = "biz-target-current"
kind = "goal"
domain = "business"
importance = 5
slot_key = "business.revenue_target.current"
content = "Reach €10,000/month from the B2B AI automation business."
data = { amount = 10000, currency = "EUR", period = "month" }

[[item]]
id = "biz-target-future"
kind = "goal"
domain = "business"
status = "future"
importance = 4
slot_key = "business.revenue_target.future"
content = "Scale to €50,000/month."
data = { amount = 50000, currency = "EUR", period = "month" }

[[item]]
id = "biz-icp"
kind = "hypothesis"
domain = "business"
slot_key = "business.icp"
content = "ICP to validate: Italian SMEs with repetitive back-office work."

[[item]]
id = "old-business"
kind = "project"
domain = "business"
status = "archived"
content = "Previous business, no longer active."

[[item]]
id = "horse"
kind = "fact"
domain = "equestrian"
subject = "horse"
importance = 4
content = "She owns a horse."
"""


class OnboardingTest(Base):
    def write(self, text: str) -> Path:
        p = self.dir / "onboarding.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def actions(self, store, path):
        return {c.item["id"]: c.action for c in onboarding.plan(store, onboarding.load_profile(path))}

    def test_dry_run_writes_nothing(self):
        make_v1_db(self.db_path)
        s = self.store()
        before = s.db.execute("SELECT * FROM memories").fetchall()
        lines = onboarding.run(s, self.write(PROFILE), do_apply=False)
        text = "\n".join(lines)
        self.assertIn("DRY RUN", text)
        self.assertIn("Nothing has been written", text)
        self.assertIn("SUPERSEDE/REPLACE", text)
        # The €10K goal saved in conversation (no slot) is flagged as a possible overlap.
        self.assertIn("possible overlap with current #1", text)
        self.assertIn("replaces = [1]", text)
        self.assertEqual([tuple(r) for r in s.db.execute("SELECT * FROM memories").fetchall()], [tuple(r) for r in before])
        self.assertEqual(len(list((self.dir / "backups").iterdir())), 1)  # only the migration backup

    def test_apply_twice_creates_zero_duplicates(self):
        make_v1_db(self.db_path)
        s = self.store()
        conv = s.count()
        path = self.write(PROFILE)
        onboarding.run(s, path, do_apply=True)
        after_first = s.count()
        self.assertEqual(after_first, conv + 5)
        self.assertEqual(set(self.actions(s, path).values()), {"UNCHANGED"})
        lines = onboarding.run(s, path, do_apply=True)
        self.assertEqual(s.count(), after_first)
        self.assertIn("Nothing needed writing", "\n".join(lines))
        # Conversation memories from the real-Mac test are all still there and current.
        for _, subject, content, _ in V1_ROWS:
            self.assertTrue(s.db.execute("SELECT 1 FROM memories WHERE content = ?", (content,)).fetchone())
        # Current target is exactly one, and archived business is history.
        self.assertEqual(self.goal_texts(s).count("Reach €10,000/month from the B2B AI automation business."), 1)
        self.assertNotIn("Previous business", context_note("now", s.working_set(), "Miss Prato"))

    def test_replaces_supersedes_a_conversation_goal(self):
        make_v1_db(self.db_path)  # memory #1 is the €10K goal saved by voice (no slot)
        s = self.store()
        profile = PROFILE.replace('id = "biz-target-current"', 'id = "biz-target-current"\nreplaces = [1]')
        path = self.write(profile)
        self.assertEqual(self.actions(s, path)["biz-target-current"], "SUPERSEDE")
        onboarding.run(s, path, do_apply=True)
        self.assertEqual(s.get(1)["status"], "superseded")
        self.assertEqual(self.goal_texts(s), ["Reach €10,000/month from the B2B AI automation business."])
        self.assertEqual(set(self.actions(s, path).values()), {"UNCHANGED"})  # idempotent with replaces too
        with self.assertRaises(onboarding.OnboardingError):
            onboarding.plan(s, onboarding.validate([{"id": "x", "kind": "goal", "content": "c", "replaces": [999]}]))

    def test_update_and_supersede_are_reported_and_applied(self):
        s = self.store()
        conv = s.remember("hypothesis", "ICP idea: law firms.", domain="business", slot_key="business.icp")
        path = self.write(PROFILE)
        acts = self.actions(s, path)
        self.assertEqual(acts["biz-icp"], "SUPERSEDE")
        self.assertEqual(acts["horse"], "ADD")
        onboarding.run(s, path, do_apply=True)
        self.assertEqual(s.get(conv["id"])["status"], "superseded")
        # Change content of one item and status of another.
        changed = PROFILE.replace("Reach €10,000/month from", "Reach €20,000/month from").replace(
            'subject = "horse"\nimportance = 4', 'subject = "horse"\nimportance = 5')
        path = self.write(changed)
        plan = {c.item["id"]: c for c in onboarding.plan(s, onboarding.load_profile(path))}
        self.assertEqual(plan["biz-target-current"].action, "UPDATE")
        self.assertTrue(plan["biz-target-current"].new_version)
        self.assertEqual(plan["horse"].action, "UPDATE")
        self.assertFalse(plan["horse"].new_version)
        text = "\n".join(onboarding.run(s, path, do_apply=False))
        self.assertIn("UPDATE", text)
        self.assertIn("kept as history", text)
        n = s.count()
        onboarding.run(s, path, do_apply=True)
        self.assertEqual(s.count(), n + 1)  # one new version, the horse updated in place
        self.assertEqual(self.goal_texts(s), ["Reach €20,000/month from the B2B AI automation business."])
        self.assertEqual(set(self.actions(s, path).values()), {"UNCHANGED"})

    def test_removed_item_is_kept_and_invalid_file_writes_nothing(self):
        s = self.store()
        onboarding.run(s, self.write(PROFILE), do_apply=True)
        n = s.count()
        shorter = PROFILE.split('[[item]]\nid = "horse"')[0]
        text = "\n".join(onboarding.run(s, self.write(shorter), do_apply=True))
        self.assertIn("kept untouched", text)
        self.assertEqual(s.count(), n)
        bad = PROFILE + '\n[[item]]\nid = "horse"\nkind = "opinion"\ncontent = ""\ncolour = "bay"\n'
        with self.assertRaises(onboarding.OnboardingError) as e:
            onboarding.run(s, self.write(bad), do_apply=True)
        msg = str(e.exception)
        for expected in ("duplicate id", "kind must be", "content is empty", "unknown field"):
            self.assertIn(expected, msg)
        self.assertEqual(s.count(), n)


# ---------------------------------------------------------------------- confirmation words
class YesTest(unittest.TestCase):
    def test_clear_yes(self):
        for t in ("Sì.", "si", "Sì, confermo", "Yes, go ahead", "ok", "va bene", "Confermo.", "certo, procedi", "D'accordo"):
            self.assertTrue(is_clear_yes(t), t)
        for t in ("No", "Aspetta", "sì ma non adesso", "non lo so", "forse", "wait", "", "cancel that", "Yes, but not now"):
            self.assertFalse(is_clear_yes(t), t)


# ---------------------------------------------------------------------- conversation engine
class BrainTest(Base):
    def test_goal_change_through_a_conversation(self):
        from assistant.brain import Brain

        s = self.store()
        s.remember("goal", "Reach €10,000/month.", "revenue target", 5, domain="business",
                   slot_key="business.revenue_target.current")
        s.remember("goal", "Scale to €50,000/month.", "revenue target", 4, domain="business", status="future",
                   slot_key="business.revenue_target.future")
        reg, ctx = self.tools(s)
        replies, requests = [], []

        class Stream:
            def __init__(self, kw):
                requests.append(kw)
                self.msg = replies.pop(0)
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self):
                for b in self.msg.content:
                    if b.type == "text":
                        yield NS(type="text", text=b.text)
            def get_final_message(self): return self.msg

        client = NS(messages=NS(stream=lambda **kw: Stream(kw)), beta=NS(messages=NS(stream=lambda **kw: Stream(kw))))
        cfg = NS(llm_model="test-model", llm_effort="low", llm_fallbacks="none", user_name="Miss Prato")
        brain = Brain(cfg, s, reg, ctx, client=client)
        msg = lambda stop, *blocks: NS(content=list(blocks), stop_reason=stop, usage=None)
        T = lambda text: NS(type="text", text=text)
        U = lambda i, name, inp: NS(type="tool_use", id=i, name=name, input=inp)

        replies += [
            msg("tool_use", U("t1", "memory_remember", {"kind": "goal", "content": "Reach €20,000/month.", "domain": "business",
                                                        "slot_key": "business.revenue_target.current"})),
            msg("end_turn", T("Vuoi che sostituisca l'obiettivo attivo di 10.000 con 20.000 euro al mese?")),
        ]
        spoken = []
        brain.respond("Il mio obiettivo attuale adesso è 20.000 euro al mese.", spoken.append)
        first_note = requests[0]["messages"][0]["content"][0]["text"]
        self.assertIn("CURRENT GOALS", first_note)
        self.assertIn("FUTURE", first_note)
        self.assertEqual(s.current_for_slot("business.revenue_target.current")["content"], "Reach €10,000/month.")
        action_id = reg.pending.action_id

        replies += [msg("tool_use", U("t2", "confirm_action", {"action_id": action_id})), msg("end_turn", T("Fatto. Annotato."))]
        brain.respond("Sì, confermo.", spoken.append)
        self.assertEqual(s.current_for_slot("business.revenue_target.current")["content"], "Reach €20,000/month.")
        replies += [msg("end_turn", T("Venti mila euro al mese."))]
        brain.respond("Qual è il mio obiettivo?", spoken.append)
        notes = [m["content"][0]["text"] for m in requests[-1]["messages"]
                 if m["role"] == "user" and isinstance(m["content"][0], dict) and m["content"][0].get("text", "").startswith("[Context")]
        last_note = notes[-1]
        self.assertIn("20,000", last_note)
        self.assertNotIn("10,000", last_note)


if __name__ == "__main__":
    unittest.main()
