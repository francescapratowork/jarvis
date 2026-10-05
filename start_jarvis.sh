#!/bin/bash
# Starts Jarvis on macOS. The first run creates a private Python environment (.venv)
# and installs the dependencies; later runs start straight away.
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Download it from https://www.python.org/downloads/macos/ and run this again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Jarvis (this takes a minute)..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
fi

if [ ! -f .venv/.deps-installed ] || [ requirements.txt -nt .venv/.deps-installed ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.deps-installed
fi

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "Created .env from .env.example — open it to add your ElevenLabs key and voice ID."
fi

exec .venv/bin/python jarvis.py
