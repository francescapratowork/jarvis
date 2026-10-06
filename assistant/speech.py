"""Audio in/out for the conversation.

Microphone  → 16 kHz PCM frames (sounddevice), with a live level for the interface.
STTSession  → ElevenLabs Scribe v2 Realtime over a WebSocket; the server's voice-activity
              detection commits a transcript as soon as the user stops speaking. While
              Jarvis speaks, silence is sent instead of the microphone (no self-hearing).
Speaker     → streaming ElevenLabs text-to-speech (Bella's voice). Sentences are synthesised
              in a background thread while the previous one plays, so there are no gaps.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger("jarvis.speech")

MIC_RATE = 16000
MIC_BLOCK = 1600  # 100 ms
TTS_RATE = 24000


# ---------------------------------------------------------------------- microphone
class Microphone:
    def __init__(self, device: int | None, on_level: Callable[[float], None]) -> None:
        self.device = device
        self.on_level = on_level
        self.stream = None
        self.queue: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.muted = threading.Event()

    def start(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        import sounddevice as sd

        self.loop, self.queue = loop, q

        def callback(indata, frames, time_info, status):  # audio thread
            data = bytes(indata)
            if self.muted.is_set():
                data = b"\x00" * len(data)
            else:
                pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(pcm * pcm))) if pcm.size else 0.0
                self.on_level(min(1.0, rms * 12.0))
            try:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, data)
            except RuntimeError:
                pass  # loop closed

        self.stream = sd.RawInputStream(
            samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=MIC_BLOCK,
            device=self.device, callback=callback,
        )
        self.stream.start()

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
            self.stream = None


# ---------------------------------------------------------------------- speech-to-text
class STTError(RuntimeError):
    pass


STT_ERROR_HINTS = {
    "auth_error": "ElevenLabs refused speech-to-text for this API key. In ElevenLabs → Developers → "
    "API Keys, edit your key and set 'Speech to Text' to Access.",
    "quota_exceeded": "ElevenLabs speech-to-text quota is used up for this billing period.",
    "unaccepted_terms": "Accept the ElevenLabs speech-to-text terms once in the ElevenLabs website.",
    "rate_limited": "ElevenLabs speech-to-text is rate-limited; try again in a moment.",
}


class STTSession:
    """One realtime transcription session. Transcripts and events go to `events` as tuples:
    ("partial", text) / ("final", text) / ("error", message) / ("closed", None)."""

    def __init__(self, api_key: str, model_id: str, vad_silence_s: float, language: str = "",
                 base_url: str = "wss://api.elevenlabs.io") -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.vad_silence_s = vad_silence_s
        self.language = language
        self.base_url = base_url
        self.conn = None
        self.events: asyncio.Queue = asyncio.Queue()
        self._sender: asyncio.Task | None = None

    async def open(self, audio: asyncio.Queue) -> None:
        from elevenlabs.realtime import ScribeRealtime
        from elevenlabs.realtime.connection import RealtimeEvents
        from elevenlabs.realtime.scribe import AudioFormat, CommitStrategy

        options = {
            "model_id": self.model_id,
            "audio_format": AudioFormat.PCM_16000,
            "sample_rate": MIC_RATE,
            "commit_strategy": CommitStrategy.VAD,
            "vad_silence_threshold_secs": self.vad_silence_s,
            "filter_background_audio": True,
            "keyterms": ["Jarvis"],
        }
        if self.language:
            options["language_code"] = self.language
        try:
            self.conn = await ScribeRealtime(api_key=self.api_key, base_url=self.base_url).connect(options)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "401" in msg or "403" in msg:
                raise STTError(STT_ERROR_HINTS["auth_error"]) from e
            raise STTError(f"could not connect to ElevenLabs speech-to-text ({type(e).__name__})") from e

        put = self.events.put_nowait
        self.conn.on(RealtimeEvents.PARTIAL_TRANSCRIPT, lambda d: put(("partial", (d or {}).get("text", ""))))
        self.conn.on(RealtimeEvents.COMMITTED_TRANSCRIPT, lambda d: put(("final", (d or {}).get("text", ""))))

        def on_error(d):
            d = d or {}
            kind = d.get("message_type") or "error"
            hint = STT_ERROR_HINTS.get(kind) or STT_ERROR_HINTS.get(kind.replace("_error", ""))
            put(("error", hint or f"speech-to-text: {kind} {d.get('error') or d.get('message') or ''}".strip()))

        self.conn.on(RealtimeEvents.ERROR, on_error)
        self.conn.on(RealtimeEvents.CLOSE, lambda *a: put(("closed", None)))
        self._sender = asyncio.create_task(self._send_audio(audio))

    async def _send_audio(self, audio: asyncio.Queue) -> None:
        try:
            while True:
                chunk = await audio.get()
                await self.conn.send({"audio_base_64": base64.b64encode(chunk).decode("ascii")})
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            self.events.put_nowait(("closed", str(e)))

    async def close(self) -> None:
        if self._sender:
            self._sender.cancel()
        if self.conn is not None:
            try:
                await self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None


# ---------------------------------------------------------------------- text-to-speech
class Speaker:
    """Speaks sentences in order. say() returns immediately; wait() blocks until done."""

    def __init__(self, api_key: str, voice_id: str, model_id: str,
                 on_start: Callable[[], None], on_level: Callable[[float], None], client=None) -> None:
        self.voice_id = voice_id
        self.model_id = model_id
        self.on_start = on_start
        self.on_level = on_level
        if client is None:
            from elevenlabs.client import ElevenLabs

            client = ElevenLabs(api_key=api_key)
        self.client = client
        self.sentences: queue.Queue = queue.Queue()
        self.audio: queue.Queue = queue.Queue(maxsize=200)
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._lock = threading.Lock()
        self._started_turn = False
        self._spoken: list[str] = []
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    # -- public
    def say(self, sentence: str) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()
        self.sentences.put(sentence)

    def wait(self, timeout: float = 120.0) -> None:
        self._idle.wait(timeout)
        with self._lock:
            self._started_turn = False
            self._spoken = []

    # -- synthesis thread: text → PCM chunks (streamed)
    def _synth_loop(self) -> None:
        while True:
            sentence = self.sentences.get()
            previous = " ".join(self._spoken[-2:]) or None
            self._spoken.append(sentence)
            try:
                stream = self.client.text_to_speech.stream(
                    voice_id=self.voice_id,
                    text=sentence,
                    model_id=self.model_id,
                    output_format=f"pcm_{TTS_RATE}",
                    previous_text=previous,
                )
                carry = b""
                for chunk in stream:
                    if not chunk:
                        continue
                    data = carry + chunk
                    cut = len(data) - (len(data) % 2)
                    carry = data[cut:]
                    if cut:
                        self.audio.put(data[:cut])
            except Exception as e:  # noqa: BLE001
                log.warning("Text-to-speech failed: %s", type(e).__name__)
            self.audio.put(None)  # end of this sentence

    # -- playback thread: PCM chunks → speakers
    def _play_loop(self) -> None:
        import sounddevice as sd

        stream = None
        while True:
            item = self.audio.get()
            if item is None:
                with self._lock:
                    self._pending -= 1
                    done = self._pending <= 0
                if done:
                    if stream is not None:
                        time.sleep(0.15)  # let the device buffer drain
                    self.on_level(0.0)
                    self._idle.set()
                continue
            if stream is None:
                stream = sd.RawOutputStream(samplerate=TTS_RATE, channels=1, dtype="int16")
                stream.start()
            with self._lock:
                first = not self._started_turn
                self._started_turn = True
            if first:
                self.on_start()
            pcm = np.frombuffer(item, dtype=np.int16).astype(np.float32) / 32768.0
            if pcm.size:
                self.on_level(min(1.0, float(np.sqrt(np.mean(pcm * pcm))) * 5.0))
            stream.write(item)
