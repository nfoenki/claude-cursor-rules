#!/bin/sh
# Installs the Cursor rules hook for Claude Code (all projects of this user).
# Safe to run again: it updates the script and never duplicates the hooks.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required." >&2; exit 1; }

mkdir -p "$HOOKS_DIR"
cp "$HERE/cursor-rules.py" "$HOOKS_DIR/cursor-rules.py"
chmod +x "$HOOKS_DIR/cursor-rules.py"

python3 - "$SETTINGS" <<'EOF'
import json, os, shutil, sys

path = sys.argv[1]
command = "~/.claude/hooks/cursor-rules.py"
wanted = {
    "SessionStart": {"hooks": [{"type": "command", "command": command, "timeout": 10}]},
    "PreToolUse": {"matcher": "Read|Edit|Write|MultiEdit|NotebookEdit",
                   "hooks": [{"type": "command", "command": command, "timeout": 10}]},
}

settings = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            settings = json.load(f)
    except ValueError as e:
        sys.exit(f"{path} is not valid JSON ({e}). Fix it, then run this script again.")
    shutil.copy2(path, path + ".bak")

hooks = settings.setdefault("hooks", {})
for event, entry in wanted.items():
    entries = hooks.setdefault(event, [])
    present = any(h.get("command") == command
                  for e in entries for h in e.get("hooks", []))
    if not present:
        entries.append(entry)

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
EOF

echo "Installed $HOOKS_DIR/cursor-rules.py and declared its hooks in $SETTINGS."
echo "Open a new Claude Code session in a project that has a .cursor/ folder."
