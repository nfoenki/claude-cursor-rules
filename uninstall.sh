#!/bin/sh
# Removes the Cursor rules hook from Claude Code. Leaves the .claude/skills links of projects in place.
set -eu

SETTINGS="$HOME/.claude/settings.json"

if [ -f "$SETTINGS" ]; then
  python3 - "$SETTINGS" <<'EOF'
import json, shutil, sys

path = sys.argv[1]
command = "~/.claude/hooks/cursor-rules.py"
with open(path) as f:
    settings = json.load(f)
shutil.copy2(path, path + ".bak")

hooks = settings.get("hooks", {})
for event in ("SessionStart", "PreToolUse"):
    kept = []
    for entry in hooks.get(event, []):
        entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != command]
        if entry["hooks"]:
            kept.append(entry)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
if not hooks:
    settings.pop("hooks", None)

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
EOF
fi

rm -f "$HOME/.claude/hooks/cursor-rules.py"
echo "Uninstalled."
