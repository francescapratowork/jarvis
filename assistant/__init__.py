"""
Jarvis conversational assistant (Phase 2A).

Runs after the protected startup sequence (clap → interface → Spotify → welcome →
AWAITING COMMAND) and turns Jarvis into a voice assistant:

  LISTENING (ElevenLabs Scribe v2 Realtime) → THINKING (Claude + tools)
  → SPEAKING (ElevenLabs, Bella's voice) → LISTENING …

Modules:
  config        settings from .env (keys are never printed or logged)
  memory        local long-term memory v2 (SQLite, data/jarvis_memory.db): kinds, status,
                replacement (superseding) and the per-turn working set
  onboarding    one-time, repeatable profile import (dry run → --apply)
  calendar      macOS Calendar/Reminders access (via calendar_helper subprocess)
  operations    confirmed calendar/reminder changes: routing, plans, action log, undo
  research      live web research (Perplexity Agent API): validation, sources, cache, budget
  business      local pipeline (companies, contacts, activities), KPIs, execution context
  business_tools  the research and business tools Claude can use
  tools         tool registry: every capability is one tool, read or write
  brain         Claude conversation loop, confirmations, sentence streaming
  speech        microphone, realtime speech-to-text, streaming text-to-speech
  conversation  the LISTENING/THINKING/SPEAKING state machine
  activation    what (re)starts a conversation: the interface now, a wake word later
"""
