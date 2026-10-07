"""Phase 2B · M3 tools: market research (Perplexity) and business execution (local CRM/KPIs).

Research tools spend money: each one first checks the local research history (same
question, still fresh → no API call). Business tools only touch the local database.
Nothing here writes Memory v2 or the calendar: decisions stay with her (memory tools,
with their confirmation rules), and calendar blocks go through the M2 calendar tools.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from .business import ACTIVITY_TYPES, CHANNELS, PERIODS, STAGES, BusinessError
from .research import (
    COMPANIES_SCHEMA, COMPARE_SCHEMA, MARKET_SCHEMA, SourceIndex, external, validate_companies, validate_findings,
)

MAX_FINDINGS = 20
MAX_COMPANIES = 20


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def _no_research(ctx) -> dict | None:
    if ctx.research is None:
        return {"status": "unavailable", "error": "research is not configured in this session"}
    return None


# ---------------------------------------------------------------------- validators (provider JSON → checked records)
def _market(parsed: dict, index: SourceIndex) -> dict:
    return {
        "summary": str(parsed.get("summary") or "")[:1500],
        "findings": validate_findings(parsed.get("findings"), index)[:MAX_FINDINGS],
        "unknowns": [str(u)[:300] for u in parsed.get("unknowns") or [] if str(u).strip()][:10],
        "evidence_quality": parsed.get("evidence_quality") if parsed.get("evidence_quality") in ("strong", "moderate", "weak") else "unstated",
        "evidence_notes": str(parsed.get("evidence_notes") or "")[:500],
    }


def _compare(parsed: dict, index: SourceIndex) -> dict:
    niches = []
    for n in parsed.get("niches") or []:
        if isinstance(n, dict) and n.get("name"):
            niches.append({"name": str(n["name"])[:80], "findings": validate_findings(n.get("findings"), index)[:10],
                           "evidence_quality": n.get("evidence_quality") if n.get("evidence_quality") in ("strong", "moderate", "weak") else "unstated"})
    return {"summary": str(parsed.get("summary") or "")[:1200], "niches": niches,
            "unknowns": [str(u)[:300] for u in parsed.get("unknowns") or []][:10],
            "scoring_note": "These are sourced findings only. There are no external market scores: any ranking or "
            "score you give is Jarvis's internal decision framework and must be presented as such."}


def _companies(limit: int):
    def validate(parsed: dict, index: SourceIndex) -> dict:
        return {"companies": validate_companies(parsed.get("companies"), index, limit),
                "notes": str(parsed.get("notes") or "")[:500]}
    return validate


def _region(region: str) -> str:
    return (region or "Italia").strip()


# ---------------------------------------------------------------------- research tools
def _research_market(ctx, topic: str, focus: str = "", region: str = "Italia", depth: str = "standard",
                     refresh: bool = False) -> dict:
    if (err := _no_research(ctx)):
        return err
    aspects = focus or ("industry structure, repetitive workflows and operational pain points, time/labour-intensive "
                        "processes, current AI adoption and common software, buying triggers and decision makers, likely "
                        "objections, existing solutions and competitors, public pricing evidence, regulatory constraints, "
                        "concrete automation opportunities")
    prompt = (f"Market research for selling AI/automation services to companies.\nTopic: {topic}\nRegion: {_region(region)}\n"
              f"Cover: {aspects}.\nGive public company examples only when a source names them. Say clearly what is unknown.")
    log(ctx, f"Research: {topic[:60]}")
    result = ctx.research.run("market", f"{topic} | {focus}", prompt, MARKET_SCHEMA, _market,
                              {"region": _region(region)}, depth=depth if depth in ("standard", "deep") else "standard",
                              refresh=bool(refresh))
    return external(result)


def _research_compare(ctx, niches: list, region: str = "Italia", criteria: str = "", refresh: bool = False) -> dict:
    if (err := _no_research(ctx)):
        return err
    names = [str(n).strip() for n in niches or [] if str(n).strip()][:5]
    if len(names) < 2:
        return {"error": "give at least two niches to compare"}
    dims = criteria or ("number and accessibility of buyers, severity and frequency of operational pain, amount of "
                        "repetitive admin work, measurable ROI potential, software maturity, willingness to pay (only "
                        "with evidence), ease of reaching decision makers, sales-cycle complexity, implementation "
                        "complexity, competitive saturation, data/regulatory sensitivity, repeatability, recurring revenue potential")
    prompt = (f"Compare these niches as targets for an AI/automation services business in {_region(region)}: "
              f"{', '.join(names)}.\nFor each niche give sourced findings on: {dims}.\nDo NOT give numeric scores.")
    log(ctx, "Research: compare " + ", ".join(names)[:60])
    return external(ctx.research.run("compare", " vs ".join(sorted(n.lower() for n in names)), prompt, COMPARE_SCHEMA,
                                     _compare, {"region": _region(region), "criteria": criteria}, refresh=bool(refresh)))


def _research_find_companies(ctx, niche: str, problem: str = "", region: str = "Italia", count: int = 10,
                             save: bool = True, refresh: bool = False) -> dict:
    if (err := _no_research(ctx)):
        return err
    count = max(1, min(MAX_COMPANIES, int(count or 10)))
    prompt = (f"Find {count} real companies in {_region(region)} in this niche: {niche}."
              + (f"\nWe are looking for companies that may have this problem: {problem}." if problem else "")
              + "\nFor each: name, website, location, industry; size ONLY if a source states it (with that source URL); a "
              "public contact person and role ONLY if a public page names them (with that URL); why it fits; and specific "
              "public evidence of the problem ONLY if you found it (with that URL). Never give email addresses or phone "
              "numbers. List every source URL used for each company.")
    log(ctx, f"Research: companies — {niche[:50]}")
    result = ctx.research.run("companies", f"{niche} | {problem}", prompt, COMPANIES_SCHEMA, _companies(count),
                              {"region": _region(region), "count": count}, refresh=bool(refresh))
    companies = result.get("companies") or []
    if result.get("status") in ("ok", None) and companies:
        saved, existing, skipped = [], [], []
        for rec in companies:
            if not rec.get("sources"):
                skipped.append(rec["name"])
                continue
            if save and ctx.business is not None:
                c, created = ctx.business.upsert_company(rec, source="research", research_run_id=result.get("run_id"),
                                                         stage="RESEARCH", niche=niche)
                (saved if created else existing).append(c["id"])
        result["companies_with_pain_evidence"] = [c for c in companies if c["evidence_level"] == "pain_evidence"]
        result["companies_matching_icp_only"] = [c for c in companies if c["evidence_level"] == "icp_match"]
        result.pop("companies", None)
        if skipped:
            result["dropped_without_sources"] = skipped
        if save and ctx.business is not None:
            result["pipeline"] = {"added": len(saved), "already_in_pipeline": len(existing),
                                  "note": "Saved as stage RESEARCH. Unknown fields are stored as unknown."}
            log(ctx, f"Pipeline: {len(saved)} new companies ({len(existing)} already known)")
    return external(result)


def _research_company(ctx, name: str, website: str = "", question: str = "", refresh: bool = False) -> dict:
    if (err := _no_research(ctx)):
        return err
    prompt = (f"Research this company: {name}" + (f" ({website})" if website else "") + ".\n"
              + (question or "What do they do, how big are they (only if a source says), which repetitive or manual "
                 "processes are visible publicly, what software do they mention, and is there any public sign of "
                 "operational pain we could solve with automation?")
              + "\nOnly sourced facts about THIS company count as FACT. Never give email addresses or phone numbers.")
    log(ctx, f"Research: company {name[:50]}")
    return external(ctx.research.run("company", f"{name} | {website} | {question}", prompt, MARKET_SCHEMA, _market,
                                     refresh=bool(refresh)))


def _research_check_claim(ctx, claim: str, refresh: bool = False) -> dict:
    if (err := _no_research(ctx)):
        return err
    prompt = f"Check this claim against current sources and say whether it is supported, contradicted or unknown:\n{claim}"
    log(ctx, "Research: check a claim")
    return external(ctx.research.run("claim", claim, prompt, MARKET_SCHEMA, _market, refresh=bool(refresh)))


def _research_history(ctx, limit: int = 10, run_id: int = 0) -> dict:
    if (err := _no_research(ctx)):
        return err
    if run_id:
        run = ctx.research.get_run(int(run_id))
        return external({"run": run}) if run else {"error": f"no research run {run_id}"}
    return {"research_runs": ctx.research.history(limit), "calls_today": ctx.research.calls_today(),
            "daily_budget": ctx.research.max_calls}


# ---------------------------------------------------------------------- business tools
def log(ctx, text: str) -> None:
    try:
        ctx.log(text)
    except Exception:  # noqa: BLE001
        pass


def _guard(fn):
    def wrapper(ctx, **kw):
        if ctx.business is None:
            return {"error": "the business pipeline is not available"}
        try:
            return fn(ctx, **kw)
        except (BusinessError, ValueError) as e:
            return {"error": str(e)}
        except LookupError as e:
            return {"error": f"no company called '{e}' in the pipeline"}
    return wrapper


@_guard
def _add_company(ctx, name: str, website: str = "", location: str = "", industry: str = "", notes: str = "",
                 stage: str = "TO_CONTACT") -> dict:
    c, created = ctx.business.upsert_company({"name": name, "website": website or None, "location": location or None,
                                              "industry": industry or None}, source="user", stage=stage)
    if notes:
        ctx.business.update_company(c["id"], notes=notes)
    log(ctx, f"Pipeline: {'added' if created else 'already had'} {c['name']}")
    return {"status": "added" if created else "already_in_pipeline", "company": ctx.business.brief(ctx.business.company(c["id"]))}


@_guard
def _list_companies(ctx, stage: str = "", query: str = "", limit: int = 20) -> dict:
    if stage and stage not in STAGES:
        return {"error": f"stage must be one of {', '.join(STAGES)}"}
    items = ctx.business.list_companies(stage, query, limit)
    return {"companies": items, "count": len(items), "pipeline": ctx.business.pipeline()}


@_guard
def _company(ctx, company_id: int = 0, name: str = "") -> dict:
    c = ctx.business.resolve_company(company_id or None, name)
    acts = [a for a in ctx.business.recent_activities(200) if a.get("company") == c["name"]][:15]
    full = ctx.business.company(c["id"])
    return {"company": ctx.business.brief(full), "notes": full.get("notes") or "", "activities": acts}


@_guard
def _log_activity(ctx, type: str, company: str = "", company_id: int = 0, channel: str = "",  # noqa: A002
                  amount_eur: float | None = None, minutes: int | None = None, count: int = 1, notes: str = "",
                  when: str = "", followup_date: str = "") -> dict:
    res = ctx.business.log_activity(type, company_id=company_id or None, company=company, channel=channel,
                                    amount_eur=amount_eur, minutes=minutes, count=int(count or 1), notes=notes,
                                    when=when, followup_date=followup_date)
    log(ctx, f"Logged: {type}" + (f" — {res['company']}" if res.get("company") else ""))
    return res


@_guard
def _set_stage(ctx, company_id: int, stage: str, reason: str = "") -> dict:
    return ctx.business.set_stage(company_id, stage, reason)


@_guard
def _update_company(ctx, company_id: int, website: str | None = None, location: str | None = None,
                    industry: str | None = None, notes: str | None = None, value_eur: float | None = None,
                    value_note: str | None = None, contact_name: str | None = None, contact_role: str | None = None,
                    contact_url: str | None = None) -> dict:
    return {"company": ctx.business.update_company(company_id, website=website, location=location, industry=industry,
                                                   notes=notes, value_eur=value_eur, value_note=value_note,
                                                   contact_name=contact_name, contact_role=contact_role,
                                                   contact_url=contact_url)}


@_guard
def _set_followup(ctx, company_id: int, date: str, note: str = "") -> dict:  # noqa: A002
    return ctx.business.set_followup(company_id, date, note)


@_guard
def _kpis(ctx, period: str = "today", compare_with: str = "") -> dict:
    if period not in PERIODS or (compare_with and compare_with not in PERIODS):
        return {"error": f"period must be one of {', '.join(PERIODS)}"}
    if compare_with:
        return ctx.business.compare(period, compare_with)
    return {"kpis": ctx.business.kpis(period), "note": "Calculated only from activities she reported. Nothing estimated."}


def _research_runs_7d(ctx) -> int:
    if ctx.research is None:
        return 0
    return int(ctx.research.db.execute("SELECT COUNT(*) FROM research_runs WHERE ts >= ?",
                                       (time.time() - 7 * 86400,)).fetchone()[0])


@_guard
def _status(ctx) -> dict:
    return ctx.business.status(_research_runs_7d(ctx))


@_guard
def _daily_check(ctx) -> dict:
    today = ctx.business.kpis("today")
    acts = [a for a in ctx.business.recent_activities(50) if a["ts"].startswith(date.today().isoformat())]
    ask: list[str] = []
    market_facing = today["outreach"] + today["follow_ups"] + today["replies"] + today["discovery_completed"]
    if not acts:
        ask.append("Nothing logged today yet: ask briefly what market-facing work happened (outreach, replies, calls, proposals).")
    if today["outreach"] and not today["replies"]:
        ask.append("Outreach was logged but no replies: ask if anyone answered.")
    if today["discovery_booked"] or ctx.business.pipeline()["by_stage"].get("DISCOVERY"):
        ask.append("A discovery is booked or open: ask how it went if it took place today.")
    return {"today": today, "logged_today": acts, "status": ctx.business.status(_research_runs_7d(ctx)),
            "ask_only": ask[:3] or ["Everything relevant seems logged: don't ask questions, just give the review."],
            "instruction": "Start from what is already known. Ask only the questions in ask_only, briefly, then log "
            "what she reports and give a short review and the next most useful action."}


@_guard
def _set_target(ctx, metric: str, value: float | None = None, period: str = "week") -> dict:
    return ctx.business.set_target(metric, value, period)


def _prepare_remove(ctx, activity_id: int) -> dict:
    a = ctx.business.activity(activity_id) if ctx.business is not None else None
    if not a or a["removed"]:
        return {"error": f"no activity {activity_id}"}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a["ts"]))
    return {"summary": f"remove the logged activity #{a['id']} ({a['type']}" + (f", {a['company']}" if a.get("company") else "")
            + f", {when}) from the business numbers", "plan": {"activity_id": int(activity_id)}}


def _remove(ctx, plan: dict) -> dict:
    res = ctx.business.remove_activity(plan["activity_id"])
    return {**res, "status": "done" if res["status"] == "removed" else "failed"}


# ---------------------------------------------------------------------- registration
def register(reg, Tool) -> None:
    depth = {"type": "string", "enum": ["standard", "deep"],
             "description": "deep = slower, much more expensive multi-step research; only if she asks for an in-depth study"}
    refresh = {"type": "boolean", "description": "ignore a recent stored result and research again (costs a new call)"}
    region = {"type": "string", "description": "default Italia"}
    reg.register(Tool(
        "research_market",
        "LIVE WEB RESEARCH (paid, slow ~30-90 s): market/industry research for her B2B automation business — "
        "workflows, pain points, AI adoption, buyers, competitors, pricing evidence, constraints, opportunities. "
        "Use only when current external evidence is genuinely needed; never for her own data (pipeline, KPIs, "
        "calendar, memory). Returns sourced findings labelled FACT/INFERENCE/HYPOTHESIS/UNKNOWN.",
        _obj({"topic": {"type": "string"}, "focus": {"type": "string", "description": "optional: the specific aspects to cover"},
              "region": region, "depth": depth, "refresh": refresh}, ["topic"]),
        _research_market,
    ))
    reg.register(Tool(
        "research_compare_niches",
        "LIVE WEB RESEARCH (paid): sourced comparison of 2-5 niches as targets for her business. No external "
        "scores: any scoring you add is your internal framework.",
        _obj({"niches": {"type": "array", "items": {"type": "string"}}, "region": region,
              "criteria": {"type": "string"}, "refresh": refresh}, ["niches"]),
        _research_compare,
    ))
    reg.register(Tool(
        "research_find_companies",
        "LIVE WEB RESEARCH (paid): find real companies in a niche (max 20) and save them to her pipeline (stage "
        "RESEARCH) when she asked for a list to work on. Separates companies with public evidence of the specific "
        "problem from companies that only match the general ICP. Unsupported fields stay UNKNOWN; no emails/phones.",
        _obj({"niche": {"type": "string"}, "problem": {"type": "string"}, "region": region,
              "count": {"type": "integer"}, "save": {"type": "boolean"}, "refresh": refresh}, ["niche"]),
        _research_find_companies,
    ))
    reg.register(Tool(
        "research_company",
        "LIVE WEB RESEARCH (paid): public research on one company (what they do, visible manual processes, software, "
        "signs of pain), e.g. before outreach or a discovery call.",
        _obj({"name": {"type": "string"}, "website": {"type": "string"}, "question": {"type": "string"}, "refresh": refresh}, ["name"]),
        _research_company,
    ))
    reg.register(Tool(
        "research_check_claim",
        "LIVE WEB RESEARCH (paid): check whether a specific market claim is supported by current sources.",
        _obj({"claim": {"type": "string"}, "refresh": refresh}, ["claim"]),
        _research_check_claim,
    ))
    reg.register(Tool(
        "research_history",
        "Free: list recent research runs stored locally (or one run in full with run_id) — check here before paying "
        "for new research on the same question.",
        _obj({"limit": {"type": "integer"}, "run_id": {"type": "integer"}}),
        _research_history,
    ))
    reg.register(Tool(
        "business_add_company",
        "Add a company she names to her pipeline (local). Duplicates are merged.",
        _obj({"name": {"type": "string"}, "website": {"type": "string"}, "location": {"type": "string"},
              "industry": {"type": "string"}, "notes": {"type": "string"},
              "stage": {"type": "string", "enum": list(STAGES)}}, ["name"]),
        _add_company,
    ))
    reg.register(Tool(
        "business_list_companies",
        "Her pipeline (local, free): companies by stage or search, with the known pipeline value.",
        _obj({"stage": {"type": "string", "enum": list(STAGES)}, "query": {"type": "string"}, "limit": {"type": "integer"}}),
        _list_companies,
    ))
    reg.register(Tool(
        "business_company",
        "One company from her pipeline with its contacts, notes, sources and activity history (local).",
        _obj({"company_id": {"type": "integer"}, "name": {"type": "string"}}),
        _company,
    ))
    reg.register(Tool(
        "business_log_activity",
        "Record something she reports having done or received (local): research, outreach, reply, positive_reply, "
        "follow_up, discovery_booked, discovery_completed, proposal_sent, won, lost, note. Moves the company's stage "
        "forward automatically; unknown companies are added. amount_eur ONLY if she states an amount. count for "
        "several untracked actions (e.g. 10 LinkedIn messages without names). followup_date (YYYY-MM-DD) to set "
        "the next follow-up.",
        _obj({"type": {"type": "string", "enum": list(ACTIVITY_TYPES)}, "company": {"type": "string"},
              "company_id": {"type": "integer"}, "channel": {"type": "string", "enum": list(CHANNELS)},
              "amount_eur": {"type": "number"}, "minutes": {"type": "integer"}, "count": {"type": "integer"},
              "notes": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DDTHH:MM if not now"},
              "followup_date": {"type": "string"}}, ["type"]),
        _log_activity,
    ))
    reg.register(Tool(
        "business_set_stage",
        "Set a company's pipeline stage explicitly (e.g. not interested → LOST, ready → TO_CONTACT).",
        _obj({"company_id": {"type": "integer"}, "stage": {"type": "string", "enum": list(STAGES)},
              "reason": {"type": "string"}}, ["company_id", "stage"]),
        _set_stage,
    ))
    reg.register(Tool(
        "business_update_company",
        "Update a company's details (local). value_eur only when she states an amount. A contact only if she gives it "
        "or it is publicly sourced.",
        _obj({"company_id": {"type": "integer"}, "website": {"type": "string"}, "location": {"type": "string"},
              "industry": {"type": "string"}, "notes": {"type": "string"}, "value_eur": {"type": "number"},
              "value_note": {"type": "string"}, "contact_name": {"type": "string"}, "contact_role": {"type": "string"},
              "contact_url": {"type": "string"}}, ["company_id"]),
        _update_company,
    ))
    reg.register(Tool(
        "business_set_followup",
        "Set (or clear with an empty date) the next follow-up date for a company (local). To also get a reminder or "
        "calendar block, use the reminders/calendar tools (they need her confirmation).",
        _obj({"company_id": {"type": "integer"}, "date": {"type": "string"}, "note": {"type": "string"}}, ["company_id", "date"]),
        _set_followup,
    ))
    reg.register(Tool(
        "business_kpis",
        "Business numbers calculated from her recorded activities (local, free): outreach, replies, response rate, "
        "discovery calls, proposals, wins, revenue, known values. compare_with for e.g. this_week vs last_week.",
        _obj({"period": {"type": "string", "enum": list(PERIODS)}, "compare_with": {"type": "string", "enum": list(PERIODS)}}),
        _kpis,
    ))
    reg.register(Tool(
        "business_status",
        "Execution context (local, free): pipeline by stage, last 7 days, overdue follow-ups, research activity and "
        "plain observations. Use before recommending what to work on or planning business time.",
        _obj({}),
        _status,
    ))
    reg.register(Tool(
        "business_daily_check",
        "'Facciamo il check della giornata': what is already logged today, the status, and the FEW questions worth asking.",
        _obj({}),
        _daily_check,
    ))
    reg.register(Tool(
        "business_set_kpi_target",
        "Store a KPI target she explicitly sets (e.g. 20 outreach per week). Never invent targets. value omitted = remove.",
        _obj({"metric": {"type": "string"}, "value": {"type": "number"},
              "period": {"type": "string", "enum": ["day", "week", "month"]}}, ["metric"]),
        _set_target,
    ))
    reg.register(Tool(
        "business_remove_activity",
        "Remove a wrongly logged activity from the numbers (kept for audit). Requires her confirmation.",
        _obj({"activity_id": {"type": "integer"}}, ["activity_id"]),
        _remove, mutates=True, prepare=_prepare_remove,
    ))
