"""Assembles the assistant: memory, calendar, tools, brain, speech, conversation."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .activation import InterfaceActivation
from .brain import Brain
from .calendar import CalendarService, check_calendar_report
from .config import AssistantConfig, load_config
from .memory import MemoryStore
from .operations import AREAS, OpsStore, resolve_calendar, resolve_reminder_list
from .tools import ToolContext, build_registry, open_mac_app

log = logging.getLogger("jarvis.assistant")


def build_brain(cfg: AssistantConfig, on_log=lambda text: None, client=None) -> Brain:
    memory = MemoryStore(cfg.data_dir / "jarvis_memory.db")
    ctx = ToolContext(
        memory=memory,
        calendar=CalendarService(cfg.calendar_backend),
        open_app=open_mac_app,
        log=on_log,
        actions=OpsStore(memory),
    )
    return Brain(cfg, memory, build_registry(), ctx, client=client)


def start_conversation(ui, music=None):
    """Start the voice assistant after the welcome. Returns the Conversation, or None if it
    can't run yet (a log line says why — no key values are ever printed)."""
    cfg = load_config()
    if not cfg.enabled:
        log.info("Conversation: off (JARVIS_CONVERSATION_ENABLED=false).")
        return None
    missing = cfg.missing_requirements()
    if missing:
        log.info(
            "Conversation: not available yet — missing in .env: %s. Jarvis stays on AWAITING COMMAND.",
            ", ".join(missing),
        )
        return None
    try:
        from .conversation import Conversation
        from .speech import Speaker

        brain = build_brain(cfg, on_log=lambda text: ui.send(log=text[:140]))
        speaker = Speaker(cfg.elevenlabs_key(), cfg.voice_id, cfg.tts_model,
                          on_start=lambda: None, on_level=lambda level: None)
        conv = Conversation(cfg, ui, brain, speaker, [InterfaceActivation(ui)], music=music)
    except Exception as e:  # noqa: BLE001
        log.warning("Conversation: could not start (%s: %s).", type(e).__name__, e)
        return None
    log.info(
        "Conversation: ready (model %s, effort %s; speech-to-text %s; voice model %s). "
        "Silence for %d s returns to AWAITING COMMAND; press Space to talk again.",
        cfg.llm_model, cfg.llm_effort, cfg.stt_model, cfg.tts_model, int(cfg.timeout_s),
    )
    conv.start()
    return conv


def run_text_chat() -> int:
    """`./start_jarvis.sh --chat`: talk to Jarvis's brain by typing (no microphone/voice)."""
    cfg = load_config()
    if not cfg.anthropic_key_set:
        print("ANTHROPIC_API_KEY is missing in .env — add it first (see README).")
        return 1
    brain = build_brain(cfg, on_log=lambda text: print(f"   · {text}"))
    print(f"Jarvis text chat (model {cfg.llm_model}, effort {cfg.llm_effort}). Type 'exit' to quit.\n")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in ("exit", "quit", "esci"):
            return 0
        if not text:
            continue
        sentences: list[str] = []
        result = brain.respond(text, sentences.append)
        if result.ignored:
            print("Jarvis: (ignored — not addressed to Jarvis)\n")
        else:
            print("Jarvis: " + " ".join(sentences) + "\n")
        if result.ended:
            return 0


def run_check_calendar() -> int:
    print("Checking access to your Calendar and Reminders...")
    print("(If macOS asks for permission, click OK / Allow.)\n")
    ok, lines = check_calendar_report()
    for line in lines:
        print(line)
    return 0 if ok else 1


def _memory_store():
    cfg = load_config()  # only JARVIS_DATA_DIR is used here; keys are never read out or printed
    path = cfg.data_dir / "jarvis_memory.db"
    store = MemoryStore(path)
    if store.backup_path is not None:
        print(f"Memory upgraded to v2 (backup of the previous database: {store.backup_path})")
    return cfg, store


def run_import_profile(args: list[str]) -> int:
    """`./start_jarvis.sh --import-profile [file] [--apply]` (dry run unless --apply)."""
    from .onboarding import OnboardingError, run

    cfg, store = _memory_store()
    files = [a for a in args if not a.startswith("--")]
    path = Path(files[0]).expanduser() if files else cfg.data_dir / "onboarding.toml"
    try:
        lines = run(store, path, do_apply="--apply" in args)
    except OnboardingError as e:
        print(f"Memory onboarding: {e}")
        return 1
    print("\n".join(lines))
    return 0


def run_show_memory(args: list[str]) -> int:
    """`./start_jarvis.sh --show-memory [--history]`: what Jarvis currently knows."""
    from .persona import context_note

    cfg, store = _memory_store()
    counts = store.counts_by_status()
    print(f"Memory database: {store.path} (schema v{store.schema_version()})")
    print("Memories by status: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
    print()
    print("What Jarvis is given on every turn (the working set):")
    print(context_note("(now)", store.working_set(), cfg.user_name))
    if "--history" in args:
        print()
        print("History (superseded / archived / completed — never used for current priorities):")
        rows = [m for m in store.recall("", limit=25, include_history=True) if m["status"] not in ("active", "future", "paused")]
        for m in rows:
            print(f"- [#{m['id']} {m['kind']} · {m['status'].upper()}" + (f" → #{m['superseded_by']}" if m.get("superseded_by") else "")
                  + f"] {m['content']}")
        if not rows:
            print("- none")
    return 0


def run_calendars(args: list[str]) -> int:
    """`./start_jarvis.sh --calendars`: calendars and lists Jarvis can write into, and the
    calendar set for each life area.
    `./start_jarvis.sh --set-calendar <area> <calendar name>` (area: business, personal,
    equestrian, growth, general) and `--set-reminder-list <area> <list name>`."""
    from types import SimpleNamespace

    from .calendar import CalendarError
    from .operations import _label

    cfg, store = _memory_store()
    actions = OpsStore(store)
    calendar = CalendarService(cfg.calendar_backend)
    ctx = SimpleNamespace(calendar=calendar, actions=actions)
    for flag, kind in (("--set-calendar", "calendar"), ("--set-reminder-list", "reminders")):
        if flag in args:
            rest = args[args.index(flag) + 1:]
            if len(rest) < 2 or rest[0] not in AREAS:
                print(f"Usage: ./start_jarvis.sh {flag} <{'|'.join(AREAS)}> <name>")
                return 1
            area, name = rest[0], " ".join(rest[1:])
            try:
                target = (resolve_reminder_list if kind == "reminders" else resolve_calendar)(ctx, area, name)
            except CalendarError as e:
                print(f"Calendar: {e}")
                return 1
            if "status" in target:
                print(f"Not saved: {target.get('error', target['status'])}")
                for key in ("choose_one_of", "writable_calendars", "reminder_lists"):
                    if target.get(key):
                        print("  Choose one of: " + "; ".join(target[key]))
                return 1
            actions.set_route(kind, area, target)
            print(f"Saved: {area} → {_label(target)}")
            return 0
    try:
        data = calendar.calendars()
    except CalendarError as e:
        print(f"Calendar: {e}")
        return 1
    print("Calendars Jarvis can write into:")
    for c in data["calendars"]:
        if c["writable"]:
            print(f"  ✅ {_label(c)}" + ("   (default in the Calendar app)" if c.get("default") else ""))
    print("Read-only calendars (Jarvis never writes into these):")
    for c in data["calendars"]:
        if not c["writable"]:
            print(f"  🔒 {_label(c)}  [{c['type']}]")
    print("Reminder lists:")
    for c in data.get("reminder_lists", []):
        print(f"  {'✅' if c['writable'] else '🔒'} {_label(c)}" + ("   (default in Reminders)" if c.get("default") else ""))
    print()
    routes, lists = actions.routes("calendar"), actions.routes("reminders")
    print("Calendar for each life area:")
    for area in AREAS:
        r = routes.get(area)
        fallback = "→ uses 'general'" if area != "general" else "→ Jarvis will ask"
        print(f"  {area:<10} {_label(r) if r else 'not set ' + fallback}")
    print("Reminder list for each life area:")
    for area in AREAS:
        r = lists.get(area)
        print(f"  {area:<10} {_label(r) if r else 'not set → the default list in Reminders'}")
    return 0


if __name__ == "__main__":
    sys.exit(run_check_calendar())
