#!/usr/bin/env python3
"""
Jarvis full-screen interface (runs as its own process, started by jarvis.py).

Shows ui/index.html in a native macOS window (WebKit, via pywebview) in full screen and
keeps it in front.

Protocol (one JSON object per line):

  jarvis.py → interface (stdin)
    {"state": "SPEAKING"}, {"level": 0.42}, {"log": "..."}, ...   forwarded to the page
    {"focus": true, "ack": 3}   bring the window to the front; answered with "focused"
    {"guard": true|false}       while on, any other app that takes focus is pushed back

  interface → jarvis.py (stdout)
    {"event": "ready", "fullscreen": true, "active": true, "seconds": 1.4}
        sent once the page has loaded, the window has finished entering full screen and
        Jarvis is the active (frontmost) app — jarvis.py waits for this before Spotify.
    {"event": "focused", "ack": 3, "active": true, "frontmost": "Python"}
    {"event": "refocused", "from": "Spotify"}   the guard took focus back from an app

The window stays open until the user closes it (Esc twice, the power button, or Cmd+Q);
it also closes if jarvis.py exits (stdin is closed).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

HTML_PATH = Path(__file__).resolve().parent / "ui" / "index.html"
FULLSCREEN_TIMEOUT_S = 8.0
ACTIVE_TIMEOUT_S = 3.0
GUARD_POLL_S = 0.1

_out_lock = threading.Lock()


def emit(**event) -> None:
    """Send an event line to jarvis.py (stdout is a pipe owned by jarvis.py)."""
    with _out_lock:
        try:
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass


class MacFocus:
    """Front/fullscreen checks via AppKit (installed with pywebview on macOS).
    On other systems every check reports success so the protocol still completes."""

    FULLSCREEN_MASK = 1 << 14  # NSWindowStyleMaskFullScreen

    def __init__(self) -> None:
        try:
            import AppKit  # noqa: F401

            self.AppKit = AppKit
        except ImportError:
            self.AppKit = None

    def app_active(self) -> bool:
        if self.AppKit is None:
            return True
        return bool(self.AppKit.NSRunningApplication.currentApplication().isActive())

    def frontmost(self) -> str:
        if self.AppKit is None:
            return ""
        app = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app is not None else ""

    def is_fullscreen(self, native) -> bool:
        if self.AppKit is None or native is None:
            return True
        try:
            return bool(native.styleMask() & self.FULLSCREEN_MASK)
        except Exception:
            return False


def main() -> int:
    try:
        import webview
    except ImportError:
        print(
            "Jarvis interface: the 'pywebview' package is missing. Run ./start_jarvis.sh "
            "(it installs it), or: .venv/bin/python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    mac = MacFocus()
    window = None
    guard_on = threading.Event()

    class Api:
        def quit(self) -> None:
            if window is not None:
                window.destroy()

    window = webview.create_window(
        "JARVIS",
        html=HTML_PATH.read_text(encoding="utf-8"),
        js_api=Api(),
        fullscreen=True,
        background_color="#01070a",
        text_select=False,
        focus=True,
    )

    def wait_active(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if mac.app_active():
                return True
            time.sleep(0.05)
        return mac.app_active()

    def bring_front() -> bool:
        window.show()  # makeKeyAndOrderFront + activateIgnoringOtherApps
        return wait_active(ACTIVE_TIMEOUT_S)

    def guard() -> None:
        """While the startup sequence runs, keep Jarvis in front: if another app (e.g.
        Spotify on launch) becomes active, take focus straight back."""
        while True:
            guard_on.wait()
            if not mac.app_active():
                thief = mac.frontmost()
                bring_front()
                emit(event="refocused", **{"from": thief})
            time.sleep(GUARD_POLL_S)

    def pump() -> None:
        t0 = time.monotonic()
        window.events.loaded.wait()
        window.show()
        # Ready = page loaded + full-screen transition finished + Jarvis is the active app.
        deadline = time.monotonic() + FULLSCREEN_TIMEOUT_S
        fullscreen = mac.is_fullscreen(window.native)
        while not fullscreen and time.monotonic() < deadline:
            if window.events.maximized.wait(0.1):  # fired by windowDidEnterFullScreen
                fullscreen = True
            fullscreen = fullscreen or mac.is_fullscreen(window.native)
        active = bring_front()
        emit(
            event="ready",
            fullscreen=fullscreen,
            active=active,
            frontmost=mac.frontmost(),
            seconds=round(time.monotonic() - t0, 2),
        )
        threading.Thread(target=guard, daemon=True).start()

        lock = threading.Lock()

        def forward(msg: dict) -> None:
            with lock:
                window.evaluate_js(
                    "window.jarvis && window.jarvis.receive(%s)" % json.dumps(msg)
                )

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "guard" in msg:
                guard_on.set() if msg.pop("guard") else guard_on.clear()
            if msg.pop("focus", False):
                ack = msg.pop("ack", None)
                active = bring_front()
                emit(event="focused", ack=ack, active=active, frontmost=mac.frontmost())
            if msg:
                try:
                    forward(msg)
                except Exception:
                    pass
        # jarvis.py exited: close the interface too.
        window.destroy()

    # Our own daemon thread (pywebview's start(func) thread is not a daemon, so a pending
    # stdin read would keep the process alive after the user closed the window).
    threading.Thread(target=pump, daemon=True).start()
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
