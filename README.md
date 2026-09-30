# claude-cursor-rules

Make [Claude Code](https://claude.com/claude-code) follow the **Cursor rules** (`.cursor/rules/*.mdc`) and **Cursor skills** (`.cursor/skills/`) of any project, with no change to the project itself.

Claude Code does not read Cursor's `.mdc` format, and it only loads skills from `.claude/skills/`. This repository installs a global hook that bridges the two, so a team can keep a single set of rules and skills in `.cursor/` whether its developers use Cursor, Claude Code, or both.

## Requirements

- Claude Code (CLI or IDE extension)
- `python3` (standard library only)
- macOS or Linux

## Install

```sh
git clone https://github.com/nfoenki/claude-cursor-rules.git ~/claude-cursor-rules
~/claude-cursor-rules/install.sh
```

Then open a new Claude Code session in a project that has a `.cursor/` folder.

`install.sh`:
- copies `cursor-rules.py` to `~/.claude/hooks/`;
- adds a `SessionStart` hook and a `PreToolUse` hook to `~/.claude/settings.json`.

Your existing settings and hooks are kept. The previous file is saved as `settings.json.bak`, and running the script again does not duplicate the hooks.

**Update:** `git pull`, then run `install.sh` again.
**Uninstall:** run `uninstall.sh`.

## What it does

| Cursor | In Claude Code |
|---|---|
| Rule with `alwaysApply: true` | Loaded at the start of every session |
| Rule with `globs` | Injected the first time Claude reads or edits a matching file |
| Rule with only a `description` | Listed; Claude reads it when it is relevant to the task |
| Manual rule | Applied when you mention it (`@rule-name`) |
| Skill `.cursor/skills/<name>/SKILL.md` | Available as a native Claude Code skill, including `/<name>` |

**Rules** are injected as context through Claude Code hooks. The hook reads `.cursor/rules/`, parses each rule's frontmatter, and returns the matching rules as `additionalContext`. Each rule is injected at most once per session.

**Skills** are linked, not copied: the hook creates `.claude/skills` → `.cursor/skills` in the project, so Claude Code loads them natively. If the project already has a real `.claude/skills/` folder, the hook links each missing skill individually and never replaces an existing one. The links are added to `.git/info/exclude`, so they never show up in `git status`.

The hook never deletes anything and never blocks a tool call. In a project without a `.cursor/` folder, it does nothing.

**Overhead:** about 30 ms per hook call. Rule content is added to the context only once per session.

## Limits

- **New skill during a conversation:** available from the next conversation, because Claude Code loads skills at startup. The same applies to a new description-only rule, which appears in the list from the next conversation.
- **New `alwaysApply` or glob rule:** applies in the current conversation, the next time a file is read or edited.
- **Edited rule:** if it was already injected, the new version is not re-injected during the conversation. Start a new one.
- **Shell access:** reading or editing a file through the shell (`sed`, `cat`, `grep`) does not trigger glob rule injection.
- **Size:** above about 40,000 characters of `alwaysApply` rules, or 30,000 characters injected before a single file access, the extra rules are only listed, and Claude has to read them itself.
- **Nested repositories:** in a session opened inside a git repository nested in another one, Claude Code does not see the parent's skills. They are only listed.

## License

MIT
