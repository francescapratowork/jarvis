"""Business execution core (Phase 2B · M3): lightweight CRM, activities, KPIs, execution context.

SQLite (the same local database as memory) is the source of truth:
  companies           prospects/clients with a pipeline stage, evidence and sources
  contacts            people at a company (only when publicly found or told by her)
  company_sources     where each piece of company evidence came from
  business_activities everything that happened (outreach, replies, calls, proposals, wins...)

Daily/weekly numbers are always *calculated from activities*, never stored separately or
estimated. Money is recorded only when she states an amount ("proposta da 3.000 euro");
"ho mandato una proposta" records a proposal with an unknown value.

Future-ready: stable integer ids, timestamps, sources and a `source`/`external_ref` on each
record leave room for Sheets/Apollo/email sync later (not implemented in M3).
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

STAGES = ("RESEARCH", "TO_CONTACT", "CONTACTED", "REPLIED", "DISCOVERY", "PROPOSAL", "WON", "LOST")
OPEN_STAGES = STAGES[:6]
ACTIVITY_TYPES = (
    "research", "outreach", "reply", "positive_reply", "follow_up", "discovery_booked",
    "discovery_completed", "proposal_sent", "won", "lost", "note",
)
# Logging one of these moves the company forward to at least this stage (never backwards).
ACTIVITY_STAGE = {
    "outreach": "CONTACTED", "follow_up": "CONTACTED", "reply": "REPLIED", "positive_reply": "REPLIED",
    "discovery_booked": "DISCOVERY", "discovery_completed": "DISCOVERY", "proposal_sent": "PROPOSAL",
    "won": "WON", "lost": "LOST",
}
CHANNELS = ("linkedin", "email", "phone", "in_person", "whatsapp", "website_form", "event", "other")
PERIODS = ("today", "yesterday", "this_week", "last_week", "last_7_days", "this_month", "last_month")

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    domain TEXT,
    website TEXT,
    location TEXT,
    industry TEXT,
    size TEXT,                     -- only with a source (size_source)
    size_source TEXT,
    niche TEXT,                    -- the hypothesis/niche it was found for
    fit_reason TEXT,
    pain_evidence TEXT,
    pain_evidence_source TEXT,
    evidence_level TEXT NOT NULL DEFAULT 'unverified',  -- pain_evidence | icp_match | unverified | user
    stage TEXT NOT NULL DEFAULT 'RESEARCH',
    stage_changed_at REAL,
    value_eur REAL,                -- only an amount she stated
    value_note TEXT,
    next_followup TEXT,            -- YYYY-MM-DD
    followup_note TEXT,
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user',   -- research | user
    research_run_id INTEGER,
    external_ref TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_activity_at REAL
);
CREATE INDEX IF NOT EXISTS companies_name_key ON companies(name_key);
CREATE INDEX IF NOT EXISTS companies_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS companies_stage ON companies(stage);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    role TEXT,
    profile_url TEXT,
    source_url TEXT,
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS company_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    url TEXT NOT NULL,
    research_run_id INTEGER,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS business_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,              -- when it happened
    logged_at REAL NOT NULL,
    type TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    contact_id INTEGER REFERENCES contacts(id),
    channel TEXT,
    amount_eur REAL,               -- only an amount she stated
    minutes INTEGER,               -- execution time, only when she reports it
    count INTEGER NOT NULL DEFAULT 1,  -- e.g. "ho mandato 10 messaggi" without naming companies
    notes TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'user_report',
    removed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS activities_ts ON business_activities(ts);
CREATE INDEX IF NOT EXISTS activities_company ON business_activities(company_id);
"""

_LEGAL = r"\b(s\.?r\.?l\.?s?|s\.?p\.?a\.?|s\.?n\.?c\.?|s\.?a\.?s\.?|s\.?s\.?|srls|spa|srl|snc|sas|ltd|llc|inc|gmbh|& c\.?|e c\.?)\b"


def name_key(name: str) -> str:
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\s(&|e)\s*c\.?(?=\s|$)", " ", t)  # "& C." / "e C."
    t = re.sub(_LEGAL, " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^[a-z]+://", "", u).removeprefix("www.")
    host = u.split("/")[0].split("?")[0]
    return host or None


def period_range(period: str, today: date | None = None) -> tuple[datetime, datetime, str]:
    """[start, end) local datetimes for a named period, plus a label."""
    d = today or date.today()
    midnight = datetime.combine(d, datetime.min.time())
    if period == "today":
        return midnight, midnight + timedelta(days=1), d.isoformat()
    if period == "yesterday":
        return midnight - timedelta(days=1), midnight, (d - timedelta(days=1)).isoformat()
    week_start = midnight - timedelta(days=d.weekday())
    if period == "this_week":
        return week_start, week_start + timedelta(days=7), f"week of {week_start.date().isoformat()}"
    if period == "last_week":
        return week_start - timedelta(days=7), week_start, f"week of {(week_start - timedelta(days=7)).date().isoformat()}"
    if period == "last_7_days":
        return midnight - timedelta(days=6), midnight + timedelta(days=1), "last 7 days"
    month_start = midnight.replace(day=1)
    if period == "this_month":
        nxt = (month_start + timedelta(days=32)).replace(day=1)
        return month_start, nxt, month_start.strftime("%Y-%m")
    if period == "last_month":
        prev = (month_start - timedelta(days=1)).replace(day=1)
        return prev, month_start, prev.strftime("%Y-%m")
    raise ValueError(f"period must be one of {', '.join(PERIODS)}")


class BusinessError(ValueError):
    pass


class BusinessStore:
    def __init__(self, memory, settings=None, min_sample: int = 10) -> None:
        self.memory = memory
        self.db = memory.db
        self.settings = settings  # operations.OpsStore (optional KPI targets)
        self.min_sample = min_sample
        with memory._lock:
            self.db.executescript(BUSINESS_SCHEMA)

    # ------------------------------------------------------------------ companies
    def find_company(self, name: str = "", domain: str = "") -> dict | None:
        d = domain_of(domain)
        if d:
            row = self.db.execute("SELECT * FROM companies WHERE domain = ? ORDER BY id LIMIT 1", (d,)).fetchone()
            if row:
                return dict(row)
        key = name_key(name)
        if key:
            row = self.db.execute("SELECT * FROM companies WHERE name_key = ? ORDER BY id LIMIT 1", (key,)).fetchone()
            if row:
                return dict(row)
        return None

    def resolve_company(self, company_id: int | None = None, name: str = "") -> dict:
        """One company by id or name. Exact (normalised) name first, then a unique partial match."""
        if company_id:
            row = self.db.execute("SELECT * FROM companies WHERE id = ?", (int(company_id),)).fetchone()
            if row is None:
                raise BusinessError(f"no company with id {company_id}")
            return dict(row)
        found = self.find_company(name=name)
        if found:
            return found
        key = name_key(name)
        if not key:
            raise BusinessError("say which company")
        rows = [dict(r) for r in self.db.execute("SELECT * FROM companies WHERE name_key LIKE ?", (f"%{key}%",))]
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise BusinessError("more than one company matches '" + name + "': "
                                + "; ".join(f"#{r['id']} {r['name']}" for r in rows[:8]))
        raise LookupError(name)

    def upsert_company(self, rec: dict, *, source: str = "user", research_run_id: int | None = None,
                       stage: str = "RESEARCH", niche: str = "") -> tuple[dict, bool]:
        """Create a company, or merge into the existing one (same domain or same normalised name).
        Returns (company, created). Existing facts are never overwritten with blanks."""
        name = " ".join(str(rec.get("name") or "").split())
        if not name:
            raise BusinessError("a company needs a name")
        stage = stage if stage in STAGES else "RESEARCH"
        existing = self.find_company(name=name, domain=rec.get("website") or rec.get("domain") or "")
        now = time.time()
        fields = {
            "website": rec.get("website"), "domain": domain_of(rec.get("website")) if rec.get("website") else rec.get("domain"),
            "location": rec.get("location"), "industry": rec.get("industry"), "size": rec.get("size"),
            "size_source": rec.get("size_source"), "fit_reason": rec.get("fit_reason"),
            "pain_evidence": rec.get("pain_evidence"), "pain_evidence_source": rec.get("pain_evidence_source"),
        }
        with self.memory._tx():
            if existing:
                sets, vals = [], []
                for k, v in fields.items():
                    if v and not existing.get(k):
                        sets.append(f"{k} = ?")
                        vals.append(v)
                if rec.get("evidence_level") == "pain_evidence" and existing["evidence_level"] != "pain_evidence":
                    sets.append("evidence_level = 'pain_evidence'")
                if niche and not existing.get("niche"):
                    sets.append("niche = ?")
                    vals.append(niche)
                if sets:
                    self.db.execute(f"UPDATE companies SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*vals, now, existing["id"]))
                company_id, created = existing["id"], False
            else:
                cur = self.db.execute(
                    "INSERT INTO companies (name, name_key, domain, website, location, industry, size, size_source, niche, "
                    "fit_reason, pain_evidence, pain_evidence_source, evidence_level, stage, stage_changed_at, source, "
                    "research_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, name_key(name), fields["domain"], fields["website"], fields["location"], fields["industry"],
                     fields["size"], fields["size_source"], niche or None, fields["fit_reason"], fields["pain_evidence"],
                     fields["pain_evidence_source"], rec.get("evidence_level") or ("user" if source == "user" else "unverified"),
                     stage, now, source, research_run_id, now, now),
                )
                company_id, created = int(cur.lastrowid), True
            for url in rec.get("sources") or []:
                if not self.db.execute("SELECT 1 FROM company_sources WHERE company_id = ? AND url = ?", (company_id, url)).fetchone():
                    self.db.execute("INSERT INTO company_sources (company_id, url, research_run_id, added_at) VALUES (?, ?, ?, ?)",
                                    (company_id, url, research_run_id, now))
            if rec.get("contact_name"):
                if not self.db.execute("SELECT 1 FROM contacts WHERE company_id = ? AND lower(name) = lower(?)",
                                       (company_id, rec["contact_name"])).fetchone():
                    self.db.execute(
                        "INSERT INTO contacts (company_id, name, role, profile_url, source_url, source, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (company_id, rec["contact_name"], rec.get("contact_role"), rec.get("contact_url"),
                         rec.get("contact_source"), source, now),
                    )
        return self.company(company_id), created

    def company(self, company_id: int) -> dict:
        row = dict(self.db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone())
        row["contacts"] = [dict(r) for r in self.db.execute(
            "SELECT id, name, role, profile_url, source_url FROM contacts WHERE company_id = ?", (company_id,))]
        row["sources"] = [r[0] for r in self.db.execute("SELECT url FROM company_sources WHERE company_id = ?", (company_id,))]
        return row

    def brief(self, c: dict) -> dict:
        """What Claude sees for a company: known fields only, UNKNOWN made explicit."""
        out: dict[str, Any] = {"id": c["id"], "name": c["name"], "stage": c["stage"]}
        for k in ("website", "location", "industry", "niche", "fit_reason", "next_followup", "followup_note"):
            if c.get(k):
                out[k] = c[k]
        out["size"] = c["size"] if c.get("size") else "UNKNOWN"
        out["evidence"] = {"pain_evidence": "specific pain found", "icp_match": "matches the general ICP only",
                           "unverified": "not verified", "user": "added by her"}.get(c.get("evidence_level"), c.get("evidence_level"))
        if c.get("pain_evidence"):
            out["pain_evidence"] = c["pain_evidence"]
        out["value_eur"] = c["value_eur"] if c.get("value_eur") is not None else "UNKNOWN"
        if c.get("contacts"):
            out["contacts"] = [{k: v for k, v in p.items() if v and k != "id"} for p in c["contacts"]]
        if c.get("sources"):
            out["sources"] = c["sources"][:5]
        return out

    def list_companies(self, stage: str = "", query: str = "", limit: int = 30) -> list[dict]:
        sql, params = "SELECT * FROM companies WHERE 1 = 1", []
        if stage:
            sql += " AND stage = ?"
            params.append(stage)
        if query:
            sql += " AND (name_key LIKE ? OR coalesce(niche, '') LIKE ? OR coalesce(industry, '') LIKE ?)"
            params += [f"%{name_key(query)}%", f"%{query}%", f"%{query}%"]
        sql += " ORDER BY CASE stage WHEN 'PROPOSAL' THEN 0 WHEN 'DISCOVERY' THEN 1 WHEN 'REPLIED' THEN 2 " \
               "WHEN 'CONTACTED' THEN 3 WHEN 'TO_CONTACT' THEN 4 WHEN 'RESEARCH' THEN 5 ELSE 6 END, updated_at DESC LIMIT ?"
        params.append(max(1, min(100, int(limit))))
        return [self.brief(self.company(r["id"])) for r in self.db.execute(sql, params)]

    def set_stage(self, company_id: int, stage: str, reason: str = "") -> dict:
        if stage not in STAGES:
            raise BusinessError(f"stage must be one of {', '.join(STAGES)}")
        c = self.resolve_company(company_id)
        with self.memory._tx():
            self.db.execute("UPDATE companies SET stage = ?, stage_changed_at = ?, updated_at = ? WHERE id = ?",
                            (stage, time.time(), time.time(), c["id"]))
            if reason:
                self._append_note(c["id"], f"{stage}: {reason}")
        return {"company": c["name"], "from": c["stage"], "to": stage}

    def _append_note(self, company_id: int, note: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        self.db.execute("UPDATE companies SET notes = trim(notes || char(10) || ?) WHERE id = ?", (f"[{stamp}] {note}", company_id))

    def update_company(self, company_id: int, **fields) -> dict:
        allowed = {"website", "location", "industry", "notes", "value_eur", "value_note", "niche"}
        c = self.resolve_company(company_id)
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "notes":
                self._append_note(c["id"], str(v))
                continue
            if k == "website":
                sets += ["website = ?", "domain = ?"]
                vals += [v, domain_of(v)]
                continue
            sets.append(f"{k} = ?")
            vals.append(v)
        with self.memory._tx():
            if sets:
                self.db.execute(f"UPDATE companies SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*vals, time.time(), c["id"]))
        if fields.get("contact_name"):
            self.upsert_company({"name": c["name"], "contact_name": fields["contact_name"],
                                 "contact_role": fields.get("contact_role"), "contact_url": fields.get("contact_url")})
        return self.brief(self.company(c["id"]))

    def set_followup(self, company_id: int, day: str, note: str = "") -> dict:
        c = self.resolve_company(company_id)
        d = date.fromisoformat(day[:10]).isoformat() if day else None
        with self.memory._tx():
            self.db.execute("UPDATE companies SET next_followup = ?, followup_note = ?, updated_at = ? WHERE id = ?",
                            (d, note or None, time.time(), c["id"]))
        return {"company": c["name"], "next_followup": d, "note": note}

    # ------------------------------------------------------------------ activities
    def log_activity(self, type: str, *, company_id: int | None = None, company: str = "", channel: str = "",  # noqa: A002
                     amount_eur: float | None = None, minutes: int | None = None, count: int = 1, notes: str = "",
                     when: str = "", followup_date: str = "", source: str = "user_report", meta: dict | None = None) -> dict:
        if type not in ACTIVITY_TYPES:
            raise BusinessError(f"activity type must be one of {', '.join(ACTIVITY_TYPES)}")
        channel = (channel or "").lower().replace(" ", "_")
        if channel and channel not in CHANNELS:
            channel = "other"
        ts = datetime.fromisoformat(when.replace(" ", "T")[:16]).timestamp() if when else time.time()
        created = False
        c = None
        if company_id or company:
            try:
                c = self.resolve_company(company_id, company)
            except LookupError:
                c, created = self.upsert_company({"name": company}, source="user", stage="TO_CONTACT")
        if count < 1:
            raise BusinessError("count must be at least 1")
        if c is not None and count != 1:
            raise BusinessError("count is only for activities not tied to one company")
        if amount_eur is not None and type not in ("proposal_sent", "won", "note"):
            raise BusinessError("an amount can only be recorded with a proposal or a win")
        now = time.time()
        stage_change = None
        with self.memory._tx():
            cur = self.db.execute(
                "INSERT INTO business_activities (ts, logged_at, type, company_id, channel, amount_eur, minutes, count, "
                "notes, meta, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, now, type, c["id"] if c else None, channel or None, amount_eur, minutes, count, notes or "",
                 json.dumps(meta or {}, ensure_ascii=False), source),
            )
            activity_id = int(cur.lastrowid)
            if c is not None:
                target = ACTIVITY_STAGE.get(type)
                updates = ["last_activity_at = ?", "updated_at = ?"]
                vals: list[Any] = [ts, now]
                if target and (target in ("WON", "LOST") or STAGES.index(target) > STAGES.index(c["stage"])) \
                        and c["stage"] not in ("WON", "LOST"):
                    updates += ["stage = ?", "stage_changed_at = ?"]
                    vals += [target, now]
                    stage_change = (c["stage"], target)
                if amount_eur is not None and type == "proposal_sent":
                    updates += ["value_eur = ?", "value_note = ?"]
                    vals += [amount_eur, "proposal amount stated by her"]
                if followup_date:
                    updates += ["next_followup = ?"]
                    vals.append(date.fromisoformat(followup_date[:10]).isoformat())
                elif type in ("reply", "positive_reply", "won", "lost", "discovery_completed") and c.get("next_followup"):
                    updates.append("next_followup = NULL")  # they answered: the pending follow-up is done
                self.db.execute(f"UPDATE companies SET {', '.join(updates)} WHERE id = ?", (*vals, c["id"]))
        out: dict[str, Any] = {"activity_id": activity_id, "type": type, "logged": True}
        if c is not None:
            out["company"] = c["name"]
            out["company_id"] = c["id"]
            if created:
                out["added_to_pipeline"] = True
            if stage_change:
                out["stage"] = f"{stage_change[0]} → {stage_change[1]}"
        if count != 1:
            out["count"] = count
        if amount_eur is not None:
            out["amount_eur"] = amount_eur
        elif type in ("proposal_sent", "won"):
            out["amount_eur"] = "UNKNOWN (not stated)"
        return out

    def activity(self, activity_id: int) -> dict | None:
        row = self.db.execute("SELECT a.*, c.name AS company FROM business_activities a LEFT JOIN companies c ON c.id = a.company_id "
                              "WHERE a.id = ?", (int(activity_id),)).fetchone()
        return dict(row) if row else None

    def remove_activity(self, activity_id: int) -> dict:
        """Mark an activity as removed (kept for audit, excluded from all numbers)."""
        with self.memory._tx():
            cur = self.db.execute("UPDATE business_activities SET removed = 1 WHERE id = ? AND removed = 0", (int(activity_id),))
        return {"status": "removed" if cur.rowcount else "not_found", "activity_id": activity_id}

    def recent_activities(self, limit: int = 15) -> list[dict]:
        rows = self.db.execute(
            "SELECT a.id, a.ts, a.type, a.channel, a.amount_eur, a.count, a.notes, c.name AS company FROM business_activities a "
            "LEFT JOIN companies c ON c.id = a.company_id WHERE a.removed = 0 ORDER BY a.ts DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = {k: v for k, v in dict(r).items() if v not in (None, "")}
            item["ts"] = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M")
            if item.get("count") == 1:
                item.pop("count")
            out.append(item)
        return out

    # ------------------------------------------------------------------ KPIs
    def kpis(self, period: str = "today", today: date | None = None) -> dict:
        start, end, label = period_range(period, today)
        s, e = start.timestamp(), end.timestamp()
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM business_activities WHERE removed = 0 AND ts >= ? AND ts < ?", (s, e))]

        def n(*types):
            return sum(r["count"] for r in rows if r["type"] in types)

        def distinct(*types):
            return len({r["company_id"] for r in rows if r["type"] in types and r["company_id"]})

        researched = self.db.execute("SELECT COUNT(*) FROM companies WHERE source = 'research' AND created_at >= ? AND created_at < ?", (s, e)).fetchone()[0]
        added = self.db.execute("SELECT COUNT(*) FROM companies WHERE created_at >= ? AND created_at < ?", (s, e)).fetchone()[0]
        outreach = n("outreach")
        replies = n("reply", "positive_reply")
        won_rows = [r for r in rows if r["type"] == "won"]
        out: dict[str, Any] = {
            "period": label,
            "companies_researched": researched,
            "prospects_added": added,
            "outreach": outreach,
            "companies_contacted": distinct("outreach"),
            "follow_ups": n("follow_up"),
            "replies": replies,
            "positive_replies": n("positive_reply"),
            "discovery_booked": n("discovery_booked"),
            "discovery_completed": n("discovery_completed"),
            "proposals_sent": n("proposal_sent"),
            "proposals_value_known_eur": sum(r["amount_eur"] for r in rows if r["type"] == "proposal_sent" and r["amount_eur"] is not None),
            "proposals_value_unknown": sum(1 for r in rows if r["type"] == "proposal_sent" and r["amount_eur"] is None),
            "won": len(won_rows),
            "revenue_won_eur": sum(r["amount_eur"] for r in won_rows if r["amount_eur"] is not None),
            "won_value_unknown": sum(1 for r in won_rows if r["amount_eur"] is None),
            "lost": n("lost"),
        }
        minutes = [r["minutes"] for r in rows if r["minutes"]]
        if minutes:
            out["reported_execution_minutes"] = sum(minutes)
        # Response rate: replies among outreach in the same period (both from her reports).
        if outreach == 0:
            out["response_rate"] = "no outreach recorded in this period"
        else:
            rate = replies / outreach
            out["response_rate"] = f"{rate:.0%} ({replies} of {outreach})"
            if outreach < self.min_sample:
                out["response_rate_note"] = (f"only {outreach} outreach recorded: too small a sample to draw "
                                             f"conclusions (at least {self.min_sample} needed)")
        targets = self.targets()
        if targets:
            out["targets_she_set"] = targets
        return out

    def compare(self, period: str, other: str, today: date | None = None) -> dict:
        a, b = self.kpis(period, today), self.kpis(other, today)
        keys = [k for k in dict.fromkeys([*a, *b]) if isinstance(a.get(k, 0), (int, float)) and isinstance(b.get(k, 0), (int, float))]
        return {"current": a, "previous": b,
                "change": {k: a.get(k, 0) - b.get(k, 0) for k in keys if a.get(k, 0) != b.get(k, 0)}}

    def targets(self) -> dict:
        if self.settings is None:
            return {}
        return self.settings.get("business.kpi_targets") or {}

    def set_target(self, metric: str, value: float | None, period: str = "week") -> dict:
        if self.settings is None:
            raise BusinessError("targets are not available")
        t = self.targets()
        key = f"{metric}/{period}"
        if value is None:
            t.pop(key, None)
        else:
            t[key] = value
        self.settings.set("business.kpi_targets", t)
        return {"targets": t}

    # ------------------------------------------------------------------ pipeline & execution context
    def pipeline(self) -> dict:
        counts = {s: 0 for s in STAGES}
        for r in self.db.execute("SELECT stage, COUNT(*) AS n FROM companies GROUP BY stage"):
            counts[r["stage"]] = r["n"]
        open_rows = [dict(r) for r in self.db.execute(
            f"SELECT id, name, stage, value_eur FROM companies WHERE stage IN ({','.join('?' * len(OPEN_STAGES))})", OPEN_STAGES)]
        with_value = [r for r in open_rows if r["value_eur"] is not None]
        late = [r for r in open_rows if r["stage"] in ("DISCOVERY", "PROPOSAL")]
        return {
            "by_stage": {k: v for k, v in counts.items() if v},
            "known_pipeline_value_eur": sum(r["value_eur"] for r in with_value),
            "opportunities_with_known_value": len(with_value),
            "late_stage_without_known_value": [r["name"] for r in late if r["value_eur"] is None],
        }

    def overdue_followups(self, today: date | None = None) -> list[dict]:
        d = (today or date.today()).isoformat()
        rows = self.db.execute(
            f"SELECT id, name, stage, next_followup, followup_note FROM companies WHERE next_followup IS NOT NULL "
            f"AND next_followup <= ? AND stage IN ({','.join('?' * len(OPEN_STAGES))}) ORDER BY next_followup",
            (d, *OPEN_STAGES)).fetchall()
        return [{k: v for k, v in dict(r).items() if v} for r in rows]

    def status(self, research_runs_7d: int = 0, today: date | None = None) -> dict:
        """Execution context for planning: facts from the data plus plain observations.
        The observations are signals for Jarvis's judgement, not a rigid algorithm."""
        pipe = self.pipeline()
        week = self.kpis("last_7_days", today)
        inventory = pipe["by_stage"].get("RESEARCH", 0) + pipe["by_stage"].get("TO_CONTACT", 0)
        overdue = self.overdue_followups(today)
        signals = []
        total = sum(pipe["by_stage"].values())
        if total == 0:
            signals.append("No prospects recorded yet: the next step is building a first list to contact (not more general research).")
        if inventory >= 5 and week.get("outreach", 0) < max(3, inventory // 3):
            signals.append(f"{inventory} prospects are researched or ready but only {week.get('outreach', 0)} outreach "
                           "were recorded in the last 7 days: work the existing list before researching more.")
        if overdue:
            signals.append(f"{len(overdue)} follow-up(s) are due or overdue.")
        if research_runs_7d >= 3 and week.get("outreach", 0) == 0:
            signals.append(f"{research_runs_7d} research runs in the last 7 days and no outreach recorded: research is "
                           "not turning into market conversations.")
        if pipe["by_stage"].get("DISCOVERY"):
            signals.append(f"{pipe['by_stage']['DISCOVERY']} company(ies) at discovery stage: make sure each call is prepared and followed up.")
        if pipe["late_stage_without_known_value"]:
            signals.append("Late-stage opportunities with no stated value: " + ", ".join(pipe["late_stage_without_known_value"][:5]) + ".")
        return {"pipeline": pipe, "last_7_days": week, "overdue_followups": overdue[:10],
                "research_runs_last_7_days": research_runs_7d, "signals": signals}
