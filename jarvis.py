#!/usr/bin/env python3
"""
Jarvis for macOS: listens to your Mac microphone and, on a double clap, opens the full-screen
Jarvis interface and runs a welcome sequence (Spotify track in the background, ElevenLabs voice).

Run:
  ./start_jarvis.sh            (first run creates a virtualenv and installs dependencies)
  # or manually:
  python3 -m pip install -r requirements.txt
  python3 jarvis.py

Everything a user is likely to change can be set in a `.env` file next to this script
(see `.env.example`). The constants below are only the defaults.

Clap detection:
  Production: calibrate → wait for ONE valid double clap → close the microphone → run the
  welcome once → keep the interface open until the user closes it. `./start_jarvis.sh --test` keeps listening and only logs claps.
  The threshold is room noise × JARVIS_SPIKE_RATIO (re-measured continuously), but never below
  JARVIS_MIN_CLAP_PEAK. Each loud sound is then checked: short (voice/music last longer),
  energetic (40 ms rms ≥ JARVIS_MIN_CLAP_RMS; clicks/keys are thin), bright (voices/thumps
  are low-pitched). The two claps must be 0.15–0.8 s apart with quiet around them, which
  rejects typing and repeated noises. Every decision is logged with its measurements.

  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size (claps are only ~10 ms long, so keep it short).
  SPIKE_RATIO   — how many times louder than the room noise a clap peak must be;
                    raise if false triggers; lower if claps are missed.
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  MIN_CLAP_PEAK / MAX_CLAP_THRESHOLD — absolute limits for the automatic threshold.
  MIN_CLAP_RMS / CLAP_RMS_RATIO / MIN_HF_RATIO / MAX_CLAP_LEN_S / QUIET_BEFORE_S / QUIET_AFTER_S — clap shape
                    and isolation checks.

Actions (macOS):
  SONG_URI      — Spotify or YouTube URL/URI (env JARVIS_SONG_URI). Spotify links are played in
                    the Spotify app via AppleScript; anything else opens in the default browser.
  JARVIS_UI_ENABLED — full-screen animated interface (ui/index.html in a native window, run by
    jarvis_ui.py as a separate process; states STARTING/ONLINE/LISTENING/THINKING/SPEAKING).
    `./start_jarvis.sh --ui-demo` previews it without microphone, music or voice.
  No work apps or websites are opened automatically. The Chrome/Cursor helpers
    (open_claude_in_chrome, open_tasaradar_in_chrome, open_cursor_window) remain for future
    voice commands but are not called.
  JARVIS_WELCOME_* — TTS after the song (ElevenLabs), played through the default output device.
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/` (WAV) and
    replayed when phrase + voice + model + format match—no repeat API call. Delete that folder
    or set JARVIS_WELCOME_CACHE_ENABLED=false to force a fresh fetch.
  The welcome sequence runs only once per process: the interface opens, Spotify starts hidden
    at full volume, after JARVIS_MUSIC_LEAD_IN_SECONDS it ducks (its own volume only), the
    voice speaks over it, then the music fades up and the interface shows AWAITING COMMAND.
  JARVIS_MUSIC_LEAD_IN_SECONDS / JARVIS_SPOTIFY_DUCK_VOLUME / _DUCK_FADE_SECONDS /
    _NORMAL_VOLUME / _RESTORE_PREVIOUS / _FADE_SECONDS — audio ducking (see .env.example).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# Bump on every release so the startup log shows which code is actually running.
JARVIS_VERSION = "2026-10-06.13 (Phase 2A: voice conversation, memory, calendar read)"
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

IS_MAC = sys.platform == "darwin"


def _env_str(name: str, default: str = "") -> str:
    v = (os.environ.get(name) or "").strip()
    return v if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)))
    except ValueError:
        return default


# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = _env_int("JARVIS_SAMPLE_RATE", 44100)
BLOCK_MS = 20
CHANNELS = 1

# A clap's peak must be this many times the room-noise peak (auto-calibrated)...
SPIKE_RATIO = _env_float("JARVIS_SPIKE_RATIO", 5.0)
# ...and never below this absolute peak, however quiet the room is (full scale = 1.0).
# Real MacBook logs showed the adaptive threshold sinking to 0.03 in a quiet room, where
# speech, typing and clicks then counted as claps. A hand clap at desk distance peaks well
# above 0.15; raise this if sounds still trigger, lower it if your claps are ignored.
MIN_CLAP_PEAK = _env_float("JARVIS_MIN_CLAP_PEAK", 0.15)
MAX_CLAP_THRESHOLD = 0.6  # cap, so a loud clap still works in a noisy room
MIN_DOUBLE_GAP_S = 0.15
MAX_DOUBLE_GAP_S = _env_float("JARVIS_MAX_DOUBLE_GAP_S", 0.8)
RETRIGGER_RATIO = 0.6  # a sound has ended once below threshold * this (or its own decay)
ONSET_RISE = 3.0  # a new sound must be this many times louder than the previous 20 ms,
#                   so a loud clap's echo/decay is never mistaken for a new sound
# Shape checks that tell a clap from other sounds. Tuned on real MacBook claps, which measured
# peak 10.7-18.1, rms 1.16-2.34 (40 ms), 45-90% above 1 kHz, but only 2-3 ms "wide": a clap is
# a very sharp spike followed by a quieter body, so width is NOT a usable test on this mic.
MAX_CLAP_LEN_S = 0.2  # claps die away fast; longer sounds are voice/music
CLAP_DECAY_RATIO = 0.3  # "died away" = block peak below this fraction of the clap's peak
# Energy test ("too little energy"): rms over 40 ms around the peak must reach
# max(JARVIS_MIN_CLAP_RMS, CLAP_RMS_RATIO x room rms). The 0.3 floor is about 4x below the
# weakest real clap measured (1.16) and does not drop in a quiet room, because claps don't
# get quieter when the room does; clicks, taps and keys carry far less energy.
MIN_CLAP_RMS = _env_float("JARVIS_MIN_CLAP_RMS", 0.3)
CLAP_RMS_RATIO = _env_float("JARVIS_CLAP_RMS_RATIO", 20.0)
MIN_HF_RATIO = 0.3  # share of energy above 1 kHz; voices and thumps are lower-pitched
QUIET_BEFORE_S = 0.4  # no other loud sound just before clap 1 (rejects typing/speech)
QUIET_AFTER_S = 0.35  # ...nor just after clap 2
NOISE_WINDOW_S = 3.0  # room noise = median block peak over this many recent seconds
CALIBRATION_S = 1.5  # measure the room before listening
LEVEL_LOG_S = _env_float("JARVIS_LEVEL_LOG_S", 5.0)  # 0 = no periodic level line
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# Spotify: "spotify:track:TRACK_ID" or https://open.spotify.com/track/...
# YouTube: https://www.youtube.com/watch?v=...
SONG_URI = _env_str(
    "JARVIS_SONG_URI",
    "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82",
)
# Legacy setting (0–100); now only used as the default for JARVIS_SPOTIFY_NORMAL_VOLUME.
SPOTIFY_VOLUME = _env_str("SPOTIFY_VOLUME")


def _volume(v: int) -> int:
    return max(0, min(100, v))


# Audio ducking (Spotify's own volume, not the Mac's): the song starts at full volume,
# after a short lead-in it ducks under the spoken welcome (still audible), then rises again.
SPOTIFY_DUCK_VOLUME = _volume(_env_int("JARVIS_SPOTIFY_DUCK_VOLUME", 35))
SPOTIFY_NORMAL_VOLUME = _volume(
    _env_int(
        "JARVIS_SPOTIFY_NORMAL_VOLUME", int(SPOTIFY_VOLUME) if SPOTIFY_VOLUME.isdigit() else 65
    )
)
# Seconds the song plays at full volume before ducking (so its beginning is clearly heard).
MUSIC_LEAD_IN_SECONDS = max(0.0, _env_float("JARVIS_MUSIC_LEAD_IN_SECONDS", 7.0))
# Seconds for the duck (full → duck volume); the voice starts once it's done.
SPOTIFY_DUCK_FADE_SECONDS = max(0.0, _env_float("JARVIS_SPOTIFY_DUCK_FADE_SECONDS", 0.5))
# Seconds for the rise back to full volume after the voice.
SPOTIFY_FADE_SECONDS = max(0.0, _env_float("JARVIS_SPOTIFY_FADE_SECONDS", 2.0))
# True = fade back up to the volume Spotify had before Jarvis started (if it was louder than
# the duck level); False = always fade up to JARVIS_SPOTIFY_NORMAL_VOLUME.
SPOTIFY_RESTORE_PREVIOUS = _env_bool("JARVIS_SPOTIFY_RESTORE_PREVIOUS", True)

# Cursor: bring existing instance to the front. Set OPEN_NEW_CURSOR_ON_DOUBLE_CLAP for a new window as well.
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = _env_bool("FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP", True)
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = _env_bool("OPEN_NEW_CURSOR_ON_DOUBLE_CLAP", False)
CURSOR_OPEN_FULLSCREEN = _env_bool("CURSOR_OPEN_FULLSCREEN", True)

# Google Chrome (fallback: default browser). URLs overridable in .env.
CLAUDE_CODE_URL = _env_str("CLAUDE_CODE_URL", "https://claude.ai/new")
# TASARADAR_URL wins; BINANCE_BTC_URL is still honoured as a fallback for older .env files.
TASARADAR_URL = _env_str("TASARADAR_URL") or _env_str("BINANCE_BTC_URL") or "https://tasaradar.com"
OPEN_CLAUDE_CODE_IN_CHROME = _env_bool("OPEN_CLAUDE_CODE_IN_CHROME", True)
OPEN_TASARADAR_IN_CHROME = _env_bool(
    "OPEN_TASARADAR_IN_CHROME", _env_bool("OPEN_BINANCE_BTC_IN_CHROME", True)
)
OPEN_CHROME_FULLSCREEN = _env_bool("OPEN_CHROME_FULLSCREEN", True)
# False = default Chrome profile (your normal user, extensions, cookies). True = temp dirs per site.
CHROME_SEPARATE_SITE_PROFILES = _env_bool("CHROME_SEPARATE_SITE_PROFILES", False)
# Which display (1 = leftmost/top-first after sorting). Falls back to the last display if absent.
CLAUDE_CHROME_MONITOR = _env_int("CLAUDE_CHROME_MONITOR", 1)
TASARADAR_CHROME_MONITOR = _env_int(
    "TASARADAR_CHROME_MONITOR", _env_int("BINANCE_CHROME_MONITOR", 3)
)

JARVIS_WELCOME_ENABLED = _env_bool("JARVIS_WELCOME_ENABLED", True)
JARVIS_WELCOME_PHRASE = _env_str(
    "JARVIS_WELCOME_PHRASE", "Welcome home, sir. All systems are online."
)
# Full-screen Jarvis interface (ui/index.html in a native window, see jarvis_ui.py).
JARVIS_UI_ENABLED = _env_bool("JARVIS_UI_ENABLED", True)

# Seconds between the music starting and the voice starting, used only when the music can't
# be ducked (YouTube/web player links). With the Spotify app the voice starts right after the
# lead-in + duck.
JARVIS_AFTER_SONG_DELAY_S = _env_float("JARVIS_AFTER_SONG_DELAY_S", 0.5)
# Save ElevenLabs PCM as WAV under .cache/jarvis_welcome/; replay skips the API when the key matches.
JARVIS_WELCOME_CACHE_ENABLED = _env_bool("JARVIS_WELCOME_CACHE_ENABLED", True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("jarvis")

MAC_MIC_HINT = (
    "On macOS, allow microphone access: System Settings → Privacy & Security → Microphone → "
    "turn on your Terminal app, then quit Terminal completely (Cmd+Q) and start Jarvis again."
)


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def peak_mono(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.max(np.abs(block)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check the input level in System Settings → Sound → Input, "
                "or try another JARVIS_INPUT_DEVICE.",
                peak,
            )
            if IS_MAC:
                log.warning("%s", MAC_MIC_HINT)
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if IS_MAC:
        log.warning("Every microphone sounds silent. %s", MAC_MIC_HINT)
    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.wav"


def _read_pcm_wav_file(path: Path) -> tuple[np.ndarray, int] | None:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return None
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return None
    if not raw:
        return None
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


class JarvisUI:
    """Client for the full-screen interface process (jarvis_ui.py).

    Messages go to the child as JSON lines on its stdin; the child answers with JSON event
    lines on its stdout ("ready", "focused", "refocused"). Every method is a safe no-op
    when the interface is disabled, failed to start, or was closed by the user.
    """

    READY_TIMEOUT_S = 20.0
    FOCUS_TIMEOUT_S = 4.0

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._reported_dead = False
        self._ready = threading.Event()
        self.ready_info: dict = {}
        self._acks: dict[int, dict] = {}
        self._ack_cond = threading.Condition()
        self._next_ack = 0
        self._t_launch = 0.0
        self._subscribers: dict[str, list] = {}

    def on_event(self, kind: str, callback) -> None:
        """Call `callback(event)` for every event of this kind from the interface
        (e.g. "activate" when Space or the mic button is pressed)."""
        self._subscribers.setdefault(kind, []).append(callback)

    def start(self) -> bool:
        if not JARVIS_UI_ENABLED:
            return False
        script = Path(__file__).resolve().parent / "jarvis_ui.py"
        if not script.is_file():
            log.warning("Jarvis interface: %s is missing — skipping the interface.", script.name)
            return False
        log.info("Jarvis interface: launching...")
        self._t_launch = time.monotonic()
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            log.warning("Jarvis interface: could not start (%s).", e)
            self.proc = None
            return False
        threading.Thread(target=self._read_events, daemon=True).start()
        threading.Thread(target=self._telemetry, daemon=True).start()
        return True

    def _read_events(self) -> None:
        proc = self.proc
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event") if isinstance(ev, dict) else None
            if kind == "ready":
                self.ready_info = ev
                self._ready.set()
            elif kind == "focused":
                with self._ack_cond:
                    self._acks[ev.get("ack")] = ev
                    self._ack_cond.notify_all()
            elif kind == "refocused":
                log.info(
                    "Jarvis interface: %s took focus — brought Jarvis back to the front.",
                    ev.get("from") or "another app",
                )
            for callback in self._subscribers.get(kind or "", []):
                try:
                    callback(ev)
                except Exception as e:  # noqa: BLE001
                    log.warning("Jarvis interface: event handler failed (%s).", e)
        # Child exited: wake anyone still waiting for it.
        self._ready.set()
        with self._ack_cond:
            self._ack_cond.notify_all()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until the interface reports it is loaded, full screen and in front."""
        if self.proc is None:
            return False
        timeout = self.READY_TIMEOUT_S if timeout is None else timeout
        if not self._ready.wait(timeout):
            log.warning(
                "Jarvis interface: not ready after %.0fs — continuing without waiting.", timeout
            )
            return False
        info = self.ready_info
        if not info:  # the child exited before becoming ready
            self.alive()
            return False
        took = time.monotonic() - self._t_launch
        if info.get("fullscreen") and info.get("active"):
            log.info("Jarvis interface: fullscreen and ready (%.1fs).", took)
        else:
            log.warning(
                "Jarvis interface: ready after %.1fs but fullscreen=%s, in front=%s "
                "(frontmost app: %s).",
                took,
                info.get("fullscreen"),
                info.get("active"),
                info.get("frontmost") or "?",
            )
        return True

    def alive(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is None:
            return True
        if not self._reported_dead and self.proc.returncode not in (0, None):
            self._reported_dead = True
            log.warning(
                "Jarvis interface: exited with code %s (see the message above; run "
                "./start_jarvis.sh to install missing packages).",
                self.proc.returncode,
            )
        return False

    def send(self, **msg) -> None:
        if not self.alive():
            return
        try:
            with self._lock:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def guard(self, on: bool) -> None:
        """While on, the interface takes focus back from any app that grabs it."""
        self.send(guard=bool(on))

    def ensure_focus(self, reason: str = "") -> bool:
        """Bring the interface to the front and wait until it confirms it is the active app."""
        if not self.alive():
            return False
        with self._ack_cond:
            self._next_ack += 1
            ack = self._next_ack
        self.send(focus=True, ack=ack)
        deadline = time.monotonic() + self.FOCUS_TIMEOUT_S
        with self._ack_cond:
            while ack not in self._acks and self.alive():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._ack_cond.wait(left)
            ev = self._acks.pop(ack, None)
        if ev is None:
            log.warning("Jarvis interface: no focus confirmation (%s).", reason or "focus")
            return False
        if not ev.get("active"):
            log.warning(
                "Jarvis interface: could not take the front (%s; frontmost app: %s).",
                reason or "focus",
                ev.get("frontmost") or "?",
            )
            return False
        return True

    def focus(self) -> None:
        self.ensure_focus()

    def wait_closed(self) -> None:
        if self.proc is not None:
            self.proc.wait()

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()

    def _telemetry(self) -> None:
        cpus = os.cpu_count() or 1
        while self.alive():
            try:
                self.send(cpu=round(min(1.0, os.getloadavg()[0] / cpus), 3))
            except OSError:
                pass
            time.sleep(2.0)


UI = JarvisUI()


def _voice_envelope(pcm: np.ndarray, rate: int, frame_s: float = 0.05) -> np.ndarray:
    """Loudness of the voice per 50 ms frame, scaled 0..1 (drives the SPEAKING animation)."""
    n = max(1, int(rate * frame_s))
    frames = len(pcm) // n
    if frames == 0:
        return np.zeros(1, dtype=np.float32)
    rms = np.sqrt(np.mean(pcm[: frames * n].reshape(frames, n) ** 2, axis=1))
    ref = float(np.percentile(rms, 95)) or 1.0
    return np.sqrt(np.clip(rms / ref, 0.0, 1.0)).astype(np.float32)


def _stream_voice_levels(env: np.ndarray, frame_s: float, stop: threading.Event) -> None:
    start = time.monotonic()
    while not stop.is_set():
        i = int((time.monotonic() - start) / frame_s)
        if i >= len(env):
            break
        UI.send(level=round(float(env[i]), 3))
        time.sleep(frame_s)
    UI.send(level=0)


def prepare_welcome_audio() -> tuple[np.ndarray, int] | None:
    """Load the spoken welcome (from cache, or from ElevenLabs) without playing it, so it can
    be fetched while Spotify is starting. Returns (samples, sample_rate) or None."""
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return None
    text = JARVIS_WELCOME_PHRASE.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return None

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        audio = _read_pcm_wav_file(cache_path)
        if audio is not None:
            log.info("Welcome voice: loaded from cache (%s).", cache_path.name)
            return audio
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return None
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return None
    try:
        log.info("Welcome voice: fetching from ElevenLabs...")
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS failed: %s", e)
        return None
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return None
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved welcome audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, pcm_rate


def play_welcome_audio(pcm: np.ndarray, rate: int) -> bool:
    """Play the spoken welcome through the default output and wait until it has finished."""
    log.info("Welcome voice: speaking (%.1fs)...", len(pcm) / max(1, rate))
    UI.send(state="SPEAKING", log="Voice: speaking", highlight=True)
    stop = threading.Event()
    levels = threading.Thread(
        target=_stream_voice_levels, args=(_voice_envelope(pcm, rate), 0.05, stop), daemon=True
    )
    try:
        sd.play(pcm, rate)
        levels.start()
        sd.wait()
    except Exception as e:
        log.warning("Could not play ElevenLabs audio: %s", e)
        return False
    finally:
        stop.set()
        UI.send(state="ONLINE", voice="READY", level=0)
    log.info("Welcome voice: finished.")
    UI.send(log="Voice: finished")
    return True


def say_jarvis_welcome() -> None:
    audio = prepare_welcome_audio()
    if audio is not None:
        play_welcome_audio(*audio)


# --- macOS helpers ------------------------------------------------------------


def _as_str(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(
    script: str, *, javascript: bool = False, timeout: float = 30.0
) -> tuple[bool, str, str]:
    """Run AppleScript (or JXA) via osascript. Returns (ok, stdout, stderr)."""
    if not IS_MAC:
        return False, "", "not macOS"
    args = ["osascript"]
    if javascript:
        args += ["-l", "JavaScript"]
    args.append("-")
    try:
        p = subprocess.run(
            args, input=script, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)
    return p.returncode == 0, p.stdout.strip(), p.stderr.strip()


def _log_osascript_failure(what: str, target_app: str, err: str) -> None:
    e = err.lower()
    if "-1743" in e or "not authorized to send apple events" in e:
        log.warning(
            "%s: macOS blocked automation. Open System Settings → Privacy & Security → "
            "Automation, find your Terminal app and turn on %s (and System Events).",
            what,
            target_app,
        )
    elif "-1719" in e or "-25211" in e or "assistive access" in e:
        log.warning(
            "%s: macOS needs Accessibility permission. Open System Settings → Privacy & "
            "Security → Accessibility and turn on your Terminal app, then restart Jarvis. (%s)",
            what,
            err,
        )
    else:
        log.warning("%s failed: %s", what, err or "unknown error")


def _mac_app_path(name: str) -> Path | None:
    """Locate Name.app in /Applications, ~/Applications, or via Spotlight."""
    if not IS_MAC:
        return None
    for base in (Path("/Applications"), Path.home() / "Applications"):
        p = base / f"{name}.app"
        if p.is_dir():
            return p
    try:
        out = subprocess.run(
            [
                "mdfind",
                f"kMDItemFSName == '{name}.app' && "
                "kMDItemContentType == 'com.apple.application-bundle'",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.strip():
            return Path(line.strip())
    return None


_SCREENS_JXA = r"""
ObjC.import('AppKit');
var screens = $.NSScreen.screens;
var H = screens.objectAtIndex(0).frame.size.height;
var out = [];
for (var i = 0; i < screens.count; i++) {
  var f = screens.objectAtIndex(i).visibleFrame;
  out.push([f.origin.x, H - (f.origin.y + f.size.height),
            f.origin.x + f.size.width, H - f.origin.y]);
}
JSON.stringify(out);
"""


@functools.lru_cache(maxsize=1)
def _mac_sorted_screen_rects() -> tuple[tuple[int, int, int, int], ...]:
    """Usable area of each display as (left, top, right, bottom) in AppleScript window
    coordinates (origin top-left of the main display), sorted left-to-right then top-to-bottom."""
    ok, out, err = _osascript(_SCREENS_JXA, javascript=True, timeout=10)
    if not ok:
        log.warning("Could not read display layout: %s", err)
        return ()
    try:
        rects = [tuple(int(round(v)) for v in r) for r in json.loads(out)]
    except (ValueError, TypeError):
        return ()
    rects.sort(key=lambda t: (t[0], t[1]))
    return tuple(rects)  # type: ignore[return-value]


def _screen_bounds(one_based_index: int) -> tuple[int, int, int, int] | None:
    rects = _mac_sorted_screen_rects()
    if not rects:
        return None
    idx = max(0, one_based_index - 1)
    if idx >= len(rects):
        log.info(
            "Display %d requested but only %d connected; using display %d.",
            one_based_index,
            len(rects),
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _mac_set_fullscreen(process_name: str, *, wait_s: float = 10.0) -> bool:
    """Put the front window of an app into native macOS fullscreen (idempotent).
    Requires Accessibility permission for the app running Jarvis (e.g. Terminal)."""
    tries = max(1, int(wait_s / 0.25))
    name = _as_str(process_name)
    script = f"""
tell application "System Events"
  repeat {tries} times
    if exists process {name} then
      if (count of windows of process {name}) > 0 then exit repeat
    end if
    delay 0.25
  end repeat
  tell process {name}
    set frontmost to true
    set value of attribute "AXFullScreen" of window 1 to true
  end tell
end tell
"""
    ok, _, err = _osascript(script, timeout=wait_s + 10)
    if not ok:
        _log_osascript_failure(f"Fullscreen for {process_name}", "System Events", err)
    return ok


# --- Spotify ------------------------------------------------------------------

_SPOTIFY_WEB_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[A-Za-z-]+/)?"
    r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)"
)


def _spotify_uri(u: str) -> str | None:
    if u.startswith("spotify:"):
        return u
    m = _SPOTIFY_WEB_RE.search(u)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return None


def _spotify_web_url(uri: str) -> str:
    parts = uri.split(":")
    if len(parts) >= 3:
        return f"https://open.spotify.com/{parts[1]}/{parts[2]}"
    return uri


def _spotify_running() -> bool:
    try:
        return (
            subprocess.run(["pgrep", "-x", "Spotify"], capture_output=True).returncode == 0
        )
    except OSError:
        return False


def _spotify_try_play(
    command: str, attempts: int, start_volume: int | None = None
) -> tuple[str, list[str]]:
    """Run a Spotify play command until the player reports "playing".

    `start_volume` (0–100) is applied to Spotify's own volume right before playing, so the
    music never starts loud. Returns (status, fields): status is "ok" (fields: name,
    artist, volume), "error" (fields: error number, message) or "notplaying" (fields:
    state, last error).
    """
    volume = "" if start_volume is None else f"set sound volume to {_volume(start_volume)}"
    # Note: AppleScript reserves short words such as st/nd/rd/th (ordinal suffixes, "1st"),
    # so variables here use long descriptive names.
    script = f"""
set lastErr to ""
repeat {attempts} times
  try
    tell application "Spotify"
      {volume}
      {command}
    end tell
    repeat 8 times
      delay 0.25
      tell application "Spotify"
        if player state is playing then
          set nowPlaying to current track
          return "ok|" & (name of nowPlaying) & "|" & (artist of nowPlaying) & "|" & (sound volume as text)
        end if
      end tell
    end repeat
  on error errMsg number errNum
    set lastErr to (errNum as text) & "|" & errMsg
    if errNum is -1743 then return "error|" & lastErr
  end try
  delay 0.5
end repeat
set playerStateText to "unknown"
try
  tell application "Spotify" to set playerStateText to (player state as text)
end try
return "notplaying|" & playerStateText & "|" & lastErr
"""
    ok, out, err = _osascript(script, timeout=attempts * 3.0 + 30)
    if not ok:
        return "error", ["", err]
    status, _, rest = out.partition("|")
    return status, rest.split("|")


def _spotify_get_volume() -> int | None:
    ok, out, _ = _osascript('tell application "Spotify" to return (sound volume as text)', timeout=10)
    return int(out) if ok and out.strip().isdigit() else None


def _spotify_set_volume(volume: int) -> bool:
    ok, _, err = _osascript(
        f'tell application "Spotify" to set sound volume to {_volume(volume)}', timeout=10
    )
    if not ok:
        log.warning("Spotify: could not set its volume: %s", err)
    return ok


def _fade_steps(start: int, end: int, seconds: float, step_s: float = 0.1) -> list[int]:
    """Volume values for a smooth (ease-in-out) fade, one every `step_s` seconds."""
    n = max(1, int(round(seconds / step_s)))
    values = []
    for i in range(1, n + 1):
        x = i / n
        eased = x * x * (3 - 2 * x)  # smoothstep: gentle start and finish
        values.append(int(round(start + (end - start) * eased)))
    return values


def _spotify_fade(start: int, end: int, seconds: float) -> bool:
    """Fade Spotify's own volume from `start` to `end` in one AppleScript (no stutter
    between steps). Only Spotify's volume changes; the Mac's system volume is untouched."""
    start, end = _volume(start), _volume(end)
    if seconds <= 0 or start == end:
        return _spotify_set_volume(end)
    steps = ", ".join(str(v) for v in _fade_steps(start, end, seconds))
    script = f"""
tell application "Spotify"
  repeat with stepVolume in {{{steps}}}
    set sound volume to (contents of stepVolume)
    delay 0.1
  end repeat
  return (sound volume as text)
end tell
"""
    ok, _, err = _osascript(script, timeout=seconds + 15)
    if not ok:
        log.warning("Spotify: fade failed (%s); setting the volume directly.", err)
        return _spotify_set_volume(end)
    return True


class SpotifyPlayback:
    """Spotify playing at its full volume; duck() lowers it under the voice and restore()
    brings it back up. Only Spotify's own volume is changed."""

    def __init__(self, previous_volume: int | None) -> None:
        self.previous_volume = previous_volume
        self.full_volume, self.full_reason = self._full_volume()
        # Never "duck" upwards if the full volume is already at or below the duck level.
        self.duck_volume = min(SPOTIFY_DUCK_VOLUME, self.full_volume)
        self.ducked = False
        self.restored = False

    def _full_volume(self) -> tuple[int, str]:
        prev = self.previous_volume
        if SPOTIFY_RESTORE_PREVIOUS and prev is not None and prev > SPOTIFY_DUCK_VOLUME:
            return prev, "your previous Spotify volume"
        return SPOTIFY_NORMAL_VOLUME, "JARVIS_SPOTIFY_NORMAL_VOLUME"

    def lead_in(self) -> None:
        """Let the song play at full volume so its beginning is clearly heard."""
        if MUSIC_LEAD_IN_SECONDS > 0:
            log.info(
                "Spotify: playing at %d%% for %.1fs before the voice...",
                self.full_volume,
                MUSIC_LEAD_IN_SECONDS,
            )
            UI.send(boot={"seconds": MUSIC_LEAD_IN_SECONDS})
            time.sleep(MUSIC_LEAD_IN_SECONDS)

    def duck(self) -> None:
        """Fade the song down to the duck level. Returns when the duck is complete, i.e.
        when the voice should start."""
        if self.duck_volume >= self.full_volume:
            log.info("Spotify: already at %d%%, no need to duck.", self.full_volume)
            return
        log.info(
            "Spotify: ducking %d%% → %d%% over %.1fs for the voice...",
            self.full_volume,
            self.duck_volume,
            SPOTIFY_DUCK_FADE_SECONDS,
        )
        self.ducked = True
        UI.send(
            state="ONLINE",
            volume_fade={"to": self.duck_volume, "seconds": SPOTIFY_DUCK_FADE_SECONDS},
            log=f"Audio ducked to {self.duck_volume}%",
        )
        if _spotify_fade(self.full_volume, self.duck_volume, SPOTIFY_DUCK_FADE_SECONDS):
            log.info("Spotify: ducked to %d%% (still audible under the voice).", self.duck_volume)

    def restore(self, *, fade: bool = True) -> None:
        if self.restored:
            return
        self.restored = True
        if not self.ducked:
            return  # still at full volume
        seconds = SPOTIFY_FADE_SECONDS if fade else 0.0
        log.info(
            "Spotify: fading music up %d%% → %d%% over %.1fs (%s)...",
            self.duck_volume,
            self.full_volume,
            seconds,
            self.full_reason,
        )
        UI.send(
            volume_fade={"to": self.full_volume, "seconds": seconds},
            log=f"Audio restored to {self.full_volume}%",
        )
        if _spotify_fade(self.duck_volume, self.full_volume, seconds):
            log.info("Spotify: restored to %d%%.", self.full_volume)


def _play_in_spotify_app(uri: str) -> SpotifyPlayback | None:
    """Launch Spotify if needed and play `uri` at full volume, logging a clear result.
    Returns a SpotifyPlayback to duck/restore the music, or None on failure."""
    if not _spotify_running():
        log.info("Spotify: starting in background...")
        # -g: don't bring it to the front; -j: launch it hidden. Jarvis keeps the screen.
        subprocess.run(["open", "-g", "-j", "-a", "Spotify"], check=False)
        deadline = time.monotonic() + 20
        while not _spotify_running() and time.monotonic() < deadline:
            time.sleep(0.5)
        if not _spotify_running():
            log.error("Spotify: ERROR — the app did not start within 20 seconds.")
            return None
        UI.ensure_focus("after Spotify launched")
        time.sleep(3)  # let it log in and load the player
        UI.ensure_focus("Spotify finished launching")
    else:
        log.info("Spotify: already running — controlling it in the background...")
    previous = _spotify_get_volume()
    playback = SpotifyPlayback(previous)
    start_volume = playback.full_volume
    log.info(
        "Spotify: asking it to play %s at %d%% (%s; Spotify volume was %s)...",
        uri,
        start_volume,
        playback.full_reason,
        f"{previous}%" if previous is not None else "unknown",
    )

    status, fields = _spotify_try_play(f"play track {_as_str(uri)}", 10, start_volume)
    if status == "notplaying":
        # Fallback: open the track via its spotify: link, then press play.
        log.info("Spotify: not playing yet (state: %s); retrying via the track link...", fields[0])
        subprocess.run(["open", "-g", uri], check=False)
        UI.ensure_focus("after opening the track link")
        time.sleep(1.5)
        status, fields = _spotify_try_play("play", 6, start_volume)

    if status == "ok":
        name, artist = (fields + ["", ""])[:2]
        log.info(
            "Spotify: SUCCESS — playing \"%s\" by %s at %d%%.", name, artist, start_volume
        )
        UI.ensure_focus("Spotify playing")  # confirmed in front before the lead-in starts
        UI.send(
            music={"track": name, "artist": artist, "volume": start_volume},
            log=f"Audio link: {name} — {artist}",
        )
        return playback
    if status == "error":
        num, msg = (fields + ["", ""])[:2]
        if num == "-1743" or "-1743" in msg or "not authorized" in msg.lower():
            log.error(
                "Spotify: ERROR — macOS did not allow Jarvis to control Spotify. Open System "
                "Settings → Privacy & Security → Automation → Terminal and turn on Spotify, "
                "then quit Terminal (Cmd+Q) and start again."
            )
        else:
            log.error("Spotify: ERROR — %s %s", num, msg)
    else:
        state, last = fields[0] if fields else "unknown", "|".join(fields[1:])
        log.error(
            "Spotify: ERROR — the app did not start playing (state: %s%s). Check that you are "
            "logged in, that the track plays when you click it in Spotify, and that no other "
            "device is controlling playback (Spotify Connect).",
            state,
            f"; last error: {last}" if last else "",
        )
    # Don't leave Spotify silent/ducked if it failed after we lowered its volume.
    if previous is not None:
        _spotify_set_volume(previous)
    return None


def play_song(uri: str) -> SpotifyPlayback | None:
    """Start the configured song. On macOS with the Spotify app this waits until Spotify
    reports it is playing (or fails), so any permission prompt shows before Chrome opens,
    and returns a SpotifyPlayback to duck/restore it. Other links (YouTube,
    web player) just open and return None (no ducking possible)."""
    u = uri.strip()
    if not u:
        log.info("No song configured (JARVIS_SONG_URI is empty).")
        return None
    spotify = _spotify_uri(u)
    if IS_MAC and spotify:
        if _mac_app_path("Spotify"):
            return _play_in_spotify_app(spotify)
        log.warning("Spotify: app not found in Applications; opening the web player instead.")
        u = _spotify_web_url(spotify)
    try:
        if IS_MAC:
            subprocess.run(["open", u], check=False)
        else:
            webbrowser.open(u)
        log.info("Opened song link: %s (volume ducking only works with the Spotify app)", u)
    except OSError as e:
        log.warning("Could not open JARVIS_SONG_URI: %s", e)
    return None


def _check_mac_output_volume() -> None:
    """Warn if the Mac's speakers are muted or at zero (no permission needed)."""
    ok, out, _ = _osascript(
        "set v to get volume settings\n"
        "return ((output volume of v) as text) & \"|\" & ((output muted of v) as text)",
        timeout=5,
    )
    if not ok:
        return
    vol, _, muted = out.partition("|")
    if muted == "true" or vol == "0":
        log.warning("Your Mac's sound is muted or at 0 — turn the volume up to hear Jarvis.")
    else:
        log.info("Mac output volume: %s%%", vol)


# --- Chrome -------------------------------------------------------------------


def _open_url_in_chrome(
    url: str, *, label: str, monitor: int, fullscreen: bool, site_key: str
) -> None:
    u = url.strip()
    if not u:
        return
    chrome = _mac_app_path("Google Chrome")
    if not chrome:
        log.warning("Google Chrome not found; opening %s in default browser.", label)
        if IS_MAC:
            subprocess.run(["open", u], check=False)
        else:
            webbrowser.open(u)
        return

    screen = _screen_bounds(monitor)
    if screen is None:
        bounds = None
    elif fullscreen:
        bounds = screen
    else:
        sl, st, sr, sb = screen
        w, h = _chrome_window_size()
        w, h = min(w, sr - sl), min(h, sb - st)
        x = sl + max(0, (sr - sl - w) // 2)
        y = st + max(0, (sb - st - h) // 2)
        bounds = (x, y, x + w, y + h)

    if CHROME_SEPARATE_SITE_PROFILES:
        args = [
            "open", "-na", str(chrome), "--args",
            f"--user-data-dir={_chrome_site_user_data_dir(site_key)}",
            "--no-first-run", "--new-window",
        ]
        if bounds:
            l, t, r, b = bounds
            args += [f"--window-position={l},{t}", f"--window-size={r - l},{b - t}"]
        if fullscreen:
            args.append("--start-fullscreen")
        args.append(u)
        try:
            subprocess.run(args, check=False)
        except OSError as e:
            log.warning("Could not open %s in Chrome: %s", label, e)
        return

    set_bounds = ""
    if bounds:
        set_bounds = "set bounds of w to {%d, %d, %d, %d}" % bounds
    script = f"""
tell application "Google Chrome"
  activate
  set w to make new window
  set URL of active tab of w to {_as_str(u)}
  {set_bounds}
  set index of w to 1
end tell
"""
    ok, _, err = _osascript(script)
    if not ok:
        _log_osascript_failure(f"Opening {label} in Chrome", "Google Chrome", err)
        subprocess.run(["open", "-a", str(chrome), u], check=False)
        return
    if fullscreen:
        time.sleep(0.6)
        _mac_set_fullscreen("Google Chrome", wait_s=3.0)
        # Let the fullscreen animation finish before the next window is created.
        time.sleep(1.2)


def open_claude_in_chrome() -> None:
    if not OPEN_CLAUDE_CODE_IN_CHROME:
        return
    _open_url_in_chrome(
        CLAUDE_CODE_URL,
        label="Claude",
        monitor=CLAUDE_CHROME_MONITOR,
        fullscreen=OPEN_CHROME_FULLSCREEN,
        site_key="claude",
    )


def open_tasaradar_in_chrome() -> None:
    if not OPEN_TASARADAR_IN_CHROME:
        return
    _open_url_in_chrome(
        TASARADAR_URL,
        label="Tasaradar",
        monitor=TASARADAR_CHROME_MONITOR,
        fullscreen=OPEN_CHROME_FULLSCREEN,
        site_key="tasaradar",
    )


# --- Cursor -------------------------------------------------------------------


def _cursor_cli(app: Path | None) -> str | None:
    if app:
        p = app / "Contents" / "Resources" / "app" / "bin" / "cursor"
        if p.is_file():
            return str(p)
    return shutil.which("cursor")


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP and not OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        return
    app = _mac_app_path("Cursor")
    cli = _cursor_cli(app)
    if not app and not cli:
        log.warning("Could not find Cursor (install it from https://cursor.com).")
        return
    quiet: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    try:
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            if app:
                # Brings the running Cursor (all its windows) to the front, or launches it.
                subprocess.run(["open", "-a", str(app)], check=False, **quiet)
            else:
                subprocess.Popen([cli], **quiet)
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            if cli:
                subprocess.Popen([cli, "-n"], **quiet)
            else:
                subprocess.run(["open", "-n", "-a", str(app)], check=False, **quiet)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if IS_MAC and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        _mac_set_fullscreen("Cursor", wait_s=15.0)


def run_double_clap_actions() -> None:
    """The welcome sequence. Runs once, after the microphone is closed:

      full-screen interface opens (STARTING) → song starts at full volume in the background
      (Spotify stays hidden) → JARVIS_MUSIC_LEAD_IN_SECONDS while the interface boots →
      song ducks (still audible) → the voice speaks over it (SPEAKING) → the voice
      finishes → the song rises back → the interface shows AWAITING COMMAND and stays
      open until the user closes it (Esc twice / power button / Cmd+Q, or Ctrl+C here).

    No work apps or websites are opened automatically any more.
    """
    UI.start()
    UI.send(state="STARTING", mic="closed", log="Double clap confirmed", highlight=True)

    # Fetch/load the voice in the background meanwhile, so it can speak right away later.
    prepared: dict = {}
    prep: threading.Thread | None = None
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():

        def _prepare() -> None:
            prepared["audio"] = prepare_welcome_audio()
            if prepared["audio"] is not None:
                UI.send(voice="READY", log="Voice module ready")

        prep = threading.Thread(target=_prepare, daemon=True)
        prep.start()

    # Nothing else happens until the interface is loaded, full screen and in front.
    if UI.alive():
        UI.wait_ready()
        UI.guard(True)  # from now on, any app that grabs focus is pushed back
    if IS_MAC:
        _check_mac_output_volume()

    music: SpotifyPlayback | None = None
    try:
        music = play_song(SONG_URI)

        if prep is not None:
            if music is not None:
                music.lead_in()  # the song at full volume while the interface boots
                voice_missing = not prep.is_alive() and prepared.get("audio") is None
                if not voice_missing:
                    music.duck()  # the voice starts as soon as the duck is done
            else:
                delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
                if delay:
                    time.sleep(delay)
            prep.join(timeout=30)
            audio = prepared.get("audio")
            UI.send(state="ONLINE", log="Core online")
            if audio is not None:
                play_welcome_audio(*audio)
            else:
                log.warning("Welcome voice: not available — continuing without it.")
        elif music is not None:
            music.lead_in()

        if music is not None:
            music.restore()
    finally:
        # Interrupted (Ctrl+C) or failed midway: never leave the music ducked.
        if music is not None and not music.restored:
            music.restore(fade=False)

    UI.send(state="ONLINE", prompt="AWAITING COMMAND", log="Awaiting command")
    UI.guard(False)  # startup done: switching apps is up to the user again
    if UI.alive():
        log.info(
            "Jarvis interface is open and awaiting commands. Close it with Esc twice, the "
            "power button or Cmd+Q (or press Ctrl+C here)."
        )
        conversation = _start_conversation(music)
        try:
            UI.wait_closed()
        finally:
            if conversation is not None:
                conversation.stop()
            UI.close()
        log.info("Jarvis interface closed.")


class ConversationMusic:
    """Keeps Spotify quieter while a voice conversation is active (Spotify's own volume)."""

    def __init__(self, music: "SpotifyPlayback", volume: int) -> None:
        self.music = music
        self.volume = min(volume, music.full_volume)
        self.ducked = False

    def duck(self) -> None:
        if self.volume < self.music.full_volume and not self.ducked:
            self.ducked = True
            _spotify_fade(self.music.full_volume, self.volume, 0.5)

    def restore(self) -> None:
        if self.ducked:
            self.ducked = False
            _spotify_fade(self.volume, self.music.full_volume, 1.5)


def _start_conversation(music: "SpotifyPlayback | None"):
    """Phase 2A: voice conversation after AWAITING COMMAND (see assistant/). Never raises."""
    try:
        from assistant.config import load_config
        from assistant.runtime import start_conversation
    except Exception as e:  # noqa: BLE001 — missing package etc.: Jarvis keeps working without it
        log.warning("Conversation: unavailable (%s). Run ./start_jarvis.sh to install packages.", e)
        return None
    cfg = load_config()
    music_ctl = None
    if music is not None and cfg.music_volume is not None:
        music_ctl = ConversationMusic(music, cfg.music_volume)
    return start_conversation(UI, music_ctl)


def run_ui_demo() -> int:
    """`./start_jarvis.sh --ui-demo`: preview the interface and its states (no microphone,
    no Spotify, no ElevenLabs). Close it with Esc twice or Cmd+Q."""
    if not UI.start():
        log.error("Jarvis interface could not start (JARVIS_UI_ENABLED=false?).")
        return 1
    UI.wait_ready()
    script = [
        (0.5, dict(state="STARTING", log="UI demo — no audio", highlight=True)),
        (0.5, dict(music={"track": "Demo Track", "artist": "Jarvis", "volume": 65}, log="Audio link: demo")),
        (0.3, dict(boot={"seconds": MUSIC_LEAD_IN_SECONDS}, voice="READY", log="Voice module ready")),
        (MUSIC_LEAD_IN_SECONDS, dict(state="ONLINE", volume_fade={"to": 35, "seconds": 0.5}, log="Audio ducked to 35%")),
        (0.6, dict(state="SPEAKING", log="Voice: speaking", highlight=True)),
    ]
    try:
        for delay, msg in script:
            time.sleep(delay)
            UI.send(**msg)
        for i in range(120):  # ~6 s of synthetic speech levels
            UI.send(level=round(0.25 + 0.7 * abs(np.sin(i / 3.1)) * (0.6 + 0.4 * np.sin(i / 11)), 3))
            time.sleep(0.05)
        UI.send(state="ONLINE", prompt="AWAITING COMMAND", level=0, voice="READY",
                volume_fade={"to": 65, "seconds": 2.0}, log="Voice: finished")
        cycle = [("LISTENING", "open"), ("THINKING", "closed"), ("ONLINE", "closed")]
        while UI.alive():
            for state, mic in cycle:
                time.sleep(6)
                prompt = "AWAITING COMMAND" if state == "ONLINE" else None
                UI.send(state=state, mic=mic, prompt=prompt, log=f"Demo state: {state}")
    except KeyboardInterrupt:
        pass
    finally:
        UI.close()
    return 0


def _hf_ratio(seg: np.ndarray) -> float:
    """Share of the signal's energy above 1 kHz (ignoring DC/rumble below 80 Hz)."""
    if seg.size < 16:
        return 0.0
    spec = np.abs(np.fft.rfft(seg - np.mean(seg))) ** 2
    freqs = np.fft.rfftfreq(seg.size, 1.0 / SAMPLE_RATE)
    total = float(np.sum(spec[freqs >= 80]))
    if total <= 0:
        return 0.0
    return float(np.sum(spec[freqs >= 1000])) / total


class ClapDetector:
    """Detects a double clap and rejects speech, typing, clicks and music.

    A loud onset is a block whose peak is over the threshold AND ONSET_RISE times louder
    than the previous block (so decaying echoes never start a new sound). Once it dies away
    it is checked: a clap is short (< MAX_CLAP_LEN_S), energetic (40 ms RMS well above the
    room's RMS) and bright (energy above 1 kHz). A double clap is two such claps
    MIN–MAX_DOUBLE_GAP_S apart, of similar loudness, with no other loud onset shortly
    before, between, or after them.

    The peak threshold is room noise × SPIKE_RATIO (room noise = median block peak over the
    last NOISE_WINDOW_S), clamped to [MIN_CLAP_PEAK, MAX_CLAP_THRESHOLD].

    feed() returns a list of (kind, info) events: "clap1", "clap2", "double",
    "rejected" (info["reason"]).
    """

    def __init__(self, block_s: float) -> None:
        self.block_s = block_s
        n = max(10, int(NOISE_WINDOW_S / block_s))
        self.history: deque[float] = deque(maxlen=n)  # block peaks
        self.rms_history: deque[float] = deque(maxlen=n)  # block RMS
        self.prev_peak = 0.0
        self.prev_block: np.ndarray = np.zeros(0, dtype=np.float32)
        self.pending: dict | None = None  # the loud sound currently being measured
        self.loud_onsets: deque[float] = deque(maxlen=64)
        self.first: dict | None = None  # confirmed clap 1
        self.second: dict | None = None  # confirmed clap 2, waiting for quiet after

    def noise_floor(self) -> float:
        return float(np.median(self.history)) if self.history else 0.0

    def threshold(self) -> float:
        return min(max(self.noise_floor() * SPIKE_RATIO, MIN_CLAP_PEAK), MAX_CLAP_THRESHOLD)

    def room_rms(self) -> float:
        return float(np.median(self.rms_history)) if self.rms_history else 0.0

    def min_clap_rms(self) -> float:
        return max(MIN_CLAP_RMS, CLAP_RMS_RATIO * self.room_rms())

    def calibrate(self, block: np.ndarray) -> None:
        block = block.reshape(-1)
        self.history.append(peak_mono(block))
        self.rms_history.append(rms_mono(block))
        self.prev_block = block
        self.prev_peak = self.history[-1]

    def reset(self) -> None:
        self.first = None
        self.second = None

    def _loud_between(self, t0: float, t1: float, exclude: tuple[float, ...]) -> bool:
        return any(t0 < t < t1 and t not in exclude for t in self.loud_onsets)

    def _measure(self, p: dict) -> dict:
        x = np.concatenate(p["blocks"])
        i = int(np.argmax(np.abs(x)))
        a = max(0, i - int(0.005 * SAMPLE_RATE))
        seg = x[a : a + int(0.040 * SAMPLE_RATE)]
        peak = float(np.max(np.abs(x)))
        rms = rms_mono(seg)
        # Width of the sound: smooth |x| over 1 ms, count time above 30% of its maximum.
        k = max(1, int(0.001 * SAMPLE_RATE))
        env = np.convolve(np.abs(x[a : a + int(0.060 * SAMPLE_RATE)]), np.ones(k) / k, "same")
        width_ms = 1000.0 * float(np.sum(env >= 0.3 * env.max())) / SAMPLE_RATE if env.size else 0.0
        return {
            "t": p["t"],
            "peak": peak,
            "rms": rms,
            "width_ms": width_ms,
            "hf": _hf_ratio(seg),
            "length": p["length"],
            "threshold": p["threshold"],
            "min_rms": p["min_rms"],
        }

    def _classify(self, p: dict, too_long: bool) -> list[tuple[str, dict]]:
        info = self._measure(p)
        reason = ""
        if too_long:
            reason = "lasted too long — voice or music?"
        elif info["rms"] < info["min_rms"]:
            reason = "too little energy — a click, tap or key press?"
        elif info["hf"] < MIN_HF_RATIO:
            reason = "too low-pitched — voice or a thump?"
        if reason:
            info["reason"] = reason
            if self.first and not self.second:
                self.first = None  # a non-clap between the claps breaks the pattern
            return [("rejected", info)]

        t = info["t"]
        if self.first is not None:
            gap = t - self.first["t"]
            loudness = max(info["peak"], self.first["peak"]) / max(
                1e-9, min(info["peak"], self.first["peak"])
            )
            if (
                MIN_DOUBLE_GAP_S <= gap <= MAX_DOUBLE_GAP_S
                and loudness <= 4.0
                and not self._loud_between(self.first["t"], t, (self.first["t"], t))
            ):
                info["gap"] = gap
                self.second = info
                return [("clap2", info)]
        # Candidate first clap: needs a quiet moment before it.
        if self._loud_between(t - QUIET_BEFORE_S, t, (t,)):
            info["reason"] = "other sounds just before it — typing or talking?"
            self.first = None
            return [("rejected", info)]
        self.first = info
        self.second = None
        return [("clap1", info)]

    def feed(self, now: float, block: np.ndarray) -> list[tuple[str, dict]]:
        block = block.reshape(-1)
        peak = peak_mono(block)
        th = self.threshold()
        events: list[tuple[str, dict]] = []

        if self.pending is not None:
            p = self.pending
            p["blocks"].append(block)
            p["length"] = now - p["t"]
            p["peak"] = max(p["peak"], peak)
            if peak < max(th * RETRIGGER_RATIO, p["peak"] * CLAP_DECAY_RATIO):
                self.pending = None
                events += self._classify(p, too_long=False)
            elif p["length"] > MAX_CLAP_LEN_S:
                self.pending = None
                events += self._classify(p, too_long=True)
        elif peak >= th and peak >= ONSET_RISE * self.prev_peak:
            self.loud_onsets.append(now)
            if self.second is not None:
                # Something loud right after clap 2: a third clap, typing, music...
                info = dict(self.second)
                info["reason"] = "more sounds right after the second clap"
                events.append(("rejected", info))
                self.reset()
            self.pending = {
                "t": now,
                "blocks": [self.prev_block, block],
                "peak": peak,
                "length": 0.0,
                "threshold": th,
                "min_rms": self.min_clap_rms(),
            }
        if self.pending is None:
            # Blocks outside a measured sound track the room noise.
            self.history.append(peak)
            self.rms_history.append(rms_mono(block))

        if self.second is not None and now - self.second["t"] >= QUIET_AFTER_S + self.second["length"]:
            info = self.second
            self.reset()
            events.append(("double", info))
        if self.first is not None and self.second is None and now - self.first["t"] > MAX_DOUBLE_GAP_S + MAX_CLAP_LEN_S:
            self.first = None

        self.prev_block = block
        self.prev_peak = peak
        return events


def _describe(info: dict) -> str:
    return "peak %.3f, rms %.3f (need %.3f), %d%% high-pitched, %.0f ms wide, threshold %.3f" % (
        info["peak"],
        info["rms"],
        info["min_rms"],
        int(100 * info["hf"]),
        info["width_ms"],
        info["threshold"],
    )


def main() -> int:
    if "--ui-demo" in sys.argv[1:]:
        log.info("Jarvis version %s — interface demo", JARVIS_VERSION)
        return run_ui_demo()
    if "--check-calendar" in sys.argv[1:]:
        from assistant.runtime import run_check_calendar

        return run_check_calendar()
    if "--chat" in sys.argv[1:]:
        from assistant.runtime import run_text_chat

        return run_text_chat()
    test_mode = "--test" in sys.argv[1:] or _env_bool("JARVIS_TEST_MODE", False)
    log.info("Jarvis version %s — running %s", JARVIS_VERSION, Path(__file__).resolve())
    log.info("Settings file: %s (%s)", ENV_PATH, "found" if ENV_PATH.is_file() else "NOT FOUND")
    blocksize = block_samples()
    block_s = blocksize / SAMPLE_RATE
    detector = ClapDetector(block_s)

    if not IS_MAC:
        log.warning(
            "This version of Jarvis targets macOS; app control (Spotify, Chrome, Cursor "
            "focus/fullscreen) is limited on %s.",
            sys.platform,
        )
    if test_mode:
        log.info(
            "CLAP TEST MODE: nothing will open and Jarvis keeps listening. "
            "Clap as often as you like; Ctrl+C to stop."
        )
    else:
        log.info(
            "Jarvis will wait for ONE double clap, run the welcome once, then stop listening."
        )
    log.info(
        "Double clap = two sharp claps %.2f–%.1fs apart. A clap must be %.1fx louder than "
        "the room and at least %.2f peak (JARVIS_SPIKE_RATIO / JARVIS_MIN_CLAP_PEAK).",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SPIKE_RATIO,
        MIN_CLAP_PEAK,
    )
    if not test_mode:
        if SONG_URI.strip():
            log.info("Double clap plays this track: %s", SONG_URI.strip())
        log.info(
            "Double clap opens the full-screen Jarvis interface%s; no other apps are opened.",
            "" if JARVIS_UI_ENABLED else " (disabled: JARVIS_UI_ENABLED=false)",
        )
        if JARVIS_WELCOME_ENABLED:
            ev, em, ef, er = elevenlabs_env_config()
            log.info(
                "Then say: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
                JARVIS_WELCOME_PHRASE.strip(),
                ev or "(unset)",
                em,
                ef,
                er,
            )

    input_idx = _choose_input_device(blocksize)
    triggered: dict | None = None

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            log.info("Measuring room noise for %.1fs — please stay quiet...", CALIBRATION_S)
            cal_rms: list[float] = []
            for _ in range(max(1, int(CALIBRATION_S / block_s))):
                data, _ = stream.read(blocksize)
                detector.calibrate(data)
                cal_rms.append(rms_mono(data))
            floor = detector.noise_floor()
            log.info(
                "Room noise: peak %.4f (rms %.4f). A clap needs peak ≥ %.3f%s and rms ≥ %.3f.",
                floor,
                float(np.median(cal_rms)),
                detector.threshold(),
                " (minimum — quiet room)" if detector.threshold() <= MIN_CLAP_PEAK else "",
                detector.min_clap_rms(),
            )
            if floor * SPIKE_RATIO > MAX_CLAP_THRESHOLD:
                log.warning(
                    "The room (or mic gain) is very loud; claps need to be close and sharp. "
                    "Lower the input volume in System Settings → Sound → Input if possible."
                )
            elif floor < 1e-5:
                log.warning("The microphone is completely silent. %s", MAC_MIC_HINT)
            log.info("Ready — clap twice.")

            window_max_peak = 0.0
            next_level_log = time.monotonic() + LEVEL_LOG_S
            last_reject_log = 0.0
            while triggered is None:
                data, _ = stream.read(blocksize)
                now = time.monotonic()
                window_max_peak = max(window_max_peak, peak_mono(data))

                for kind, info in detector.feed(now, data):
                    if kind == "clap1":
                        log.info("Clap 1 heard (%s) — clap again...", _describe(info))
                    elif kind == "clap2":
                        log.info(
                            "Clap 2 heard after %.2fs (%s) — checking it's not part of other noise...",
                            info["gap"],
                            _describe(info),
                        )
                    elif kind == "rejected":
                        if test_mode or now - last_reject_log >= 2.0:
                            log.info("Ignored a sound: %s (%s)", info["reason"], _describe(info))
                            last_reject_log = now
                    elif kind == "double":
                        log.info("DOUBLE CLAP DETECTED!")
                        if test_mode:
                            log.info("Test mode: the welcome would run now. Clap again to re-test.")
                        else:
                            triggered = info

                if LEVEL_LOG_S > 0 and now >= next_level_log:
                    th = detector.threshold()
                    log.info(
                        "Level: room noise %.4f | loudest %.4f | clap threshold %.3f",
                        detector.noise_floor(),
                        window_max_peak,
                        th,
                    )
                    window_max_peak = 0.0
                    next_level_log = now + LEVEL_LOG_S

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("Try another JARVIS_SAMPLE_RATE (e.g. 48000) or JARVIS_INPUT_DEVICE.")
        if IS_MAC:
            log.error("%s", MAC_MIC_HINT)
        return 1

    # Production: the microphone is closed now; nothing else can trigger Jarvis.
    log.info("Microphone closed — Jarvis is no longer listening. Running the welcome sequence...")
    try:
        run_double_clap_actions()
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    log.info("Welcome sequence finished. Jarvis has stopped. Run ./start_jarvis.sh to use it again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
