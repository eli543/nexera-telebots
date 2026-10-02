#!/usr/bin/env python3
import sys
import os
import json
import stat
import time

KILL_SWITCH = os.path.join(os.environ.get("FLEET_HOME") or os.path.expanduser("~/.claude/telegram-fleet"), "deliver-nudge.off")


CREATE_TOOLS = ("Write", "NotebookEdit")


DOC_EXTS = (
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".csv", ".md", ".markdown", ".txt", ".rtf", ".html", ".htm",
    ".odt", ".ipynb",
)


EXCLUDE_FRAGMENTS = (
    os.path.expanduser("~").lower() + "/.claude/",
    "/.git/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/.next/",
    "/__pycache__/",
    "/.cache/",
)


RATE_STATE_FILE = "ratelimit.json"
RATE_COOLDOWN_MAX = 86400.0
RATE_MAX_BYTES = 1000000


COOLDOWN_SUPPRESS_MIN = 60.0


def _small_json(path):
    """Parse a small regular JSON file, else None. Stats before it opens, so a FIFO, a
    directory or an oversized file is skipped instead of read."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > RATE_MAX_BYTES:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cooldown_until(state):
    """Epoch seconds when this chat's flood-ban cooldown expires, 0.0 if none is live."""
    try:
        sidecar = _small_json(os.path.join(state, "stream", RATE_STATE_FILE)) or {}
        cooldown = sidecar.get("cooldown")
        if not isinstance(cooldown, dict) or not cooldown:
            return 0.0
        access = _small_json(os.path.join(state, "access.json")) or {}
        allow = access.get("allowFrom") or []
        row = cooldown.get(str(allow[0])) if allow else None
        if not isinstance(row, dict):
            return 0.0
        until = float(row.get("until") or 0)
        remaining = until - time.time()
        if remaining < COOLDOWN_SUPPRESS_MIN or remaining > RATE_COOLDOWN_MAX:
            return 0.0
        return until
    except Exception:
        return 0.0


def _clock(epoch):
    """Wall-clock label for an epoch in the machine's own zone ."""
    try:
        t = time.localtime(epoch)
        stamp = "%d:%02d%s %s" % (t.tm_hour % 12 or 12, t.tm_min,
                                  "am" if t.tm_hour < 12 else "pm",
                                  time.strftime("%Z", t))
        if t.tm_yday != time.localtime().tm_yday:
            stamp = time.strftime("%A ", t) + stamp
        return stamp
    except Exception:
        return ""


def cooldown_note(filename, until):
    when = _clock(until)
    return (
        "delivery guard: chat under Telegram cooldown%s, not nudging. Telegram is "
        "rejecting sends for this chat (HTTP 429), so do not try to deliver \"%s\" "
        "over Telegram until the cooldown clears."
    ) % ((" until " + when) if when else "", filename)


def build_message(filename, is_telegram=True):
    return (
        "DELIVERABLE NUDGE: You created the document \"%s\". If it is a requested "
        "deliverable, attach it with the Telegram reply tool files=[...] so the user can "
        "open it. Use PDF for a formatted document when appropriate, and plain text for "
        "copy-paste material. Skip internal notes and code/config files. If you already "
        "delivered it, ignore this reminder."
    ) % filename


def main():
    if not os.environ.get("TELEGRAM_STATE_DIR"):
        return
    if os.path.exists(KILL_SWITCH):
        return

    raw = sys.stdin.read()
    if not raw or not raw.strip():
        return

    data = json.loads(raw)
    if not isinstance(data, dict):
        return


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
    state = os.environ.get("TELEGRAM_STATE_DIR") or ""
    until = _cooldown_until(state) if state else 0.0
    if until > 0:
        msg = cooldown_note(filename, until)
    else:
        msg = build_message(filename, bool(state))
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

        pass
    sys.exit(0)
