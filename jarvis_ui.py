#!/usr/bin/env python3
"""
Jarvis full-screen interface (runs as its own process, started by jarvis.py).

Shows ui/index.html in a native macOS window (WebKit, via pywebview) in full screen and
keeps it in front. jarvis.py sends one JSON message per line on stdin, e.g.
  {"state": "SPEAKING"}   {"level": 0.42}   {"log": "Voice: speaking"}   {"focus": true}
which are forwarded to the page (window.jarvis.receive). {"focus": true} brings the
window back to the front (e.g. after Spotify was launched).

The window stays open until the user closes it (Esc twice, the power button, or Cmd+Q);
it also closes if jarvis.py exits (stdin is closed).
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

HTML_PATH = Path(__file__).resolve().parent / "ui" / "index.html"


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

    window = None

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

    def pump() -> None:
        window.events.loaded.wait()
        window.show()  # bring to the front and activate
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
            if msg.pop("focus", False):
                window.show()
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
