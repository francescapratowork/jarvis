"""What (re)starts a conversation.

Every source calls `trigger(reason)`; the conversation engine doesn't care where it came
from. Phase 2A: the interface (Space key or the mic button). Later, a local "Jarvis" wake
word can be added as another ActivationSource without touching the engine.
"""

from __future__ import annotations

from typing import Callable, Protocol


class ActivationSource(Protocol):
    def start(self, trigger: Callable[[str], None]) -> None: ...

    def stop(self) -> None: ...


class InterfaceActivation:
    """Space bar / mic button in the full-screen interface (sent by jarvis_ui.py)."""

    def __init__(self, ui) -> None:
        self.ui = ui
        self._trigger: Callable[[str], None] | None = None

    def start(self, trigger: Callable[[str], None]) -> None:
        self._trigger = trigger
        self.ui.on_event("activate", lambda ev: self._trigger and self._trigger("interface"))

    def stop(self) -> None:
        self._trigger = None
