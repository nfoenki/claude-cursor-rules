#!/usr/bin/env python3
"""Expose Cursor rules (.cursor/rules) and skills (.cursor/skills) to Claude Code.

One script, two hook events (see ~/.claude/settings.json):

SessionStart
  - project root = outermost git repository containing the session directory (a session opened in a
    nested clone still gets the parent project's rules); outside git, outermost ancestor holding a
    .cursor/ folder. Never climbs to $HOME or above.
  - alwaysApply rules of the root scope (or of a nested scope containing the session dir) -> inlined
  - other rules and skills -> index
  - .cursor/skills -> linked as .claude/skills so Claude Code loads them natively (from the next
    session on). Never deletes anything; the links are added to .git/info/exclude.
  - session opened in a folder holding several projects -> short per-project index only.

PreToolUse (Read / Edit / Write / MultiEdit / NotebookEdit)
  - finds the project of the touched file, and injects every rule that applies to it and has not been
    injected yet in this session: glob-matched rules, and alwaysApply rules of its scopes.

Prints nothing when no Cursor rules apply. Never fails nor blocks anything.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "vendor", ".venv", "venv", "coverage",
             ".next", ".turbo", ".cache", "target", "__pycache__", "Library", ".Trash"}
MAX_DEPTH = 8           # descendants scanned below the project root
WORKSPACE_DEPTH = 4     # descendants scanned when the session dir is not a project
ALWAYS_BUDGET = 40_000  # chars of alwaysApply content inlined at session start
TOOL_BUDGET = 30_000    # chars of rule content injected before one tool call
DEADLINE = time.monotonic() + 6  # the hook timeout is 10 s: stop scanning well before
HOME = os.path.realpath(os.path.expanduser("~"))
STATE_DIR = os.path.join(tempfile.gettempdir(), "claude-cursor-rules")
EXCLUDE_HEADER = "# Cursor skills linked for Claude Code by ~/.claude/hooks/cursor-rules.py"


# ---------------------------------------------------------------- discovery

def has_cursor(d):
    return (os.path.isdir(os.path.join(d, ".cursor", "rules"))
            or os.path.isdir(os.path.join(d, ".cursor", "skills"))
            or os.path.isfile(os.path.join(d, ".cursorrules")))


def ancestors(path):
    """path, its parent, ... stopping before $HOME and before the filesystem root."""
    out, d = [], path
    while d not in (HOME, os.sep) and os.path.dirname(d) != d:
        out.append(d)
        d = os.path.dirname(d)
    return out


def find_root(path):
    chain = ancestors(path)
    gits = [d for d in chain if os.path.exists(os.path.join(d, ".git"))]
    if gits:
        return gits[-1]
    cursors = [d for d in chain if has_cursor(d)]
    return cursors[-1] if cursors else None


def nearest_git(path):
    for d in ancestors(path):
        if os.path.exists(os.path.join(d, ".git")):
            return d
    return None


def is_within(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def rel(path, root):
    return os.path.relpath(path, root)


def parse_frontmatter(text):
    """Flat YAML subset: `key: value`, inline lists `[a, b]`, block lists (`key:` then `- item`)."""
    meta, body = {}, text
    if not text.startswith("---"):
        return meta, body
    end = text.find("\n---", 3)
    if end == -1:
        return meta, body
    key = None
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and key:
            item = stripped[2:].strip().strip('"').strip("'")
            meta[key] = f"{meta[key]},{item}" if meta[key] else item
            continue
        if ":" in line and not line[:1].isspace():
            k, v = line.split(":", 1)
            key, v = k.strip(), v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = ",".join(p.strip().strip('"').strip("'") for p in v[1:-1].split(",") if p.strip())
            meta[key] = v.strip('"').strip("'")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def scan(root, max_depth):
    rules, skills, legacy = [], [], []
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() > DEADLINE:
            break
        depth = dirpath.count(os.sep) - root_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and depth < max_depth]
        if ".cursorrules" in filenames:
            legacy.append(os.path.join(dirpath, ".cursorrules"))
        parts = dirpath.split(os.sep)
        if ".cursor" in parts:
            i = len(parts) - 1 - parts[::-1].index(".cursor")
            sub = parts[i + 1] if i + 1 < len(parts) else None
            scope = os.sep.join(parts[:i]) or os.sep
            if sub == "rules":
                for f in filenames:
                    if f.endswith((".mdc", ".md")):
                        rules.append((os.path.join(dirpath, f), scope))
            elif sub == "skills" and "SKILL.md" in filenames:
                skills.append(os.path.join(dirpath, "SKILL.md"))
    return sorted(rules), sorted(skills), sorted(legacy)


def load_rules(root, max_depth=MAX_DEPTH):
    """-> (rules, skills, legacy); each rule is a dict path/scope/kind/desc/globs/body."""
    raw_rules, skills, legacy = scan(root, max_depth)
    rules = []
    for path, scope in raw_rules:
        text = read(path)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        globs = meta.get("globs", "")
        desc = meta.get("description", "")
        if meta.get("alwaysApply", "").lower() == "true":
            kind = "always"
        elif globs:
            kind = "globs"
        elif desc:
            kind = "requested"
        else:
            kind = "manual"
        rules.append({"path": path, "scope": scope, "kind": kind, "desc": desc, "globs": globs, "body": body.strip()})
    return rules, skills, legacy


# ---------------------------------------------------------------- globs

def split_globs(value):
    """Comma-separated patterns; commas inside {braces} do not split."""
    out, cur, depth = [], "", 0
    for c in value:
        if c == "{":
            depth += 1
        elif c == "}" and depth:
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    out.append(cur)
    return [p.strip().strip('"').strip("'") for p in out if p.strip().strip('"').strip("'")]


def glob_to_regex(pattern):
    p = pattern[2:] if pattern.startswith("./") else pattern
    p = p.lstrip("/")
    if p.endswith("/"):
        p += "**"
    if "/" not in p:  # bare pattern (`*.lua`): matches at any depth, like gitignore
        p = "**/" + p
    out, i, depth = "", 0, 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 2] == "**":
                if p[i + 2:i + 3] == "/":
                    out += "(?:.*/)?"
                    i += 3
                else:
                    out += ".*"
                    i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "{":
            out += "(?:"
            depth += 1
        elif c == "}" and depth:
            out += ")"
            depth -= 1
        elif c == "," and depth:
            out += "|"
        elif c == "[" and "]" in p[i + 1:]:
            j = p.index("]", i + 1)
            cls = p[i + 1:j]
            out += "[" + ("^" + cls[1:] if cls.startswith("!") else cls) + "]"
            i = j
        else:
            out += re.escape(c)
        i += 1
    return "^" + out + "$"


def glob_match(globs, relpath):
    for pattern in split_globs(globs):
        try:
            if re.match(glob_to_regex(pattern), relpath):
                return pattern
        except re.error:
            continue
    return None


# ---------------------------------------------------------------- session state

def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "")
    return os.path.join(STATE_DIR, safe + ".json") if safe else None


def load_state(session_id):
    path = state_path(session_id)
    data = read(path) if path else None
    try:
        return set(json.loads(data)) if data else set()
    except Exception:
        return set()


def save_state(session_id, injected):
    path = state_path(session_id)
    if not path:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(injected), f)
    except Exception:
        pass


def prune_state():
    try:
        limit = time.time() - 7 * 86400
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if os.path.getmtime(path) < limit:
                os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------- skills links

def git_exclude(scope, entries):
    """Adds /path entries to the info/exclude of the repository holding scope."""
    if not entries:
        return
    try:
        top = subprocess.run(["git", "-C", scope, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        git_path = subprocess.run(["git", "-C", scope, "rev-parse", "--git-path", "info/exclude"],
                                  capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return
    if not top or not git_path:
        return
    exclude = git_path if os.path.isabs(git_path) else os.path.join(scope, git_path)
    lines = (read(exclude) or "").splitlines()
    wanted = ["/" + rel(os.path.join(scope, e), os.path.realpath(top)) for e in entries]
    missing = [w for w in wanted if w not in lines]
    if not missing:
        return
    try:
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            prefix = "" if not lines or lines[-1] == "" else "\n"
            header = [] if EXCLUDE_HEADER in lines else [EXCLUDE_HEADER]
            f.write(prefix + "\n".join(header + missing) + "\n")
    except Exception:
        pass


def link_skills(scope):
    """Links scope/.cursor/skills into scope/.claude/skills without deleting anything.
    -> (names already reachable through .claude/skills before this run, names linked now)."""
    src = os.path.join(scope, ".cursor", "skills")
    dst = os.path.join(scope, ".claude", "skills")
    try:
        names = sorted(n for n in os.listdir(src) if os.path.isfile(os.path.join(src, n, "SKILL.md")))
    except Exception:
        return set(), set()
    before = {n for n in names if os.path.isfile(os.path.join(dst, n, "SKILL.md"))}
    linked, entries = set(), []
    try:
        if not os.path.lexists(dst):
            claude_dir = os.path.dirname(dst)
            if os.path.lexists(claude_dir) and not os.path.isdir(claude_dir):
                return before, linked
            os.makedirs(claude_dir, exist_ok=True)
            os.symlink(os.path.join("..", ".cursor", "skills"), dst)
            linked, entries = set(names), [".claude/skills"]
        elif os.path.isdir(dst) and not os.path.islink(dst):
            for n in names:
                if not os.path.lexists(os.path.join(dst, n)):
                    os.symlink(os.path.join("..", "..", ".cursor", "skills", n), os.path.join(dst, n))
                    linked.add(n)
                    entries.append(f".claude/skills/{n}")
        # dst is a symlink or a file of the user's own: left untouched
    except Exception:
        pass
    git_exclude(scope, entries)
    return before, linked


# ---------------------------------------------------------------- SessionStart

def rule_heading(rule, root):
    scope_rel = rel(rule["scope"], root)
    scope_note = "" if scope_rel == "." else f" (scope: {scope_rel}/, globs relative to it)"
    return rel(rule["path"], root) + scope_note


def project_context(root, cwd, session_id):
    rules, skills, legacy = load_rules(root)
    if not rules and not skills and not legacy:
        return None

    always, index, injected = [], [], set()
    used = 0
    for rule in rules:
        head, desc = rule_heading(rule, root), rule["desc"]
        if rule["kind"] == "always":
            if rule["scope"] != root and not is_within(cwd, rule["scope"]):
                index.append(f"- [ALWAYS within scope] {head} — injected when you touch a file under it — {desc}")
            elif used + len(rule["body"]) <= ALWAYS_BUDGET:
                quote = f"> {desc}\n" if desc else ""
                always.append(f"### {head}\n{quote}\n{rule['body']}\n")
                used += len(rule["body"])
                injected.add(rule["path"])
            else:
                index.append(f"- [ALWAYS - read it now, not inlined for size] {head} — {desc}")
        elif rule["kind"] == "globs":
            index.append(f"- [auto-attached] {head} — globs: `{rule['globs']}` — {desc}")
        elif rule["kind"] == "requested":
            index.append(f"- [agent-requested] {head} — {desc}")
        else:
            index.append(f"- [manual] {head}")
    save_state(session_id, injected)

    native, manual_only, skill_lines = [], [], []
    scopes = sorted({p.split(os.sep + ".cursor" + os.sep)[0] for p in skills})
    cwd_git = nearest_git(cwd)
    status = {}
    for scope in scopes:
        before, linked = link_skills(scope)
        visible = is_within(cwd, scope) and (cwd_git is None or is_within(scope, cwd_git))
        status[scope] = (before if visible else set(), linked)
    for path in skills:
        scope = path.split(os.sep + ".cursor" + os.sep)[0]
        meta, _ = parse_frontmatter(read(path) or "")
        name = meta.get("name") or os.path.basename(os.path.dirname(path))
        folder = os.path.basename(os.path.dirname(path))
        before, linked = status.get(scope, (set(), set()))
        if folder in before:
            if meta.get("disable-model-invocation", "").lower() == "true":
                manual_only.append(name)
            else:
                native.append(name)
            continue
        note = " — linked into .claude/skills just now: native from the next session" if folder in linked else ""
        if meta.get("disable-model-invocation", "").lower() == "true":
            note += " — user-invocable only: follow it only when the user asks for it"
        skill_lines.append(f"- {name} ({rel(path, root)}) — {meta.get('description', '')}{note}")

    where = "" if cwd == root else (
        f" Session opened in `{rel(cwd, root)}/`, inside this project: the project rules apply there too, "
        f"and paths below are relative to the project root.")
    out = [
        f"# Cursor rules for this project ({root})",
        "",
        "This project defines Cursor rules. `.cursor/rules/` is the source of truth; treat these rules as "
        "project instructions with the same authority as CLAUDE.md." + where + " How they reach you:",
        "- alwaysApply rules (inlined below) apply to all work in the repository.",
        "- [auto-attached] and [ALWAYS within scope] rules are injected automatically, as a hook message, the "
        "first time you Read, Edit or Write a file they cover; follow them as soon as they appear. If you "
        "change files another way (Bash, sed, scripts), Read the matching rule files yourself first.",
        "- [agent-requested] rules: Read the rule when its description is relevant to the current task.",
        "- [manual] rules: only apply when the user mentions them (e.g. @rule-name).",
        "- If a rule requires a tool that is not available (e.g. an MCP server), say so instead of silently skipping it.",
    ]
    if legacy:
        out += ["", "Legacy `.cursorrules` files (read and apply them as always-on rules): "
                + ", ".join(rel(p, root) for p in legacy)]
    if always:
        out += ["", "## alwaysApply rules", ""] + always
    if index:
        out += ["", "## Other rules"] + index
    if native or manual_only or skill_lines:
        out += ["", "## Cursor skills"]
        if native:
            out.append("Available natively (invoke them with the Skill tool): " + ", ".join(native))
        if manual_only:
            out.append("Loaded natively but user-invocable only (disable-model-invocation): "
                       + ", ".join(f"/{n}" for n in manual_only) + " — do not start them yourself; suggest them when relevant.")
        if skill_lines:
            out.append("Not loaded as native skills in this session: when a task matches one, Read its SKILL.md and follow it.")
            out += skill_lines
    return "\n".join(out)


def workspace_context(cwd, session_id):
    """Session dir holds several projects: list them, inline nothing."""
    save_state(session_id, set())
    if cwd == HOME:
        return None
    raw_rules, skills, legacy = scan(cwd, WORKSPACE_DEPTH)
    projects = {}
    for path, scope in raw_rules:
        meta, _ = parse_frontmatter(read(path) or "")
        entry = projects.setdefault(scope, {"always": [], "other": 0, "skills": 0})
        if meta.get("alwaysApply", "").lower() == "true":
            entry["always"].append(os.path.basename(path))
        else:
            entry["other"] += 1
    for path in skills:
        scope = path.split(os.sep + ".cursor" + os.sep)[0]
        projects.setdefault(scope, {"always": [], "other": 0, "skills": 0})["skills"] += 1
    for path in legacy:
        projects.setdefault(os.path.dirname(path), {"always": [], "other": 0, "skills": 0})["always"].append(".cursorrules")
    if not projects:
        return None

    lines = [
        f"# Cursor rules in this workspace ({cwd})",
        "",
        "The session directory is not a single project: it contains several projects with Cursor rules. "
        "None is loaded yet. The rules of a project (alwaysApply and matching globs) are injected "
        "automatically the first time you Read, Edit or Write one of its files. Before other work on a "
        "project (planning, Bash edits), Read its alwaysApply rules listed below, and its "
        "`.cursor/skills/` when a skill fits. Never apply one project's rules to another project.",
        "",
    ]
    for scope in sorted(projects):
        p = projects[scope]
        always = ", ".join(f"{rel(scope, cwd)}/.cursor/rules/{n}" if n != ".cursorrules" else f"{rel(scope, cwd)}/.cursorrules"
                           for n in p["always"]) or "none"
        lines.append(f"- `{rel(scope, cwd)}/` — alwaysApply: {always}; other rules: {p['other']}; skills: {p['skills']}")
    return "\n".join(lines)


def session_start(payload):
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())
    session_id = payload.get("session_id")
    prune_state()
    root = find_root(cwd)
    context = project_context(root, cwd, session_id) if root else workspace_context(cwd, session_id)
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


# ---------------------------------------------------------------- PreToolUse

def pre_tool_use(payload):
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())
    target = os.path.realpath(target if os.path.isabs(target) else os.path.join(cwd, target))
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    root = find_root(folder)
    if not root:
        return
    rules, _, _ = load_rules(root)
    session_id = payload.get("session_id")
    injected = load_state(session_id)

    matched = []
    for rule in rules:
        if rule["path"] in injected or not is_within(target, rule["scope"]):
            continue
        if rule["kind"] == "always":
            matched.append((rule, "alwaysApply"))
        elif rule["kind"] == "globs":
            pattern = glob_match(rule["globs"], rel(target, rule["scope"]))
            if pattern:
                matched.append((rule, f"glob `{pattern}`"))
    if not matched:
        return

    blocks, overflow, used = [], [], 0
    for rule, why in matched:
        head = rule_heading(rule, root)
        if used + len(rule["body"]) <= TOOL_BUDGET:
            quote = f"> {rule['desc']}\n" if rule["desc"] else ""
            blocks.append(f"### {head} — {why}\n{quote}\n{rule['body']}\n")
            used += len(rule["body"])
        else:
            overflow.append(f"- {head} — {why}")
        injected.add(rule["path"])
    save_state(session_id, injected)

    out = [f"# Cursor rules for `{rel(target, root)}` (project {root})",
           "",
           "These Cursor rules apply to the file you are about to touch. Follow them for this file and "
           "every other file they cover, for the rest of the session. They will not be repeated."]
    if blocks:
        out += [""] + blocks
    if overflow:
        out += ["", "Not inlined for size, Read them now:"] + overflow
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(out)}}))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if payload.get("hook_event_name") == "PreToolUse":
        pre_tool_use(payload)
    else:
        session_start(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the session nor the tool call
        print(f"cursor-rules hook error: {e}", file=sys.stderr)
    sys.exit(0)
