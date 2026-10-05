#!/bin/bash
# Updates Jarvis's code to the latest version on GitHub (main branch).
# Your settings (.env), Python environment (.venv) and voice cache (.cache) are never touched.
set -e
cd "$(dirname "$0")"
ZIP_URL="https://github.com/francescapratowork/jarvis/archive/refs/heads/main.zip"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading the latest Jarvis..."
curl -fsSL -o "$TMP/jarvis.zip" "$ZIP_URL"
unzip -q "$TMP/jarvis.zip" -d "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'jarvis-*' | head -n 1)"
if [ -z "$SRC" ] || [ ! -f "$SRC/jarvis.py" ]; then
  echo "The download looks wrong; nothing was changed."
  exit 1
fi

for item in "$SRC"/* "$SRC"/.[!.]*; do
  [ -e "$item" ] || continue
  name="$(basename "$item")"
  case "$name" in .env|.venv|.cache) continue ;; esac
  if [ -d "$item" ]; then
    rm -rf "./$name.new" && cp -R "$item" "./$name.new" && rm -rf "./$name" && mv "./$name.new" "./$name"
  else
    # Copy then rename, so this script can safely replace itself while running.
    cp "$item" "./$name.new" && mv -f "./$name.new" "./$name"
  fi
done
chmod +x start_jarvis.sh update_jarvis.sh

echo "Jarvis updated in: $(pwd)"
grep -m 1 '^JARVIS_VERSION' jarvis.py | sed 's/^JARVIS_VERSION = /Version: /'
if [ -f .env ]; then echo "Your .env settings were kept."; fi
