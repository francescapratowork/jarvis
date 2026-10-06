"""
Jarvis conversational assistant (Phase 2A).

Runs after the protected startup sequence (clap → interface → Spotify → welcome →
AWAITING COMMAND) and turns Jarvis into a voice assistant:

  LISTENING (ElevenLabs Scribe v2 Realtime) → THINKING (Claude + tools)
  → SPEAKING (ElevenLabs, Bella's voice) → LISTENING …

Modules:
  config        settings from .env (keys are never printed or logged)
  memory        local long-term memory (SQLite, data/jarvis_memory.db)
  calendar      macOS Calendar/Reminders access (via calendar_helper subprocess)
  tools         tool registry: every capability is one tool, read or write
  brain         Claude conversation loop, confirmations, sentence streaming
  speech        microphone, realtime speech-to-text, streaming text-to-speech
  conversation  the LISTENING/THINKING/SPEAKING state machine
  activation    what (re)starts a conversation: the interface now, a wake word later
"""
