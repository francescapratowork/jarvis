"""The voice conversation state machine.

  AWAITING COMMAND --(activation)--> LISTENING
  LISTENING --(you finish a sentence)--> THINKING --(first audio)--> SPEAKING
  SPEAKING --(Bella finished)--> LISTENING
  LISTENING --(~60 s without speech, or "grazie, basta così")--> AWAITING COMMAND

Runs in its own thread with an asyncio loop (realtime speech-to-text), while Claude and
text-to-speech run in worker threads so the interface always shows the real state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Callable

from .activation import ActivationSource
from .brain import Brain, TurnResult
from .config import AssistantConfig
from .speech import Microphone, Speaker, STTError, STTSession

log = logging.getLogger("jarvis.conversation")

UNMUTE_TAIL_S = 0.35  # keep the mic muted briefly after Bella stops (room echo)
MERGE_WINDOW_S = 0.3  # a second sentence arriving this quickly joins the first


def resolve_input_device() -> int | None:
    spec = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if not spec:
        return None
    import sounddevice as sd

    if spec.isdigit():
        return int(spec)
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] >= 1 and spec.lower() in dev["name"].lower():
            return i
    return None


class Conversation:
    def __init__(
        self,
        cfg: AssistantConfig,
        ui,
        brain: Brain,
        speaker: Speaker,
        activations: list[ActivationSource],
        music=None,
        stt_factory: Callable[[], STTSession] | None = None,
        mic_factory: Callable[[Callable[[float], None]], Microphone] | None = None,
    ) -> None:
        self.cfg = cfg
        self.ui = ui
        self.brain = brain
        self.speaker = speaker
        self.activations = activations
        self.music = music
        self.stt_factory = stt_factory or (
            lambda: STTSession(cfg.elevenlabs_key(), cfg.stt_model, cfg.stt_vad_silence_s, cfg.stt_language)
        )
        self.mic_factory = mic_factory or (lambda on_level: Microphone(resolve_input_device(), on_level))
        self.loop: asyncio.AbstractEventLoop | None = None
        self.wake: asyncio.Queue | None = None
        self.active = False
        self.state = "AWAITING"
        self._stop = threading.Event()
        self._last_level_sent = 0.0
        self._last_partial_sent = 0.0
        self.thread: threading.Thread | None = None
        speaker.on_start = lambda: self._set("SPEAKING")
        speaker.on_level = self._voice_level

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="conversation")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.loop and self.wake:
            self.loop.call_soon_threadsafe(self.wake.put_nowait, "stop")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001
            log.warning("Conversation stopped: %s", e)

    def trigger(self, reason: str) -> None:
        if self.loop and self.wake and not self.active:
            self.loop.call_soon_threadsafe(self.wake.put_nowait, reason)

    async def _main(self) -> None:
        self.wake = asyncio.Queue()
        for source in self.activations:
            source.start(self.trigger)
        if self.cfg.autostart:
            self.wake.put_nowait("startup")
        while not self._stop.is_set():
            reason = await self.wake.get()
            if reason == "stop" or self._stop.is_set():
                break
            self.active = True
            try:
                await self._session(reason)
            finally:
                self.active = False
                self._set("AWAITING")
        for source in self.activations:
            source.stop()

    # ------------------------------------------------------------------ interface
    def _set(self, state: str, **extra) -> None:
        self.state = state
        if state == "AWAITING":
            self.ui.send(state="ONLINE", prompt="AWAITING COMMAND", mic="closed", level=0, **extra)
        else:
            self.ui.send(state=state, **extra)

    def _mic_level(self, level: float) -> None:
        now = time.monotonic()
        if self.state == "LISTENING" and now - self._last_level_sent >= 0.06:
            self._last_level_sent = now
            self.ui.send(level=round(level, 3))

    def _voice_level(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_level_sent >= 0.05 or level == 0.0:
            self._last_level_sent = now
            self.ui.send(level=round(level, 3))

    # ------------------------------------------------------------------ one conversation
    async def _session(self, reason: str) -> None:
        log.info("Conversation: listening (%s).", reason)
        if self.music is not None:
            await asyncio.to_thread(self.music.duck)
        audio: asyncio.Queue = asyncio.Queue(maxsize=600)
        mic = self.mic_factory(self._mic_level)
        stt = self.stt_factory()
        try:
            try:
                await stt.open(audio)
            except STTError as e:
                log.warning("Conversation: %s", e)
                self.ui.send(log=f"Speech-to-text unavailable: {e}"[:140], highlight=True)
                await asyncio.to_thread(self._say_now, "Non riesco ad accedere al riconoscimento vocale. Controlla il Terminale.")
                return
            mic.start(self.loop, audio)
            self._set("LISTENING", mic="open", log="Listening")
            deadline = time.monotonic() + self.cfg.timeout_s
            reconnects = 0
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.info("Conversation: %d s of silence — back to AWAITING COMMAND.", int(self.cfg.timeout_s))
                    self.ui.send(log="Silence — conversation paused (press Space to talk)")
                    break
                try:
                    kind, payload = await asyncio.wait_for(stt.events.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                if kind == "partial" and payload:
                    deadline = time.monotonic() + self.cfg.timeout_s  # she is talking
                    now = time.monotonic()
                    if now - self._last_partial_sent > 0.2:
                        self._last_partial_sent = now
                        self.ui.send(terminal={"cmd": payload[-80:], "out": "Listening…"})
                elif kind == "final":
                    text = (payload or "").strip()
                    if len(text) < 2 or not any(c.isalnum() for c in text):
                        continue
                    text = await self._merge_following(stt, text)
                    ended = await self._handle_utterance(mic, stt, text)
                    deadline = time.monotonic() + self.cfg.timeout_s
                    if ended:
                        break
                    self._set("LISTENING", mic="open")
                elif kind == "error":
                    log.warning("Conversation: %s", payload)
                    self.ui.send(log=str(payload)[:140], highlight=True)
                    if "Access" in str(payload) or "quota" in str(payload) or "terms" in str(payload):
                        await asyncio.to_thread(self._say_now, "Il riconoscimento vocale non è disponibile. Controlla il Terminale.")
                        break
                elif kind == "closed":
                    reconnects += 1
                    if reconnects > 3:
                        break
                    await stt.close()
                    stt = self.stt_factory()
                    try:
                        await stt.open(audio)
                    except STTError as e:
                        log.warning("Conversation: %s", e)
                        break
        finally:
            mic.stop()
            await stt.close()
            if self.music is not None:
                await asyncio.to_thread(self.music.restore)

    async def _merge_following(self, stt: STTSession, text: str) -> str:
        """If she continues within a moment, treat it as one request."""
        end = time.monotonic() + MERGE_WINDOW_S
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return text
            try:
                kind, payload = await asyncio.wait_for(stt.events.get(), timeout=left)
            except asyncio.TimeoutError:
                return text
            if kind == "final" and payload and payload.strip():
                text = f"{text} {payload.strip()}"
                end = time.monotonic() + MERGE_WINDOW_S

    async def _handle_utterance(self, mic: Microphone, stt: STTSession, text: str) -> bool:
        mic.muted.set()  # never transcribe Jarvis's own voice
        log.info("You: %s", text)
        self._set("THINKING", mic="closed", log=f"You: {text}"[:140], terminal={"cmd": text[-80:], "out": "Thinking…"})
        result: TurnResult = await asyncio.to_thread(self._respond, text)
        await asyncio.sleep(UNMUTE_TAIL_S)
        while not stt.events.empty():  # drop anything heard while muted
            stt.events.get_nowait()
        mic.muted.clear()
        if result.error:
            self.ui.send(log=f"Error: {result.error}"[:140], highlight=True)
        elif result.ignored:
            self.ui.send(log="(not addressed to Jarvis — ignored)")
        elif result.spoken:
            log.info("Jarvis: %s", result.spoken)
            self.ui.send(log=f"Jarvis: {result.spoken}"[:140], terminal={"cmd": text[-80:], "out": result.spoken[:160]})
        return result.ended

    # ------------------------------------------------------------------ worker threads
    def _respond(self, text: str) -> TurnResult:
        result = self.brain.respond(text, self.speaker.say)
        self.speaker.wait()
        return result

    def _say_now(self, sentence: str) -> None:
        self.speaker.say(sentence)
        self.speaker.wait()
