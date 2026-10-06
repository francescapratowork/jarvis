"""The conversation engine: Claude + tools, streamed sentence by sentence.

For each user utterance:
  1. append a user message: a context note (time, profile memories) + the transcript;
  2. stream Claude's reply; every completed sentence is handed to the speaker at once,
     so Bella starts talking while Claude is still writing;
  3. run any tool calls (write tools only become pending actions), append the results
     and continue until Claude stops calling tools.

History is append-only (required for thinking blocks to stay valid). Short-term context
is this message list; long-term memory lives in MemoryStore.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import AssistantConfig
from .memory import MemoryStore
from .persona import IGNORE_MARKER, context_note, system_prompt
from .tools import ToolContext, ToolRegistry

log = logging.getLogger("jarvis.brain")

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 6
MAX_USER_TURNS = 40  # then start a fresh short-term context (long-term memory remains)
MAX_TOKENS = 8000

_IT_WORDS = {"che", "cosa", "di", "il", "la", "le", "per", "non", "sono", "ho", "domani", "oggi", "mi", "un", "una", "grazie", "ciao", "quando", "dove", "come", "devo"}


def looks_italian(text: str) -> bool:
    words = set(re.findall(r"[a-zàèéìòù]+", text.lower()))
    return len(words & _IT_WORDS) >= 1


@dataclass
class TurnResult:
    spoken: str = ""
    ignored: bool = False
    ended: bool = False
    error: str = ""


class SentenceSplitter:
    """Collects streamed text and emits whole sentences as soon as they are complete."""

    _END = re.compile(r"([.!?…]+[\"'”»)]?)(\s+)|(\n+)")

    def __init__(self, emit: Callable[[str], None], min_chars: int = 18) -> None:
        self.emit = emit
        self.min_chars = min_chars
        self.buf = ""
        self.all = ""

    def feed(self, text: str) -> None:
        self.buf += text
        self.all += text
        while True:
            cut = None
            for m in self._END.finditer(self.buf):
                if m.end() >= self.min_chars:
                    cut = m.end()
                    break
            if cut is None:
                return
            sentence, self.buf = self.buf[:cut], self.buf[cut:]
            self._emit(sentence)

    def flush(self) -> None:
        if self.buf.strip():
            self._emit(self.buf)
        self.buf = ""

    def _emit(self, sentence: str) -> None:
        raw = sentence.strip()
        if not raw or raw.startswith(IGNORE_MARKER[:8]):  # "<silence" — never spoken
            return
        clean = re.sub(r"[*_#`]+", "", raw).strip()
        if clean:
            self.emit(clean)


class Brain:
    def __init__(
        self,
        cfg: AssistantConfig,
        memory: MemoryStore,
        registry: ToolRegistry,
        ctx: ToolContext,
        client=None,
    ) -> None:
        self.cfg = cfg
        self.memory = memory
        self.registry = registry
        self.ctx = ctx
        if client is None:
            import anthropic  # reads ANTHROPIC_API_KEY from the environment; never logged

            client = anthropic.Anthropic(max_retries=2, timeout=60.0)
        self.client = client
        self.system = system_prompt(cfg.user_name)
        self.messages: list[dict] = []
        self.turn = 0
        self.last_user_text = ""
        self.user_turns_in_context = 0

    # ------------------------------------------------------------------ request
    def _stream(self):
        kwargs = dict(
            model=self.cfg.llm_model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            tools=self.registry.definitions(),
            messages=self.messages,
            output_config={"effort": self.cfg.llm_effort},
            cache_control={"type": "ephemeral"},
        )
        if self.cfg.llm_fallbacks == "default" and self.cfg.llm_model in FALLBACK_MODELS:
            return self.client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.stream(**kwargs)

    def _user_message(self, transcript: str) -> dict:
        now = datetime.now().astimezone()
        now_text = now.strftime("%A %d %B %Y, %H:%M") + f" ({now.tzname()})"
        note = context_note(now_text, self.memory.working_set(), self.cfg.user_name)
        return {"role": "user", "content": [{"type": "text", "text": note}, {"type": "text", "text": transcript}]}

    # ------------------------------------------------------------------ turn
    def respond(self, transcript: str, on_sentence: Callable[[str], None]) -> TurnResult:
        self.turn += 1
        self.registry.new_turn(self.turn, self.ctx)
        if self.user_turns_in_context >= MAX_USER_TURNS:
            self.messages = []  # fresh short-term context; long-term memory is unaffected
            self.user_turns_in_context = 0
        self.user_turns_in_context += 1
        self.last_user_text = transcript
        self.messages.append(self._user_message(transcript))
        self.memory.log_turn("user", transcript)
        ended = {"v": False}
        self.ctx.end_conversation = lambda: ended.__setitem__("v", True)

        splitter = SentenceSplitter(on_sentence)
        italian = looks_italian(transcript)
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                t0 = time.monotonic()
                with self._stream() as stream:
                    for event in stream:
                        if getattr(event, "type", "") == "text":
                            splitter.feed(event.text)
                    message = stream.get_final_message()
                self.messages.append({"role": "assistant", "content": message.content})
                usage = getattr(message, "usage", None)
                log.debug(
                    "Claude round %.2fs stop=%s cache_read=%s",
                    time.monotonic() - t0,
                    message.stop_reason,
                    getattr(usage, "cache_read_input_tokens", None),
                )
                if message.stop_reason == "refusal":
                    splitter.flush()
                    if not splitter.all.strip():
                        on_sentence("Mi dispiace, su questo non posso aiutarti." if italian else "I'm sorry, I can't help with that.")
                    break
                tool_uses = [b for b in message.content if getattr(b, "type", "") == "tool_use"]
                if not tool_uses or message.stop_reason == "max_tokens":
                    break
                splitter.flush()  # speak any sentence written before the tool call
                results = []
                for block in tool_uses:
                    result = self._run_tool(block.name, block.input)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _json(result),
                            **({"is_error": True} if isinstance(result, dict) and "error" in result else {}),
                        }
                    )
                self.messages.append({"role": "user", "content": results})
            splitter.flush()
        except Exception as e:  # noqa: BLE001 — never let one turn kill the conversation
            reason = _describe_error(e)
            log.warning("Brain: %s", reason)
            on_sentence(
                "Ho un problema a collegarmi al mio cervello in questo momento." if italian
                else "I'm having trouble reaching my reasoning service right now."
            )
            self._repair_history()
            return TurnResult(error=reason)

        text = splitter.all.strip()
        ignored = text == IGNORE_MARKER or (not text and not ended["v"])
        if text and not ignored:
            self.memory.log_turn("assistant", text)
        return TurnResult(spoken=text, ignored=ignored, ended=ended["v"])

    def _run_tool(self, name: str, args) -> dict:
        try:
            if name == "confirm_action":
                return self.registry.confirm(str((args or {}).get("action_id", "")), self.ctx, self.turn,
                                             user_text=self.last_user_text)
            if name == "cancel_action":
                return self.registry.cancel(self.ctx)
            return self.registry.execute(name, args or {}, self.ctx, self.turn)
        except TypeError as e:  # wrong/missing arguments
            return {"error": f"invalid arguments for {name}: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{name} failed: {type(e).__name__}: {e}"}

    def _repair_history(self) -> None:
        """After a failed request the history may end mid-exchange (a user message with no
        reply, or tool results with no reply). Start a fresh context rather than editing it."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages = []
            self.user_turns_in_context = 0


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)[:20000]


def _describe_error(e: Exception) -> str:
    name = type(e).__name__
    if name == "AuthenticationError":
        return "Anthropic rejected the API key (check ANTHROPIC_API_KEY in .env)"
    if name == "PermissionDeniedError":
        return "the Anthropic API key has no access to this model"
    if name == "NotFoundError":
        return "unknown Claude model (check JARVIS_LLM_MODEL in .env)"
    if name == "RateLimitError":
        return "Anthropic rate limit reached"
    if name == "APIConnectionError":
        return "no connection to Anthropic"
    status = getattr(e, "status_code", None)
    return f"{name}{f' ({status})' if status else ''}"
