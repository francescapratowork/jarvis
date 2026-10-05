#!/usr/bin/env python3
"""
Jarvis for macOS: listens to your Mac microphone and, on a double clap, runs a welcome
sequence (Spotify track, Chrome windows, ElevenLabs voice, Cursor).

Run:
  ./start_jarvis.sh            (first run creates a virtualenv and installs dependencies)
  # or manually:
  python3 -m pip install -r requirements.txt
  python3 jarvis.py

Everything a user is likely to change can be set in a `.env` file next to this script
(see `.env.example`). The constants below are only the defaults.

Clap detection:
  Jarvis measures the room noise for a couple of seconds at startup and keeps re-measuring it
  (median loudness of the last few seconds), so the clap threshold follows your room
  automatically. A clap is a short, sharp sound whose peak is JARVIS_SPIKE_RATIO times louder
  than the room noise; two claps 0.1–0.8 s apart trigger the welcome.
  Run `./start_jarvis.sh --test` to only test claps (nothing opens, and you can clap repeatedly).
  Every few seconds (JARVIS_LEVEL_LOG_S) a level line shows the room noise, the loudest recent
  sound and the current clap threshold.

  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size (claps are only ~10 ms long, so keep it short).
  SPIKE_RATIO   — how many times louder than the room noise a clap peak must be;
                    raise if false triggers; lower if claps are missed.
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  MIN_CLAP_PEAK / MAX_CLAP_THRESHOLD — absolute limits for the automatic threshold.

Actions (macOS):
  SONG_URI      — Spotify or YouTube URL/URI (env JARVIS_SONG_URI). Spotify links are played in
                    the Spotify app via AppleScript; anything else opens in the default browser.
  OPEN_CLAUDE_CODE_IN_CHROME / OPEN_TASARADAR_IN_CHROME — open each site in a new Chrome window
    (CLAUDE_CODE_URL / TASARADAR_URL), placed on CLAUDE_CHROME_MONITOR / TASARADAR_CHROME_MONITOR
    (1-based, displays sorted left-to-right then top-to-bottom).
  OPEN_CHROME_FULLSCREEN / CURSOR_OPEN_FULLSCREEN — native macOS fullscreen (needs Accessibility
    permission for your Terminal app). Without that permission the window just fills the screen.
  CHROME_SEPARATE_SITE_PROFILES — if True, uses a temp --user-data-dir per site (not your normal
    profile). Default False so Claude/Tasaradar use your usual Chrome profile and logins.
  FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP — bring Cursor to the front (launches it if not running).
  OPEN_NEW_CURSOR_ON_DOUBLE_CLAP — also open a new Cursor window.
  JARVIS_WELCOME_* — TTS after the song (ElevenLabs), played through the default output device.
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/` (WAV) and
    replayed when phrase + voice + model + format match—no repeat API call. Delete that folder
    or set JARVIS_WELCOME_CACHE_ENABLED=false to force a fresh fetch.
  The welcome sequence runs only once per process. The assistant speaks in the background so Cursor
    opens without waiting for playback to finish (restart the script to run again).
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

load_dotenv(Path(__file__).resolve().parent / ".env")

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

# A clap's peak must be this many times the room-noise peak (auto-calibrated).
SPIKE_RATIO = _env_float("JARVIS_SPIKE_RATIO", 4.0)
COOLDOWN_S = 1.0  # after a double clap, ignore claps for this long
MIN_DOUBLE_GAP_S = 0.1
MAX_DOUBLE_GAP_S = _env_float("JARVIS_MAX_DOUBLE_GAP_S", 0.8)
CLAP_REFRACTORY_S = 0.07  # a clap's own echo/decay is not a second clap
RETRIGGER_RATIO = 0.6  # sound must fall below threshold * this before the next clap counts
NOISE_WINDOW_S = 3.0  # room noise = median block peak over this many recent seconds
CALIBRATION_S = 1.5  # measure the room before listening
MIN_CLAP_PEAK = 0.03  # threshold never goes below this (full scale = 1.0)
MAX_CLAP_THRESHOLD = 0.6  # ...or above this, so a loud clap still works in a noisy room
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
# Optional Spotify volume (0–100) set before playing; empty = leave unchanged.
SPOTIFY_VOLUME = _env_str("SPOTIFY_VOLUME")

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
# Seconds after launching SONG_URI before speaking (gives Spotify/browser time to start).
JARVIS_AFTER_SONG_DELAY_S = _env_float("JARVIS_AFTER_SONG_DELAY_S", 1.0)
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


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play cached welcome audio: %s", e)
        return False
    return True


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


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    text = JARVIS_WELCOME_PHRASE.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing welcome from cache: %s", cache_path)
        if _play_pcm_wav_file(cache_path):
            return
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return
    try:
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
        return
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved welcome audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, pcm_rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play ElevenLabs audio: %s", e)


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


def _play_in_spotify_app(uri: str) -> None:
    volume = ""
    if SPOTIFY_VOLUME.isdigit():
        volume = f"set sound volume to {max(0, min(100, int(SPOTIFY_VOLUME)))}"
    # Spotify may still be starting up, so retry until it reports it is playing.
    script = f"""
tell application "Spotify"
  {volume}
  set ok to false
  repeat 30 times
    try
      play track {_as_str(uri)}
      delay 0.5
      if player state is playing then
        set ok to true
        exit repeat
      end if
    end try
    delay 0.5
  end repeat
  return ok
end tell
"""
    ok, out, err = _osascript(script, timeout=60)
    if not ok:
        _log_osascript_failure("Spotify playback", "Spotify", err)
    elif out != "true":
        log.warning("Spotify did not start playing %s (is it logged in?).", uri)
    else:
        log.info("Spotify is playing %s", uri)


def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    spotify = _spotify_uri(u)
    if IS_MAC and spotify and _mac_app_path("Spotify"):
        # Runs in the background: a cold Spotify start can take several seconds.
        threading.Thread(target=_play_in_spotify_app, args=(spotify,), daemon=True).start()
        return
    if spotify and not u.startswith("http"):
        log.info("Spotify app not found; opening the web player instead.")
        u = _spotify_web_url(spotify)
    try:
        if IS_MAC:
            subprocess.run(["open", u], check=False)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


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
    """Run outside the mic loop so sleeps do not stall capture."""
    play_song(SONG_URI)
    open_claude_in_chrome()
    open_tasaradar_in_chrome()
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()
    open_cursor_window()


class ClapDetector:
    """Detects double claps from per-block peak levels, with an adaptive noise floor.

    The noise floor is the median block peak over the last NOISE_WINDOW_S seconds, so it
    follows the room (fans, AC, traffic) and is barely moved by the claps themselves.
    """

    def __init__(self, block_s: float) -> None:
        self.block_s = block_s
        self.history: deque[float] = deque(maxlen=max(10, int(NOISE_WINDOW_S / block_s)))
        self.armed = True
        self.last_hit: float | None = None
        self.first_clap: float | None = None
        self.last_double = -1e9

    def noise_floor(self) -> float:
        return float(np.median(self.history)) if self.history else 0.0

    def threshold(self) -> float:
        return min(max(self.noise_floor() * SPIKE_RATIO, MIN_CLAP_PEAK), MAX_CLAP_THRESHOLD)

    def calibrate(self, peak: float) -> None:
        self.history.append(peak)

    def feed(self, now: float, peak: float, rms: float) -> str | None:
        """Process one block. Returns "clap", "double" or None."""
        threshold = self.threshold()
        event: str | None = None
        if not self.armed and peak < threshold * RETRIGGER_RATIO:
            if self.last_hit is None or now - self.last_hit >= CLAP_REFRACTORY_S:
                self.armed = True
        if self.armed and peak >= threshold:
            self.armed = False
            self.last_hit = now
            if now - self.last_double < COOLDOWN_S:
                pass
            elif self.first_clap is not None and (
                MIN_DOUBLE_GAP_S <= now - self.first_clap <= MAX_DOUBLE_GAP_S
            ):
                event = "double"
                self.first_clap = None
                self.last_double = now
            elif self.first_clap is not None and now - self.first_clap < MIN_DOUBLE_GAP_S:
                pass  # same clap ringing on
            else:
                event = "clap"
                self.first_clap = now
        else:
            # Only quiet blocks update the room-noise estimate.
            self.history.append(peak)
        if self.first_clap is not None and now - self.first_clap > MAX_DOUBLE_GAP_S:
            self.first_clap = None
        return event


def main() -> int:
    test_mode = "--test" in sys.argv[1:] or _env_bool("JARVIS_TEST_MODE", False)
    blocksize = block_samples()
    block_s = blocksize / SAMPLE_RATE
    detector = ClapDetector(block_s)
    welcome_sequence_done = False

    if not IS_MAC:
        log.warning(
            "This version of Jarvis targets macOS; app control (Spotify, Chrome, Cursor "
            "focus/fullscreen) is limited on %s.",
            sys.platform,
        )
    if test_mode:
        log.info("CLAP TEST MODE: nothing will open. Clap as often as you like; Ctrl+C to stop.")
    log.info(
        "Double clap = two claps %.1f–%.1fs apart. Sensitivity: clap must be %.1fx louder "
        "than the room (JARVIS_SPIKE_RATIO). Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SPIKE_RATIO,
    )
    if not test_mode:
        if SONG_URI.strip():
            log.info("Double clap plays this track: %s", SONG_URI.strip())
        else:
            log.info("JARVIS_SONG_URI is empty — set it to play one song on each double clap.")
        if OPEN_CLAUDE_CODE_IN_CHROME:
            log.info(
                "Then open Claude in Chrome%s on display %d: %s",
                " fullscreen" if OPEN_CHROME_FULLSCREEN else "",
                CLAUDE_CHROME_MONITOR,
                CLAUDE_CODE_URL,
            )
        if OPEN_TASARADAR_IN_CHROME:
            log.info(
                "Then open Tasaradar in Chrome%s on display %d: %s",
                " fullscreen" if OPEN_CHROME_FULLSCREEN else "",
                TASARADAR_CHROME_MONITOR,
                TASARADAR_URL,
            )
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            log.info(
                "Then bring Cursor to the front (launching it if needed)%s.",
                " in fullscreen" if CURSOR_OPEN_FULLSCREEN else "",
            )
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            log.info("Double clap will also open a new Cursor window.")
        if JARVIS_WELCOME_ENABLED:
            ev, em, ef, er = elevenlabs_env_config()
            log.info(
                "After song + %.2fs: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
                JARVIS_AFTER_SONG_DELAY_S,
                JARVIS_WELCOME_PHRASE.strip(),
                ev or "(unset)",
                em,
                ef,
                er,
            )

    input_idx = _choose_input_device(blocksize)

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
                detector.calibrate(peak_mono(data))
                cal_rms.append(rms_mono(data))
            floor = detector.noise_floor()
            log.info(
                "Room noise: peak %.4f (rms %.4f). Clap threshold set to peak %.4f.",
                floor,
                float(np.median(cal_rms)),
                detector.threshold(),
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
            window_max_rms = 0.0
            next_level_log = time.monotonic() + LEVEL_LOG_S
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.debug("Input overflow")
                now = time.monotonic()
                peak = peak_mono(data)
                rms = rms_mono(data)
                window_max_peak = max(window_max_peak, peak)
                window_max_rms = max(window_max_rms, rms)

                event = detector.feed(now, peak, rms)
                if event == "clap":
                    log.info(
                        "Clap 1 heard (peak %.3f, rms %.4f, threshold %.3f) — clap again "
                        "within %.1fs...",
                        peak,
                        rms,
                        detector.threshold(),
                        MAX_DOUBLE_GAP_S,
                    )
                elif event == "double":
                    log.info(
                        "Clap 2 heard (peak %.3f, rms %.4f) — DOUBLE CLAP DETECTED!",
                        peak,
                        rms,
                    )
                    if test_mode:
                        log.info("Test mode: would run the welcome now. Clap again to re-test.")
                    elif welcome_sequence_done:
                        log.info("Welcome already ran; restart Jarvis to run it again.")
                    else:
                        welcome_sequence_done = True
                        log.info("Running welcome sequence.")
                        threading.Thread(target=run_double_clap_actions, daemon=True).start()

                if LEVEL_LOG_S > 0 and now >= next_level_log:
                    th = detector.threshold()
                    hint = ""
                    if th * 0.5 <= window_max_peak < th:
                        hint = (
                            " — a sound reached %d%% of the threshold; if that was a clap, "
                            "lower JARVIS_SPIKE_RATIO" % int(100 * window_max_peak / th)
                        )
                    log.info(
                        "Level: room noise %.4f | loudest %.4f (rms %.4f) | clap threshold %.4f%s",
                        detector.noise_floor(),
                        window_max_peak,
                        window_max_rms,
                        th,
                        hint,
                    )
                    window_max_peak = 0.0
                    window_max_rms = 0.0
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
