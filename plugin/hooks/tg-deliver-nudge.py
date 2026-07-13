#!/usr/bin/env python3
# tg-deliver-nudge.py - PostToolUse hook
#
# Purpose: the instant Claude CREATES a document (Write / NotebookEdit), tap it
# on the shoulder so it hands the file to the user itself - tappable in Telegram,
# or opened/printed in a terminal/desktop session - instead of leaving a file path
# they can't reach. Claude keeps full judgment about whether the document is a real
# deliverable; this hook only injects a reminder at the exact moment it matters,
# which is far more reliable than a session-start rule that fades as the session
# fills up.
#
# SAFETY CONTRACT (this hook must never break a single thing):
#   - It runs AFTER the tool already executed, so it cannot block, undo, or
#     interrupt anything.
#   - On ANY error it emits nothing and exits 0 (fail-silent).
#   - It writes to stdout ONLY on the success path, and only a single valid JSON
#     object. Every no-op path writes nothing.
#   - It never sends anything and never touches the user's files. It only adds a
#     note to Claude's context.
#   - Kill switch: create $FLEET_HOME/deliver-nudge.off to disable (FLEET_HOME
#     defaults to ~/.claude/telegram-fleet). This lives OUTSIDE the plugin dir on
#     purpose: the plugin cache is replaced on every update, so a kill switch kept
#     inside it would silently reset - the fleet home is stable across updates.
#
# Output schema (Claude Code PostToolUse additionalContext):
#   {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}

import sys
import os
import json

FLEET_HOME = os.environ.get("FLEET_HOME") or os.path.expanduser("~/.claude/telegram-fleet")
KILL_SWITCH = os.path.join(FLEET_HOME, "deliver-nudge.off")

# Tools that bring a document into existence with content. Deliberately NOT
# "Edit" - iterative edits would fire repeat nudges, and a document almost always
# comes into being via Write (or NotebookEdit for notebooks).
CREATE_TOOLS = ("Write", "NotebookEdit")

# Deliverable-ish document extensions. Things the user might read, send, or copy-paste.
DOC_EXTS = (
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".csv", ".md", ".markdown", ".txt", ".rtf", ".html", ".htm",
    ".odt", ".ipynb",
)

# Path fragments that are NEVER a user deliverable - skip the nudge entirely so
# it stays quiet on internal housekeeping. (Lowercased substring match against
# the absolute path with a trailing slash.) Note: /tmp is intentionally NOT here
# - generated PDFs/images are routinely staged in /tmp.
EXCLUDE_FRAGMENTS = (
    os.path.expanduser("~").lower() + "/.claude/",  # memory, hooks, settings, specs
    "/.git/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/.next/",
    "/__pycache__/",
    "/.cache/",
)


def build_message(filename, is_telegram):
    if is_telegram:
        deliver = (
            "Hand it to the user in your Telegram reply - attach it via the reply tool's "
            "files=[...] so it lands as a tappable file, not a path. If it is something "
            "they would read or forward (proposal, report, summary, any formatted doc), "
            "give it as a PDF - that is their default for anything they would look at or "
            "send. If it is something they would copy-paste (a handoff prompt for a person "
            "/ another AI / another session, or a snippet), put the text in the message or "
            "attach it as plain text so it is easy to copy."
        )
    else:
        deliver = (
            "Present it to the user directly - print the full content inline and/or open "
            "the file on screen so they can read or grab it now, never just a path. "
            "Read/forward docs should be a PDF; copy-paste material should be shown as text."
        )
    return (
        "DELIVERABLE NUDGE (automatic, from the document-surfacing hook). "
        "You just created the document \"%s\". Before you finish this turn, decide whether "
        "the user would actually want it - something they asked you to produce, or would "
        "want to read, send, or copy-paste. Lean toward giving it to them. %s "
        "Skip ONLY pure-internal artifacts they cannot use: setups-log entries, vault "
        "housekeeping, your own memory/scratch notes, or code/config files. If you have "
        "already surfaced it, ignore this."
    ) % (filename, deliver)


def main():
    if os.path.exists(KILL_SWITCH):
        return

    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return

    data = json.loads(raw)
    if not isinstance(data, dict):
        return

    # Only nudge the main thread. A subagent writing a file has no Telegram chat
    # to reply to, and the main agent is what talks to the user.
    if data.get("agent_id") or data.get("agentId"):
        return

    tool = data.get("tool_name") or ""
    if tool not in CREATE_TOOLS:
        return

    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return
    fp = ti.get("file_path") or ti.get("notebook_path") or ""
    if not fp or not isinstance(fp, str):
        return

    low_ext = fp.lower()
    if not low_ext.endswith(DOC_EXTS):
        return

    norm = os.path.abspath(os.path.expanduser(fp)).lower()
    haystack = norm + "/"
    for frag in EXCLUDE_FRAGMENTS:
        if frag in haystack:
            return

    filename = os.path.basename(fp)
    is_tg = bool(os.environ.get("TELEGRAM_STATE_DIR"))
    msg = build_message(filename, is_tg)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail-silent: never break a turn. Emit nothing.
        pass
    sys.exit(0)
