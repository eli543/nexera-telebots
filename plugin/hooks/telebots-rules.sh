#!/usr/bin/env bash
# Render channel instructions from installed fleet data, only for bot sessions.
set -eu
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0
hook_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 - "$hook_dir/../rules/TELEBOTS-RULES.md" <<'PY'
import json
import os
from pathlib import Path
import re
import sys


def fail():
    # Config values can contain private text. Never echo them in diagnostics.
    sys.stderr.write("telebots-rules: could not render valid rules within 9000 characters; check fleet.json and ux.json.\n")
    raise SystemExit(1)


def path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing path")
    return Path(value).expanduser().resolve()


def one_line(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid text")
    return value


try:
    fleet_home = path(os.environ.get("FLEET_HOME") or str(Path.home() / ".claude/telegram-fleet"))
    config_path = path(os.environ.get("FLEET_CONFIG") or str(fleet_home / "fleet.json"))
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {"bots": [], "options": {}}
    if not isinstance(config, dict) or not isinstance(config.get("bots", []), list):
        raise ValueError("invalid fleet")
    bots = config.get("bots", [])
    options = config.get("options", {})
    if not isinstance(options, dict) or any(not isinstance(bot, dict) for bot in bots):
        raise ValueError("invalid fleet")
    state_dir = path(os.environ["TELEGRAM_STATE_DIR"])
    matches = [bot for bot in bots if path(bot.get("state_dir")) == state_dir]
    if len(matches) > 1:
        raise ValueError("ambiguous bot")
    bot_name = one_line(matches[0].get("name") or "Claude") if matches else "Claude"
    notes_dir = str(path(options.get("notes_dir") or "~/Telebots Notes"))
    ux = json.loads((fleet_home / "ux.json").read_text(encoding="utf-8"))
    reactions = ux["reactions"]
    if not isinstance(reactions, list) or len(reactions) != 7:
        raise ValueError("invalid reactions")
    rows = []
    seen = set()
    for reaction in reactions:
        emoji = one_line(reaction["emoji"])
        phrase = one_line(reaction["phrase"])
        key_line = one_line(reaction["key_line"])
        if emoji in seen or "\ufe0f" in emoji:
            raise ValueError("invalid emoji")
        seen.add(emoji)
        # JSON quoting preserves the exact phrase and legend without treating
        # punctuation in local configuration as a Markdown table delimiter.
        rows.append("- " + emoji + ": " + json.dumps(phrase, ensure_ascii=False)
                    + "; /key: " + json.dumps(key_line, ensure_ascii=False))
    if seen != {"❤", "👍", "👎", "🔥", "👏", "😁", "🥰"}:
        raise ValueError("unknown reaction")
    substitutions = {
        "BOT_NAME": json.dumps(bot_name, ensure_ascii=False),
        "NOTES_DIR": json.dumps(notes_dir, ensure_ascii=False),
        "FLEET_SIZE": str(len(bots) or 1),
        "REACTIONS_TABLE": "\n".join(rows),
    }
    template = Path(sys.argv[1]).read_text(encoding="utf-8")
    names = re.findall(r"\{\{([A-Z_]+)\}\}", template)
    if set(names) != set(substitutions):
        raise ValueError("invalid template")
    rendered = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: substitutions[match[1]], template)
    if len(rendered) > 9000:
        raise ValueError("rules too large")
    output = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rendered}}, ensure_ascii=False)
    if len(output) > 10000:
        raise ValueError("hook output too large")
except (OSError, ValueError, KeyError, TypeError, RuntimeError):
    fail()
print(output)
PY
