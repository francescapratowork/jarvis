"""Assistant settings, read from the environment (.env is loaded by jarvis.py).

API keys are only ever reported as "set" / "missing" — never printed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _str(name: str, default: str = "") -> str:
    v = (os.environ.get(name) or "").strip()
    return v if v else default


def _bool(name: str, default: bool) -> bool:
    v = _str(name).lower()
    return default if not v else v in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class AssistantConfig:
    enabled: bool
    autostart: bool
    timeout_s: float
    music_volume: int | None
    user_name: str
    # Claude
    llm_model: str
    llm_effort: str
    llm_fallbacks: str
    # ElevenLabs
    stt_model: str
    stt_vad_silence_s: float
    stt_language: str
    tts_model: str
    voice_id: str
    # storage / integrations
    data_dir: Path
    calendar_backend: str

    @property
    def anthropic_key_set(self) -> bool:
        return bool(_str("ANTHROPIC_API_KEY"))

    @property
    def elevenlabs_key_set(self) -> bool:
        return bool(_str("ELEVENLABS_API_KEY"))

    @staticmethod
    def elevenlabs_key() -> str:
        return _str("ELEVENLABS_API_KEY")

    def missing_requirements(self) -> list[str]:
        missing = []
        if not self.anthropic_key_set:
            missing.append("ANTHROPIC_API_KEY")
        if not self.elevenlabs_key_set:
            missing.append("ELEVENLABS_API_KEY")
        if not self.voice_id:
            missing.append("ELEVENLABS_VOICE_ID")
        return missing


def load_config() -> AssistantConfig:
    music = _str("JARVIS_CONVERSATION_MUSIC_VOLUME", "25")
    return AssistantConfig(
        enabled=_bool("JARVIS_CONVERSATION_ENABLED", True),
        autostart=_bool("JARVIS_CONVERSATION_AUTOSTART", True),
        timeout_s=max(10.0, _float("JARVIS_CONVERSATION_TIMEOUT_S", 60.0)),
        music_volume=None if music.lower() in ("", "off", "none") else max(0, min(100, _int("JARVIS_CONVERSATION_MUSIC_VOLUME", 25))),
        user_name=_str("JARVIS_USER_NAME", "Miss Prato"),
        llm_model=_str("JARVIS_LLM_MODEL", "claude-opus-5-5"),
        llm_effort=_str("JARVIS_LLM_EFFORT", "low"),
        llm_fallbacks=_str("JARVIS_LLM_FALLBACKS", "default"),
        stt_model=_str("JARVIS_STT_MODEL", "scribe_v2_realtime"),
        stt_vad_silence_s=min(3.0, max(0.3, _float("JARVIS_STT_VAD_SILENCE_S", 0.7))),
        stt_language=_str("JARVIS_STT_LANGUAGE", ""),
        tts_model=_str("JARVIS_REPLY_TTS_MODEL", "eleven_flash_v2_5"),
        voice_id=_str("ELEVENLABS_VOICE_ID"),
        data_dir=Path(_str("JARVIS_DATA_DIR", str(ROOT / "data"))).expanduser(),
        calendar_backend=_str("JARVIS_CALENDAR_BACKEND", "auto").lower(),
    )
