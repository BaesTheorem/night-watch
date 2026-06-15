#!/bin/bash
# Night Watch — install the launchd services for the current checkout.
# Fills the __DIR__ / __PYTHON__ placeholders in launchd/*.plist.template,
# writes real plists to ~/Library/LaunchAgents, and (re)loads them.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
UID_="$(id -u)"

[ -x "$PY" ] || { echo "Missing venv. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
mkdir -p "$LA" "$DIR/logs"

for t in "$DIR"/launchd/*.plist.template; do
  label="$(basename "$t" .plist.template)"
  dest="$LA/$label.plist"
  sed -e "s#__DIR__#$DIR#g" -e "s#__PYTHON__#$PY#g" "$t" > "$dest"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$dest" 2>/dev/null || launchctl load "$dest"
  echo "installed $label"
done
echo "Night Watch services installed. Open the GUI with:  $PY app.py"
