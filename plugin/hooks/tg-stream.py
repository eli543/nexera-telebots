#!/usr/bin/env python3
"""Telegram live work box: mission, tasks, progress and folded recap."""

import sys
import os
import json
import math
import re
import stat
import time
import fcntl
import shlex
import signal
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo


WORKING_EMOJI = os.environ.get("STREAM_EMOJI", "\U0001F47E")
THINKING_EMOJI = os.environ.get("STREAM_THINK_EMOJI", "\U0001F4AD")


MIN_EDIT_INTERVAL = float(os.environ.get("STREAM_MIN_EDIT", "2.5"))


TYPING_MIN_INTERVAL = float(os.environ.get("STREAM_TYPING_MIN", "4.0"))


ANIM_HANDOFF_TICKS = float(os.environ.get("STREAM_ANIM_HANDOFF", "3.0"))


LOCK_WAIT_TOOL = float(os.environ.get("STREAM_LOCK_WAIT", "2.0"))


LOCK_WAIT_POST_FACTOR = 3.0
LOCK_WAIT_POLL = 0.01


RICH_AGENT_EDIT_STEP = 0.5


def __getattr__(name):
    if name in ("request", "parse"):
        import urllib.parse
        import urllib.request
        globals()["request"] = urllib.request
        globals()["parse"] = urllib.parse
        return globals()[name]
    if name in ("HTTPError", "URLError"):
        import urllib.error
        globals()["HTTPError"] = urllib.error.HTTPError
        globals()["URLError"] = urllib.error.URLError
        return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
STALE_SECONDS = float(os.environ.get("STREAM_STALE", "240"))
ANIM_INTERVAL = float(os.environ.get("STREAM_ANIM", "3"))
ANIM_MAX_RUN = float(os.environ.get("STREAM_ANIM_MAX", "7200"))
ANIM_MAX_IDLE = float(os.environ.get("STREAM_ANIM_IDLE", "1800"))


ANIM_RESPAWN_MIN_LIFE = float(os.environ.get("STREAM_ANIM_RESPAWN_MIN", "30"))


NOREPLY_GRACE = float(os.environ.get("STREAM_NOREPLY_GRACE", "90"))


CODEX_BG_MAX_AGE = float(os.environ.get("STREAM_CODEX_BG_MAX_AGE", "7200"))
CODEX_PROCESS_KEY_MAX = 1024


CODEX_PROCESS_START_GRACE = 3.0


CODEX_KEYLESS_HOLD_MAX = float(os.environ.get("STREAM_CODEX_KEYLESS_HOLD", "900"))


BASH_TASK_PATH_MAX = 1024
BASH_TASK_ID_MAX = 128
BASH_TASK_TAIL_BYTES = 4096


BASH_TASK_EXIT_PREFIX = "[exited"
BASH_TASK_KILLED_MARK = "[killed]"
BASH_BG_LABEL = "background task"
MAX_STORED_STEPS = 600
TG_HARD_LIMIT = 4000
LIVE_CAP = 100
LIVE_STREAM = 8
HTTP_TIMEOUT = 3
BAR_CELLS = 5
BAR_WIN = 1
BAR_FILL = "▪︎"
BAR_EMPTY = "▫︎"
SUB = "  ↳ "


RICH_FLAG = "rich-stream"


RICH_MIN_EDIT_INTERVAL = 1.5
RICH_ANIM_INTERVAL = 1.5
RICH_SLOW_INTERVAL = 4.5


CADENCE_FAST_MAX = float(os.environ.get("STREAM_CADENCE_FAST_MAX", "300"))
CADENCE_MEDIUM_MAX = float(os.environ.get("STREAM_CADENCE_MEDIUM_MAX", "900"))
CADENCE_SLOW_MAX = float(os.environ.get("STREAM_CADENCE_SLOW_MAX", "1800"))
CADENCE_MEDIUM_INTERVAL = float(os.environ.get("STREAM_CADENCE_MEDIUM", "3.0"))
CADENCE_SLOW_INTERVAL = float(os.environ.get("STREAM_CADENCE_SLOW", "4.5"))
CADENCE_MARATHON_INTERVAL = float(os.environ.get("STREAM_CADENCE_MARATHON", "6.0"))
CADENCE_MEDIUM_TYPING = float(os.environ.get("STREAM_CADENCE_MEDIUM_TYPING", "8.0"))
CADENCE_SLOW_TYPING = float(os.environ.get("STREAM_CADENCE_SLOW_TYPING", "12.0"))
CADENCE_MARATHON_TYPING = float(
    os.environ.get("STREAM_CADENCE_MARATHON_TYPING", "20.0"))
RICH_FRAME_LIMIT = 3500


RICH_TEXT_LIMIT = 32768
RICH_BLOCK_LIMIT = 500
RICH_RETRY_FRAME_LIMIT = 2400
RICH_ERROR_DESC_LIMIT = 180


RICH_BR = "  \n"


RICH_MISSION_CAP = 60
RICH_ACTIVITY_CAP = 46
RICH_BAR_CELLS = 8
RICH_TOOL_LINES = 60
RICH_PLAN_SUBJECT_CAP = 180
RICH_PLAN_META_CAP = 200
RICH_TOOL_LINE_CAP = 180
RICH_TABLE_CELL_CAP = 34
RICH_TRANSIENT_LIMIT = 3
RICH_RATE_LIMIT = 2
RICH_HARD_LIMIT = 2
RICH_HEALTH_LOG = "rich-health.log"
RICH_HEALTH_LINES = 200
RICH_OP_TARGETS = 32
RICH_EVENT_BUDGET = 18.0


RATE_STATE_FILE = "ratelimit.json"


RATE_COOLDOWN_MAX = float(os.environ.get("STREAM_RATE_COOLDOWN_MAX", "86400"))

RATE_COOLDOWN_MIN = float(os.environ.get("STREAM_RATE_COOLDOWN_MIN", "1"))


MAX_BOX_SENDS_PER_TURN = int(os.environ.get("STREAM_MAX_BOX_SENDS", "6"))


RATE_BUCKET_SECONDS = float(os.environ.get("STREAM_RATE_BUCKET", "10"))


RATE_WINDOWS = (
    (float(os.environ.get("STREAM_RATE_W1", "60")),
     float(os.environ.get("STREAM_RATE_B1", "100"))),
    (float(os.environ.get("STREAM_RATE_W2", "600")),
     float(os.environ.get("STREAM_RATE_B2", "500"))),
    (float(os.environ.get("STREAM_RATE_W3", "1800")),
     float(os.environ.get("STREAM_RATE_B3", "900"))),
    (float(os.environ.get("STREAM_RATE_W4", "3600")),
     float(os.environ.get("STREAM_RATE_B4", "1300"))),
)


RATE_LOW_WATER = float(os.environ.get("STREAM_RATE_LOW_WATER", "0.75"))


RATE_PRESSURE_MAX = float(os.environ.get("STREAM_RATE_PRESSURE_MAX", "20.0"))


RATE_LEDGER_SPAN = max((w for w, _ in RATE_WINDOWS), default=3600.0)


_SDIR_CHAT = {}


def _chat_for_sdir(sdir):
    try:
        return _SDIR_CHAT.get(sdir) if sdir else None
    except Exception:
        return None


BOT_IDENTITIES = {}


MISSION_EMOJI = os.environ.get("STREAM_MISSION_EMOJI", "\U0001F3AF")
AGENT_EMOJI = os.environ.get("STREAM_AGENT_EMOJI", "\U0001F916")
MISSION_FILE = "mission.txt"
MISSION_CAP = 60


MISSION_DEFAULT = "on it"


MISSION_EMPTY = "working"
MISSION_VOICE = "voice note"


VOICE_BODIES = ("voice message", "voice note", "voice", "audio message", "audio")
PANEL_ROWS = 6
PANEL_LABEL_CAP = 40
PANEL_TYPE_CAP = 24
PANEL_MODEL_CAP = 20
PANEL_MODEL_STORE = 60
PANEL_MAX_STORED = 60
PANEL_NO_MODEL = "session"


RICH_LIVE_LANE_ROWS = int(os.environ.get("STREAM_LIVE_LANE_ROWS", "4"))


CONTEXT_TAIL_BYTES = 256 * 1024
CONTEXT_MODEL_STATE_BYTES = 4096
CONTEXT_BAR_CELLS = 8
CONTEXT_BAR_FILL = "▓"
CONTEXT_BAR_EMPTY = "░"
CONTEXT_UNKNOWN_PCT = "??"


CONTEXT_EFFORT_TAGS = {
    "low": "L", "medium": "M", "high": "H", "xhigh": "XH", "max": "MX",
}


CONTEXT_SCAN_LIMIT = 500


BG_NOTIFY_TAIL_BYTES = int(os.environ.get(
    "STREAM_BG_NOTIFY_TAIL", str(CONTEXT_TAIL_BYTES)))


BG_NOTIFY_DEEP_TAIL_BYTES = int(os.environ.get(
    "STREAM_BG_NOTIFY_DEEP_TAIL", str(8 * CONTEXT_TAIL_BYTES)))
BG_NOTIFY_MAX_RECORDS = int(os.environ.get("STREAM_BG_NOTIFY_MAX", "24"))
BG_NOTIFY_SWEEP_MIN = float(os.environ.get("STREAM_BG_NOTIFY_SWEEP_MIN", "1.0"))
BG_NOTIFY_CLOCK_SLACK = 2.0
AGENT_HISTORY_MAX = 60
AGENT_HISTORY_LABEL_CAP = 60
AGENT_HISTORY_ID_CAP = 160
AGENT_HISTORY_SESSION_CAP = 96
AGENT_HISTORY_SESSION_MAX = 60


AGENT_COMPLETED_OUTCOMES = ("completed", "complete", "done", "success", "succeeded")
ET_ZONE = None  # datetime.fromtimestamp uses the system local time zone.


MODELS_CACHE_FILE = "models-cache.json"
MODELS_CACHE_TTL = 24 * 3600
MODELS_CACHE_RETRY = 3600
MODELS_CACHE_MAX_BYTES = 256 * 1024
MODELS_CACHE_MAX_MODELS = 200
MODELS_REFRESH_MODE = "models-refresh"
MODELS_REFRESH_TIMEOUT = 20
MODELS_API_URL = "https://api.anthropic.com/v1/models?limit=100"
MODELS_API_VERSION = "2023-06-01"
MODELS_KEYCHAIN_ACCOUNT = ""
MODELS_KEYCHAIN_SERVICE = "nexera-telebots-judge"
CLAUDE_LANE_FAMILIES = ("mythos", "fable", "opus", "sonnet", "haiku")
LANE_FAMILY_CAP = 24
CODEX_CONFIG_BYTES = 64 * 1024


AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
AGENT_QUEUED_PREFIX = "Message queued for delivery to "
AGENT_RESUME_LABEL = "resumed agent"
AGENT_META_MAX_BYTES = 4096
TOOL_RESULT_JSON_MAX = 64 * 1024


RECAP_MARK = "\U0001F4CB What I did"
INTERRUPTED_RECAP_MARK = "⏸ Interrupted by user"
INTERRUPTED_OUTCOME = "interrupted by user"
CONT_MARK = "\U0001F4CB continued"
CONT_DIVIDER = "· · ·"


REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
REACT_TOOL = "mcp__plugin_telegram_telegram__react"
PLUGIN_PREFIX = "mcp__plugin_telegram_telegram__"
MDV2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")

FAMILIES = {
    "files":   ("\U0001F50D", "Looking through files", "Looked through files", "file", "files"),
    "web":     ("\U0001F310", "Researching the web", "Researched the web", "web search", "web searches"),
    "write":   ("✏️", "Writing", "Wrote / edited files", "edit", "edits"),
    "run":     ("⚙️", "Running commands", "Ran commands", "command", "commands"),
    "helpers": ("\U0001F916", "Subagents", "Subagents", "subagent", "subagents"),
    "plan":    ("\U0001F4DD", "Planning", "Planned the work", "plan", "plans"),
    "tool":    ("\U0001F527", "Setting up tools", "Set up tools", "tool", "tools"),
    "other":   ("•", "Working", "Worked", "step", "steps"),
}


def hard_exit():
    sys.exit(0)


def fam_get(fam):
    return FAMILIES.get(fam, FAMILIES["other"])


def noun(fam, n):
    f = fam_get(fam)
    return f[3] if n == 1 else f[4]


def escape_mdv2(s):
    out = []
    for ch in s:
        if ch in MDV2_SPECIAL:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def tg_len(s):
    try:
        return len(s.encode("utf-16-le")) // 2
    except Exception:
        return len(s)


def _utf16_clip(s, max_units):
    if max_units <= 0:
        return ""
    try:
        b = s.encode("utf-16-le")
        if len(b) // 2 <= max_units:
            return s
        b = b[: max_units * 2]
        if len(b) >= 2:
            last = int.from_bytes(b[-2:], "little")
            if 0xD800 <= last <= 0xDBFF:
                b = b[:-2]
        out = b.decode("utf-16-le", "ignore")
        n = len(out) - len(out.rstrip("\\"))
        if n % 2 == 1:
            out = out[:-1]
        return out
    except Exception:
        out = s[:max_units]
        n = len(out) - len(out.rstrip("\\"))
        return out[:-1] if n % 2 == 1 else out


def fmt_elapsed(secs):
    secs = int(secs if secs and secs > 0 else 0)
    return "%d:%02d" % (secs // 60, secs % 60)


def activity_bar(frame):
    pos = frame % BAR_CELLS
    return "".join(BAR_FILL if ((i - pos) % BAR_CELLS) < BAR_WIN else BAR_EMPTY for i in range(BAR_CELLS))


def thinking_line(frame):
    dots = "." * (frame % 4)
    return "%s thinking%s" % (THINKING_EMOJI, dots)


def read_env_token(env_path):
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def read_chat_id(access_path):
    try:
        with open(access_path, "r") as f:
            data = json.load(f)
        allow = data.get("allowFrom") or []
        if allow:
            return str(allow[0])
    except Exception:
        pass
    return ""


def basename(p):
    try:
        return os.path.basename(str(p)) or str(p)
    except Exception:
        return str(p)


def short(v, n):
    v = "" if v is None else " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


def rich_enabled(sdir):
    try:
        return os.path.exists(os.path.join(os.path.dirname(sdir), RICH_FLAG))
    except Exception:
        return False


def bot_identity(sdir):
    """Resolve a display name and emoji from this bot's configured state directory."""
    try:
        fleet_home = os.path.expanduser(os.environ.get(
            "FLEET_HOME", "~/.claude/telegram-fleet"))
        with open(os.path.join(fleet_home, "fleet.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        state_dir = os.path.realpath(os.path.dirname(sdir))
        for bot in config.get("bots", []):
            configured = bot.get("state_dir")
            if isinstance(configured, str) and configured and os.path.realpath(
                    os.path.expanduser(configured)) == state_dir:
                name, emoji = bot.get("name"), bot.get("emoji")
                return (name.strip() if isinstance(name, str) and name.strip() else "Claude",
                        emoji.strip() if isinstance(emoji, str) and emoji.strip() else "🤖")
    except Exception:
        pass
    return ("Claude", "🤖")


def sanitize_rich(value, table=False, cap=None):
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " · ").replace("\t", " ").replace("\x00", "")


    bidi_controls = {
        "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
        "\u2066", "\u2067", "\u2068", "\u2069", "\u206a", "\u206b", "\u206c", "\u206d",
        "\u206e", "\u206f",
    }
    text = "".join(ch for ch in text if ch not in bidi_controls)


    replacements = {
        "`": "'", "*": "＊", "_": "＿", "~": "～", "=": "＝",
        "|": "/", "[": "(", "]": ")", "<": "‹", ">": "›",
        "$": "＄", "#": "＃", "&": "＆", "\\": "/",
    }
    text = "".join(replacements.get(ch, ch) for ch in text)
    text = " ".join(text.split())
    if text.startswith("-"):
        text = "−" + text[1:]
    limit = RICH_TABLE_CELL_CAP if table and cap is None else cap
    if limit is not None:
        try:
            limit = max(1, int(limit))
        except Exception:
            limit = None
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _rich_markup_balanced(frame):
    text = str(frame or "")
    if not text or "\x00" in text:
        return False
    allowed_tags = {"<details>", "</details>", "<summary>", "</summary>",
                    "<footer>", "</footer>"}
    if any(token not in allowed_tags
           for token in re.findall(r"<[^>\n]+>", text)):
        return False
    tag_free = re.sub(r"</?(?:details|summary|footer)>", "", text)
    if "<" in tag_free or ">" in tag_free:
        return False
    if text.count("**") % 2:
        return False
    if any(not value.strip() for value in re.findall(
            r"<summary>(.*?)</summary>", text, flags=re.S)):
        return False
    stack = []
    summary_seen = False
    for match in re.finditer(r"</?(?:details|summary|footer)>", text):
        token = match.group(0)
        if token == "<details>":


            if stack:
                return False
            stack.append("details")
            summary_seen = False
        elif token == "<summary>":
            if stack != ["details"] or summary_seen:
                return False
            summary_seen = True
            stack.append("summary")
        elif token == "</summary>":
            if not stack or stack.pop() != "summary":
                return False
        elif token == "</details>":
            if stack != ["details"] or not summary_seen:
                return False
            stack.pop()
            summary_seen = False
        elif token == "<footer>":
            if stack:
                return False
            stack.append("footer")
        elif token == "</footer>":
            if stack != ["footer"]:
                return False
            stack.pop()
    return not stack


def _rich_wire_fits(frame):
    try:
        if not isinstance(frame, str) or not frame:
            return False
        text = frame


        block_estimate = (text.count("\n") + 1 + text.count("<details>")
                          + text.count("<summary>"))
        return bool(
            len(text.encode("utf-8")) <= RICH_TEXT_LIMIT
            and block_estimate <= RICH_BLOCK_LIMIT
            and _rich_markup_balanced(text))
    except Exception:
        return False


def _rich_frame_fits(frame, frame_limit=RICH_FRAME_LIMIT):
    try:
        return bool(len(str(frame)) < max(1, int(frame_limit))
                    and _rich_wire_fits(frame))
    except Exception:
        return False


def _rich_omission_marker(lines, omitted, tool_omitted=0, agent_omitted=0):
    out = list(lines)
    if omitted > 0:
        marker = "- +%d lower-priority detail%s hidden" % (
            omitted, "" if omitted == 1 else "s")
        inserted = False
        for index, line in enumerate(out):
            if line.startswith(("<details><summary>📋 Plan",
                                "<details><summary>📋 The plan")):
                out.insert(index + 1, marker)
                inserted = True
                break
            if line.startswith("**📋 Plan"):
                at = index + 1
                while at < len(out) and not out[at]:
                    at += 1
                out.insert(at, "… %d lower-priority detail%s hidden" % (
                    omitted, "" if omitted == 1 else "s"))
                inserted = True
                break
        if not inserted:
            for index, line in enumerate(out):
                if line == "**Steps**":
                    at = index + 1
                    while at < len(out) and not out[at]:
                        at += 1
                    out.insert(at, "… %d lower-priority detail%s hidden" % (
                        omitted, "" if omitted == 1 else "s"))
                    break
    if tool_omitted > 0:
        for index, line in enumerate(out):
            if line.startswith(("<details><summary>🔧 Tools", "**🔧 Tools")):
                at = index + 1
                while at < len(out) and not out[at]:
                    at += 1
                out.insert(at, "… %d earlier not shown" % tool_omitted)
                break


    agent_start = next((i for i, line in enumerate(out)
                        if line.startswith("<details><summary>🤖 Agents")), None)
    if agent_start is not None:
        agent_end = next((i for i in range(agent_start + 1, len(out))
                          if out[i] == "</details>"), None)
        if agent_end is not None:
            row_indexes = [i for i in range(agent_start + 1, agent_end)
                           if out[i].startswith(("├ ", "└ "))]
            for index, row_index in enumerate(row_indexes):
                out[row_index] = ("├" if agent_omitted or
                                  index < len(row_indexes) - 1 else "└")\
                    + out[row_index][1:]
            if agent_omitted:
                insert_at = agent_end
                while insert_at > agent_start + 1 and not out[insert_at - 1]:
                    insert_at -= 1
                out.insert(insert_at, "└ … +%d earlier" % agent_omitted)
    return out


def _budget_rich_frame(frame, frame_limit=RICH_FRAME_LIMIT, force_trim=False):
    text = str(frame or "")
    lines = text.splitlines()
    omitted = 0
    tool_omitted = 0
    agent_omitted = 0
    cleaned = []
    for line in lines:
        match = re.match(
            r"^- (?:\[x\] )?\+(\d+) (?:earlier|lower-priority details? hidden)$",
            line)
        if match:
            omitted += int(match.group(1))
            continue
        match = re.match(r"^… (\d+) earlier not shown$", line)
        if match:
            tool_omitted += int(match.group(1))
            continue
        match = re.match(r"^[├└] … \+(\d+) earlier$", line)
        if match:
            agent_omitted += int(match.group(1))
            continue
        match = re.match(
            r"^… (\d+) (?:earlier steps? not shown|lower-priority details? hidden|"
            r"plan details? hidden|step details? hidden)$", line)
        if match:
            omitted += int(match.group(1))
            continue
        cleaned.append(line)
    lines = cleaned
    removed = 0
    def candidate_text():
        return "\n".join(_rich_omission_marker(
            lines, omitted, tool_omitted, agent_omitted))

    while True:
        candidate = candidate_text()
        if _rich_frame_fits(candidate, frame_limit) and (removed or not force_trim):
            return candidate

        groups = [
            ("tool", [i for i, line in enumerate(lines) if line.startswith("- at ")]),
            ("plan", [i for i, line in enumerate(lines) if line.startswith("- [x] ")]),
            ("plan", [i for i, line in enumerate(lines)
                      if line.startswith("- [ ] ") and not line.endswith(" ◀")]),
        ]
        active = [i for i, line in enumerate(lines)
                  if line.startswith("- [ ] ") and line.endswith(" ◀")]
        if len(active) > 1:
            groups.append(("plan", active[:-1]))
        groups.append(("decoration", [
            i for i, line in enumerate(lines)
            if line in ("- nothing logged yet", "- nothing yet", "- no steps logged")
        ]))
        agent_rows = [i for i, line in enumerate(lines)
                      if line.startswith(("├ ", "└ "))]
        if agent_rows:
            groups.append(("agent", [agent_rows[-1]]))
        selected = next(((kind, group[0]) for kind, group in groups if group), None)
        kind, pick = selected if selected else (None, None)
        if pick is None and force_trim:
            dividers = [i for i, line in enumerate(lines) if line == "---"]
            pick = dividers[-1] if dividers else None
            kind = "decoration" if pick is not None else None
        if pick is None and force_trim:
            summary = next((i for i, line in enumerate(lines)
                            if " · tap</summary>" in line), None)
            if summary is not None:
                lines[summary] = lines[summary].replace(
                    " · tap</summary>", "</summary>", 1)
                removed += 1
                continue
        if pick is None and force_trim:


            shrunk = False
            for index in range(len(lines) - 1, -1, -1):
                match = re.search(r"<summary>([^<]+)</summary>", lines[index])
                if not match:
                    continue
                label = match.group(1)
                if len(label) > 1:
                    lines[index] = (lines[index][:match.start(1)] + label[:-1]
                                    + lines[index][match.end(1):])
                elif len(lines) == 1 and lines[index] ==\
                        "<details><summary>%s</summary></details>" % label:
                    lines[index] = label
                else:
                    continue
                removed += 1
                shrunk = True
                break
            if shrunk:
                continue
        if pick is None and force_trim:
            candidate = candidate_text()
            protected = any(
                line.startswith(("🎯", "👾", "💭", "⏳", "|"))
                or line in ("<bold>Mission<bold>", "<bold>Agents<bold>")
                or line.startswith("Mission · ")
                for line in lines)
            if not protected and "<details>" in candidate:


                visible = re.sub(r"</?(?:details|summary)>", "", candidate)
                visible = "\n".join(
                    line for line in visible.splitlines() if line.strip())
                if visible and len(visible) < len(candidate)\
                        and _rich_wire_fits(visible):
                    lines = visible.splitlines()
                    removed += 1
                    continue
            if not protected and "<details>" not in candidate:
                shrunk_plain = False
                for index in range(len(lines) - 1, -1, -1):
                    if not lines[index]:
                        continue
                    lines[index] = lines[index][:-1]
                    if not lines[index]:
                        del lines[index]
                    removed += 1
                    shrunk_plain = True
                    break
                if shrunk_plain:
                    continue
        if pick is None:


            candidate = candidate_text()
            return candidate if _rich_wire_fits(candidate) and\
                (removed or not force_trim) else None
        removed += 1
        if kind == "plan":
            omitted += 1
        elif kind == "tool":
            tool_omitted += 1
        elif kind == "agent":
            agent_omitted += 1
        del lines[pick]


def _rich_plan_trim_index(entries):
    for wanted in (("completed", "cancelled"), ("pending",), ("in_progress",)):
        matches = [i for i, entry in enumerate(entries) if entry[0] in wanted]
        if wanted == ("in_progress",) and len(matches) <= 1:
            continue
        if matches:
            return matches[0]
    return 0 if entries else None


def _rich_plan_trim_order(entries):
    statuses = [entry[0] for entry in entries]
    order = []
    for wanted in (("completed", "cancelled"), ("pending",), ("in_progress",)):
        order.extend(i for i, status in enumerate(statuses) if status in wanted)
    known = set(order)
    order.extend(i for i in range(len(entries)) if i not in known)
    return order


def _entries_after_trim(entries, order, count):
    if count <= 0:
        return list(entries)
    removed = set(order[:count])
    return [entry for index, entry in enumerate(entries) if index not in removed]


def _minimum_fitting_drop(total, build, fits):
    high_frame = build(total)
    if not fits(high_frame):
        return total, high_frame, False
    low, high = 1, total
    while low < high:
        middle = (low + high) // 2
        if fits(build(middle)):
            high = middle
        else:
            low = middle + 1
    frame = build(low)
    return low, frame, True


def _bounded_api_description(value):
    text = " ".join(str(value or "").replace("\x00", " ").split())


    text = re.sub(
        r'"(?:\\.|[^"\\])*"\s*:\s*'
        r'(?:"(?:\\.|[^"\\])*"|[^,}\]]+)',
        '"[redacted]"', text)
    secret_label = (
        r"(?:x[\s_-]?api[\s_-]?key|api[\s_-]?key|access[\s_-]?token|"
        r"refresh[\s_-]?token|auth[\s_-]?token|oauth[\s_-]?token|"
        r"bot[\s_-]?token|session[\s_-]?token|client[\s_-]?secret|"
        r"private[\s_-]?key|signing[\s_-]?key|token|secret|password|"
        r"authorization|credentials?)")
    composite_label = (
        r"(?:[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)*[_-]"
        r"(?:token|secret|password|key))")
    camel_label = (
        r"(?:[a-z][A-Za-z0-9]*(?:Token|Secret|Password|Passphrase|ApiKey|"
        r"APIKey|SecretKey|PrivateKey|SigningKey|Credential|Credentials|"
        r"Authorization|AuthHeader))")
    credential_marker = re.search(
        r"\b(?:Bearer|Basic)\s+|"
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|"
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}\b|"
        r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b|"
        r"\b(?:" + composite_label + r"|" + camel_label + r"|"
        + secret_label + r")\b",
        text, flags=re.I)
    if credential_marker:


        prefix = text[:credential_marker.start()].rstrip(" \t:;,\"'{[")
        safe = (prefix + " " if prefix else "") + "[redacted]"
        return safe[:RICH_ERROR_DESC_LIMIT]
    text = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
                  lambda match: match.group(1) + " [redacted]",
                  text, flags=re.I)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
                  "[redacted]", text)
    text = re.sub(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}\b",
                  "[redacted]", text, flags=re.I)
    text = re.sub(r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b", "[redacted]", text)
    secret_value = r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,;}\]&]+)"
    text = re.sub(
        r"\b" + composite_label + r"\b[\"']?\s*[:=]\s*"
        r"(?:(?:Bearer|Basic)\s+)?" + secret_value,
        "[redacted]", text, flags=re.I)
    text = re.sub(
        r"\b" + composite_label + r"\b\s+"
        r"(?:(?:Bearer|Basic)\s+)?" + secret_value,
        "[redacted]", text, flags=re.I)
    text = re.sub(
        r"\b" + secret_label + r"\b[\"']?\s*[:=]\s*"
        r"(?:(?:Bearer|Basic)\s+)?" + secret_value,
        "[redacted]", text, flags=re.I)
    text = re.sub(
        r"\b" + secret_label + r"\b\s+"
        r"(?:(?:Bearer|Basic)\s+)?" + secret_value,
        "[redacted]", text, flags=re.I)
    text = re.sub(
        r"\b\S*(?:token|secret|password|authorization|api[\s_-]?key)\S*\b",
        "[redacted]", text, flags=re.I)
    text = re.sub(r"(.)\1{31,}", lambda match: match.group(1) * 8 + "…", text)
    return text[:RICH_ERROR_DESC_LIMIT]


def _health_api_description(value):
    return _bounded_api_description(value)


def _rich_clock(seconds):
    try:
        total = max(0, int(float(seconds or 0)))
    except Exception:
        total = 0
    return "%02d:%02d" % (total // 60, total % 60)


def rich_bar(done, total):
    try:
        d = max(0, int(done))
        n = max(0, int(total))
    except Exception:
        d, n = 0, 0
    d = min(d, n) if n else 0
    filled = int(RICH_BAR_CELLS * d / n) if n else 0
    return "▰" * filled + "▱" * (RICH_BAR_CELLS - filled)


def _normalize_plan_status(status):
    status = str(status or "pending").strip().lower()
    if status == "deleted":
        status = "cancelled"
    return status if status in ("pending", "in_progress", "completed", "cancelled")\
        else "pending"


def _plan_copy(plan):
    if not isinstance(plan, list):
        return []
    return [dict(item) for item in plan if isinstance(item, dict)]


def _update_rich_plan(plan, tool, tinput, tresponse, agent_id=None):
    out = _plan_copy(plan)
    if agent_id or tool not in ("TaskCreate", "TaskUpdate"):
        return out
    ti = tinput if isinstance(tinput, dict) else {}
    tr = tresponse if isinstance(tresponse, dict) else {}
    if tr.get("success") is False or tr.get("error"):
        return out
    if tool == "TaskCreate":
        task = tr.get("task") if isinstance(tr.get("task"), dict) else {}


        task_id = task.get("id") or tr.get("taskId") or tr.get("task_id")
        subject = task.get("subject") or ti.get("subject")
        if not task_id or not subject or any(str(x.get("id")) == str(task_id) for x in out):
            return out
        out.append({
            "id": str(task_id),
            "subject": str(subject)[:RICH_PLAN_META_CAP],
            "description": str(ti.get("description") or "")[:RICH_PLAN_META_CAP],
            "activeForm": str(ti.get("activeForm") or "")[:RICH_PLAN_META_CAP],
            "status": _normalize_plan_status(task.get("status") or "pending"),
        })
        return out

    if tr.get("success") is not True:
        return out
    requested_id = ti.get("taskId") or ti.get("task_id")
    task = tr.get("task") if isinstance(tr.get("task"), dict) else {}
    response_id = tr.get("taskId") or tr.get("task_id") or task.get("id")
    if not requested_id or not response_id or str(requested_id) != str(response_id):
        return out
    item = next((x for x in out if str(x.get("id")) == str(response_id)), None)
    if item is None:
        return out
    updated = tr.get("updatedFields")
    updated = {str(field) for field in updated if isinstance(field, str)}\
        if isinstance(updated, list) else set()
    for source, target in (("subject", "subject"), ("description", "description"),
                           ("activeForm", "activeForm")):


        if task.get(source) is not None:
            value = str(task.get(source))
            item[target] = value[:RICH_PLAN_META_CAP]
        elif source in updated and source in ti and ti.get(source) is not None:
            value = str(ti.get(source))
            item[target] = value[:RICH_PLAN_META_CAP]
    if isinstance(tr.get("statusChange"), dict) and tr["statusChange"].get("to"):
        item["status"] = _normalize_plan_status(tr["statusChange"].get("to"))
    elif task.get("status") is not None:
        item["status"] = _normalize_plan_status(task.get("status"))
    elif "status" in updated and "status" in ti:
        item["status"] = _normalize_plan_status(ti.get("status"))
    return out


def _rich_state_present(st):
    return isinstance(st, dict) and any(k in st for k in (
        "rich_turn", "rich_box", "rich_failed", "rich_tools",
        "folded_above_rich", "prev_recap_rich"))


def _rich_turn_active(sdir, st):
    if not isinstance(st, dict) or st.get("rich_failed"):
        return False
    if "rich_turn" in st:
        return bool(st.get("rich_turn"))

    return bool(rich_enabled(sdir) and not st.get("msgid"))


def _active_agent_count(st):
    try:
        rows = st.get("panel") if isinstance(st, dict) else None
        if not isinstance(rows, list):
            return 0
        return sum(1 for r in rows if isinstance(r, dict) and not r.get("done"))
    except Exception:
        return 0


def _cadence_tier(st, now=None):
    age = None
    try:
        if isinstance(st, dict):
            candidate = (time.time() if now is None else float(now))\
                - float(st.get("proc_start"))
            if math.isfinite(candidate):
                age = candidate
    except Exception:
        age = None
    if age is not None and age < CADENCE_FAST_MAX:
        return {"tier": "fast",
                "rich_edit": RICH_MIN_EDIT_INTERVAL,
                "rich_anim": RICH_ANIM_INTERVAL,
                "plain_edit": MIN_EDIT_INTERVAL,
                "plain_anim": ANIM_INTERVAL,
                "typing": TYPING_MIN_INTERVAL}
    if age is None or age < CADENCE_MEDIUM_MAX:
        return {"tier": "medium",
                "rich_edit": CADENCE_MEDIUM_INTERVAL,
                "rich_anim": CADENCE_MEDIUM_INTERVAL,
                "plain_edit": CADENCE_MEDIUM_INTERVAL,
                "plain_anim": CADENCE_MEDIUM_INTERVAL,
                "typing": CADENCE_MEDIUM_TYPING}
    if age < CADENCE_SLOW_MAX:
        return {"tier": "slow",
                "rich_edit": CADENCE_SLOW_INTERVAL,
                "rich_anim": CADENCE_SLOW_INTERVAL,
                "plain_edit": CADENCE_SLOW_INTERVAL,
                "plain_anim": CADENCE_SLOW_INTERVAL,
                "typing": CADENCE_SLOW_TYPING}
    return {"tier": "marathon",
            "rich_edit": CADENCE_MARATHON_INTERVAL,
            "rich_anim": CADENCE_MARATHON_INTERVAL,
            "plain_edit": CADENCE_MARATHON_INTERVAL,
            "plain_anim": CADENCE_MARATHON_INTERVAL,
            "typing": CADENCE_MARATHON_TYPING}


def _effective_min_edit(sdir, st):


    tier = _cadence_tier(st)
    if isinstance(st, dict) and st.get("rich_slow"):


        return _rate_floor(sdir, max(tier["rich_edit"], RICH_SLOW_INTERVAL))
    if _rich_turn_active(sdir, st) or (isinstance(st, dict) and st.get("rich_box")):


        extra = max(0, _active_agent_count(st) - 1)
        if extra:
            return _rate_floor(sdir, min(max(RICH_SLOW_INTERVAL, tier["rich_edit"]),
                       tier["rich_edit"] + RICH_AGENT_EDIT_STEP * extra))
        return _rate_floor(sdir, tier["rich_edit"])
    return _rate_floor(sdir, tier["plain_edit"])


def _effective_anim_interval(sdir, st):


    tier = _cadence_tier(st)
    if isinstance(st, dict) and st.get("rich_slow"):
        return _rate_floor(sdir, max(tier["rich_anim"], RICH_SLOW_INTERVAL))
    if _rich_turn_active(sdir, st) or (isinstance(st, dict) and st.get("rich_box")):
        return _rate_floor(sdir, tier["rich_anim"])
    return _rate_floor(sdir, tier["plain_anim"])


def family_of(tool, agent_id):
    if agent_id:
        return "helpers"
    t = tool or ""
    if t in ("Read", "Grep", "Glob"):
        return "files"
    if t in ("WebFetch", "WebSearch") or t.startswith("mcp__claude_ai_Tavily__"):
        return "web"
    if t in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "write"
    if t == "Bash":
        return "run"
    if t in ("Task", "Agent"):
        return "helpers"
    if t == "TodoWrite":
        return "plan"
    if t == "ToolSearch":
        return "tool"
    return "other"


def step_kind(tool, agent_id):
    if tool in ("Task", "Agent") and not agent_id:
        return "subtitle"
    if agent_id:
        return "subwork"
    return ""


_CODEX_GPT_MODEL_RE = re.compile(
    r"^gpt-([0-9][0-9a-z.]*)(?:-([a-z0-9][a-z0-9.-]*))?$")
_CODEX_SHELL_PUNCTUATION = "();<>|&\n"

_CODEX_CONFIG_MEMO = {}


def _codex_model_label(value):
    text = value.strip() if isinstance(value, str) else ""
    lowered = text.lower() if len(text) <= PANEL_MODEL_STORE else ""
    lowered = re.sub(r"-(?:19|20)[0-9]{2}-?[0-9]{2}-?[0-9]{2}$", "", lowered)
    match = _CODEX_GPT_MODEL_RE.match(lowered)
    if match:
        words = [w for w in (match.group(2) or "").split("-") if w]
        return short("GPT-" + match.group(1) + "".join(
            " " + w[:1].upper() + w[1:] for w in words), LANE_FAMILY_CAP)
    if lowered.startswith("codex-"):
        words = [w for w in lowered[len("codex-"):].split("-") if w]
        if words:
            return short("Codex" + "".join(
                " " + w[:1].upper() + w[1:] for w in words), LANE_FAMILY_CAP)
    return "Codex"


def _codex_config_model():
    home = os.environ.get("CODEX_HOME") or os.path.join(
        os.path.expanduser("~"), ".codex")
    path = os.path.join(home, "config.toml")
    if path in _CODEX_CONFIG_MEMO:
        return _CODEX_CONFIG_MEMO[path]
    model = ""
    try:
        info = os.stat(path)
        if stat.S_ISREG(info.st_mode):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(CODEX_CONFIG_BYTES)
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    break
                match = re.match(
                    r"""^model\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$""", stripped)
                if match:
                    model = (match.group(1) if match.group(1) is not None
                             else match.group(2)).strip()
                    break
    except Exception:
        model = ""
    _CODEX_CONFIG_MEMO[path] = model
    return model


def _codex_exec_model(command):
    if not isinstance(command, str) or "codex exec" not in " ".join(command.split()):
        return ""


    joined = command.replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        lexer = shlex.shlex(joined, posix=True, punctuation_chars=_CODEX_SHELL_PUNCTUATION)
        lexer.whitespace = lexer.whitespace.replace("\n", "")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except (TypeError, ValueError):
        tokens = joined.split()
    start = None
    for index in range(len(tokens) - 1):
        if basename(tokens[index]) == "codex" and tokens[index + 1] == "exec":
            start = index + 2
            break
    if start is None:
        return ""
    segment = []
    for token in tokens[start:]:


        chars = set(token)
        if chars and chars <= set(_CODEX_SHELL_PUNCTUATION) and not chars & {"<", ">"}:
            break
        segment.append(token)
    explicit = None
    configured = None
    blocked = (bool(segment) and segment[0] in ("resume", "fork", "review"))\
        or any(token.startswith("CODEX_HOME=") for token in tokens[:start])
    index = 0
    while index < len(segment):
        token = segment[index]
        following = segment[index + 1] if index + 1 < len(segment) else ""
        if token in ("-m", "--model"):
            if explicit is None:
                explicit = following
            index += 2
            continue
        if token.startswith("--model="):
            if explicit is None:
                explicit = token.split("=", 1)[1]
        elif token.startswith("-m") and not token.startswith("--"):
            if explicit is None:
                explicit = token[2:].lstrip("=")
        elif token in ("-c", "--config") or token.startswith("--config=")\
                or (token.startswith("-c") and not token.startswith("--")):
            if token in ("-c", "--config"):
                pair = following
                index += 1
            elif token.startswith("--config="):
                pair = token.split("=", 1)[1]
            else:
                pair = token[2:]
            key, sep, val = pair.partition("=")
            if sep and key.strip() == "model" and configured is None:
                configured = val.strip().strip("\"'")
            elif sep and key.strip() == "profile":
                blocked = True
        elif token in ("-p", "--profile", "--oss", "--local-provider")\
                or token.startswith(("--profile=", "--local-provider=")):
            blocked = True
        index += 1
    if explicit is not None:
        return explicit.strip()
    if configured is not None:
        return configured
    return "" if blocked else _codex_config_model()


def _codex_bash_caption(tinput):
    ti = tinput if isinstance(tinput, dict) else {}
    command = ti.get("command")
    if not isinstance(command, str):
        return ""
    normalized = " ".join(command.split())
    if "codex exec" not in normalized:
        return ""
    try:
        argv = shlex.split(command)
    except (TypeError, ValueError):
        argv = normalized.split()

    def option(flag):
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return ""

    output = option("-o")


    tier = _codex_model_label(_codex_exec_model(command))
    stem = os.path.splitext(basename(str(output).rstrip(";|&")))[0] if output else ""
    for suffix in ("-report", "_report", ".report"):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    slug = stem.strip(" ._-").replace("_", "-")
    return short(tier + ((" · " + slug) if slug else ""), 80)


def _codex_process_key(tinput):
    ti = tinput if isinstance(tinput, dict) else {}
    command = ti.get("command")
    if not isinstance(command, str) or "codex exec" not in " ".join(command.split()):
        return ""
    try:
        argv = shlex.split(command)
        output = argv[argv.index("-o") + 1]
    except (TypeError, ValueError, IndexError):
        return ""
    key = str(output).rstrip(";|&")
    if not key or len(key) > CODEX_PROCESS_KEY_MAX or "\x00" in key\
            or "\n" in key or "\r" in key:
        return ""
    return key


def _is_background_codex(tinput):
    ti = tinput if isinstance(tinput, dict) else {}
    return ti.get("run_in_background") is True and bool(_codex_bash_caption(ti))


def _is_background_bash(tinput):
    ti = tinput if isinstance(tinput, dict) else {}
    if ti.get("run_in_background") is not True:
        return False
    if not isinstance(ti.get("command"), str) or not ti.get("command").strip():
        return False
    return not _codex_bash_caption(ti)


def _bash_bg_caption(tinput):
    ti = tinput if isinstance(tinput, dict) else {}
    if not _is_background_bash(ti):
        return ""
    desc = ti.get("description")
    if not isinstance(desc, str) or not desc.strip():
        head = str(ti.get("command") or "").strip()
        head = head.split("\n", 1)[0].split("<<", 1)[0].strip()
        desc = short(head, 60) if head else ""
    return short(BASH_BG_LABEL + ((" · " + desc) if desc else ""), 80)


def caption_of(tool, tinput, agent_id):
    t = tool or ""
    ti = tinput if isinstance(tinput, dict) else {}
    if t in ("Task", "Agent") and not agent_id:
        return short(ti.get("description") or "a task", 240)
    if t == "Bash":
        codex = _codex_bash_caption(ti)
        if codex:
            return codex
        bash_bg = _bash_bg_caption(ti)
        if bash_bg:
            return bash_bg
        desc = ti.get("description")
        if desc:
            return short(desc, 280)


        command = str(ti.get("command") or "").strip()
        if not command:
            return "running a command"
        head = command.split("\n", 1)[0].split("<<", 1)[0].strip()
        return short(head or "running a command", 60)
    if t == "Read":
        return "reading " + basename(ti.get("file_path") or "a file")
    if t in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "editing " + basename(ti.get("file_path") or "a file")
    if t == "Grep":
        return "searching for: " + short(ti.get("pattern") or "…", 140)
    if t == "Glob":
        return "finding files: " + short(ti.get("pattern") or "…", 140)
    if t == "WebFetch":
        return "reading " + short(ti.get("url") or "a web page", 140)
    if t == "WebSearch" or t.startswith("mcp__claude_ai_Tavily__"):
        return "searching the web: " + short(ti.get("query") or "…", 140)
    if t == "TodoWrite":
        return "updating the plan"
    if t == "ToolSearch":
        return "loading a tool"
    if t.startswith("mcp__"):
        parts = t.split("__")
        if len(parts) >= 3:
            return short((parts[1] + ": " + parts[2]).replace("_", " "), 140)
        return short(t.replace("mcp__", "").replace("_", " "), 140)
    return short(t.replace("_", " "), 140)


def mission_from_prompt(text):
    t = short(text or "", MISSION_CAP)
    if not t:
        return MISSION_EMPTY
    if t.strip("()[] ").lower() in VOICE_BODIES:
        return MISSION_VOICE
    return t


def _is_notification_prompt(text):
    return (text or "").lstrip().startswith("<task-notification>")


_CODEX_TASK_TERMINAL_STATUSES = {"completed", "failed", "killed", "stopped"}


_NOTIFICATION_SUMMARY_VERBS = {
    "finished": "completed", "completed": "completed",
    "succeeded": "completed", "failed": "failed",
    "errored": "failed", "killed": "killed",
    "cancelled": "killed", "canceled": "killed",
    "stopped": "stopped",
}


def _terminal_notification_results(text):
    raw = (text or "").lstrip()
    if not raw.startswith("<task-notification>"):
        return []
    out, cursor = [], 0
    open_tag, close_tag = "<task-notification>", "</task-notification>"
    while True:
        start = raw.find(open_tag, cursor)
        if start < 0:
            break
        end = raw.find(close_tag, start + len(open_tag))
        if end < 0:
            break
        block = raw[start + len(open_tag):end]

        def tag_value(tag):
            left, right = "<%s>" % tag, "</%s>" % tag
            a = block.find(left)
            b = block.find(right, a + len(left)) if a >= 0 else -1
            return block[a + len(left):b].strip() if a >= 0 and b >= 0 else ""

        status = tag_value("status").lower()
        tuid = tag_value("tool-use-id")
        if status not in _CODEX_TASK_TERMINAL_STATUSES:
            summary = tag_value("summary")
            summary_lower = summary.lower()
            if summary_lower.startswith("agent ") and '"' in summary_lower:
                tail = summary_lower.rsplit('"', 1)[-1].strip()
                verb = tail.split(None, 1)[0].strip(".,:;") if tail else ""
                status = _NOTIFICATION_SUMMARY_VERBS.get(verb, "")
        if tuid and status in _CODEX_TASK_TERMINAL_STATUSES:
            out.append((tuid, status))
        cursor = end + len(close_tag)
    return out


def _terminal_notification_agent_labels(text):
    raw = (text or "").lstrip()
    if not raw.startswith("<task-notification>"):
        return []
    out, cursor = [], 0
    open_tag, close_tag = "<task-notification>", "</task-notification>"
    while True:
        start = raw.find(open_tag, cursor)
        if start < 0:
            break
        end = raw.find(close_tag, start + len(open_tag))
        if end < 0:
            break
        block = raw[start + len(open_tag):end]

        def tag_value(tag):
            left, right = "<%s>" % tag, "</%s>" % tag
            a = block.find(left)
            b = block.find(right, a + len(left)) if a >= 0 else -1
            return block[a + len(left):b].strip() if a >= 0 and b >= 0 else ""

        cursor = end + len(close_tag)
        summary = tag_value("summary")


        if not summary.lower().startswith("agent ") or summary.count('"') < 2:
            continue
        label = summary[summary.find('"') + 1:summary.rfind('"')].strip()
        status = tag_value("status").lower()
        if status not in _CODEX_TASK_TERMINAL_STATUSES:
            tail = summary.lower().rsplit('"', 1)[-1].strip()
            verb = tail.split(None, 1)[0].strip(".,:;") if tail else ""
            status = _NOTIFICATION_SUMMARY_VERBS.get(verb, "")
        if label and status in _CODEX_TASK_TERMINAL_STATUSES:
            out.append((label, status, tag_value("tool-use-id")))
    return out


def _terminal_notification_agent_ids(text):
    raw = (text or "").lstrip()
    if not raw.startswith("<task-notification>"):
        return []
    out, cursor = [], 0
    open_tag, close_tag = "<task-notification>", "</task-notification>"
    while True:
        start = raw.find(open_tag, cursor)
        if start < 0:
            break
        end = raw.find(close_tag, start + len(open_tag))
        if end < 0:
            break
        block = raw[start + len(open_tag):end]

        def tag_value(tag):
            left, right = "<%s>" % tag, "</%s>" % tag
            a = block.find(left)
            b = block.find(right, a + len(left)) if a >= 0 else -1
            return block[a + len(left):b].strip() if a >= 0 and b >= 0 else ""

        cursor = end + len(close_tag)
        task_id = _clean_agent_id(tag_value("task-id"))
        summary = tag_value("summary")
        label = ""
        if summary.lower().startswith("agent ") and summary.count('"') >= 2:
            label = summary[summary.find('"') + 1:summary.rfind('"')].strip()
        status = tag_value("status").lower()
        if status not in _CODEX_TASK_TERMINAL_STATUSES:
            summary_lower = summary.lower()
            if summary_lower.startswith("agent ") and '"' in summary_lower:
                tail = summary_lower.rsplit('"', 1)[-1].strip()
                verb = tail.split(None, 1)[0].strip(".,:;") if tail else ""
                status = _NOTIFICATION_SUMMARY_VERBS.get(verb, "")
        if task_id and status in _CODEX_TASK_TERMINAL_STATUSES:
            out.append((task_id, status, tag_value("tool-use-id"), label))
    return out


def model_label(m):
    m = (m or "").strip()
    if not m:
        return PANEL_NO_MODEL
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    return short(m, PANEL_MODEL_CAP) or PANEL_NO_MODEL


def _context_model_label(value):
    raw = short(value if isinstance(value, str) else "", PANEL_MODEL_STORE)
    if not raw:
        return "Unknown model"
    cleaned = model_label(raw)
    lowered = cleaned.lower().replace("_", "-")
    for family in ("mythos", "fable", "opus", "sonnet", "haiku"):
        match = re.search(r"(?:^|-)%s(?:-|$)([0-9][0-9.-]*)?" % family, lowered)
        if match:
            version = (match.group(1) or "").strip("-.")
            if version:
                version = re.sub(r"-+", ".", version)
                return "%s %s" % (family.title(), version)
            return family.title()
    if lowered.startswith(("gpt-", "codex-")) or "codex" in lowered:
        words = cleaned.replace("_", "-").split("-")
        return " ".join("GPT" if word.lower() == "gpt" else
                        "Codex" if word.lower() == "codex" else word
                        for word in words if word) or "Codex"
    return " ".join(part[:1].upper() + part[1:] for part in
                    cleaned.replace("_", "-").split("-") if part)\
        or "Unknown model"


def _configured_context_model(model_state):
    text = model_state if isinstance(model_state, str) else ""
    for line in text.splitlines():
        if line.strip().upper().startswith("MODEL="):
            return line.split("=", 1)[1].strip()
    return text.strip().splitlines()[0] if text.strip() else ""


def _configured_context_effort(model_state):
    text = model_state if isinstance(model_state, str) else ""
    for line in text.splitlines():
        if line.strip().upper().startswith("EFFORT="):
            return line.split("=", 1)[1].strip()
    return ""


def _context_effort_label(value):
    try:
        text = value.strip().lower() if isinstance(value, str) else ""
    except Exception:
        return ""
    return text if text in CONTEXT_EFFORT_TAGS else ""


def _context_effort_tag(value):
    key = _context_effort_label(value)
    return " (%s)" % CONTEXT_EFFORT_TAGS[key] if key else ""


def _store_context_effort(st, effort):
    if not isinstance(st, dict):
        return ""
    key = _context_effort_label(effort)
    if key:
        st["ctx_effort"] = key
    else:
        st.pop("ctx_effort", None)
    return key


def _context_cap(model, model_state=""):
    serving = str(model or "").lower()
    configured = str(model_state or "").lower()
    if "[1m]" in configured or "fable" in configured or "fable" in serving:
        return 1_000_000
    if "haiku" in configured or "haiku" in serving:
        return 200_000
    return 200_000


def _usage_token(value):
    if isinstance(value, bool) or not isinstance(value, (int, float))\
            or not math.isfinite(value):
        return 0
    return max(0, int(value))


def _read_context_snapshot(path, model_state=""):
    configured_model = _configured_context_model(model_state)
    fallback_model = _context_model_label(configured_model)
    try:
        if not isinstance(path, str) or not path:
            return None, fallback_model
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return None, fallback_model
        start = max(0, int(info.st_size) - CONTEXT_TAIL_BYTES)
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            payload = handle.read(CONTEXT_TAIL_BYTES)
        if start:
            boundary = payload.find(b"\n")
            payload = payload[boundary + 1:] if boundary >= 0 else b""
        for raw_line in reversed(payload.splitlines()):
            try:
                record = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            if record.get("isSidechain") is True or record.get("type") != "assistant":
                continue
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            usage = message.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            if "input_tokens" not in usage:
                continue
            model = message.get("model") or configured_model
            used = (_usage_token(usage.get("input_tokens"))
                    + _usage_token(usage.get("cache_read_input_tokens"))
                    + _usage_token(usage.get("cache_creation_input_tokens")))
            cap = _context_cap(model, model_state)
            pct = max(0, min(100, int(round((used / cap) * 100)))) if cap else 0
            return pct, _context_model_label(model)
    except Exception:
        return None, fallback_model
    return (0 if start == 0 else None), fallback_model


def _read_context_model_state(sdir):
    try:
        path = os.path.join(os.path.dirname(sdir), "model-state")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(CONTEXT_MODEL_STATE_BYTES)
    except Exception:
        return ""


def _turn_context_effort(sdir):
    return _context_effort_label(
        _configured_context_effort(_read_context_model_state(sdir)))


def _context_projects_dir(project_dir=None):
    root = project_dir if isinstance(project_dir, str) and project_dir else\
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        resolved = os.path.abspath(root)
        return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                            resolved.replace(os.sep, "-"))
    except Exception:
        return None


def _context_transcript_path(transcript_path=None, session_id=None,
                             project_dir=None):
    if isinstance(transcript_path, str) and transcript_path.strip():
        return transcript_path.strip()
    sid = str(session_id or "").strip()
    if not sid or os.path.basename(sid) != sid or not re.match(r"^[A-Za-z0-9._-]+$", sid):
        return None
    projects_dir = _context_projects_dir(project_dir)
    if not projects_dir:
        return None
    return os.path.join(projects_dir, sid + ".jsonl")


def _context_newest_transcript(projects_dir):
    try:
        if not isinstance(projects_dir, str) or not projects_dir:
            return None
        names = os.listdir(projects_dir)
    except Exception:
        return None
    best_path, best_mtime = None, None
    for name in names[:CONTEXT_SCAN_LIMIT]:
        if not isinstance(name, str) or not name.endswith(".jsonl"):
            continue
        path = os.path.join(projects_dir, name)
        try:
            info = os.stat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            mtime = info.st_mtime
        except Exception:
            continue
        if best_mtime is None or mtime > best_mtime:
            best_path, best_mtime = path, mtime
    return best_path


def _transcript_session_key(path):
    try:
        name = os.path.basename(str(path or ""))
        return name[:-len(".jsonl")] if name.endswith(".jsonl") else ""
    except Exception:
        return ""


def _iso_epoch(value):
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
        except Exception:
            continue
        try:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
            return float(parsed.timestamp())
        except Exception:
            return None
    return None


def _notification_record_text(record):
    if not isinstance(record, dict):
        return ""
    kind = record.get("type")
    text = None
    if kind == "queue-operation":
        text = record.get("content")
    elif kind == "attachment":
        attachment = record.get("attachment")
        if isinstance(attachment, dict):
            text = attachment.get("prompt")
    elif kind == "user":
        message = record.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            text = message.get("content")
    if not isinstance(text, str) or not text:
        return ""
    return text if text.lstrip().startswith("<task-notification>") else ""


def _transcript_notifications(path, tail_bytes=None, limit=None):
    out = []
    budget = BG_NOTIFY_TAIL_BYTES if tail_bytes is None else int(tail_bytes)
    cap = BG_NOTIFY_MAX_RECORDS if limit is None else int(limit)
    try:
        if not isinstance(path, str) or not path or budget <= 0 or cap <= 0:
            return out
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return out
        start = max(0, int(info.st_size) - budget)
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            payload = handle.read(budget)
        if start:
            boundary = payload.find(b"\n")
            payload = payload[boundary + 1:] if boundary >= 0 else b""
        if b"<task-notification>" not in payload:
            return out
        for raw in reversed(payload.splitlines()):
            if b"<task-notification>" not in raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            text = _notification_record_text(record)
            if not text:
                continue
            out.append((_iso_epoch(record.get("timestamp")), text))
            if len(out) >= cap:
                break
    except Exception:
        return out
    return out


def _bg_notify_transcript_path(st):
    state = st if isinstance(st, dict) else {}
    sid = state.get("rich_session_id")
    if not isinstance(sid, str) or not sid:
        rows = state.get("panel")
        for row in (rows if isinstance(rows, list) else []):
            if not isinstance(row, dict) or row.get("background") is not True\
                    or row.get("done"):
                continue
            candidate = row.get("history_session_id")
            if isinstance(candidate, str) and candidate:
                sid = candidate
                break
    if not isinstance(sid, str) or not sid:
        return None
    try:
        return _context_transcript_path(None, session_id=sid)
    except Exception:
        return None


def _turn_context_ex(sdir, transcript_path=None, session_id=None, project_dir=None):
    model_state = _read_context_model_state(sdir)
    path = _context_transcript_path(
        transcript_path, session_id=session_id, project_dir=project_dir)
    pct, model = _read_context_snapshot(path, model_state=model_state)
    if pct is not None:
        return pct, model, None
    want = str(session_id or "").strip() or _transcript_session_key(path)
    newest = _context_newest_transcript(_context_projects_dir(project_dir))
    if newest and newest != path and (
            not want or _transcript_session_key(newest) == want):
        alt_pct, alt_model = _read_context_snapshot(newest, model_state=model_state)
        if alt_pct is not None:
            return alt_pct, alt_model, None
    pending = None
    if path and want:
        try:
            pending = None if os.path.exists(path) else path
        except Exception:
            pending = None
    return pct, model, pending


def _turn_context(sdir, transcript_path=None, session_id=None, project_dir=None):
    pct, model, _pending = _turn_context_ex(
        sdir, transcript_path=transcript_path, session_id=session_id,
        project_dir=project_dir)
    return pct, model


def _refresh_pending_context(sdir, st):
    if not isinstance(st, dict):
        return False
    path = st.get("ctx_pending_path")
    if not isinstance(path, str) or not path:
        return False
    try:
        info = os.stat(path)
    except Exception:
        return False
    try:
        if not stat.S_ISREG(info.st_mode):
            st.pop("ctx_pending_path", None)
            return False
        model_state = _read_context_model_state(sdir)
        pct, model = _read_context_snapshot(path, model_state=model_state)
        if pct is None or pct <= 0:
            if int(info.st_size) > CONTEXT_TAIL_BYTES:
                st.pop("ctx_pending_path", None)
            return False
        st.pop("ctx_pending_path", None)
        st["ctx_pct"] = pct
        st["ctx_model"] = _context_model_label(model)


        _store_context_effort(
            st, _configured_context_effort(model_state))
        return True
    except Exception:
        st.pop("ctx_pending_path", None)
        return False


def _context_line(st):


    state = st if isinstance(st, dict) else {}
    model = _context_model_label(state.get("ctx_model"))
    effort = _context_effort_tag(state.get("ctx_effort"))
    raw_pct = state.get("ctx_pct")
    pct = None if isinstance(raw_pct, bool) or not isinstance(raw_pct, (int, float))\
        or not math.isfinite(raw_pct)\
        else max(0, min(100, int(round(raw_pct))))
    filled = 0 if pct is None else min(
        CONTEXT_BAR_CELLS, int(round((pct / 100) * CONTEXT_BAR_CELLS)))
    bar = CONTEXT_BAR_FILL * filled + CONTEXT_BAR_EMPTY * (CONTEXT_BAR_CELLS - filled)
    shown = CONTEXT_UNKNOWN_PCT if pct is None else str(pct)
    return "🧠 %s%s · %s %s%% context" % (
        sanitize_rich(model, cap=32), effort, bar, shown)


def _et_day(timestamp=None):
    try:
        value = time.time() if timestamp is None else float(timestamp)
        if not math.isfinite(value):
            raise ValueError("non-finite timestamp")
    except Exception:
        value = time.time()
    return datetime.fromtimestamp(value, ET_ZONE).strftime("%Y-%m-%d")


_CLAUDE_LANE_LABEL_RE = re.compile(
    r"^(?:Mythos|Fable|Opus|Sonnet|Haiku) [0-9]+(?:\.[0-9]+)*$")
_ISO_TIME_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,9}))?)?)?"
    r"\s*(Z|[+-][0-9]{2}:?[0-9]{2})?$", re.I)
_CODEX_LANE_PREFIX_RE = re.compile(
    r"^(?:GPT-[0-9][0-9a-z.]*|Codex)(?: [a-z0-9][a-z0-9.]*)*\s*·\s*", re.I)
_CODEX_LANE_ONLY_RE = re.compile(
    r"^GPT-[0-9][0-9a-z.]*(?: [a-z0-9][a-z0-9.]*)*$", re.I)


_MODELS_CACHE_MEMO = {}


def _claude_version_label(value):
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > PANEL_MODEL_STORE:
        return ""
    text = text.split("[", 1)[0].strip()
    text = re.sub(r"-(?:19|20)[0-9]{6}$", "", text)
    label = _context_model_label(text)
    return label if _CLAUDE_LANE_LABEL_RE.match(label) else ""


def _claude_alias_family(value):
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text or len(text) > PANEL_MODEL_STORE:
        return ""
    text = text.split("[", 1)[0].strip()
    if text.startswith("claude-"):
        text = text[len("claude-"):]
    return text if text in CLAUDE_LANE_FAMILIES else ""


def _iso_epoch(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _ISO_TIME_RE.match(value.strip())
    if not match:
        return None
    try:
        stamp = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                         int(match.group(4) or 0), int(match.group(5) or 0),
                         int(match.group(6) or 0), tzinfo=ZoneInfo("UTC")).timestamp()
        if match.group(7):
            stamp += float("0." + match.group(7))
        zone = (match.group(8) or "Z").upper()
        if zone != "Z":
            digits = zone[1:].replace(":", "")
            offset = int(digits[:2]) * 3600 + int(digits[2:4]) * 60
            stamp = stamp - offset if zone[0] == "+" else stamp + offset
    except Exception:
        return None
    return stamp if math.isfinite(stamp) else None


def _models_cache_path(sdir=None):
    base = sdir if isinstance(sdir, str) and sdir else ""
    if not base:
        state_dir = os.environ.get("TELEGRAM_STATE_DIR", "")
        base = os.path.join(state_dir, "stream") if state_dir else ""
    return os.path.join(base, MODELS_CACHE_FILE) if base else ""


def _models_cache_parse(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, []
    if not isinstance(data, dict):
        return None, []
    fetched = data.get("fetched_at")
    items = data.get("models")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float))\
            or not math.isfinite(fetched) or not isinstance(items, list):
        return None, []
    models = []
    for item in items[:MODELS_CACHE_MAX_MODELS]:
        if not isinstance(item, dict):
            continue
        mid = item.get("id")
        created = _iso_epoch(item.get("created_at"))
        if isinstance(mid, str) and mid.strip() and len(mid) <= PANEL_MODEL_STORE\
                and created is not None:
            models.append((created, mid.strip()))
    return float(fetched), models


def _models_cache_models(sdir=None, now=None):
    path = _models_cache_path(sdir)
    if not path:
        return []
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MODELS_CACHE_MAX_BYTES:
            return []
        identity = (info.st_ino, info.st_size, info.st_mtime_ns)
        memo = _MODELS_CACHE_MEMO.get(path)
        if memo is not None and memo[0] == identity:
            fetched, models = memo[1], memo[2]
        else:
            with open(path, "rb") as handle:
                raw = handle.read(MODELS_CACHE_MAX_BYTES + 1)
            fetched, models = (None, []) if len(raw) > MODELS_CACHE_MAX_BYTES\
                else _models_cache_parse(raw)
            _MODELS_CACHE_MEMO.clear()
            _MODELS_CACHE_MEMO[path] = (identity, fetched, models)
        if fetched is None:
            return []
        age = (time.time() if now is None else float(now)) - fetched
        if not math.isfinite(age) or age < -300 or age > MODELS_CACHE_TTL:
            return []
        return list(models)
    except Exception:
        return []


def _models_cache_newest(family, sdir=None, now=None):
    word = str(family or "").strip().lower()
    if word not in CLAUDE_LANE_FAMILIES:
        return ""
    best = None
    for created, mid in _models_cache_models(sdir, now=now):
        if word in mid.lower() and (best is None or (created, mid) > best):
            best = (created, mid)
    return best[1] if best else ""


def _models_cache_maybe_refresh(sdir, now=None):
    try:
        fleet_home = os.path.expanduser(os.environ.get(
            "FLEET_HOME", "~/.claude/telegram-fleet"))
        with open(os.path.join(fleet_home, "fleet.json"), encoding="utf-8") as handle:
            if json.load(handle).get("options", {}).get("tldr_judge") is not True:
                return False
    except Exception:
        return False
    if os.environ.get("STREAM_NO_SPAWN") == "1":
        return False
    path = _models_cache_path(sdir)
    if not path:
        return False
    current = time.time() if now is None else now
    try:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                return False
            if 0 <= current - info.st_mtime < MODELS_CACHE_RETRY:
                return False
            if _models_cache_models(sdir, now=current):
                return False
            os.utime(path, None)
        else:
            with open(path, "a"):
                pass
    except Exception:
        return False
    try:
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), MODELS_REFRESH_MODE, path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        return False
    return True


def _models_keychain_key():
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", MODELS_KEYCHAIN_SERVICE, "-w"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    key = result.stdout.decode("utf-8", "replace").strip()
    return key if re.fullmatch(r"[A-Za-z0-9_\-]{16,512}", key) else ""


def _models_fetch(key):
    config = ('url = "%s"\nheader = "x-api-key: %s"\nheader = "anthropic-version: %s"\n'
              % (MODELS_API_URL, key, MODELS_API_VERSION))
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--silent", "--fail", "--max-time",
             str(MODELS_REFRESH_TIMEOUT), "--connect-timeout", "5", "--config", "-"],
            input=config.encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=MODELS_REFRESH_TIMEOUT + 5)
    except Exception:
        return b""
    finally:
        config = None
    if result.returncode != 0:
        return b""
    return result.stdout[:4 * MODELS_CACHE_MAX_BYTES]


def _models_from_api(body):
    try:
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except Exception:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    models = []
    for item in items[:MODELS_CACHE_MAX_MODELS]:
        if not isinstance(item, dict):
            continue
        mid, created = item.get("id"), item.get("created_at")
        if not isinstance(mid, str) or not isinstance(created, str):
            continue
        mid, created = mid.strip(), created.strip()
        if mid and len(mid) <= PANEL_MODEL_STORE and _iso_epoch(created) is not None:
            models.append({"id": mid, "created_at": created})
    if not any(word in item["id"].lower() for item in models
               for word in CLAUDE_LANE_FAMILIES):
        return []
    return models


def _models_refresh_main(path):
    if not os.environ.get("TELEGRAM_STATE_DIR"):
        return False
    try:
        fleet_home = os.path.expanduser(os.environ.get(
            "FLEET_HOME", "~/.claude/telegram-fleet"))
        with open(os.path.join(fleet_home, "fleet.json"), encoding="utf-8") as handle:
            if json.load(handle).get("options", {}).get("tldr_judge") is not True:
                return False
    except Exception:
        return False
    tmp = ""
    try:
        if not isinstance(path, str) or os.path.basename(path) != MODELS_CACHE_FILE:
            return False
        folder = os.path.dirname(path)
        if not folder or not os.path.isdir(folder):
            return False
        key = _models_keychain_key()
        body = _models_fetch(key) if key else b""
        key = ""
        models = _models_from_api(body)
        if not models:
            return False
        payload = json.dumps({"fetched_at": time.time(),
                              "source": MODELS_API_URL.split("?", 1)[0],
                              "models": models}, ensure_ascii=True, sort_keys=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        tmp = ""
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _agent_family_bare(model, agent_type=""):
    value = "%s %s" % (model or "", agent_type or "")
    lowered = value.lower()
    for needle, label in (("mythos", "Mythos"), ("fable", "Fable"),
                          ("opus", "Opus"), ("sonnet", "Sonnet"),
                          ("haiku", "Haiku"), ("codex", "Codex"),
                          ("gpt", "Codex")):
        if needle in lowered:
            return label
    cleaned = _context_model_label(model or agent_type)
    return cleaned if cleaned != "Unknown model" else "Agent"


def _agent_family(model, agent_type="", served=None, live=False, sdir=None, now=None):
    label = _claude_version_label(served)
    if label:
        return label
    text = model.strip() if isinstance(model, str) else ""
    if text.lower().startswith(("gpt-", "codex")):
        return _codex_model_label(text)
    label = _claude_version_label(text)
    if label:
        return label
    if live:
        family = _claude_alias_family(text)
        if family:
            label = _claude_version_label(
                _models_cache_newest(family, sdir=sdir, now=now))
            if label:
                return label
    return _agent_family_bare(model, agent_type)


def _panel_row_family(row, sdir=None, now=None):
    row = row if isinstance(row, dict) else {}
    frozen = row.get("lane_family")
    if isinstance(frozen, str) and frozen.strip():
        return short(frozen, LANE_FAMILY_CAP)
    return _agent_family(row.get("mdl"), row.get("typ"), served=row.get("srv"),
                         live=not row.get("done"), sdir=sdir, now=now)


def _codex_step_family(step):
    return _codex_model_label(step.get("codex_model") if isinstance(step, dict) else "")


def _codex_lane_label(value):
    text = short(value or "Codex", AGENT_HISTORY_LABEL_CAP)
    if _CODEX_LANE_ONLY_RE.match(text):
        return "Codex"
    cleaned = _CODEX_LANE_PREFIX_RE.sub("", text, count=1).strip()
    return cleaned or "Codex"


def _history_number(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float))\
            or not math.isfinite(value):
        value = default
    try:
        number = float(value)
    except Exception:
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    return max(0.0, number)


def _empty_agent_history(now=None):
    return {"day": _et_day(now), "total": 0, "session_totals": {}, "rows": []}


def _agent_history_active(st, sdir=""):
    return bool(isinstance(st, dict) and (
        "agent_history" in st or _rich_state_present(st)
        or (sdir and rich_enabled(sdir))))


def _normalize_agent_history(value, now=None):
    current = time.time() if now is None else now
    today = _et_day(current)
    if isinstance(value, dict) and str(value.get("day") or "") == today:
        rows = value.get("rows") if isinstance(value.get("rows"), list) else []
        total = int(_history_number(value.get("total")))
        raw_session_totals = value.get("session_totals")
        raw_session_totals = raw_session_totals\
            if isinstance(raw_session_totals, dict) else {}
    elif isinstance(value, list):

        rows, total, raw_session_totals = value, 0, {}
    else:
        return _empty_agent_history(current)
    normalized = []
    positions = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        t0 = _history_number(item.get("t0"))
        secs = _history_number(item.get("secs"))
        closed_at = _history_number(item.get("closed_at"), t0 + secs)
        day = str(item.get("day") or _et_day(closed_at))
        if day != today:
            continue
        model = short(item.get("model") or "", PANEL_MODEL_STORE)
        family = short(item.get("family") or _agent_family(model), 24)
        clean = {
            "id": short(item.get("id") or "", AGENT_HISTORY_ID_CAP),
            "label": short(item.get("label") or "agent", AGENT_HISTORY_LABEL_CAP),
            "family": family or "Agent",
            "model": model,
            "t0": t0,
            "secs": secs,
            "outcome": short(item.get("outcome") or "completed", 20).lower(),
            "session_id": short(item.get("session_id") or "",
                                AGENT_HISTORY_SESSION_CAP),
            "day": day,
            "closed_at": closed_at,
        }
        identity = clean.get("id")
        if identity and identity in positions:
            normalized[positions[identity]] = clean
        else:
            if identity:
                positions[identity] = len(normalized)
            normalized.append(clean)
    normalized.sort(key=lambda item: (item.get("closed_at", 0), item.get("t0", 0)))
    normalized = normalized[-AGENT_HISTORY_MAX:]


    row_session_counts = {}
    for item in normalized:
        if not _agent_outcome_completed(item.get("outcome")):
            continue
        sid = item.get("session_id") or ""
        row_session_counts[sid] = row_session_counts.get(sid, 0) + 1
    session_totals = {}
    for raw_sid, raw_count in raw_session_totals.items():
        sid = short(raw_sid or "", AGENT_HISTORY_SESSION_CAP)
        if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
            session_totals[sid] = int(_history_number(raw_count))
    for sid, count in row_session_counts.items():
        session_totals[sid] = max(session_totals.get(sid, 0), count)
    if len(session_totals) > AGENT_HISTORY_SESSION_MAX:
        keep = {item.get("session_id") or "" for item in normalized}
        preferred = [(sid, count) for sid, count in session_totals.items()
                     if sid in keep]
        others = [(sid, count) for sid, count in session_totals.items()
                  if sid not in keep]
        session_totals = dict(
            (others + preferred)[-AGENT_HISTORY_SESSION_MAX:])
    total = max(0, total, sum(row_session_counts.values()))
    return {"day": today, "total": total,
            "session_totals": session_totals, "rows": normalized}


def _agent_outcome_completed(outcome):
    return str(outcome or "completed").strip().lower() in AGENT_COMPLETED_OUTCOMES


def _agent_bump_total(history, session_id, delta):
    history["total"] = max(0, int(_history_number(history.get("total"))) + delta)
    totals = history.get("session_totals")
    totals = totals if isinstance(totals, dict) else {}
    sid = str(session_id or "")
    totals[sid] = max(0, int(_history_number(totals.get(sid))) + delta)
    history["session_totals"] = totals


def _closed_agent_entry(row, kind, session_id, now):
    is_codex = kind == "codex"
    label = (row.get("cap") if is_codex else row.get("lab")) or\
        ("Codex" if is_codex else row.get("typ") or "agent")
    if is_codex:
        label = _codex_lane_label(label)


    model = (row.get("codex_model") or "Codex") if is_codex\
        else row.get("mdl") or row.get("typ") or ""
    family = _codex_step_family(row) if is_codex\
        else _panel_row_family(row, now=now)
    t0 = _history_number(row.get("t0"), now)
    secs = _history_number(row.get("secs"), max(0, now - t0))
    raw_outcome = row.get("codex_terminal_status") if is_codex else row.get("outcome")
    if not raw_outcome and is_codex and row.get("codex_stale"):
        raw_outcome = "stale"
    outcome = str(raw_outcome or "completed").strip().lower()
    if outcome in ("done", "complete", "success", "succeeded"):
        outcome = "completed"
    if is_codex:


        suffix = " · " + outcome
        if str(label).lower().endswith(suffix.lower()):
            label = str(label)[:-len(suffix)].rstrip()
    token = row.get("tuid") or "%d:%s" % (int(t0 * 1000), label)
    launch_session = row.get("history_session_id") or session_id or ""
    identity = short("%s:%s:%s" % (launch_session, kind, token),
                     AGENT_HISTORY_ID_CAP)
    closed_at = max(t0, t0 + secs)
    return {
        "id": identity,
        "label": short(label, AGENT_HISTORY_LABEL_CAP),
        "family": family,
        "model": short(model, PANEL_MODEL_STORE),
        "t0": t0,
        "secs": secs,
        "outcome": short(outcome, 20),
        "session_id": short(launch_session, AGENT_HISTORY_SESSION_CAP),
        "day": _et_day(closed_at),
        "closed_at": closed_at,
    }


def _capture_closed_agents(history, panel, steps, session_id="", now=None):
    current = time.time() if now is None else now
    out = _normalize_agent_history(history, now=current)
    rows = out.get("rows") or []
    positions = {item.get("id"): index for index, item in enumerate(rows)
                 if item.get("id")}
    candidates = []
    for row in (panel if isinstance(panel, list) else []):
        if isinstance(row, dict) and row.get("done"):
            candidates.append((_closed_agent_entry(
                row, "agent", session_id, current), row))
    for row in (steps if isinstance(steps, list) else []):
        if isinstance(row, dict) and row.get("codex_bg") and row.get("done"):
            candidates.append((_closed_agent_entry(
                row, "codex", session_id, current), row))
    for item, source in candidates:
        if item.get("day") != out.get("day"):
            continue
        identity = item.get("id")
        completed = _agent_outcome_completed(item.get("outcome"))
        if identity and identity in positions:
            previous = rows[positions[identity]]
            rows[positions[identity]] = item


            if completed != _agent_outcome_completed(previous.get("outcome")):
                _agent_bump_total(out, item.get("session_id") or "",
                                  1 if completed else -1)
        else:
            positions[identity] = len(rows)
            rows.append(item)
            if completed and source.get("history_recorded_day") != out.get("day"):
                _agent_bump_total(out, item.get("session_id") or "", 1)
        source["history_recorded_day"] = out.get("day")
    out["rows"] = rows
    return _normalize_agent_history(out, now=current)


def _agent_display_records(steps, panel, history, session_id="", now=None):
    current = time.time() if now is None else now
    records = []
    for row in (panel if isinstance(panel, list) else []):
        if not isinstance(row, dict) or row.get("done"):
            continue
        records.append({
            "label": short(row.get("lab") or row.get("typ") or "agent",
                           AGENT_HISTORY_LABEL_CAP),
            "family": _panel_row_family(row, now=current),
            "running": True,
            "secs": max(0, current - _history_number(row.get("t0"), current)),
            "outcome": "running",
        })
    for row in (steps if isinstance(steps, list) else []):
        if not isinstance(row, dict) or not row.get("codex_bg") or row.get("done"):
            continue
        records.append({
            "label": _codex_lane_label(row.get("cap") or "Codex"),
            "family": _codex_step_family(row), "running": True,
            "secs": max(0, current - _history_number(row.get("t0"), current)),
            "outcome": "running",
        })
    running = len(records)
    today = _normalize_agent_history(history, now=current)
    known_ids = {item.get("id") for item in today.get("rows", [])
                 if item.get("id")}
    current_closed = []
    for row in (panel if isinstance(panel, list) else []):
        if isinstance(row, dict) and row.get("done"):
            item = _closed_agent_entry(row, "agent", session_id, current)
            if item.get("day") == today.get("day")\
                    and item.get("id") not in known_ids:
                current_closed.append(item)
                known_ids.add(item.get("id"))
    for row in (steps if isinstance(steps, list) else []):
        if isinstance(row, dict) and row.get("codex_bg") and row.get("done"):
            item = _closed_agent_entry(row, "codex", session_id, current)
            if item.get("day") == today.get("day")\
                    and item.get("id") not in known_ids:
                current_closed.append(item)
                known_ids.add(item.get("id"))
    current_closed.sort(
        key=lambda item: (item.get("closed_at", 0), item.get("t0", 0)),
        reverse=True)
    closed_rows = [item for item in today.get("rows", [])
                   if str(item.get("session_id") or "") == str(session_id or "")]
    for item in current_closed + list(reversed(closed_rows)):
        if str(item.get("session_id") or "") != str(session_id or ""):
            continue
        records.append({
            "label": item.get("label") or "agent",
            "family": item.get("family") or _agent_family(item.get("model")),
            "running": False,
            "secs": _history_number(item.get("secs")),
            "outcome": item.get("outcome") or "completed",
        })


    total = max(0, int(today.get("total") or 0),
                sum(1 for item in today.get("rows", [])
                    if _agent_outcome_completed(item.get("outcome")))
                + sum(1 for item in current_closed
                      if _agent_outcome_completed(item.get("outcome"))))
    session_total = max(
        len(closed_rows) + sum(
            1 for item in current_closed
            if str(item.get("session_id") or "") == str(session_id or "")),
        int((today.get("session_totals") or {}).get(str(session_id or "")) or 0))
    visible_closed = len(records) - running
    return records, running, total, max(0, session_total - visible_closed)


def _agent_tree_lines(records, omitted=0):
    rows = list(records or [])
    bodies = []
    for item in rows:
        label = sanitize_rich(item.get("label") or "agent", cap=AGENT_HISTORY_LABEL_CAP)
        family = sanitize_rich(item.get("family") or "Agent", cap=24)
        clock = fmt_elapsed(_history_number(item.get("secs")))
        if item.get("running"):
            state = "running · %s" % clock
        else:
            outcome = sanitize_rich(
                str(item.get("outcome") or "completed").lower(), cap=20)\
                or "completed"
            word = "done" if outcome in ("completed", "complete", "done", "success")\
                else outcome
            state = "%s %s" % (clock, word)
        bodies.append("%s (%s) · %s" % (label, family, state))
    total_lines = len(bodies) + (1 if omitted else 0)
    out = []
    for index, body in enumerate(bodies):
        out.append(("└" if index == total_lines - 1 else "├") + " " + body)
    if omitted:
        out.append("└ … +%d earlier" % omitted)
    return out or ["- nothing yet"]


def load_state(sdir):
    try:
        with open(os.path.join(sdir, "state.json"), "r") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return {}
        if "agent_history" in state:
            state["agent_history"] = _normalize_agent_history(
                state.get("agent_history"))
        return state
    except Exception:
        return {}


def save_state(sdir, st):
    try:
        tmp = os.path.join(sdir, "state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, os.path.join(sdir, "state.json"))
    except Exception:
        pass


def clear_state(sdir):
    try:
        os.remove(os.path.join(sdir, "state.json"))
    except Exception:
        pass


def _rate_path(sdir):
    return os.path.join(sdir or "", RATE_STATE_FILE)


def _rate_read(sdir):
    try:
        with open(_rate_path(sdir), "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _rate_write(sdir, data):
    try:
        tmp = _rate_path(sdir) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _rate_path(sdir))
        return True
    except Exception:
        return False


def cooldown_remaining(sdir, chat, now=None):
    if not sdir or chat is None:
        return 0.0
    try:
        now = time.time() if now is None else float(now)
        row = (_rate_read(sdir).get("cooldown") or {}).get(str(chat))
        if not isinstance(row, dict):
            return 0.0
        until = float(row.get("until") or 0)
        remaining = until - now
        if remaining <= 0 or remaining > RATE_COOLDOWN_MAX:
            return 0.0
        return remaining
    except Exception:
        return 0.0


def cooldown_arm(sdir, chat, retry_after, now=None):
    if not sdir or chat is None:
        return 0.0
    try:
        now = time.time() if now is None else float(now)
        seconds = float(retry_after)
        if seconds != seconds or seconds < 0:
            seconds = RATE_COOLDOWN_MIN
        seconds = max(RATE_COOLDOWN_MIN, min(RATE_COOLDOWN_MAX, seconds))
    except Exception:
        return 0.0
    try:
        data = _rate_read(sdir)
        cooldown = data.get("cooldown")
        cooldown = dict(cooldown) if isinstance(cooldown, dict) else {}
        existing = 0.0
        row = cooldown.get(str(chat))
        if isinstance(row, dict):
            try:
                existing = float(row.get("until") or 0)
            except Exception:
                existing = 0.0
        until = now + seconds

        if existing > until and (existing - now) <= RATE_COOLDOWN_MAX:
            until = existing
        cooldown[str(chat)] = {
            "until": until,
            "retry_after": int(seconds),
            "stamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }

        if len(cooldown) > 8:
            cooldown = dict(list(cooldown.items())[-8:])
        data["cooldown"] = cooldown
        _rate_write(sdir, data)
        return until - now
    except Exception:
        return 0.0


def _turn_key(proc_start):
    try:
        return "%.3f" % float(proc_start)
    except Exception:
        return ""


def box_sends_used(sdir, proc_start):
    key = _turn_key(proc_start)
    if not sdir or not key:
        return 0
    try:
        row = _rate_read(sdir).get("sends")
        if not isinstance(row, dict) or str(row.get("turn")) != key:
            return 0
        return max(0, int(row.get("n") or 0))
    except Exception:
        return 0


def box_send_allowed(sdir, proc_start):
    try:
        if MAX_BOX_SENDS_PER_TURN <= 0:
            return True
        return box_sends_used(sdir, proc_start) < MAX_BOX_SENDS_PER_TURN
    except Exception:
        return True


def box_send_record(sdir, proc_start):
    key = _turn_key(proc_start)
    if not sdir or not key:
        return 0
    try:
        data = _rate_read(sdir)
        row = data.get("sends")
        n = 0
        if isinstance(row, dict) and str(row.get("turn")) == key:
            try:
                n = max(0, int(row.get("n") or 0))
            except Exception:
                n = 0
        n += 1
        data["sends"] = {"turn": key, "n": n}
        _rate_write(sdir, data)
        return n
    except Exception:
        return 0


def rate_record(sdir, chat, now=None):
    if not sdir or chat is None:
        return
    try:
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            return
        bkt = int(now // RATE_BUCKET_SECONDS)
        data = _rate_read(sdir)
        ledger = data.get("ledger")
        ledger = dict(ledger) if isinstance(ledger, dict) else {}
        chat_buckets = ledger.get(str(chat))
        chat_buckets = dict(chat_buckets) if isinstance(chat_buckets, dict) else {}
        try:
            chat_buckets[str(bkt)] = int(chat_buckets.get(str(bkt)) or 0) + 1
        except Exception:
            chat_buckets[str(bkt)] = 1


        cutoff = bkt - int(RATE_LEDGER_SPAN // RATE_BUCKET_SECONDS) - 1
        pruned = {}
        for k, v in chat_buckets.items():
            try:
                if int(k) >= cutoff:
                    pruned[k] = int(v)
            except Exception:
                continue
        ledger[str(chat)] = pruned

        if len(ledger) > 8:
            ledger = {str(chat): pruned}


        fresh = _rate_read(sdir)
        fresh["ledger"] = ledger
        _rate_write(sdir, fresh)
    except Exception:
        return


def rate_pressure(sdir, chat, now=None):
    if not sdir or chat is None:
        return 0.0
    try:
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            return 0.0
        buckets = (_rate_read(sdir).get("ledger") or {}).get(str(chat))
        if not isinstance(buckets, dict):
            return 0.0


        cur = int(now // RATE_BUCKET_SECONDS)
        parsed = []
        for k, v in buckets.items():
            try:
                bi = int(k)
                c = int(v)
            except Exception:
                continue
            if c > 0 and bi <= cur + 1:
                parsed.append((bi, c))
        if not parsed:
            return 0.0
        floor = 0.0
        for window, budget in RATE_WINDOWS:
            if window <= 0 or budget <= 0:
                continue
            b_min = int((now - window) // RATE_BUCKET_SECONDS)
            count = 0
            for bi, c in parsed:
                if bi >= b_min:
                    count += c
            low = RATE_LOW_WATER * budget
            if count <= low:
                continue
            if count >= budget:
                contrib = RATE_PRESSURE_MAX
            else:
                span = budget - low
                contrib = (RATE_PRESSURE_MAX * (count - low) / span
                           if span > 0 else RATE_PRESSURE_MAX)
            if contrib > floor:
                floor = contrib
        if not math.isfinite(floor) or floor < 0:
            return 0.0
        return min(floor, RATE_PRESSURE_MAX)
    except Exception:
        return 0.0


def _rate_floor(sdir, base, now=None):
    try:
        rp = rate_pressure(sdir, _chat_for_sdir(sdir), now)
        return base if base >= rp else rp
    except Exception:
        return base


def _acquire_state_lock(path, budget=None):
    try:
        fh = open(path, "w")
    except Exception:
        return None, True
    if budget is None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            return fh, True
        except Exception:

            try:
                fh.close()
            except Exception:
                pass
            return None, True
    deadline = time.time() + max(0.0, budget)
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh, True
        except BlockingIOError:
            pass
        except Exception:
            try:
                fh.close()
            except Exception:
                pass
            return None, True
        if time.time() >= deadline:
            try:
                fh.close()
            except Exception:
                pass
            return None, False
        time.sleep(LOCK_WAIT_POLL)


def fresh_state(now, ghosts=None, mission="", prev_recap=None, ctx_pct=None,
                ctx_model=None, agent_history=None, ctx_pending_path=None,
                ctx_effort=None):
    pm, pt, pr = None, None, False
    if prev_recap:
        try:
            pm, pt = prev_recap[:2]
            pr = bool(prev_recap[2]) if len(prev_recap) > 2 else False
        except Exception:
            pm, pt, pr = None, None, False
    fresh = {
        "msgid": None, "steps": [], "proc_start": now,
        "last_edit": 0, "last_activity": 0, "last_reply": 0, "last_react": now,
        "last_spawn": now, "frame": 0, "folded": False, "last_live": None,
        "agents": {}, "next_agent_n": 1, "ghosts": ghosts or [], "typing_done": False,
        "reanchor": False, "box_is_last": False, "folded_above_msgid": None,
        "replied_this_turn": False, "mission": mission or "", "panel": [],
        "prev_recap_msgid": pm, "prev_recap_text": pt,
        "noreply_grace_until": None,
    }
    if pr:
        fresh["prev_recap_rich"] = True


    if ctx_model is not None:
        fresh["ctx_pct"] = ctx_pct
        fresh["ctx_model"] = _context_model_label(ctx_model)


        if isinstance(ctx_pending_path, str) and ctx_pending_path:
            fresh["ctx_pending_path"] = ctx_pending_path


        _store_context_effort(fresh, ctx_effort)
    if agent_history is not None:
        fresh["agent_history"] = _normalize_agent_history(agent_history, now=now)
    return fresh


def _seed_rich_turn(st, sdir, session_id=None, plan=None):
    carried_plan = _plan_copy(plan)
    if carried_plan:
        st["rich_plan"] = carried_plan
        st["rich_session_id"] = str(session_id or "")
    if not rich_enabled(sdir):
        return st
    st.update({
        "rich_turn": True,
        "rich_box": False,
        "rich_failed": False,
        "rich_failed_msgid": None,
        "rich_slow": False,
        "rich_429_streak": 0,
        "rich_transient_streak": 0,
        "rich_hard_streak": 0,
        "rich_pending_open": False,
        "rich_health_logged": False,
        "rich_health_classes": [],
        "rich_plan": carried_plan,
        "rich_tools": [],
        "rich_tool_count": 0,
        "rich_session_id": str(session_id or ""),
        "rich_orphans": [],
        "folded_above_rich": False,
    })
    return st


def _session_plan(st, session_id):
    if not isinstance(st, dict):
        return []
    old = st.get("rich_session_id")
    if session_id is None or (old and str(old) == str(session_id)):
        return _plan_copy(st.get("rich_plan"))
    return []


def _boundary_plan(st, session_id):
    return [
        item for item in _session_plan(st, session_id)
        if _normalize_plan_status(item.get("status")) in ("pending", "in_progress")
    ]


def _pointer_is_rich(st, mid):
    if not mid or not isinstance(st, dict):
        return False
    target = str(mid)


    return bool(
        (target == str(st.get("msgid")) and st.get("rich_box")) or
        (target == str(st.get("folded_above_msgid")) and
         st.get("folded_above_rich")) or
        (target == str(st.get("prev_recap_msgid")) and st.get("prev_recap_rich")))


def _rich_failed_owner(st, mid):
    if isinstance(st, dict) and "rich_failed_msgid" in st:
        owner = st.get("rich_failed_msgid")
        return bool(st.get("rich_failed") and mid and owner is not None
                    and str(owner) == str(mid))

    return bool(
        mid and isinstance(st, dict) and st.get("rich_failed")
        and str(st.get("msgid")) == str(mid) and _pointer_is_rich(st, mid))


def _reserve_rich_targets(st, targets):
    if not isinstance(st, dict):
        return None, ()
    clean = tuple(dict.fromkeys(str(x) for x in (targets or []) if x is not None))
    if not clean:
        return None, ()
    try:
        generation = int(st.get("rich_op_seq") or 0) + 1
    except Exception:
        generation = 1
    raw = st.get("rich_op_latest")
    latest = dict(raw) if isinstance(raw, dict) else {}
    for target in clean:
        latest.pop(target, None)
        latest[target] = generation
    if len(latest) > RICH_OP_TARGETS:
        latest = dict(list(latest.items())[-RICH_OP_TARGETS:])
    st["rich_op_seq"] = generation
    st["rich_op_latest"] = latest
    return generation, clean


def _rich_op_current(st, generation, targets):
    if generation is None:
        return True
    latest = st.get("rich_op_latest") if isinstance(st, dict) else None
    if not isinstance(latest, dict) or not targets:
        return False
    try:
        wanted = int(generation)
    except Exception:
        return False
    for target in targets:
        try:
            if int(latest.get(str(target))) != wanted:
                return False
        except Exception:
            return False
    return True


def _rich_deferred_targets(kind, mid, text):
    if kind in ("rfold", "rrecap"):
        return (mid,)
    if kind in ("rrefold", "mixed_refold"):
        try:
            return (mid, text[0])
        except Exception:
            return (mid,)
    return ()


def _tag_rich_deferred(st, deferred):
    for index, op in enumerate(deferred or []):
        try:
            kind, mid, payload = op[:3]
        except Exception:
            continue
        targets = _rich_deferred_targets(kind, mid, payload)
        if not targets:
            continue
        generation, targets = _reserve_rich_targets(st, targets)
        deferred[index] = tuple(op[:4]) + (generation, targets)
    return deferred


def _invalidate_rich_targets(st, *targets):
    _reserve_rich_targets(st, targets)


def _stash_ghost_state(st, mid):
    if not isinstance(st, dict) or mid is None:
        return
    ghosts = st.get("ghosts")
    ghosts = list(ghosts) if isinstance(ghosts, list) else []
    if str(mid) not in [str(x) for x in ghosts]:
        ghosts.append(mid)
    st["ghosts"] = ghosts[-10:]


def all_steps_done(steps):
    return bool(steps) and all(s.get("done") for s in (steps or []))


def _has_open_tool_step(steps, now=None, cap=None):
    now = time.time() if now is None else now
    cap = CODEX_BG_MAX_AGE if cap is None else cap
    for step in (steps or []):
        if not isinstance(step, dict) or step.get("done")\
                or step.get("codex_bg") or step.get("bash_bg"):
            continue
        started = step.get("t0")
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            continue
        if (now - float(started)) < cap:
            return True
    return False


def _codex_bg_started(step):
    started = (step or {}).get("t0")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    return None


def _codex_bg_due(step, now):
    if not (step or {}).get("codex_bg") or (step or {}).get("done"):
        return False
    started = _codex_bg_started(step)
    return started is not None and (now - started) >= CODEX_BG_MAX_AGE


def _has_open_codex_bg(steps, now=None):
    now = time.time() if now is None else now
    return any(s.get("codex_bg") and not s.get("done") and not _codex_bg_due(s, now)
               for s in (steps or []))


def _open_codex_bg_steps(steps, now=None):
    now = time.time() if now is None else now
    rows = steps if isinstance(steps, list) else []
    return [dict(s) for s in rows
            if s.get("codex_bg") and not s.get("done") and not _codex_bg_due(s, now)]


def _codex_expiry_finishes(steps, now):
    due = False
    for step in (steps or []):
        if step.get("done"):
            continue
        if _codex_bg_due(step, now) or _bash_bg_due(step, now):
            due = True
            continue
        return False
    return due


def _expire_codex_bg(steps, now=None, history_state=None, session_id=""):
    now = time.time() if now is None else now
    expired = 0
    for step in (steps or []):
        if not _codex_bg_due(step, now):
            continue
        cap = step.get("cap") or "Codex"
        if not step.get("codex_stale"):
            step["cap"] = short(cap + " · stale", 80)
        step["codex_stale"] = True
        step["done"] = True
        started = _codex_bg_started(step)
        step["secs"] = max(0, now - started) if started is not None else 0
        expired += 1
    if expired and isinstance(history_state, dict):
        history_state["agent_history"] = _capture_closed_agents(
            history_state.get("agent_history"), [], steps,
            session_id or history_state.get("rich_session_id") or "", now=now)
    return expired


def _bash_bg_started(step):
    started = (step or {}).get("t0")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    return None


def _bash_bg_due(step, now):
    if not (step or {}).get("bash_bg") or (step or {}).get("done"):
        return False
    started = _bash_bg_started(step)
    return started is not None and (now - started) >= CODEX_BG_MAX_AGE


def _has_open_bash_bg(steps, now=None):
    now = time.time() if now is None else now
    return any(isinstance(s, dict) and s.get("bash_bg") and not s.get("done")
               and not _bash_bg_due(s, now) for s in (steps or []))


def _open_bash_bg_steps(steps, now=None):
    now = time.time() if now is None else now
    rows = steps if isinstance(steps, list) else []
    return [dict(s) for s in rows if isinstance(s, dict)
            and s.get("bash_bg") and not s.get("done") and not _bash_bg_due(s, now)]


def _expire_bash_bg(steps, now=None):
    now = time.time() if now is None else now
    expired = 0
    for step in (steps or []):
        if not _bash_bg_due(step, now):
            continue
        cap = step.get("cap") or BASH_BG_LABEL
        if not step.get("bash_stale"):
            step["cap"] = short(cap + " · stale", 80)
        step["bash_stale"] = True
        step["done"] = True
        started = _bash_bg_started(step)
        step["secs"] = max(0, now - started) if started is not None else 0
        expired += 1
    return expired


_BASH_TASK_ID_RE = re.compile(
    r"running in background with ID:\s*([A-Za-z0-9_-]{1,%d})" % BASH_TASK_ID_MAX,
    re.I)
_BASH_TASK_ID_KEY_RE = re.compile(
    r"background_?task_?id\"?\s*[:=]\s*\"?([A-Za-z0-9_-]{1,%d})" % BASH_TASK_ID_MAX,
    re.I)
_BASH_TASK_OUT_RE = re.compile(
    r"(/[^\s\"'\\]{1,%d}/tasks/[A-Za-z0-9_-]{1,%d}\.output)"
    % (BASH_TASK_PATH_MAX, BASH_TASK_ID_MAX))


def _bash_task_handle(tresponse):
    task_id, out_path = "", ""
    text = ""
    if isinstance(tresponse, dict):
        for key, value in tresponse.items():
            if str(key).replace("_", "").lower() in ("backgroundtaskid", "taskid")\
                    and isinstance(value, str):
                task_id = value.strip()
                break
        try:
            text = json.dumps(tresponse, default=str)
        except Exception:
            text = " ".join(str(v) for v in tresponse.values())
    elif isinstance(tresponse, str):
        text = tresponse
    else:
        return "", ""
    if not task_id:
        match = _BASH_TASK_ID_RE.search(text) or _BASH_TASK_ID_KEY_RE.search(text)
        if match:
            task_id = match.group(1)
    path_match = _BASH_TASK_OUT_RE.search(text)
    if path_match:
        out_path = path_match.group(1)
    if not task_id and out_path:
        task_id = basename(out_path)[:-len(".output")]
    if not task_id or len(task_id) > BASH_TASK_ID_MAX\
            or not re.match(r"^[A-Za-z0-9_-]+$", task_id):
        return "", ""
    if out_path and (len(out_path) > BASH_TASK_PATH_MAX
                     or not out_path.startswith("/")):
        out_path = ""
    return task_id, out_path


def _bash_task_status(step):
    path = (step or {}).get("bash_task_out")
    if not isinstance(path, str) or not (0 < len(path) <= BASH_TASK_PATH_MAX):
        return None
    if not path.startswith("/") or any(c in path for c in ("\x00", "\n", "\r")):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > BASH_TASK_TAIL_BYTES:
                fh.seek(size - BASH_TASK_TAIL_BYTES)
            tail = fh.read(BASH_TASK_TAIL_BYTES)
    except Exception:
        return None
    lines = [ln.strip() for ln in tail.decode("utf-8", "replace").splitlines()
             if ln.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if last == BASH_TASK_KILLED_MARK:
        return "killed"
    if last.startswith(BASH_TASK_EXIT_PREFIX):
        code = re.search(r"(-?\d+)", last)
        if code and code.group(1) != "0":
            return "failed"
        return "completed"
    return ""


def _close_gone_bash_bg(steps, now=None):
    now = time.time() if now is None else now
    closed = 0
    for step in (steps or []):
        if not isinstance(step, dict) or not step.get("bash_bg") or step.get("done"):
            continue
        started = _bash_bg_started(step)
        if started is not None and (now - started) < CODEX_PROCESS_START_GRACE:

            continue
        status = _bash_task_status(step)
        if not status:


            if not (status is None and started is not None
                    and (now - started) >= CODEX_KEYLESS_HOLD_MAX):
                continue
            status = "stopped"
        step["done"] = True
        step["secs"] = max(0, now - started) if started is not None else 0


        step["outcome"] = status
        step["bash_inferred"] = True
        closed += 1
    return closed


def _bash_lanes_missing_out(steps):
    return [s for s in (steps if isinstance(steps, list) else [])
            if isinstance(s, dict) and s.get("bash_bg") and not s.get("done")
            and s.get("bash_task_id") and not s.get("bash_task_out")
            and not s.get("bash_out_probed")]


def _bash_backfill_task_out(steps, path, tail_bytes=None):
    lanes = _bash_lanes_missing_out(steps)
    if not lanes:
        return 0
    budget = BG_NOTIFY_TAIL_BYTES if tail_bytes is None else int(tail_bytes)
    try:
        if not isinstance(path, str) or not path or budget <= 0:
            return 0
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return 0
        start = max(0, int(info.st_size) - budget)
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            payload = handle.read(budget)
    except Exception:
        return 0
    if b"/tasks/" not in payload:
        return 0
    text = payload.decode("utf-8", "replace")
    found = 0
    for lane in lanes:
        lane["bash_out_probed"] = True
        task_id = lane.get("bash_task_id")
        if not isinstance(task_id, str) or not re.match(r"^[A-Za-z0-9_-]+$", task_id):
            continue

        match = re.search(
            r"(/[^\s\"'\\]{1,%d}/tasks/%s\.output)"
            % (BASH_TASK_PATH_MAX, re.escape(task_id)), text)
        if not match:
            continue
        candidate = match.group(1)
        if len(candidate) <= BASH_TASK_PATH_MAX and candidate.startswith("/"):
            lane["bash_task_out"] = candidate
            found += 1
    return found


def _bash_bg_accept_launch(step, tresponse):
    if not isinstance(step, dict) or not step.pop("bash_bg_requested", False):
        return False
    task_id, out_path = _bash_task_handle(tresponse)
    if not task_id:
        return False
    step["bash_bg"] = True
    step["bash_task_id"] = task_id
    if out_path:
        step["bash_task_out"] = out_path
    return True


def _unghost(st, mid):
    g = st.get("ghosts") or []
    if mid in g:
        st["ghosts"] = [x for x in g if x != mid]


def _replied_this_turn(st):
    v = st.get("replied_this_turn")
    if v is None:
        return bool(st.get("folded_above_msgid"))
    return bool(v)


def _noreply_below(st, steps):
    return bool(st.get("msgid") and not st.get("folded") and st.get("box_is_last")
                and not _replied_this_turn(st) and steps)


def _grace_deadline(st):
    v = (st or {}).get("noreply_grace_until")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _grace_shape(st, steps):
    return bool(st.get("msgid") and st.get("folded") and st.get("box_is_last")
                and not _replied_this_turn(st) and steps)


def _grace_armed(st, steps):
    return _grace_deadline(st) is not None and _grace_shape(st, steps)


def _grace_due(st, steps, now=None):
    dl = _grace_deadline(st)
    if dl is None or not _grace_shape(st, steps):
        return False
    return (time.time() if now is None else now) >= dl


def _prev_pair(mid, text, rich=False):
    valid = _is_rich_recap_text(text) if rich else _is_recap_text(text)
    if mid and valid:
        return mid, text
    return None, None


def _mission_override(sdir, proc_start):
    if not proc_start:
        return ""
    try:
        p = os.path.join(os.path.dirname(sdir), MISSION_FILE)
        stt = os.stat(p)
        if stt.st_mtime < proc_start:
            return ""


        if not stat.S_ISREG(stt.st_mode):
            return ""


        with open(p, "r", encoding="utf-8") as f:
            first = f.readline()
        return short(first, MISSION_CAP)
    except Exception:
        return ""


def current_mission(sdir, st):
    return _mission_override(sdir, (st or {}).get("proc_start")) or MISSION_DEFAULT


def _mission_paths(sdir):
    try:
        base = os.path.dirname(sdir)
        if base in ("", "/", "."):
            return []
        raw = os.path.join(base, MISSION_FILE)
        out = []
        for c in (os.path.abspath(raw), raw, os.path.realpath(raw)):
            if c and c not in out:
                out.append(c)
        return out
    except Exception:
        return []


def _is_mission_write(sdir, tool, tinput):
    t = tool or ""
    if t not in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        return False
    targets = _mission_paths(sdir)
    if not targets:
        return False
    ti = tinput if isinstance(tinput, dict) else {}
    if t == "Bash":
        cmd = ti.get("command")
        return isinstance(cmd, str) and any(p in cmd for p in targets)
    fp = ti.get("file_path")
    return isinstance(fp, str) and any(fp.endswith(p) for p in targets)


def _panel_open(panel, tuid, tinput, now, session_id=""):
    ti = tinput if isinstance(tinput, dict) else {}
    panel = list(panel or [])
    row = {
        "tuid": tuid or "",
        "lab": short(ti.get("description") or "a task", PANEL_LABEL_CAP),
        "typ": short(ti.get("subagent_type") or "agent", PANEL_TYPE_CAP),
        "mdl": short(ti.get("model") or "", PANEL_MODEL_STORE),
        "t0": now, "done": False, "secs": 0,
    }
    if session_id:
        row["history_session_id"] = short(session_id, AGENT_HISTORY_SESSION_CAP)


    if ti.get("run_in_background") is True:
        row["background_requested"] = True
    panel.append(row)
    if len(panel) > PANEL_MAX_STORED:
        panel = panel[len(panel) - PANEL_MAX_STORED:]
    return panel


def _is_async_agent_launch(tresponse):
    if isinstance(tresponse, dict):


        if tresponse.get("isAsync") is True:
            return True
        if tresponse.get("isAsync") is False:
            return False


        if tresponse.get("success") is not False\
                and _clean_agent_id(tresponse.get("resumedAgentId")):
            return True
        status = str(tresponse.get("status") or "").strip().lower().replace("-", "_")
        if status in ("async_launched", "background_launched"):
            return True
        if status in ("completed", "complete", "succeeded", "success", "done",
                      "failed", "error", "cancelled", "canceled", "killed",
                      "stopped"):
            return False
        try:
            text = json.dumps(tresponse, sort_keys=True)
        except Exception:
            text = " ".join(str(v) for v in tresponse.values())
    elif isinstance(tresponse, str):
        text = tresponse
    else:
        return False
    lowered = text.lower()
    return "async agent launched" in lowered or (
        "agentid" in lowered.replace("_", "") and "background" in lowered)


def _panel_record_served_model(panel, tuid, tresponse):
    if not isinstance(tresponse, dict):
        return False
    served = tresponse.get("resolvedModel")
    served = served.strip() if isinstance(served, str) else ""
    if not served or len(served) > PANEL_MODEL_STORE\
            or not _claude_version_label(served):
        return False
    rows = panel if isinstance(panel, list) else []
    if tuid:
        row = next((r for r in rows if isinstance(r, dict) and not r.get("done")
                    and r.get("tuid") == tuid), None)
    else:
        row = next((r for r in reversed(rows) if isinstance(r, dict)
                    and not r.get("done") and not r.get("tuid")), None)
    if row is None:
        return False
    row["srv"] = served
    return True


def _panel_accept_background_launch(panel, tuid, tresponse):
    rows = panel if isinstance(panel, list) else []
    row = None
    if tuid:
        row = next((r for r in rows if isinstance(r, dict)
                    and not r.get("done") and r.get("tuid") == tuid), None)
    else:
        row = next((r for r in reversed(rows) if isinstance(r, dict)
                    and not r.get("done") and not r.get("tuid")), None)
    if row is None:
        return False
    requested = row.pop("background_requested", False) is True
    response_failed = False
    response_terminal = False
    if isinstance(tresponse, dict):
        status = str(tresponse.get("status") or "").strip().lower().replace("-", "_")
        response_failed = bool(tresponse.get("success") is False
                               or tresponse.get("error")
                               or status in ("failed", "error", "cancelled", "canceled"))
        response_terminal = bool(
            tresponse.get("isAsync") is False
            or status in ("completed", "complete", "succeeded", "success", "done",
                          "killed", "stopped"))
        if response_failed:
            row["outcome"] = status if status in (
                "failed", "error", "cancelled", "canceled") else "failed"
        elif response_terminal and status in ("killed", "stopped"):
            row["outcome"] = status
    launched = not response_failed and not response_terminal and (
        requested or _is_async_agent_launch(tresponse))
    if launched:
        row["background"] = True
    return launched


def _clean_agent_id(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if AGENT_ID_RE.match(value) else ""


def _agent_result_dict(tresponse):
    if isinstance(tresponse, dict):
        return tresponse
    if isinstance(tresponse, str):
        parts = [tresponse]
    elif isinstance(tresponse, list):
        parts = [block.get("text") for block in tresponse
                 if isinstance(block, dict) and isinstance(block.get("text"), str)]
    else:
        return None
    if not parts or sum(len(part) for part in parts) > TOOL_RESULT_JSON_MAX:
        return None
    text = "".join(parts).strip()
    if not text.startswith("{") or (
            "gentId" not in text and AGENT_QUEUED_PREFIX not in text):
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _background_agent_signal(tresponse):
    data = _agent_result_dict(tresponse)
    if not data or data.get("success") is False or data.get("error"):
        return "", ""
    resumed = _clean_agent_id(data.get("resumedAgentId"))
    if resumed:
        return resumed, "resumed"
    message = data.get("message")
    pin = data.get("pin")
    if data.get("success") is True and isinstance(message, str)\
            and message.startswith(AGENT_QUEUED_PREFIX) and isinstance(pin, dict):
        pinned = _clean_agent_id(pin.get("id"))
        if pinned:
            return pinned, "queued"


    launched = _clean_agent_id(data.get("agentId"))
    status = str(data.get("status") or "").strip().lower().replace("-", "_")
    if launched and (data.get("isAsync") is True
                     or status in ("async_launched", "background_launched")):
        return launched, "launched"
    return "", ""


def _panel_stamp_agent_id(panel, tuid, tresponse):
    data = _agent_result_dict(tresponse)
    if not data:
        return False
    aid = _clean_agent_id(data.get("agentId"))\
        or _clean_agent_id(data.get("resumedAgentId"))
    if not aid:
        return False
    rows = panel if isinstance(panel, list) else []
    if tuid:
        row = next((r for r in rows if isinstance(r, dict) and not r.get("done")
                    and r.get("tuid") == tuid), None)
    else:
        row = next((r for r in reversed(rows) if isinstance(r, dict)
                    and not r.get("done") and not r.get("tuid")), None)
    if row is None:
        return False
    row["agent_id"] = aid
    return True


def _subagent_meta(session_id, agent_id, project_dir=None):
    try:
        aid = _clean_agent_id(agent_id)
        transcript = _context_transcript_path(
            None, session_id=session_id, project_dir=project_dir) if aid else None
        if not transcript or not transcript.endswith(".jsonl"):
            return {}
        path = os.path.join(transcript[:-len(".jsonl")], "subagents",
                            "agent-%s.meta.json" % aid)
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > AGENT_META_MAX_BYTES:
            return {}
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.loads(handle.read(AGENT_META_MAX_BYTES + 1))
        if not isinstance(data, dict):
            return {}
        out = {}
        for key in ("description", "agentType", "model", "toolUseId"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
        return out
    except Exception:
        return {}


def _panel_register_background_agent(panel, tuid, agent_id, kind, now, tinput=None,
                                     session_id="", meta=None):
    rows = list(panel) if isinstance(panel, list) else []
    aid = _clean_agent_id(agent_id)
    if not aid or kind not in ("resumed", "queued", "launched"):
        return rows, None
    tuid = tuid if isinstance(tuid, str) else ""
    loaded = []

    def info():
        if not loaded:
            value = meta() if callable(meta) else meta
            loaded.append(value if isinstance(value, dict) else {})
        return loaded[0]

    mine = [r for r in rows if isinstance(r, dict) and r.get("agent_id") == aid]
    if not mine:
        origin = info().get("toolUseId")
        legacy = next((r for r in rows if isinstance(r, dict) and origin
                       and r.get("tuid") == origin and not r.get("agent_id")), None)
        if legacy is not None:
            legacy["agent_id"] = aid
            mine = [legacy]
    if kind == "queued":
        live = next((r for r in reversed(mine) if not r.get("done")), None)
        if live is not None:
            return rows, live
    if tuid:
        own = next((r for r in rows if isinstance(r, dict)
                    and r.get("tuid") == tuid), None)
        if own is not None:
            return rows, own
    row = next((r for r in reversed(mine) if r.get("done")), None)
    if row is None:
        ti = tinput if isinstance(tinput, dict) else {}
        summary = ti.get("summary") if isinstance(ti.get("summary"), str) else ""
        row = {
            "lab": short(info().get("description") or summary or AGENT_RESUME_LABEL,
                         PANEL_LABEL_CAP),
            "typ": short(info().get("agentType") or "agent", PANEL_TYPE_CAP),
            "mdl": short(info().get("model") or "", PANEL_MODEL_STORE),
        }
        rows.append(row)
        if len(rows) > PANEL_MAX_STORED:
            rows = rows[len(rows) - PANEL_MAX_STORED:]
    for key in ("outcome", "history_recorded_day", "background_requested",
                "close_by_agent_id"):
        row.pop(key, None)


    by_agent = kind == "queued" or not tuid
    row.update({"tuid": tuid or "agent-%s-%d" % (aid, int(now * 1000)), "t0": now,
                "done": False, "secs": 0, "background": True, "agent_id": aid,
                "registered_by": kind})
    if by_agent:
        row["close_by_agent_id"] = True
    if session_id:
        row["history_session_id"] = short(session_id, AGENT_HISTORY_SESSION_CAP)
    return rows, row


def _panel_close(panel, tuid, now, history_state=None, session_id=""):
    panel = list(panel or [])
    row = None
    if tuid:
        for r in panel:
            if not r.get("done") and r.get("tuid") == tuid:
                row = r
                break
    else:
        for r in reversed(panel):
            if not r.get("done") and not r.get("tuid"):
                row = r
                break
    if row is not None:
        row["done"] = True
        row["secs"] = max(0, now - (row.get("t0") or now))
        if isinstance(history_state, dict):
            history_state["agent_history"] = _capture_closed_agents(
                history_state.get("agent_history"), panel, [],
                session_id or history_state.get("rich_session_id") or "", now=now)
    return panel


def _background_agent_started(row):
    started = (row or {}).get("t0")
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    return None


def _background_agent_due(row, now):
    if not isinstance(row, dict) or row.get("background") is not True\
            or row.get("done"):
        return False
    started = _background_agent_started(row)
    return started is not None and (now - started) >= CODEX_BG_MAX_AGE


def _has_open_background_agents(panel, now=None):
    now = time.time() if now is None else now
    rows = panel if isinstance(panel, list) else []
    return any(isinstance(row, dict) and row.get("background") is True
               and not row.get("done") and not _background_agent_due(row, now)
               for row in rows)


def _open_background_agent_rows(panel, now=None):
    now = time.time() if now is None else now
    rows = panel if isinstance(panel, list) else []
    return [dict(row) for row in rows if isinstance(row, dict)
            and row.get("background") is True and not row.get("done")
            and not _background_agent_due(row, now)]


def _open_background_agent_steps(steps, panel, now=None):
    del now
    rows = panel if isinstance(panel, list) else []
    ids = {row.get("tuid") for row in rows if row.get("tuid")}
    source = steps if isinstance(steps, list) else []
    return [dict(step) for step in source if isinstance(step, dict)
            and step.get("tuid") in ids]


def _expire_background_agents(panel, now=None, history_state=None, session_id=""):
    now = time.time() if now is None else now
    expired = 0
    rows = panel if isinstance(panel, list) else []
    for row in rows:
        if not _background_agent_due(row, now):
            continue
        row["done"] = True
        row["outcome"] = "stale"
        started = _background_agent_started(row)
        row["secs"] = max(0, now - started) if started is not None else 0
        expired += 1
    if expired and isinstance(history_state, dict):
        history_state["agent_history"] = _capture_closed_agents(
            history_state.get("agent_history"), panel, [],
            session_id or history_state.get("rich_session_id") or "", now=now)
    return expired


def _families_in_order(steps):
    order = []
    for s in steps:
        f = s.get("fam", "other")
        if f not in order:
            order.append(f)
    return order


def _subagent_titles(steps):
    return [s.get("cap", "a task") for s in steps if s.get("k") == "subtitle"]


def _helper_run_count(steps):
    runs, i, n = 0, 0, len(steps)
    while i < n:
        if steps[i].get("fam") == "helpers":
            runs += 1
            while i < n and steps[i].get("fam") == "helpers":
                i += 1
        else:
            i += 1
    return runs


def _agent_label(meta):
    num = meta.get("n")
    atype = meta.get("type") or "agent"
    return ("%d %s" % (num, atype)) if num else atype


def mission_line(mission):
    return "%s %s" % (MISSION_EMOJI, mission)


def _live_step_caption(step, now=None):
    cap = short(step.get("cap") or "working", LIVE_CAP)
    started = step.get("t0")
    if (step.get("codex") and not step.get("done")
            and isinstance(started, (int, float)) and not isinstance(started, bool)):
        now = time.time() if now is None else now
        cap = short("%s · %s" % (cap, fmt_elapsed(now - started)), LIVE_CAP)
    return cap


def _panel_counts(panel):
    active = sum(1 for r in (panel or []) if not r.get("done"))
    return active, len(panel or []) - active


def _panel_row_bits(r, now):
    dur = r.get("secs") if r.get("done") else (now - (r.get("t0") or now))
    return (short(r.get("typ") or "agent", PANEL_TYPE_CAP),
            model_label(r.get("mdl")),
            short(r.get("lab") or "a task", PANEL_LABEL_CAP),
            fmt_elapsed(dur))


def _panel_pick(panel):
    rows = list(panel or [])
    active = [r for r in rows if not r.get("done")]
    done = [r for r in rows if r.get("done")]
    if len(active) >= PANEL_ROWS:
        shown = active[-PANEL_ROWS:]
    else:
        shown = active + done[-(PANEL_ROWS - len(active)):]
    return shown, len(rows) - len(shown)


def panel_lines(panel, now=None):


    rows = list(panel) if isinstance(panel, list) else []
    if not rows:
        return []
    now = time.time() if now is None else now
    active, done = _panel_counts(rows)
    bits = []
    if active:
        bits.append("%d active" % active)
    if done:
        bits.append("%d done" % done)
    out = ["%s Agents · %s" % (AGENT_EMOJI, " · ".join(bits))]
    shown, hidden = _panel_pick(rows)
    for r in shown:
        typ, mdl, lab, clk = _panel_row_bits(r, now)
        out.append("%s%s %s · %s · %s · %s" % (
            SUB, "✓" if r.get("done") else "⏳", typ, mdl, lab, clk))
    if hidden > 0:
        out.append("%s… +%d more" % (SUB, hidden))
    return out


def panel_recap_lines(panel, now=None, preserve_open=False):


    rows = list(panel) if isinstance(panel, list) else []
    if not rows:
        return []
    now = time.time() if now is None else now
    out = []
    for r in rows[:PANEL_ROWS]:
        typ, mdl, lab, clk = _panel_row_bits(r, now)
        state = ""
        if preserve_open:
            state = ("✓ finished · " if r.get("done") else "◉ running · ")
        out.append("%s %s%s · %s · %s · %s" % (
            AGENT_EMOJI, state, typ, mdl, lab, clk))
    if len(rows) > PANEL_ROWS:
        out.append("+%d more" % (len(rows) - PANEL_ROWS))
    return out


def _rich_plan_counts(plan):
    rows = _plan_copy(plan)
    eligible = [x for x in rows if _normalize_plan_status(x.get("status")) != "cancelled"]
    done = sum(1 for x in eligible if _normalize_plan_status(x.get("status")) == "completed")
    return done, len(eligible)


def _rich_step_counts(steps):
    rows = [x for x in (steps or []) if isinstance(x, dict)
            and x.get("k") != "subtitle"]
    return sum(1 for x in rows if x.get("done")), len(rows)


def _rich_plan_line(item):
    status = _normalize_plan_status(item.get("status"))
    subject = sanitize_rich(item.get("subject") or "task", cap=RICH_PLAN_SUBJECT_CAP)
    if status == "completed":
        return "- [x] " + subject
    if status == "in_progress":
        return "- [ ] **%s** ◀" % subject
    if status == "cancelled":
        return "- [x] ~~%s~~" % subject
    return "- [ ] " + subject


def _rich_current(steps):
    rows = [x for x in (steps or []) if isinstance(x, dict)]
    current = next((x for x in reversed(rows) if not x.get("done")
                    and x.get("k") != "subtitle"), None)
    if current is None:
        return "thinking"
    return sanitize_rich(current.get("cap") or "working", cap=220) or "working"


def _rich_headline(text, cap):
    out = sanitize_rich(text, cap=None)
    if len(out) <= cap:
        return out
    head = out[:cap - 1]
    space = head.rfind(" ")
    if space >= cap // 2:
        head = head[:space]
    return head.rstrip(" ·-,;:") + "…"


def _rich_activity(steps, frame, panel=None):


    rows = [x for x in (steps or []) if isinstance(x, dict)]
    current = next((x for x in reversed(rows) if not x.get("done")
                    and x.get("k") != "subtitle" and not x.get("codex_bg")
                    and not x.get("bash_bg")), None)
    if current is None:


        open_lanes = [x for x in rows if x.get("codex_bg") and not x.get("done")]
        open_agents = [x for x in (panel if isinstance(panel, list) else [])
                       if isinstance(x, dict) and x.get("background") is True
                       and not x.get("done")]
        open_tasks = [x for x in rows if x.get("bash_bg") and not x.get("done")]
        lane_count = len(open_lanes) + len(open_agents)
        if lane_count:
            return "⏳ Waiting on the agent%s" % ("" if lane_count == 1 else "s")


        if open_tasks:
            return "⏳ Waiting on background work"
        return thinking_line(frame)
    cap = _rich_headline(current.get("cap") or "working",
                         RICH_ACTIVITY_CAP - 2) or "working"
    return "⏳ %s" % cap


def _rich_lane_state(row):
    raw = str((row or {}).get("codex_terminal_status") or
              (row or {}).get("outcome") or (row or {}).get("status") or "").lower()


    if not raw and ((row or {}).get("codex_stale") or (row or {}).get("bash_stale")):
        raw = "stale"
    if raw in ("failed", "error"):
        return "! fail"
    if raw in ("killed", "cancelled", "canceled"):
        return "✕ kill"
    if raw in ("stopped", "stale"):
        return "■ stop"
    if raw in ("queued", "pending"):
        return "○ queued"
    if (row or {}).get("done") or raw in ("completed", "done", "success"):
        return "✓ done"
    return "◉ live"


def _pgrep_escape(value):
    specials = set("\\.^$*+?{}[]()|")
    return "".join(("\\" + char) if char in specials else char for char in value)


def _pgrep_literal(value):
    return "(^|[[:space:]])-o[[:space:]]+" + _pgrep_escape(value) + "([[:space:]]|$)"


def _shell_literal_token(value):
    if not isinstance(value, str) or not value:
        return False
    if any(char in value for char in "$`~*?[]{}!\\'\"" ):
        return False
    return not any(char.isspace() for char in value)


def _codex_key_pattern(key):
    if not isinstance(key, str) or not (0 < len(key) <= CODEX_PROCESS_KEY_MAX):
        return ""
    if any(char in key for char in ("\x00", "\n", "\r")):
        return ""
    if _shell_literal_token(key):
        return _pgrep_literal(key)
    tail = []
    for segment in key.split("/"):
        if segment in ("", ".", "..") or not _shell_literal_token(segment):
            tail = []
            continue
        tail.append(segment)
    if not tail:
        return ""


    return ("(^|[[:space:]])-o[[:space:]]+(.*/)?"
            + _pgrep_escape("/".join(tail)) + "([[:space:]]|$)")


def _codex_alive(step=None):
    row = step if isinstance(step, dict) else {}
    key = row.get("codex_key")
    key = key if isinstance(key, str) and 0 < len(key) <= CODEX_PROCESS_KEY_MAX else ""
    pattern = _codex_key_pattern(key) if key else ""
    pattern = pattern or "codex exec"
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-f", pattern],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=1.5)
        if out.returncode == 0:
            return True
        if out.returncode == 1:
            return False
        return None
    except Exception:
        return None


def _close_gone_codex_bg(steps, now=None, history_state=None, session_id=""):
    now = time.time() if now is None else now
    results = {}
    closed = 0
    for step in (steps or []):
        if not isinstance(step, dict) or not step.get("codex_bg") or step.get("done"):
            continue
        raw_key = step.get("codex_key")
        key = raw_key if isinstance(raw_key, str)\
            and 0 < len(raw_key) <= CODEX_PROCESS_KEY_MAX else None


        if key is not None and not _codex_key_pattern(key):
            key = None
        if key not in results:
            results[key] = _codex_alive(step)
        started = _codex_bg_started(step)
        if results[key] is not False:


            if not (key is None and started is not None
                    and now - started >= CODEX_KEYLESS_HOLD_MAX):
                continue
        if key is not None and started is not None\
                and now - started < CODEX_PROCESS_START_GRACE:
            continue
        step["done"] = True
        step["secs"] = max(0, now - started) if started is not None else 0


        step["codex_terminal_status"] = "stopped"
        step["codex_inferred"] = True
        closed += 1
    if closed and isinstance(history_state, dict):
        history_state["agent_history"] = _capture_closed_agents(
            history_state.get("agent_history"), [], steps,
            session_id or history_state.get("rich_session_id") or "", now=now)
    return closed


def _rich_lane_rows(steps, panel, now=None, fold=False, preserve_open=False):
    now = time.time() if now is None else now
    rows = []
    for row in (panel if isinstance(panel, list) else []):
        if not isinstance(row, dict):
            continue
        state = _rich_lane_state(row)


        family = sanitize_rich(_panel_row_family(row, now=now), cap=16) or "Agent"
        suffix = " (%s)" % family
        label = "%s%s" % (
            sanitize_rich(row.get("lab") or row.get("typ") or "agent",
                          cap=max(1, RICH_TABLE_CELL_CAP - len(suffix))),
            suffix)
        if state == "○ queued":
            clock = "--:--"
        else:
            dur = row.get("secs") if row.get("done") else now - (row.get("t0") or now)
            clock = _rich_clock(dur)
        if fold and not preserve_open and state == "◉ live" and not (
                row.get("background") is True
                and not _background_agent_due(row, now)):
            state, clock = "■ ended", "\u2014"
        rows.append((sanitize_rich(label, table=True), state, clock))
    for step in (steps or []):
        if not isinstance(step, dict) or not step.get("codex_bg"):
            continue
        state = _rich_lane_state(step)
        if state == "○ queued":
            clock = "--:--"
        else:
            dur = step.get("secs") if step.get("done") else now - (step.get("t0") or now)
            clock = _rich_clock(dur)

        suffix = " (%s)" % (sanitize_rich(_codex_step_family(step), cap=16) or "Codex")
        label = "%s%s" % (
            sanitize_rich(_codex_lane_label(step.get("cap") or "Codex"),
                          cap=max(1, RICH_TABLE_CELL_CAP - len(suffix))),
            suffix)
        rows.append((sanitize_rich(label, table=True), state, clock))


    for step in (steps or []):
        if not isinstance(step, dict) or not step.get("bash_bg"):
            continue
        state = _rich_lane_state(step)
        if state == "○ queued":
            clock = "--:--"
        else:
            dur = step.get("secs") if step.get("done") else now - (step.get("t0") or now)
            clock = _rich_clock(dur)
        suffix = " (Task)"
        label = "%s%s" % (
            sanitize_rich(step.get("cap") or BASH_BG_LABEL,
                          cap=max(1, RICH_TABLE_CELL_CAP - len(suffix))),
            suffix)
        rows.append((sanitize_rich(label, table=True), state, clock))
    return rows[:PANEL_ROWS + 2]


def _live_lane_identity(row, kind, session_id, now):
    try:
        return _closed_agent_entry(row, kind, session_id, now).get("id") or None
    except Exception:
        return None


def _rich_history_lane_rows(steps, panel, history, session_id, now, room):
    if room <= 0:
        return []


    if not (history.get("rows") if isinstance(history, dict) else history):
        return []
    today = _normalize_agent_history(history, now=now)
    rows = today.get("rows") or []
    if not rows:
        return []
    want = str(session_id or "")
    seen = set()
    for row in (panel if isinstance(panel, list) else []):
        if isinstance(row, dict):
            seen.add(_live_lane_identity(row, "agent", session_id, now))
    for step in (steps if isinstance(steps, list) else []):
        if isinstance(step, dict) and step.get("codex_bg"):
            seen.add(_live_lane_identity(step, "codex", session_id, now))
    shaped = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("session_id") or "") != want:
            continue
        identity = item.get("id")
        if identity and identity in seen:
            continue


        shaped.append({
            "lab": item.get("label") or "agent",
            "typ": item.get("family") or "Agent",


            "lane_family": item.get("family") or "",
            "mdl": item.get("model") or "",
            "t0": _history_number(item.get("t0")),
            "done": True,
            "secs": _history_number(item.get("secs")),


            "outcome": item.get("outcome") or "completed",
        })
    if not shaped:
        return []


    return _rich_lane_rows([], shaped[-room:], now)


def _rich_live_lanes(steps, panel, history=None, session_id="", now=None):
    now = time.time() if now is None else now
    current = _rich_lane_rows(steps, panel, now)
    room = RICH_LIVE_LANE_ROWS - len(current)
    if room <= 0:
        return current, len(current)
    carried = _rich_history_lane_rows(
        steps, panel, history, session_id, now, room)
    return current + carried, len(current)


def _rich_tool_rows(tools):
    total = None
    if isinstance(tools, dict):
        total = tools.get("count")
        tools = tools.get("items")
    rows = tools if isinstance(tools, list) else []
    out = []
    for item in rows[-RICH_TOOL_LINES:]:
        if isinstance(item, dict):
            cap = item.get("cap") or item.get("line") or item.get("tool") or "tool"


            secs = item.get("elapsed") if item.get("elapsed") is not None\
                else item.get("secs") or 0
        else:
            cap, secs = item, 0
        out.append("- at %s · %s" % (
            _rich_clock(secs), sanitize_rich(cap, cap=RICH_TOOL_LINE_CAP)))
    try:
        total = max(len(out), int(total)) if total is not None else len(out)
    except Exception:
        total = len(out)
    return out, total


def _rich_lane_table(lanes):
    if not lanes:
        return []


    lines = ["| %s Agent | Time |" % AGENT_EMOJI, "|:---|---:|"]


    lines.extend("| %s %s | %s |" % (state.split()[0] if state else "·", label, clock)
                 for label, state, clock in lanes)
    return lines


def _assemble_rich_live(name, emoji, mission, elapsed, done, total, activity,
                        plan_rows, plan_omitted, lanes, tool_rows, tool_count,
                        cadence, fallback, frame=0, context_line=None,
                        agent_records=None, agent_running=0, agent_done=0,
                        agent_omitted=0):
    del name, emoji


    top = RICH_BR.join([


        "%s %s" % (MISSION_EMOJI, mission),
        "%s Working · %s · %s" % (WORKING_EMOJI, activity_bar(frame), elapsed),
        context_line or _context_line({}),
        activity,
    ])
    lines = [top]


    lines.extend(["", "---"])


    table = _rich_lane_table(lanes)
    if table:
        lines.extend(["", *table, "", "---"])


    lines.extend(["", "<details><summary>📋 Plan · %d/%d done · tap</summary>" %
                  (done, total)])
    if plan_omitted:
        lines.append("- +%d lower-priority detail%s hidden" % (
            plan_omitted, "" if plan_omitted == 1 else "s"))
    lines.extend(plan_rows or ["- nothing logged yet"])
    lines.append("</details>")
    lines.extend(["", "<details><summary>🔧 Tools · %d call%s · tap</summary>" %
                  (tool_count, "" if tool_count == 1 else "s")])
    tool_omitted = max(0, tool_count - len(tool_rows))
    if tool_omitted:
        lines.append("… %d earlier not shown" % tool_omitted)
    lines.extend(tool_rows or ["- nothing yet"])
    lines.append("</details>")


    del agent_records, agent_running, agent_done, agent_omitted


    del cadence
    if fallback:
        lines.extend(["", "⚠ plain fallback"])
    return "\n".join(lines)


def _assemble_plain_fallback(name, emoji, mission, elapsed, done, total, activity,
                             plan_items, lanes, tool_rows, tool_count, frame=0,
                             context_line=None, agent_records=None, agent_running=0,
                             agent_done=0, agent_omitted=0, lane_floor=None):


    del name, emoji


    del agent_records, agent_running, agent_done, agent_omitted
    plan_rows = []
    for item in plan_items:
        status = _normalize_plan_status(item.get("status"))
        subject = sanitize_rich(item.get("subject") or "task",
                                cap=RICH_PLAN_SUBJECT_CAP)
        if status == "completed":
            plan_rows.append("- [x] " + subject)
        elif status == "cancelled":
            plan_rows.append("- [x] %s (cancelled)" % subject)
        elif status == "in_progress":
            plan_rows.append("- [ ] %s ◀" % subject)
        else:
            plan_rows.append("- [ ] " + subject)
    kept_tools = list(tool_rows)
    kept_plan = list(plan_rows)


    kept_lanes = list(lanes or [])
    lane_keep = len(kept_lanes) if lane_floor is None else max(0, int(lane_floor))
    plan_omitted = 0
    while True:
        lines = [


            "%s %s" % (MISSION_EMOJI, mission),
            "%s Working · %s · %s" % (WORKING_EMOJI, activity_bar(frame), elapsed),
            context_line or _context_line({}),
            activity,
        ]


        if kept_lanes:
            lines.append("%s Agent · Time" % AGENT_EMOJI)
            lines.extend("- %s %s · %s" % (state.split()[0] if state else "·",
                                           label, clock)
                         for label, state, clock in kept_lanes)
        lines.append("📋 Plan · %d/%d done" % (done, total))
        if plan_omitted:
            lines.append("- +%d earlier" % plan_omitted)
        lines.extend(kept_plan or ["- nothing logged yet"])
        lines.append("🔧 Tools · %d call%s" % (
            tool_count, "" if tool_count == 1 else "s"))
        tool_omitted = max(0, tool_count - len(kept_tools))
        if tool_omitted:
            lines.append("… %d earlier not shown" % tool_omitted)
        lines.extend(kept_tools or ["- nothing yet"])
        lines.append("live · plain fallback")
        text = "\n".join(lines)
        if len(text) < RICH_FRAME_LIMIT:
            return text
        if kept_tools:
            kept_tools.pop(0)
            continue
        if kept_plan:
            kept_plan.pop(0)
            plan_omitted += 1
            continue


        if len(kept_lanes) > lane_keep:
            kept_lanes.pop(lane_keep)
            continue


        return text[: RICH_FRAME_LIMIT - 1]


def render_rich_live(sdir, steps, start, agents, mission="", panel=None,
                     plan=None, tools=None, cadence=RICH_MIN_EDIT_INTERVAL,
                     fallback=False, frame=0, state=None):
    del agents
    now = time.time()
    name, emoji = bot_identity(sdir)


    mission_text = _rich_headline(mission or MISSION_DEFAULT,
                                  RICH_MISSION_CAP) or MISSION_DEFAULT
    activity = _rich_activity(steps, frame, panel)
    plan_items = _plan_copy(plan)
    if plan_items:
        done, total = _rich_plan_counts(plan_items)
        plan_entries = [
            (_normalize_plan_status(item.get("status")), _rich_plan_line(item))
            for item in plan_items
        ]
    else:


        done, total = 0, 0
        plan_entries = []
    tools_kept, tool_count = _rich_tool_rows(tools)
    view_state = state if isinstance(state, dict) else {}


    lanes, lane_floor = _rich_live_lanes(
        steps, panel, view_state.get("agent_history"),
        view_state.get("rich_session_id"), now)
    context = _context_line(view_state)
    agent_entries, agent_running, agent_done, agent_omitted = _agent_display_records(
        steps, panel, view_state.get("agent_history"),
        view_state.get("rich_session_id"), now=now)
    kept_agents = list(agent_entries)
    omitted = 0

    elapsed = fmt_elapsed(now - start) if start else "0:00"
    if fallback:
        return _assemble_plain_fallback(
            name, emoji, mission_text, elapsed, done, total, activity,
            plan_items, lanes, tools_kept, tool_count, frame, context,
            kept_agents, agent_running, agent_done, agent_omitted,
            lane_floor=lane_floor)
    while True:
        plan_rows = [entry[1] for entry in plan_entries]
        text = _assemble_rich_live(
            name, emoji, mission_text, elapsed, done, total, activity,
            plan_rows, omitted, lanes, tools_kept, tool_count, cadence, fallback,
            frame, context, kept_agents, agent_running, agent_done, agent_omitted)
        if _rich_frame_fits(text):
            return text
        if tools_kept:
            tools_kept.pop(0)
            continue
        trim_index = _rich_plan_trim_index(plan_entries)
        if trim_index is not None:
            plan_entries.pop(trim_index)
            omitted += 1
            continue


        if len(lanes) > lane_floor:
            lanes.pop(lane_floor)
            continue


        return _budget_rich_frame(text)


def _rich_outcome(steps, outcome=None):
    if outcome:
        return sanitize_rich(outcome, cap=60)


    states = [str((x or {}).get("codex_terminal_status") or "").lower()
              for x in (steps or []) if isinstance(x, dict)
              and not x.get("codex_inferred")]
    if "failed" in states:
        return "finished with issues"
    if "killed" in states or "stopped" in states:
        return "stopped"
    return "done"


def _rich_summary_sections(mission, lanes, plan_done, plan_total, plan_rows,
                           plan_omitted, step_rows, step_omitted, tool_rows,
                           tool_count, agent_done_today=0):
    del tool_rows, tool_count
    lines = ["", "**Mission**" + RICH_BR + mission, ""]
    table = _rich_lane_table(lanes)
    if table:
        lines.extend(["**Agents · %d done today**" % agent_done_today,
                      "", *table, ""])
    lines.extend(["**📋 Plan · %d/%d done**" % (plan_done, plan_total), ""])
    if plan_omitted:
        lines.append("… %d plan detail%s hidden" % (
            plan_omitted, "" if plan_omitted == 1 else "s"))
    lines.extend(plan_rows or ["- nothing logged yet"])
    lines.extend(["", "**Steps**", ""])
    if step_omitted:
        lines.append("… %d step detail%s hidden" % (
            step_omitted, "" if step_omitted == 1 else "s"))
    lines.extend(step_rows or ["- no steps logged"])
    return lines


def _assemble_rich_final(name, emoji, outcome, elapsed, done, total, mission,
                         lanes, plan_done, plan_total, plan_rows, plan_omitted,
                         step_rows, step_omitted, tool_rows, tool_count, cadence,
                         agent_done_today=0):


    del name, emoji

    lane_meta = "" if not lanes else " · %d agent%s" % (
        len(lanes), "" if len(lanes) == 1 else "s")


    if outcome == INTERRUPTED_OUTCOME:
        summary = "%s after %s · %d/%d steps%s · tap" % (
            INTERRUPTED_RECAP_MARK, elapsed, done, total, lane_meta)
    else:
        summary = "✅ %s in %s · %d/%d steps%s · tap" % (
            outcome[:1].upper() + outcome[1:], elapsed, done, total, lane_meta)
    lines = ["<details><summary>%s</summary>" % summary]


    lines.extend(_rich_summary_sections(
        mission, lanes, plan_done, plan_total, plan_rows, plan_omitted,
        step_rows, step_omitted, tool_rows, tool_count, agent_done_today))
    del cadence
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def render_rich_final(sdir, steps, start, n_agents=0, mission="", panel=None,
                      plan=None, cadence=RICH_MIN_EDIT_INTERVAL, outcome=None,
                      tools=None, agent_history=None):
    del n_agents
    rows_plan = _plan_copy(plan)
    plan_done, plan_total = _rich_plan_counts(rows_plan)
    plan_entries = [
        (_normalize_plan_status(item.get("status")),
         _rich_plan_line(item).replace("**", "").replace(" ◀", ""))
        for item in rows_plan
    ]
    useful = [x for x in (steps or []) if isinstance(x, dict)
              and x.get("k") != "subtitle"]
    done, total = _rich_step_counts(useful)
    step_entries = [
        ("completed" if item.get("done") else "pending",
         ("- [x] " if item.get("done") else "- [ ] ")
         + sanitize_rich(item.get("cap") or "work", cap=RICH_PLAN_SUBJECT_CAP))
        for item in useful
    ]
    if not plan_entries and not step_entries and not steps:
        return None, 0
    now = time.time()
    name, emoji = bot_identity(sdir)
    mission_text = _rich_headline(mission or MISSION_DEFAULT,
                                  RICH_MISSION_CAP) or MISSION_DEFAULT
    elapsed = fmt_elapsed(now - start) if start else "0:00"
    outcome_text = _rich_outcome(steps, outcome)
    interrupted = outcome_text == INTERRUPTED_OUTCOME

    lanes = _rich_lane_rows(
        steps, panel, now, fold=True, preserve_open=interrupted)
    tool_rows, tool_count = _rich_tool_rows(tools)
    agent_done_today = int(
        _normalize_agent_history(agent_history, now=now).get("total") or 0)
    plan_omitted = 0
    step_omitted = 0
    while True:
        plan_rows = [entry[1] for entry in plan_entries]
        step_rows = [entry[1] for entry in step_entries]
        frame = _assemble_rich_final(
            name, emoji, outcome_text, elapsed, done, total, mission_text, lanes,
            plan_done, plan_total, plan_rows, plan_omitted, step_rows,
            step_omitted, tool_rows, tool_count, cadence, agent_done_today)
        if _rich_frame_fits(frame):
            return frame, len(steps or [])


        trim_index = _rich_plan_trim_index(plan_entries)
        if trim_index is not None:
            plan_entries.pop(trim_index)
            plan_omitted += 1
            continue
        trim_index = _rich_plan_trim_index(step_entries)
        if trim_index is not None:
            step_entries.pop(trim_index)
            step_omitted += 1
            continue


        return (frame if _rich_wire_fits(frame) else None), len(steps or [])


def _is_rich_recap_text(text):


    if not isinstance(text, str) or "</details>" not in text:
        return False
    if "<details><summary>Run record" in text and text.startswith("✅ "):
        return True
    return text.startswith(("<details><summary>✅ ",
                            "<details><summary>" + INTERRUPTED_RECAP_MARK))


def merge_rich_recap(base, sdir, steps, start, mission="", panel=None, plan=None,
                     cadence=RICH_MIN_EDIT_INTERVAL, tools=None, outcome=None,
                     agent_history=None):
    if not _is_rich_recap_text(base) or not steps:
        return None
    end = base.rfind("</details>")
    if end < 0:
        return None
    del sdir
    now = time.time()
    agent_done_today = int(
        _normalize_agent_history(agent_history, now=now).get("total") or 0)
    outcome_text = _rich_outcome(steps, outcome)
    interrupted = outcome_text == INTERRUPTED_OUTCOME
    useful = [item for item in (steps or []) if isinstance(item, dict)
              and item.get("k") != "subtitle"]
    done, total = _rich_step_counts(useful)
    lanes = _rich_lane_rows(
        steps, panel, now, fold=True, preserve_open=interrupted)
    lane_meta = "" if not lanes else " · %d agent%s" % (
        len(lanes), "" if len(lanes) == 1 else "s")
    took = fmt_elapsed(now - start) if start else "0:00"
    local_header = ("%s after %s · %d/%d steps" % (
        INTERRUPTED_RECAP_MARK, took, done, total) + lane_meta) if interrupted else\
        ("📋 Continued after %s · %d/%d steps" % (took, done, total))
    plan_source = _plan_copy(plan)
    plan_done, plan_total = _rich_plan_counts(plan_source)
    plan_entries = [
        (_normalize_plan_status(item.get("status")),
         _rich_plan_line(item).replace("**", "").replace(" ◀", ""))
        for item in plan_source
    ]
    step_entries = [
        ("completed" if item.get("done") else "pending",
         ("- [x] " if item.get("done") else "- [ ] ")
         + sanitize_rich(item.get("cap") or "work", cap=RICH_PLAN_SUBJECT_CAP))
        for item in useful
    ]
    tool_rows, tool_count = _rich_tool_rows(tools)
    mission_text = sanitize_rich(mission or MISSION_DEFAULT, cap=220)
    plan_omitted = 0
    step_omitted = 0
    while True:
        sections = _rich_summary_sections(
            mission_text, lanes, plan_done, plan_total,
            [entry[1] for entry in plan_entries], plan_omitted,
            [entry[1] for entry in step_entries], step_omitted,
            tool_rows, tool_count, agent_done_today)
        lines = ["", local_header] + sections
        candidate = base[:end] + "\n".join(lines) + "\n" + base[end:]
        if _rich_frame_fits(candidate):
            return candidate

        trim_index = _rich_plan_trim_index(plan_entries)
        if trim_index is not None:
            plan_entries.pop(trim_index)
            plan_omitted += 1
            continue
        trim_index = _rich_plan_trim_index(step_entries)
        if trim_index is not None:
            step_entries.pop(trim_index)
            step_omitted += 1
            continue
        return None


def render_live(steps, frame, start, agents, mission="", panel=None):
    steps = steps or []
    agents = agents or {}
    elapsed = fmt_elapsed(time.time() - start) if start else "0:00"
    rows = ["%s Working · %s · %s" % (WORKING_EMOJI, activity_bar(frame), elapsed)]
    window = steps[-LIVE_STREAM:]
    shown_inflight = False
    k, n = 0, len(window)
    while k < n:
        s = window[k]
        if s.get("k") in ("subtitle", "subwork"):
            run = []
            while k < n and window[k].get("k") in ("subtitle", "subwork"):
                run.append(window[k])
                k += 1
            per, order, spawns = {}, [], []
            for s2 in run:
                if s2.get("k") == "subtitle":
                    spawns.append(s2.get("cap") or "a task")
                    continue
                aid = s2.get("aid") or "?"
                if aid not in per:
                    order.append(aid)
                per[aid] = s2
            any_inflight = any(not per[a].get("done") for a in order)
            rows.append("%s Subagents:" % ("⏳" if any_inflight else "✓"))
            for aid in order:
                s2 = per[aid]
                done2 = s2.get("done")
                if not done2:
                    shown_inflight = True
                rows.append("%s%s 🤖 %s: %s" % (
                    SUB, "✓" if done2 else "⏳", _agent_label(agents.get(aid, {})),
                    _live_step_caption(s2)))
            if not order and spawns:
                for t in spawns[:4]:
                    rows.append("%s⏳ 🤖 %s" % (SUB, short(t, LIVE_CAP)))
                    shown_inflight = True
        else:
            done1 = s.get("done")
            if not done1:
                shown_inflight = True
            rows.append("%s %s" % ("✓" if done1 else "⏳", _live_step_caption(s)))
            k += 1
    if not shown_inflight:


        oldest = next((s for s in steps if not s.get("done")), None)
        if oldest is not None:
            rows.append("⏳ " + _live_step_caption(oldest))
        elif _has_open_background_agents(panel):
            rows.append("⏳ Waiting on the agent%s" % (
                "" if sum(1 for row in panel if isinstance(row, dict)
                          and row.get("background") is True
                          and not row.get("done")) == 1 else "s"))
        else:
            rows.append(thinking_line(frame))


    head = [mission_line(mission)] if mission else []
    pan = panel_lines(panel)
    while True:
        out = "\n".join(head + rows[:1] + pan + rows[1:])
        if tg_len(out) <= TG_HARD_LIMIT or not (head or pan):
            return out
        if pan:
            pan.pop()
        else:
            head.pop()


def _assemble_final(head_lines, detail_lines, header, omitted, lead_lines=None,
                    trail_lines=None):


    body_lines = list(lead_lines or [])
    body_lines.extend(head_lines)
    if omitted:
        body_lines.append(escape_mdv2("…earlier omitted"))
    body_lines.extend(detail_lines)
    body_lines.extend(trail_lines or [])
    if not body_lines:
        body_lines = [escape_mdv2("…")]
    body = [("**>" if k == 0 else ">") + ln for k, ln in enumerate(body_lines)]
    body[-1] = body[-1] + "||"
    return header + "\n" + "\n".join(body)


def _plain_plan_entries(plan):
    rows = _plan_copy(plan)
    entries = []
    for item in rows:
        status = _normalize_plan_status(item.get("status"))
        subject = short(item.get("subject") or "task", RICH_PLAN_SUBJECT_CAP)
        if status == "completed":
            line = "- [x] " + subject
        elif status == "cancelled":
            line = "- [x] %s (cancelled)" % subject
        elif status == "in_progress":
            line = "- [ ] %s (in progress)" % subject
        else:
            line = "- [ ] " + subject
        entries.append((status, line))
    return entries


def _plain_tool_rows(tools):
    total = None
    if isinstance(tools, dict):
        total = tools.get("count")
        tools = tools.get("items")
    rows = tools if isinstance(tools, list) else []
    out = []
    for item in rows[-RICH_TOOL_LINES:]:
        if isinstance(item, dict):
            cap = item.get("cap") or item.get("line") or item.get("tool") or "tool"
            secs = item.get("elapsed") if item.get("elapsed") is not None\
                else item.get("secs") or 0
        else:
            cap, secs = item, 0
        out.append("- at %s · %s" % (
            _rich_clock(secs), short(cap, RICH_TOOL_LINE_CAP)))
    try:
        total = max(len(out), int(total)) if total is not None else len(out)
    except Exception:
        total = len(out)
    return out, max(0, total)


def _plain_summary_static(steps, n_agents, mission, panel, outcome):
    interrupted = outcome == INTERRUPTED_OUTCOME
    _, family_head, step_detail = _final_body_parts(
        steps, n_agents, mission, panel, outcome=outcome)
    lead = []
    if mission:
        lead.append(escape_mdv2(mission_line(mission)))
    agent_rows = panel_recap_lines(
        panel, preserve_open=interrupted)
    if agent_rows:
        lead.append(escape_mdv2("Agents"))
        lead.extend(escape_mdv2(line) for line in agent_rows)
    return lead, [escape_mdv2("Steps")] + family_head, step_detail


def _plain_summary_groups(static_lead, static_head, plan_entries,
                          plan_done, plan_total, plan_omitted, tool_rows,
                          tool_count):
    lead = list(static_lead)
    lead.append(escape_mdv2("📋 Plan · %d/%d done" % (plan_done, plan_total)))
    if plan_omitted:
        lead.append(escape_mdv2("… %d plan detail%s hidden" % (
            plan_omitted, "" if plan_omitted == 1 else "s")))
    lead.extend(escape_mdv2(entry[1]) for entry in plan_entries)
    if not plan_entries and not plan_omitted:
        lead.append(escape_mdv2("- nothing logged yet"))
    head = list(static_head)


    del tool_rows, tool_count
    trail = []
    return lead, head, trail


def render_final(steps, start, n_agents=0, mission="", panel=None, plan=None,
                 tools=None, outcome=None):
    if not steps:
        return None, 0
    enhanced = plan is not None or tools is not None or outcome == INTERRUPTED_OUTCOME
    if not enhanced:


        lead, head_lines, esc_detail = _final_body_parts(
            steps, n_agents, mission, panel)
        took = (" · " + fmt_elapsed(time.time() - start)) if start else ""
        header = escape_mdv2(RECAP_MARK + took + " · tap to expand")
        kept = list(esc_detail)
        omitted = False
        while True:
            msg = _assemble_final(head_lines, kept, header, omitted, lead)
            if tg_len(msg) <= TG_HARD_LIMIT or not kept:
                break
            kept.pop(0)
            omitted = True
        msg = _assemble_final(head_lines, kept, header, omitted, lead)
        if tg_len(msg) > TG_HARD_LIMIT:
            msg = _utf16_clip(msg, TG_HARD_LIMIT - 2)
            if not msg.endswith("||"):
                msg += "||"
        return msg, len(steps)

    now = time.time()
    took = fmt_elapsed(now - start) if start else "0:00"
    useful = [item for item in (steps or []) if isinstance(item, dict)
              and item.get("k") != "subtitle"]
    done, total = _rich_step_counts(useful)
    lanes = _rich_lane_rows(
        steps, panel, now, fold=True,
        preserve_open=outcome == INTERRUPTED_OUTCOME)
    if outcome == INTERRUPTED_OUTCOME:
        lane_meta = "" if not lanes else " · %d agent%s" % (
            len(lanes), "" if len(lanes) == 1 else "s")
        header_text = "%s · %s · %d/%d steps%s · tap to expand" % (
            INTERRUPTED_RECAP_MARK, took, done, total, lane_meta)
    else:
        header_text = RECAP_MARK + ((" · " + took) if start else "")\
            + " · tap to expand"
    header = escape_mdv2(header_text)
    plan_rows = _plan_copy(plan)
    plan_done, plan_total = _rich_plan_counts(plan_rows)
    plan_entries = _plain_plan_entries(plan_rows)
    tool_rows, tool_count = _plain_tool_rows(tools)
    static_lead, static_head, step_detail = _plain_summary_static(
        steps, n_agents, mission, panel, outcome)
    plan_order = _rich_plan_trim_order(plan_entries)

    def build(tool_drop=0, plan_drop=0, step_drop=0):
        kept_plan = _entries_after_trim(plan_entries, plan_order, plan_drop)
        kept_tools = tool_rows[tool_drop:]
        lead, head_lines, trail = _plain_summary_groups(
            static_lead, static_head, kept_plan, plan_done, plan_total,
            plan_drop, kept_tools, tool_count)
        return _assemble_final(
            head_lines, step_detail[step_drop:], header, bool(step_drop),
            lead, trail)

    msg = build()
    if tg_len(msg) <= TG_HARD_LIMIT:
        return msg, len(steps)


    tool_drop = 0
    plan_drop = 0
    if plan_entries:
        plan_drop, msg, fitted = _minimum_fitting_drop(
            len(plan_entries), lambda count: build(tool_drop, count, 0),
            lambda frame: tg_len(frame) <= TG_HARD_LIMIT)
        if fitted:
            return msg, len(steps)
    if step_detail:
        _, msg, fitted = _minimum_fitting_drop(
            len(step_detail),
            lambda count: build(tool_drop, plan_drop, count),
            lambda frame: tg_len(frame) <= TG_HARD_LIMIT)
        if fitted:
            return msg, len(steps)
    return None, len(steps)


def _final_body_parts(steps, n_agents=0, mission="", panel=None, outcome=None):
    interrupted = outcome == INTERRUPTED_OUTCOME
    order = _families_in_order(steps)
    parts = []
    for f in order:
        if f == "helpers":


            subtitle_count = len(_subagent_titles(
                [s for s in steps if s.get("fam") == "helpers"]))
            distinct_aids = len({s.get("aid") for s in steps
                                 if s.get("fam") == "helpers" and s.get("aid")})
            c = max(n_agents, distinct_aids, subtitle_count)
            if c == 0:
                c = _helper_run_count(steps)
            parts.append("%s %d %s" % (fam_get("helpers")[0], c, noun("helpers", c)))
        else:
            c = len([s for s in steps if s.get("fam") == f])
            parts.append("%s %d %s" % (fam_get(f)[0], c, noun(f, c)))
    summary = " · ".join(parts)

    detail, i = [], 0
    while i < len(steps):
        s = steps[i]
        fam = s.get("fam", "other")
        if fam == "helpers":
            j = i
            while j < len(steps) and steps[j].get("fam") == "helpers":
                j += 1
            run = steps[i:j]
            titles = _subagent_titles(run)
            helper_open = interrupted and any(not item.get("done") for item in run)
            detail.append(("⏳ " if helper_open else "✓ ")
                          + fam_get("helpers")[0] + " Subagents")
            for t in titles:
                detail.append(SUB + t)
            acts, seen = [], set()
            for s2 in run:
                if s2.get("k") == "subwork":
                    aid = s2.get("aid") or "?"
                    if aid not in seen:
                        seen.add(aid)
                        acts.append(s2.get("cap") or "working")
            for a in acts:
                detail.append(SUB + short(a, 200))
            if not titles and not acts:
                detail.append(SUB + "%d steps" % len(run))
            i = j
        else:
            marker = "⏳ " if interrupted and not s.get("done") else "✓ "
            detail.append(marker + fam_get(fam)[0] + " "
                          + (s.get("cap") or fam_get(fam)[2]))
            i += 1

    head_lines = [escape_mdv2(summary)] if len(detail) > 1 else []
    esc_detail = [escape_mdv2(x) for x in detail]


    lead = []
    if mission:
        lead.append(escape_mdv2(mission_line(mission)))
    for ln in panel_recap_lines(panel, preserve_open=interrupted):
        lead.append(escape_mdv2(ln))
    return lead, head_lines, esc_detail


def _is_recap_text(t):
    if not isinstance(t, str) or not t.startswith((RECAP_MARK,
                                                   INTERRUPTED_RECAP_MARK)):
        return False
    return t.rstrip().endswith("||")


def _continuation_parts(steps, start, n_agents=0, mission="", panel=None):
    lead, head_lines, esc_detail = _final_body_parts(steps, n_agents, mission, panel)
    took = (" · " + fmt_elapsed(time.time() - start)) if start else ""
    fixed = [escape_mdv2(CONT_DIVIDER), escape_mdv2(CONT_MARK + took)]
    return fixed + lead + head_lines, esc_detail


def merge_recap(base, steps, start, n_agents=0, mission="", panel=None, limit=None,
                plan=None, tools=None, outcome=None):
    if not _is_recap_text(base) or not steps:
        return None
    limit = TG_HARD_LIMIT if limit is None else limit
    trunk = base.rstrip()[:-2]
    enhanced = plan is not None or tools is not None or outcome == INTERRUPTED_OUTCOME
    if not enhanced:
        fixed, detail = _continuation_parts(
            steps, start, n_agents, mission, panel)
        kept = list(detail)
        omitted = False
        while True:
            lines = list(fixed)
            if omitted:
                lines.append(escape_mdv2("…earlier omitted"))
            lines.extend(kept)
            msg = trunk + "\n" + "\n".join(">" + ln for ln in lines) + "||"
            if tg_len(msg) <= limit:
                return msg
            if not kept:
                return None
            kept.pop(0)
            omitted = True

    now = time.time()
    took = (" · " + fmt_elapsed(now - start)) if start else ""
    interrupted = outcome == INTERRUPTED_OUTCOME
    if interrupted:
        useful = [item for item in (steps or []) if isinstance(item, dict)
                  and item.get("k") != "subtitle"]
        step_done, step_total = _rich_step_counts(useful)
        lanes = _rich_lane_rows(
            steps, panel, now, fold=True, preserve_open=True)
        lane_meta = "" if not lanes else " · %d agent%s" % (
            len(lanes), "" if len(lanes) == 1 else "s")
        local_header = "%s%s · %d/%d steps%s" % (
            INTERRUPTED_RECAP_MARK, took, step_done, step_total, lane_meta)
    else:
        local_header = CONT_MARK + took
    fixed = [escape_mdv2(CONT_DIVIDER), escape_mdv2(local_header)]
    plan_rows = _plan_copy(plan)
    plan_done, plan_total = _rich_plan_counts(plan_rows)
    plan_entries = _plain_plan_entries(plan_rows)
    tool_rows, tool_count = _plain_tool_rows(tools)
    static_lead, static_head, detail = _plain_summary_static(
        steps, n_agents, mission, panel, outcome)
    plan_order = _rich_plan_trim_order(plan_entries)

    def build(tool_drop=0, plan_drop=0, step_drop=0):
        kept_plan = _entries_after_trim(plan_entries, plan_order, plan_drop)
        kept_tools = tool_rows[tool_drop:]
        lead, head_lines, trail = _plain_summary_groups(
            static_lead, static_head, kept_plan, plan_done, plan_total,
            plan_drop, kept_tools, tool_count)
        lines = list(fixed) + lead + head_lines
        if step_drop:
            lines.append(escape_mdv2("…earlier omitted"))
        lines.extend(detail[step_drop:])
        lines.extend(trail)
        return trunk + "\n" + "\n".join(">" + line for line in lines) + "||"

    msg = build()
    if tg_len(msg) <= limit:
        return msg

    tool_drop = 0
    plan_drop = 0
    if plan_entries:
        plan_drop, msg, fitted = _minimum_fitting_drop(
            len(plan_entries), lambda count: build(tool_drop, count, 0),
            lambda frame: tg_len(frame) <= limit)
        if fitted:
            return msg
    if detail:
        _, msg, fitted = _minimum_fitting_drop(
            len(detail), lambda count: build(tool_drop, plan_drop, count),
            lambda frame: tg_len(frame) <= limit)
        if fitted:
            return msg
    return None


def _noreply_merge(st, steps, mission="", outcome=None):
    pm, merged, _ = _noreply_merge_ex(st, steps, mission, outcome=outcome)
    return pm, merged


def _noreply_merge_ex(st, steps, mission="", outcome=None):
    raw_mid = st.get("prev_recap_msgid")
    is_rich = _pointer_is_rich(st, raw_mid)
    pm, pt = _prev_pair(raw_mid, st.get("prev_recap_text"),
                        rich=is_rich)
    if not pm or str(pm) == str(st.get("msgid")):
        return None, None, False
    enhanced = _rich_state_present(st)
    plan = st.get("rich_plan") if enhanced else None
    tools = _rich_tools_payload(st) if enhanced else None
    if is_rich:


        merged = merge_rich_recap(
            pt, "", steps, st.get("proc_start"), mission, st.get("panel"),
            plan, _effective_min_edit("", st), tools=tools, outcome=outcome,
            agent_history=st.get("agent_history"))
    else:
        merged = merge_recap(pt, steps, st.get("proc_start"),
                             len(st.get("agents") or {}), mission, st.get("panel"),
                             plan=plan, tools=tools, outcome=outcome)
    return (pm, merged, is_rich) if merged else (None, None, False)


class Telegram:
    def __init__(self, token, chat_id, dry, sdir=None):
        self.base = "https://api.telegram.org/bot%s/" % token
        self.chat = str(chat_id)
        self.dry = dry


        self.sdir = sdir


        try:
            if sdir:
                _SDIR_CHAT[sdir] = str(chat_id)
        except Exception:
            pass
        self._event_started = time.monotonic()

    def reset_event_budget(self):
        self._event_started = time.monotonic()

    def cooldown_remaining(self):
        try:
            return cooldown_remaining(self.sdir, self.chat)
        except Exception:
            return 0.0

    def _call(self, method, params, _retry=True, _meta=None):
        if self.dry:
            shown = params
            if isinstance(params, dict) and "rich_message" in params:
                shown = dict(params)
                try:
                    content = json.loads(shown.get("rich_message") or "{}")
                    size = len(str(content.get("markdown") or ""))
                except Exception:
                    size = 0
                shown["rich_message"] = "<rich frame %d chars>" % size
            sys.stderr.write("[DRY] %s %s\n" % (method, json.dumps(shown)[:700]))
            return {"ok": True, "result": {"message_id": 4242}}


        remaining = self.cooldown_remaining()
        if remaining > 0:
            if _meta is not None:
                _meta["error_class"] = "rate_limit"
                _meta["error_description"] =\
                    "cooldown active: %d s remaining" % int(remaining)
            return None


        try:
            from urllib import request, parse
            from urllib.error import HTTPError, URLError
        except Exception:
            if _meta is not None:
                _meta["error_class"] = "network"
            return None
        try:
            body = parse.urlencode(params).encode()
            req = request.Request(self.base + method, data=body)


            rate_record(self.sdir, self.chat)
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            info = {}


            if e.code == 429 or _meta is not None:
                try:
                    info = json.loads(e.read().decode())
                except Exception:
                    info = {}
            if _meta is not None:
                description = _bounded_api_description(
                    info.get("description") if isinstance(info, dict) else "")
                if description:
                    _meta["error_description"] = description
            if e.code == 429:


                if _meta is not None and not _meta.get("_rate_limit_event"):
                    _meta["_rate_limit_event"] = True
                    _meta["rate_limit_count"] = 1


                try:
                    ra = max(0, int(info.get("parameters", {}).get("retry_after", 1)))
                except Exception:
                    ra = 1
                cooldown_arm(self.sdir, self.chat, ra)
                if not _retry:
                    if _meta is not None:
                        _meta["error_class"] = "rate_limit"
                    return None
                if self.cooldown_remaining() > 0:


                    if _meta is not None:
                        _meta["error_class"] = "rate_limit"
                    return None
                if _meta is None:

                    time.sleep(min(ra, 2))
                    return self._call(method, params, _retry=False, _meta=_meta)


                time.sleep(min(ra, 2))
                return self._call(method, params, _retry=False, _meta=_meta)
            if _meta is not None:
                _meta["error_class"] = "api_%d" % e.code
            return None
        except Exception as exc:
            if _meta is not None:
                reason = exc.reason if isinstance(exc, URLError) else exc
                if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                    _meta["error_class"] = "timeout"
                elif isinstance(reason, ConnectionResetError) or\
                        "connection reset" in str(reason).lower():
                    _meta["error_class"] = "connection_reset"
                else:
                    _meta["error_class"] = "network"
            return None

    def send(self, text, markdown=False):
        p = {"chat_id": self.chat, "text": text, "disable_notification": "true"}
        if markdown:
            p["parse_mode"] = "MarkdownV2"
        r = self._call("sendMessage", p)
        try:
            if r and r.get("ok"):
                return str(r["result"]["message_id"])
        except Exception:
            pass
        return None

    def send_once(self, text, markdown=False):
        p = {"chat_id": self.chat, "text": text, "disable_notification": "true"}
        if markdown:
            p["parse_mode"] = "MarkdownV2"
        r = self._call("sendMessage", p, _retry=False)
        try:
            if r and r.get("ok"):
                return str(r["result"]["message_id"])
        except Exception:
            pass
        return None

    def edit(self, msgid, text, markdown=False):
        p = {"chat_id": self.chat, "message_id": str(msgid), "text": text}
        if markdown:
            p["parse_mode"] = "MarkdownV2"
        r = self._call("editMessageText", p)
        return bool(r and r.get("ok"))

    def _rich_result(self, response, meta, want_message=False):
        if isinstance(response, dict) and response.get("ok"):
            result = response.get("result")
            if want_message:
                try:
                    mid = result.get("message_id") if isinstance(result, dict) else None
                    if mid is None:
                        mid = response.get("message_id")
                    if mid is not None:
                        return {"ok": True, "message_id": str(mid), "error_class": ""}
                except Exception:
                    pass
                return {"ok": False, "error_class": "malformed"}
            return {"ok": True, "error_class": ""}
        error_class = (meta or {}).get("error_class")
        if not error_class and isinstance(response, dict):
            code = response.get("error_code")
            error_class = "rate_limit" if code == 429 else ("api_%s" % code if code else None)
        failed = {"ok": False, "error_class": error_class or "malformed"}
        description = (meta or {}).get("error_description")
        if not description and isinstance(response, dict):
            description = response.get("description") or response.get("error_description")
        description = _bounded_api_description(description)
        if description:
            failed["error_description"] = description
        if (meta or {}).get("rate_limit_count"):
            failed["rate_limit_count"] = int(meta["rate_limit_count"])
        return failed

    def _rich_transport(self, method, base_params, frame, want_message=False):
        prepared = _budget_rich_frame(frame)
        if prepared is None:
            return {"ok": False, "error_class": "content_budget",
                    "error_description": "rich frame cannot fit fixed budget"}


        retry_frame = _budget_rich_frame(
            prepared, frame_limit=RICH_RETRY_FRAME_LIMIT, force_trim=True)
        if retry_frame is None:
            return {"ok": False, "error_class": "content_budget",
                    "error_description": "rich frame has no retry-safe give-way"}

        def invoke(markdown, allow_rate_retry=True):
            content = {"markdown": markdown, "skip_entity_detection": True}
            if set(content) != {"markdown", "skip_entity_detection"}:
                return {"ok": False, "error_class": "content_key"}
            params = dict(base_params)
            params["rich_message"] = json.dumps(
                content, ensure_ascii=False, separators=(",", ":"))
            meta = {}
            response = self._call(
                method, params, _retry=allow_rate_retry, _meta=meta)
            result = self._rich_result(response, meta, want_message=want_message)
            result["frame"] = markdown
            return result

        first = invoke(prepared)


        if method == "editMessageText"\
                and first.get("error_class") == "api_400"\
                and "message is not modified" in str(
                    first.get("error_description") or "").lower():
            first.update({"ok": True, "error_class": "", "frame": prepared,
                          "idempotent": True})
            return first
        if first.get("ok") or first.get("error_class") != "api_400":
            return first


        retry = invoke(retry_frame, allow_rate_retry=False)
        retry["api_400_retried"] = True
        first_description = first.get("error_description")
        if first_description:
            retry["first_error_description"] = first_description
        if method == "editMessageText"\
                and retry.get("error_class") == "api_400"\
                and "message is not modified" in str(
                    retry.get("error_description") or "").lower():
            retry.update({"ok": True, "error_class": "", "frame": retry_frame,
                          "idempotent": True})
        return retry

    def send_rich(self, frame):
        p = {"chat_id": self.chat, "disable_notification": "true"}
        return self._rich_transport(
            "sendRichMessage", p, frame, want_message=True)

    def edit_rich(self, msgid, frame):
        p = {"chat_id": self.chat, "message_id": str(msgid)}
        return self._rich_transport(
            "editMessageText", p, frame, want_message=False)

    def delete(self, msgid):
        r = self._call("deleteMessage", {"chat_id": self.chat, "message_id": str(msgid)})
        return bool(r and r.get("ok"))

    def typing(self):
        self._call("sendChatAction", {"chat_id": self.chat, "action": "typing"})

    def typing_once(self):
        self._call("sendChatAction",
                   {"chat_id": self.chat, "action": "typing"}, _retry=False)


def _normalize_rich_result(result):
    if result is True:
        return {"ok": True, "error_class": ""}
    if not isinstance(result, dict):
        return {"ok": False, "error_class": "malformed"}
    if result.get("ok"):
        return result
    error_class = result.get("error_class")
    if not error_class:
        kind = str(result.get("kind") or "").lower()
        if kind in ("rate", "rate_limit", "429"):
            error_class = "rate_limit"
        elif kind == "transient":
            code = str(result.get("error_code") or "").lower()
            error_class = code if code in ("timeout", "connection_reset", "network") else "network"
        elif kind == "terminal":
            code = result.get("error_code")
            error_class = "api_%s" % code if code else "api_rejection"
    if not error_class and result.get("error_code") == 429:
        error_class = "rate_limit"
    failed = {"ok": False, "error_class": str(error_class or "malformed")}
    description = result.get("error_description") or result.get("description")
    description = _bounded_api_description(description)
    if description:
        failed["error_description"] = description
    for key in ("frame", "api_400_retried", "first_error_description"):
        if key in result:
            failed[key] = result[key]
    try:
        if result.get("rate_limit_count"):
            failed["rate_limit_count"] = max(1, int(result["rate_limit_count"]))
    except Exception:
        pass
    return failed


def _apply_rich_result(st, result, owner_id=None, sdir=None):
    result = _normalize_rich_result(result)
    if result.get("ok"):
        st["rich_429_streak"] = 0
        st["rich_transient_streak"] = 0
        st["rich_hard_streak"] = 0
        st["rich_slow"] = False
        st["rich_pending_open"] = False
        st["rich_failed"] = False
        st["rich_failed_msgid"] = None
        st.pop("rich_error_description", None)
        return "ok", ""
    error_class = result.get("error_class") or "malformed"
    description = _bounded_api_description(result.get("error_description"))
    st.pop("rich_error_description", None)
    if description:
        st["rich_error_description"] = description
    if sdir:
        _append_rich_health(sdir, st, error_class)
    if error_class == "rate_limit":
        st["rich_transient_streak"] = 0
        st["rich_hard_streak"] = 0
        st["rich_429_streak"] = min(
            999, int(_history_number(st.get("rich_429_streak"))) + 1)
        if st["rich_429_streak"] >= RICH_RATE_LIMIT:
            st["rich_slow"] = True
        return "skip", error_class
    elif error_class in ("timeout", "connection_reset", "network"):
        st["rich_429_streak"] = 0
        st["rich_hard_streak"] = 0
        st["rich_transient_streak"] = min(
            999, int(_history_number(st.get("rich_transient_streak"))) + 1)
        if st["rich_transient_streak"] >= RICH_TRANSIENT_LIMIT:
            st["rich_slow"] = True
        return "skip", error_class
    elif error_class == "content_budget":
        st["rich_429_streak"] = 0
        st["rich_transient_streak"] = 0
        st["rich_hard_streak"] = min(
            RICH_HARD_LIMIT,
            int(_history_number(st.get("rich_hard_streak"))) + 1)
        if st["rich_hard_streak"] < RICH_HARD_LIMIT:
            return "skip", error_class
    elif error_class == "api_400" and result.get("api_400_retried") is True:
        st["rich_429_streak"] = 0
        st["rich_transient_streak"] = 0
        st["rich_hard_streak"] = RICH_HARD_LIMIT
    else:


        st["rich_429_streak"] = 0
        st["rich_hard_streak"] = 0
        st["rich_transient_streak"] = min(
            999, int(_history_number(st.get("rich_transient_streak"))) + 1)
        if st["rich_transient_streak"] >= RICH_TRANSIENT_LIMIT:
            st["rich_slow"] = True
        return "skip", error_class
    st["rich_failed"] = True


    st["rich_failed_msgid"] = str(owner_id) if owner_id is not None else None
    st["rich_pending_open"] = False
    return "fallback", error_class


def _rich_result_for_target(st, target, result, sdir=None):
    if isinstance(st, dict) and target is not None\
            and str(st.get("msgid")) == str(target):
        return _apply_rich_result(st, result, owner_id=target, sdir=sdir)
    normalized = _normalize_rich_result(result)
    if normalized.get("ok"):
        return "ok", ""
    error_class = normalized.get("error_class") or "malformed"
    if sdir:
        st.pop("rich_error_description", None)
        description = _bounded_api_description(normalized.get("error_description"))
        if description:
            st["rich_error_description"] = description
        _append_rich_health(sdir, st, error_class)
    return "skip", error_class


def _append_rich_health(sdir, st, error_class):
    if not isinstance(st, dict) or not st.get("rich_turn") or not rich_enabled(sdir):
        return
    seen = st.get("rich_health_classes")
    seen = list(seen) if isinstance(seen, list) else []
    safe = "".join(ch for ch in str(error_class or "unknown").lower()
                   if ch.isalnum() or ch in ("_", "-"))[:40] or "unknown"
    if safe in seen:
        return
    description = _health_api_description(st.get("rich_error_description"))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        path = os.path.join(sdir, RICH_HEALTH_LOG)
        try:
            with open(path, "r") as f:
                prior = f.read().splitlines()[-(RICH_HEALTH_LINES - 1):]
        except OSError:
            prior = []
        with open(path, "w") as f:
            if prior:
                f.write("\n".join(prior) + "\n")
            suffix = " " + description if description else ""
            f.write("%s %s%s\n" % (stamp, safe, suffix))
        st["rich_health_logged"] = True
        st["rich_health_classes"] = (seen + [safe])[-12:]
    except Exception:
        pass


def _plain_fallback_live(sdir, st):
    return render_rich_live(
        sdir, st.get("steps") or [], st.get("proc_start"),
        st.get("agents") or {}, current_mission(sdir, st), st.get("panel"),
        st.get("rich_plan"), _rich_tools_payload(st),
        _effective_min_edit(sdir, st), fallback=True, frame=st.get("frame", 0),
        state=st)


def _prepare_plain_fallback_state(sdir, st, error_class):


    if not st.get("rich_turn") or not rich_enabled(sdir):
        return None
    _append_rich_health(sdir, st, error_class)
    old_mid = st.get("msgid")
    if old_mid and st.get("rich_box"):
        orphans = st.get("rich_orphans")
        orphans = list(orphans) if isinstance(orphans, list) else []
        if str(old_mid) not in [str(x) for x in orphans]:
            orphans.append(old_mid)
        st["rich_orphans"] = orphans[-6:]
    st["rich_failed"] = True
    st["rich_box"] = False
    st["msgid"] = None
    return _plain_fallback_live(sdir, st)


def _cold_start_plain_state(sdir, tg, st, error_class):
    plain = _prepare_plain_fallback_state(sdir, st, error_class)
    if plain is None:
        return None
    try:
        sender = getattr(tg, "send_once", None)
        mid = sender(plain, markdown=False) if callable(sender)\
            else tg.send(plain, markdown=False)
    except Exception:
        mid = None
    if mid:
        st["msgid"] = mid
        st["last_live"] = plain
        st["last_edit"] = time.time()
        st["box_is_last"] = True
    return mid


def _try_rich_send_state(sdir, tg, st, frame):
    try:
        result = tg.send_rich(frame)
    except Exception as exc:
        result = {"ok": False,
                  "error_class": "timeout" if isinstance(exc, TimeoutError)
                  else "connection_reset" if isinstance(exc, ConnectionResetError)
                  else "network"}
    action, error_class = _apply_rich_result(st, result, sdir=sdir)
    if action == "ok":
        normalized = _normalize_rich_result(result)
        mid = normalized.get("message_id")
        st["last_live"] = normalized.get("frame") or frame
        return (str(mid), "rich") if mid is not None else (None, "skip")
    if action == "fallback":
        mid = _cold_start_plain_state(sdir, tg, st, error_class)
        return (mid, "plain") if mid is not None else (None, "skip")
    st["rich_pending_open"] = True
    return None, "skip"


def _rich_deferred_valid(st, mid, live_intent, generation, targets):
    if not isinstance(st, dict) or not _rich_op_current(st, generation, targets):
        return False
    if not live_intent:
        return True
    return bool(
        str(st.get("msgid")) == str(mid) and _pointer_is_rich(st, mid)
        and not st.get("folded") and not st.get("rich_failed"))


def _rich_deferred_edit(sdir, tg, mid, frame, live_fallback=True, intent="live",
                        generation=None, targets=None):
    live_intent = intent == "live"


    blf = None
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        if not _rich_deferred_valid(
                load_state(sdir), mid, live_intent, generation, targets):
            return False
    except Exception:
        return False
    finally:
        if blf is not None:
            try:
                fcntl.flock(blf, fcntl.LOCK_UN)
                blf.close()
            except Exception:
                pass

    try:
        result = tg.edit_rich(mid, frame)
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            result = {"ok": False, "error_class": "timeout"}
        elif isinstance(exc, ConnectionResetError):
            result = {"ok": False, "error_class": "connection_reset"}
        else:
            result = {"ok": False, "error_class": "network"}

    ok = False
    fallback_frame = None
    fallback_identity = None
    blf = None
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        st = load_state(sdir)
        if not _rich_deferred_valid(st, mid, live_intent, generation, targets):
            return False
        if not live_intent:


            normalized = _normalize_rich_result(result)
            ok = bool(normalized.get("ok"))
            if ok:


                used_frame = normalized.get("frame") or frame
                if str(st.get("prev_recap_msgid")) == str(mid):
                    st["prev_recap_text"] = used_frame
                    st["prev_recap_rich"] = True


                if str(st.get("msgid")) == str(mid):
                    st["last_live"] = used_frame
                save_state(sdir, st)
            else:
                reason = normalized.get("error_class") or "malformed"
                description = _bounded_api_description(
                    normalized.get("error_description"))
                if description:
                    st["rich_error_description"] = description
                _append_rich_health(sdir, st, reason)
                if intent != "recap":
                    _stash_ghost_state(st, mid)
                save_state(sdir, st)
        else:
            st["rich_box"] = True
            action, error_class = _apply_rich_result(
                st, result, owner_id=mid, sdir=sdir)
            if action == "ok":
                ok = True
                st["last_live"] = _normalize_rich_result(result).get("frame") or frame
            elif action == "fallback":
                _append_rich_health(sdir, st, error_class)
                if live_fallback and not st.get("folded") and st.get("rich_turn")\
                        and rich_enabled(sdir):
                    fallback_frame = _prepare_plain_fallback_state(
                        sdir, st, error_class)
                    if fallback_frame is not None:
                        fallback_identity = (
                            st.get("proc_start"),
                            str(st.get("rich_session_id") or ""),
                            str(mid))
            save_state(sdir, st)
    except Exception:
        return False
    finally:
        if blf is not None:
            try:
                fcntl.flock(blf, fcntl.LOCK_UN)
                blf.close()
            except Exception:
                pass


    candidate_mid = None
    if fallback_frame is not None and rich_enabled(sdir):
        try:
            sender = getattr(tg, "send_once", None)
            candidate_mid = sender(fallback_frame, markdown=False)\
                if callable(sender) else tg.send(fallback_frame, markdown=False)
        except Exception:
            candidate_mid = None
    if candidate_mid is not None:


        try:
            box_send_record(sdir, (fallback_identity or (None,))[0])
        except Exception:
            pass
        blf = None
        try:
            blf = open(os.path.join(sdir, "lock"), "w")
            fcntl.flock(blf, fcntl.LOCK_EX)
            st = load_state(sdir)
            same_turn = bool(
                fallback_identity
                and st.get("proc_start") == fallback_identity[0]
                and str(st.get("rich_session_id") or "") == fallback_identity[1])
            select_candidate = bool(
                same_turn and _rich_op_current(st, generation, targets)
                and st.get("rich_turn") and rich_enabled(sdir)
                and not st.get("folded") and st.get("msgid") is None
                and st.get("rich_failed")
                and str(st.get("rich_failed_msgid")) == fallback_identity[2])
            if select_candidate:
                st["msgid"] = str(candidate_mid)
                st["rich_box"] = False
                st["last_live"] = fallback_frame
                st["last_edit"] = time.time()
                st["box_is_last"] = True
            else:
                _stash_ghost_state(st, candidate_mid)
            save_state(sdir, st)
        finally:
            if blf is not None:
                try:
                    fcntl.flock(blf, fcntl.LOCK_UN)
                    blf.close()
                except Exception:
                    pass
    return ok


def _animator_lock_held(sdir):
    probe = None
    try:
        probe = open(os.path.join(sdir, "anim.lock"), "w")
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:


        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass
            return True
        return False

    try:
        fcntl.flock(probe, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        probe.close()
    except Exception:
        pass
    return False


def _animator_will_repaint(sdir, st, last_edit, now, alive=None):
    try:
        if alive is None:
            alive = _animator_lock_held(sdir)
        if not alive:
            return False
        window = ANIM_HANDOFF_TICKS * _effective_anim_interval(sdir, st)
        return (now - (last_edit or 0)) <= window
    except Exception:
        return False


def spawn_animator(sdir=None):
    if os.environ.get("STREAM_NO_SPAWN") == "1":
        return


    if sdir and _animator_lock_held(sdir):
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "animate"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _noreply_cleanup_inline(sdir, tg, st, steps, mission):
    msgid = st.get("msgid")
    target, merged, target_rich = _noreply_merge_ex(st, steps, mission)
    ok = False
    if merged:
        try:
            if target_rich:
                _invalidate_rich_targets(st, target)
                result = tg.edit_rich(target, merged)


                normalized = _normalize_rich_result(result)
                ok = bool(normalized.get("ok"))
                if ok:
                    merged = normalized.get("frame") or merged
                else:
                    st.pop("rich_error_description", None)
                    description = _bounded_api_description(
                        normalized.get("error_description"))
                    if description:
                        st["rich_error_description"] = description
                    _append_rich_health(
                        sdir, st,
                        normalized.get("error_class") or "malformed")
            elif not target_rich:
                ok = tg.edit(target, merged, markdown=True)
        except Exception:
            ok = False
            if target_rich:
                st.pop("rich_error_description", None)
                _append_rich_health(sdir, st, "network")
        if not ok and target_rich:
            return None, False
    st["noreply_grace_until"] = None
    st["folded"] = True
    st["msgid"] = None
    if st.get("rich_box"):
        st["rich_box"] = False
    st["box_is_last"] = False
    if ok:
        st["prev_recap_text"] = merged
        if target_rich:
            st["prev_recap_rich"] = True
        st["last_live"] = merged
        st["steps"] = []
        _unghost(st, target)
    return msgid, True


def _noreply_grace_sweep(sdir, tg):
    trailing, fired, track_trailing = None, False, False
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
    except Exception:
        return False
    try:
        st = load_state(sdir)
        steps = (st or {}).get("steps")
        steps = steps if isinstance(steps, list) else []
        panel = (st or {}).get("panel")
        panel = panel if isinstance(panel, list) else []
        history_sink = st if _agent_history_active(st, sdir) else None
        reconciled = _close_gone_codex_bg(steps, history_state=history_sink)
        agent_expired = _expire_background_agents(panel, history_state=history_sink)
        reconciled += _close_gone_bash_bg(steps)
        agent_expired += _expire_bash_bg(steps)
        if st and (_has_open_codex_bg(steps)
                   or _has_open_background_agents(panel)
                   or _has_open_bash_bg(steps)):
            if reconciled or agent_expired:
                st["steps"] = steps
                st["panel"] = panel
                save_state(sdir, st)
        elif st and _grace_due(st, steps):
            track_trailing = _rich_state_present(st)
            trailing, fired = _noreply_cleanup_inline(
                sdir, tg, st, steps, current_mission(sdir, st))
            save_state(sdir, st)
        elif st and (reconciled or agent_expired):
            st["steps"] = steps
            st["panel"] = panel
            save_state(sdir, st)
    finally:
        try:
            fcntl.flock(blf, fcntl.LOCK_UN)
            blf.close()
        except Exception:
            pass
    if trailing:
        try:
            deleted = tg.delete(trailing)
        except Exception:
            deleted = False
        if not deleted and track_trailing:
            _stash_ghost(sdir, trailing)
    return fired


def _fold_to_done(sdir, tg):
    trailing = None
    rich_state = False
    completed = True
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
    except Exception:
        return True
    try:
        st = load_state(sdir)
        rich_state = _rich_state_present(st)
        if not st or st.get("folded"):
            return True
        msgid = st.get("msgid")
        if not msgid:
            st["folded"] = True
            save_state(sdir, st)
            return True
        steps = st.get("steps")
        steps = steps if isinstance(steps, list) else []
        history_sink = st if _agent_history_active(st, sdir) else None
        _close_gone_codex_bg(steps, history_state=history_sink)
        _expire_codex_bg(steps, history_state=history_sink)
        _close_gone_bash_bg(steps)
        _expire_bash_bg(steps)
        panel = st.get("panel")
        panel = panel if isinstance(panel, list) else []
        _expire_background_agents(panel, history_state=history_sink)
        st["steps"] = steps
        st["panel"] = panel


        if _has_open_background_agents(panel) or _has_open_bash_bg(steps):
            save_state(sdir, st)
            return False
        if _noreply_below(st, steps):


            trailing, completed = _noreply_cleanup_inline(
                sdir, tg, st, steps, current_mission(sdir, st))
            save_state(sdir, st)
        else:
            above = st.get("folded_above_msgid")
            target, drop = msgid, None
            if above and above != msgid and st.get("box_is_last"):
                target, drop = above, msgid
            target_rich = _pointer_is_rich(st, target)
            above_rich = _pointer_is_rich(st, above)
            text, _ = _render_final_state(
                sdir, st, st.get("steps") or [], current_mission(sdir, st),
                rich=target_rich)
            ok = False
            action = ""
            rich_retry = False
            if text:
                try:
                    if target_rich:
                        _invalidate_rich_targets(st, target)
                    if target_rich and not _rich_failed_owner(st, target):
                        rich_result = tg.edit_rich(target, text)
                        action, error_class = _rich_result_for_target(
                            st, target, rich_result, sdir=sdir)
                        ok = action == "ok"
                        if ok:
                            text = _normalize_rich_result(rich_result).get("frame") or text
                        rich_retry = action == "skip"
                        if action == "fallback":
                            _append_rich_health(sdir, st, error_class)
                    elif not target_rich:
                        ok = tg.edit(target, text, markdown=True)
                except Exception:
                    ok = False
                    rich_retry = bool(target_rich and
                                      not _rich_failed_owner(st, target))
                    if rich_retry:
                        st.pop("rich_error_description", None)
                        _append_rich_health(sdir, st, "network")
            if ok:
                st["folded"] = True
                st["last_live"] = text
                if drop:


                    st["msgid"] = target
                    if rich_state:
                        st["rich_box"] = target_rich
                    st["folded_above_msgid"] = None
                    if rich_state:
                        st["folded_above_rich"] = False
                    st["box_is_last"] = False


                    st["replied_this_turn"] = False
                    _unghost(st, target)
                    trailing = drop
                save_state(sdir, st)
            elif target_rich and _rich_failed_owner(st, target):


                st["folded"] = True
                st["rich_failed_msgid"] = str(target)
                st["box_is_last"] = False
                if above and str(above) != str(target):


                    st["msgid"] = above
                    if rich_state:
                        st["rich_box"] = above_rich
                    _unghost(st, above)
                else:
                    st["msgid"] = None
                    if rich_state:
                        st["rich_box"] = False
                st["folded_above_msgid"] = None
                if rich_state:
                    st["folded_above_rich"] = False
                trailing = target
                save_state(sdir, st)
            elif drop and target_rich and st.get("rich_failed"):


                st["folded"] = True
                st["msgid"] = target
                if rich_state:
                    st["rich_box"] = True
                    st["folded_above_rich"] = False
                st["folded_above_msgid"] = None
                st["box_is_last"] = False
                st["replied_this_turn"] = False
                trailing = drop
                save_state(sdir, st)
            elif target_rich:


                completed = not rich_retry
                save_state(sdir, st)
    finally:
        try:
            fcntl.flock(blf, fcntl.LOCK_UN)
            blf.close()
        except Exception:
            pass
    if trailing:
        try:
            deleted = tg.delete(trailing)
        except Exception:
            deleted = False
        if not deleted and rich_state:
            _stash_ghost(sdir, trailing)
    return completed


def run_animator(sdir, tg):
    off_path = os.path.join(os.path.dirname(sdir), "stream.off")


    try:
        alf = open(os.path.join(sdir, "anim.lock"), "w")
    except Exception:
        return
    try:
        fcntl.flock(alf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:


        try:
            alf.close()
        except Exception:
            pass
        return
    started = time.time()
    killed = False


    exited_cleanly = False
    grace_wait = None
    last_typing = 0.0
    last_sweep = 0.0
    try:
        while True:
            if os.path.exists(off_path):
                killed = True
                break
            st = load_state(sdir)


            try:
                tg.reset_event_budget()
            except Exception:
                pass
            tick_interval = _effective_anim_interval(sdir, st)
            if not st or st.get("folded"):


                _steps = (st or {}).get("steps") if st else None
                _steps = _steps if isinstance(_steps, list) else []
                if st and _grace_armed(st, _steps):
                    now = time.time()
                    if now >= _grace_deadline(st):
                        if _noreply_grace_sweep(sdir, tg):
                            break
                        time.sleep(tick_interval)
                        continue
                    if grace_wait is None:
                        grace_wait = now


                    if (now - grace_wait) <= (NOREPLY_GRACE + 2 * tick_interval):
                        time.sleep(tick_interval)
                        continue
                break
            grace_wait = None
            msgid = st.get("msgid")
            if not msgid:
                break
            now = time.time()
            lane_steps = st.get("steps")
            lane_steps = lane_steps if isinstance(lane_steps, list) else []
            lane_panel = st.get("panel")
            lane_panel = lane_panel if isinstance(lane_panel, list) else []
            codex_open = _has_open_codex_bg(lane_steps, now)
            codex_due = any(_codex_bg_due(s, now) for s in lane_steps)
            codex_expiry_ends = _codex_expiry_finishes(lane_steps, now)
            agent_open = _has_open_background_agents(lane_panel, now)
            agent_due = any(_background_agent_due(row, now)
                            for row in lane_panel if isinstance(row, dict))


            bash_open = _has_open_bash_bg(lane_steps, now)
            bash_due = any(_bash_bg_due(s, now) for s in lane_steps
                           if isinstance(s, dict))
            background_open = codex_open or agent_open or bash_open
            background_due = codex_due or agent_due or bash_due


            tool_open = _has_open_tool_step(lane_steps, now)
            la = st.get("last_activity", 0)
            lr = st.get("last_reply", 0)
            lrx = st.get("last_react", 0)
            last_touch = max(la, lr, lrx)


            if not (background_open or background_due) and\
                    (now - started) > ANIM_MAX_RUN:
                if _fold_to_done(sdir, tg):
                    break
                time.sleep(tick_interval)
                continue
            if not (background_open or background_due or tool_open) and\
                    last_touch and (now - last_touch) > ANIM_MAX_IDLE:
                if _fold_to_done(sdir, tg):
                    break
                time.sleep(tick_interval)
                continue


            if not st.get("typing_done") and not (
                    codex_expiry_ends and not agent_open and not agent_due) and\
                    (now - last_typing) >= max(
                        _cadence_tier(st, now)["typing"],
                        rate_pressure(sdir, _chat_for_sdir(sdir), now)):
                try:
                    tg.typing()
                    last_typing = now
                except Exception:
                    pass


            do_edit = None
            reanchor_deferred = []
            lane_finished = False
            rendered = False
            rich_attempted = False
            blf = None
            try:
                blf = open(os.path.join(sdir, "lock"), "w")
                fcntl.flock(blf, fcntl.LOCK_EX)
                st2 = load_state(sdir)
                rich_state2 = _rich_state_present(st2)


                ctx_refreshed = _refresh_pending_context(sdir, st2)\
                    if rich_state2 else False
                steps2 = st2.get("steps")
                steps2 = steps2 if isinstance(steps2, list) else []
                panel2 = st2.get("panel")
                panel2 = panel2 if isinstance(panel2, list) else []
                has_codex_bg = any(s.get("codex_bg") and not s.get("done")
                                   for s in steps2)
                has_agent_bg = any(isinstance(row, dict)
                                   and row.get("background") is True
                                   and not row.get("done") for row in panel2)
                has_bash_bg = any(isinstance(s, dict) and s.get("bash_bg")
                                  and not s.get("done") for s in steps2)
                expire_now = time.time()\
                    if has_codex_bg or has_agent_bg or has_bash_bg else None
                history_sink = st2 if _agent_history_active(st2, sdir) else None


                swept = 0
                backfilled = 0
                if (has_agent_bg or has_bash_bg)\
                        and (time.time() - last_sweep) >= BG_NOTIFY_SWEEP_MIN:
                    first_sweep = last_sweep == 0.0
                    last_sweep = time.time()


                    if has_bash_bg and _bash_lanes_missing_out(steps2):


                        backfilled = 1
                        _bash_backfill_task_out(
                            steps2, _bg_notify_transcript_path(st2),
                            tail_bytes=(BG_NOTIFY_DEEP_TAIL_BYTES
                                        if first_sweep else None))
                    swept = _sweep_background_notifications(
                        sdir, st2, expire_now,
                        tail_bytes=(BG_NOTIFY_DEEP_TAIL_BYTES if first_sweep else None))
                reconciled = _close_gone_codex_bg(
                    steps2, expire_now, history_state=history_sink)\
                    if has_codex_bg else 0
                expired = _expire_codex_bg(
                    steps2, expire_now, history_state=history_sink) if has_codex_bg else 0
                agent_expired = _expire_background_agents(
                    panel2, expire_now, history_state=history_sink)\
                    if has_agent_bg else 0


                bash_closed = _close_gone_bash_bg(steps2, expire_now)\
                    if has_bash_bg else 0
                bash_expired = _expire_bash_bg(steps2, expire_now) if has_bash_bg else 0
                if reconciled or expired or agent_expired or swept\
                        or bash_closed or bash_expired or backfilled:
                    st2["steps"] = steps2
                    st2["panel"] = panel2
                    lane_finished = (
                        not _has_open_codex_bg(steps2, expire_now)
                        and not _has_open_background_agents(panel2, expire_now)
                        and not _has_open_bash_bg(steps2, expire_now)
                        and all_steps_done(steps2))
                    if lane_finished:
                        st2["typing_done"] = True
                reanchored = False


                if not lane_finished and st2.get("reanchor") and\
                        not st2.get("box_is_last") and st2.get("msgid") and\
                        not st2.get("folded") and\
                        box_send_allowed(sdir, st2.get("proc_start")):


                    old_mid = st2.get("msgid")
                    old_rich = _pointer_is_rich(st2, old_mid)
                    fr = st2.get("frame", 0) + 1
                    nl = _render_live_state(
                        sdir, st2, steps2, frame=fr,
                        mission=current_mission(sdir, st2))
                    opened_live = nl
                    if old_rich:
                        rich_attempted = True
                    if old_rich and not st2.get("rich_failed"):
                        send_st = dict(st2)


                        send_st["msgid"] = None
                        send_st["rich_box"] = False
                        new_mid, new_mode = _try_rich_send_state(
                            sdir, tg, send_st, nl)
                        for key, value in send_st.items():
                            if key.startswith("rich_") and key not in\
                                    ("rich_box", "rich_orphans"):
                                st2[key] = value
                        if new_mode in ("rich", "plain"):
                            opened_live = send_st.get("last_live") or nl
                    elif old_rich and st2.get("rich_failed")\
                            and not rich_enabled(sdir):
                        new_mid, new_mode = None, "skip"
                    else:
                        try:
                            new_mid = tg.send(nl, markdown=False)
                        except Exception:
                            new_mid = None
                        new_mode = "plain"
                    if new_mid:
                        fold_text = None
                        if steps2:
                            fold_text, _ = _render_final_state(
                                sdir, st2, steps2, current_mission(sdir, st2),
                                rich=old_rich)
                        above = st2.get("folded_above_msgid")
                        above_rich = bool(st2.get("folded_above_rich"))
                        if fold_text:
                            if above and above != old_mid:
                                if above_rich == old_rich:
                                    reanchor_deferred.append(
                                        ("rrefold" if old_rich else "refold", above,
                                         (old_mid, fold_text), True))
                                else:
                                    reanchor_deferred.append(
                                        ("mixed_refold", above,
                                         (old_mid, fold_text, old_rich, above_rich), True))
                            else:
                                reanchor_deferred.append(
                                    ("rfold" if old_rich else "edit",
                                     old_mid, fold_text, True))
                            st2["folded_above_msgid"] = old_mid
                            if rich_state2:
                                st2["folded_above_rich"] = old_rich
                        else:
                            reanchor_deferred.append(
                                ("delete", old_mid, None, True))
                        st2["msgid"] = new_mid
                        box_send_record(sdir, st2.get("proc_start"))
                        if rich_state2:
                            st2["rich_box"] = new_mode == "rich"
                        st2["frame"] = fr
                        st2["last_edit"] = time.time()
                        if new_mode == "rich" or old_rich:
                            st2["last_live"] = opened_live
                        st2["reanchor"] = False
                        st2["box_is_last"] = True
                        reanchored = True
                        rendered = True
                    else:


                        pass
                if not reanchored and not rendered and not rich_attempted and\
                        st2.get("msgid") and\
                        not st2.get("folded") and\
                        not (st2.get("rich_failed") and st2.get("rich_box")) and\
                        (reconciled or expired or agent_expired or swept or
                         (time.time() - st2.get("last_edit", 0)) >=
                         _effective_min_edit(sdir, st2)):
                    rendered = True
                    fr = st2.get("frame", 0) + 1


                    nl = _render_live_state(
                        sdir, st2, steps2, frame=fr,
                        mission=current_mission(sdir, st2))
                    st2["frame"] = fr
                    st2["last_edit"] = time.time()
                    if nl != st2.get("last_live"):
                        current_rich = _pointer_is_rich(st2, st2.get("msgid"))
                        do_edit = (st2["msgid"], nl, current_rich)
                        if not current_rich:
                            st2["last_live"] = nl
                if reanchor_deferred:
                    _tag_rich_deferred(st2, reanchor_deferred)
                if reconciled or expired or agent_expired or swept or rendered\
                        or rich_attempted or ctx_refreshed\
                        or bash_closed or bash_expired or backfilled:
                    save_state(sdir, st2)
            except Exception:
                pass
            finally:
                if blf is not None:
                    try:
                        fcntl.flock(blf, fcntl.LOCK_UN)
                        blf.close()
                    except Exception:
                        pass
            if reanchor_deferred:
                _run_deferred(sdir, tg, reanchor_deferred)
            if do_edit:
                st3 = load_state(sdir)
                if st3.get("msgid") == do_edit[0] and not st3.get("folded"):
                    if do_edit[2]:
                        _rich_deferred_edit(sdir, tg, do_edit[0], do_edit[1],
                                            live_fallback=True)
                    else:
                        try:
                            tg.edit(do_edit[0], do_edit[1], markdown=False)
                        except Exception:
                            pass
            if lane_finished:
                if _fold_to_done(sdir, tg):
                    break
                time.sleep(_effective_anim_interval(sdir, load_state(sdir)))
                continue
            time.sleep(_effective_anim_interval(sdir, load_state(sdir)))
        exited_cleanly = True
    finally:


        handoff = False
        try:
            if not killed:
                st = load_state(sdir)
            else:
                st = None
            if st and st.get("msgid") and not st.get("folded"):
                if not exited_cleanly\
                        and (time.time() - started) >= ANIM_RESPAWN_MIN_LIFE\
                        and (_has_open_codex_bg(st.get("steps"))
                             or _has_open_background_agents(st.get("panel"))
                             or _has_open_bash_bg(st.get("steps"))
                             or _has_open_tool_step(st.get("steps"))):
                    handoff = True
                elif st.get("steps"):
                    _fold_to_done(sdir, tg)
        except Exception:
            pass
        try:
            fcntl.flock(alf, fcntl.LOCK_UN)
            alf.close()
        except Exception:
            pass
        if handoff:
            try:
                spawn_animator(sdir)
            except Exception:
                pass


def _rich_tools_payload(st):
    items = st.get("rich_tools") if isinstance(st.get("rich_tools"), list) else []
    try:
        count = max(len(items), int(st.get("rich_tool_count") or 0))
    except Exception:
        count = len(items)
    return {"items": items[-RICH_TOOL_LINES:], "count": max(0, count)}


def _render_live_state(sdir, st, steps=None, frame=None, mission=None):
    steps = st.get("steps") if steps is None else steps
    frame = st.get("frame", 0) if frame is None else frame
    mission = current_mission(sdir, st) if mission is None else mission
    if _pointer_is_rich(st, st.get("msgid")) or\
            (_rich_turn_active(sdir, st) and not st.get("msgid")):
        return render_rich_live(
            sdir, steps or [], st.get("proc_start"), st.get("agents") or {}, mission,
            st.get("panel"), st.get("rich_plan"), _rich_tools_payload(st),
            _effective_min_edit(sdir, st), bool(st.get("rich_failed")), frame=frame,
            state=st)
    if _rich_state_present(st) and st.get("rich_failed"):
        return render_rich_live(
            sdir, steps or [], st.get("proc_start"), st.get("agents") or {}, mission,
            st.get("panel"), st.get("rich_plan"), _rich_tools_payload(st),
            _effective_min_edit(sdir, st), fallback=True, frame=frame, state=st)
    return render_live(steps or [], frame, st.get("proc_start"),
                       st.get("agents") or {}, mission, st.get("panel"))


def _render_final_state(sdir, st, steps=None, mission=None, rich=None,
                        outcome=None):
    steps = st.get("steps") if steps is None else steps
    mission = current_mission(sdir, st) if mission is None else mission
    rich = bool(st.get("rich_box")) if rich is None else bool(rich)
    enhanced = _rich_state_present(st)
    plan = st.get("rich_plan") if enhanced else None
    tools = _rich_tools_payload(st) if enhanced else None
    if rich:
        return render_rich_final(
            sdir, steps or [], st.get("proc_start"), len(st.get("agents") or {}),
            mission, st.get("panel"), plan, _effective_min_edit(sdir, st),
            outcome=outcome, tools=tools, agent_history=st.get("agent_history"))
    return render_final(steps or [], st.get("proc_start"),
                        len(st.get("agents") or {}), mission, st.get("panel"),
                        plan=plan, tools=tools, outcome=outcome)


def _collapse_ops(msgid, steps, start, n_agents=0, mission="", panel=None,
                  sdir=None, plan=None, rich=False, cadence=RICH_MIN_EDIT_INTERVAL,
                  tools=None, outcome=None, agent_history=None):
    if not msgid:
        return []
    if steps:
        if rich and sdir:
            htext, _ = render_rich_final(sdir, steps, start, n_agents, mission,
                                         panel, plan, cadence, outcome=outcome,
                                         tools=tools, agent_history=agent_history)
        else:
            htext, _ = render_final(
                steps, start, n_agents, mission, panel, plan=plan, tools=tools,
                outcome=outcome)
        if htext:
            return [("rfold" if rich else "edit", msgid, htext, True)]
    return [("delete", msgid, None, False)]


def _collapse_prior(st, mission="", sdir=None):
    return _collapse_prior_ex(st, mission, sdir=sdir)[0]


def _collapse_prior_ex(st, mission="", sdir=None):
    deferred = []
    msgid = st.get("msgid")


    steps = st.get("steps")
    steps = steps if isinstance(steps, list) else []
    folded = st.get("folded")
    interrupted = bool(not folded and not st.get("typing_done"))
    cutoff_outcome = INTERRUPTED_OUTCOME if interrupted else None
    enhanced = _rich_state_present(st)
    fold_plan = st.get("rich_plan") if enhanced else None
    fold_tools = _rich_tools_payload(st) if enhanced else None
    above = st.get("folded_above_msgid")
    panel = st.get("panel")
    box_last = bool(st.get("box_is_last"))
    msg_rich = _pointer_is_rich(st, msgid)
    above_rich = _pointer_is_rich(st, above)


    inh_mid, inh_text = _prev_pair(
        st.get("prev_recap_msgid"), st.get("prev_recap_text"),
        rich=bool(st.get("prev_recap_rich")))
    prev_mid, prev_text = None, None
    if msgid and not folded and box_last and _replied_this_turn(st):
        if above and steps and not (above_rich and _rich_failed_owner(st, above)):
            if above_rich and sdir:
                text, _ = render_rich_final(
                    sdir, steps, st.get("proc_start"), len(st.get("agents") or {}),
                    mission, panel, fold_plan, _effective_min_edit(sdir, st),
                    outcome=cutoff_outcome, tools=fold_tools,
                    agent_history=st.get("agent_history"))
            else:
                text, _ = render_final(steps, st.get("proc_start"),
                                       len(st.get("agents") or {}), mission, panel,
                                       plan=fold_plan, tools=fold_tools,
                                       outcome=cutoff_outcome)
            if text:


                deferred.append(("rrecap" if above_rich else "recap", above, text, True))
                prev_mid, prev_text = above, text
                above = None
        deferred.append(("delete", msgid, None, True))
    elif msgid and not folded and box_last and steps:


        tgt, merged, tgt_rich = _noreply_merge_ex(
            st, steps, mission, outcome=cutoff_outcome)
        if merged:
            deferred.append(("rrecap" if tgt_rich else "recap", tgt, merged, True))
            prev_mid, prev_text = tgt, merged
        deferred.append(("delete", msgid, None, True))
    elif _grace_armed(st, steps):


        tgt, merged, tgt_rich = _noreply_merge_ex(st, steps, mission)
        if merged:
            deferred.append(("rrecap" if tgt_rich else "recap", tgt, merged, True))
            prev_mid, prev_text = tgt, merged
        deferred.append(("delete", msgid, None, True))
    elif not folded:
        if msg_rich and _rich_failed_owner(st, msgid):
            ops = [("delete", msgid, None, True)] if msgid else []
        else:
            ops = _collapse_ops(msgid, steps, st.get("proc_start"),
                                len(st.get("agents") or {}), mission, panel,
                                sdir=sdir, plan=fold_plan, rich=msg_rich,
                                cadence=_effective_min_edit(sdir or "", st),
                                tools=fold_tools, outcome=cutoff_outcome,
                                agent_history=st.get("agent_history"))
        deferred.extend(ops)
        if ops and ops[0][0] in ("edit", "rfold") and not box_last:
            prev_mid, prev_text = msgid, ops[0][2]
    elif msgid and not box_last:


        prev_mid, prev_text = _prev_pair(msgid, st.get("last_live"), rich=msg_rich)
    if not prev_mid:
        prev_mid, prev_text = inh_mid, inh_text
    if above and above != msgid and (folded or steps):
        deferred.append(("delete", above, None, False))


    protected = {str(x) for x in (msgid, above, prev_mid) if x is not None}
    rich_orphans = st.get("rich_orphans")
    for orphan in (rich_orphans if isinstance(rich_orphans, list) else []):
        key = str(orphan)
        if key in protected:
            continue
        matched = False
        for i, op in enumerate(deferred):
            if op[0] == "delete" and str(op[1]) == key:
                matched = True
                if not op[3]:
                    deferred[i] = ("delete", op[1], None, True)
                break
        if not matched:
            deferred.append(("delete", orphan, None, True))


    _gh = st.get("ghosts")
    for g in (_gh if isinstance(_gh, list) else []):
        key = str(g)
        if key in protected or any(
                op[0] == "delete" and str(op[1]) == key for op in deferred):
            continue
        deferred.append(("delete", g, None, False))
    return deferred, prev_mid, prev_text


def _is_fresh_open_box(st, now=None):
    pending_open = bool(
        isinstance(st, dict) and not st.get("msgid")
        and st.get("rich_turn") and st.get("rich_pending_open") is True)
    if not st or (not st.get("msgid") and not pending_open) or st.get("folded"):
        return False
    ps = st.get("proc_start")
    if not ps:
        return False
    now = time.time() if now is None else now
    steps = st.get("steps")
    if not isinstance(steps, list):
        return False
    panel = st.get("panel")
    if not isinstance(panel, list):
        return False
    carried_agents = {}
    for row in panel:
        if not isinstance(row, dict):
            return False
        started = _background_agent_started(row)
        if row.get("background") is not True or row.get("done")\
                or started is None or started >= ps:
            return False
        if row.get("tuid"):
            carried_agents[row.get("tuid")] = started
    if steps:
        for step in steps:
            started = _codex_bg_started(step)
            if step.get("codex_bg") and not step.get("done")\
                    and started is not None and started < ps:
                continue


            bash_started = _bash_bg_started(step)
            if step.get("bash_bg") and not step.get("done")\
                    and bash_started is not None and bash_started < ps:
                continue
            if step.get("tuid") in carried_agents:
                continue
            return False
    return (now - ps) < STALE_SECONDS


def _open_fresh_box(sdir, tg, ghosts=None, mission="", prev_recap=None,
                    carried_steps=None, carried_panel=None, rich_plan=None,
                    session_id=None, rich_op_state=None, ctx_pct=None,
                    ctx_model=None, agent_history=None, ctx_pending_path=None,
                    ctx_effort=None):
    now = time.time()
    fresh = fresh_state(
        now, ghosts=ghosts, mission=mission, prev_recap=prev_recap,
        ctx_pct=ctx_pct, ctx_model=ctx_model, agent_history=agent_history,
        ctx_pending_path=ctx_pending_path, ctx_effort=ctx_effort)
    _seed_rich_turn(fresh, sdir, session_id=session_id, plan=rich_plan)
    if isinstance(rich_op_state, dict) and "rich_op_seq" in rich_op_state:
        try:
            fresh["rich_op_seq"] = int(rich_op_state.get("rich_op_seq") or 0)
        except Exception:
            fresh["rich_op_seq"] = 0
        latest = rich_op_state.get("rich_op_latest")
        if isinstance(latest, dict):
            fresh["rich_op_latest"] = dict(latest)


    fresh["steps"] = list(carried_steps or [])
    fresh["panel"] = list(carried_panel or [])
    shown = current_mission(sdir, fresh)
    try:
        typer = getattr(tg, "typing_once", None) if fresh.get("rich_turn") else None
        typer() if callable(typer) else tg.typing()
    except Exception:
        pass
    mid = None
    if fresh.get("rich_turn"):
        try:
            live = render_rich_live(
                sdir, fresh["steps"], now, {}, shown, fresh.get("panel"),
                fresh.get("rich_plan"),
                {"items": fresh.get("rich_tools"),
                 "count": fresh.get("rich_tool_count")},
                _effective_min_edit(sdir, fresh), frame=fresh.get("frame", 0),
                state=fresh)
            result = tg.send_rich(live)
        except Exception as exc:
            result = {"ok": False,
                      "error_class": "timeout" if isinstance(exc, TimeoutError)
                      else "connection_reset" if isinstance(exc, ConnectionResetError)
                      else "network"}
        action, error_class = _apply_rich_result(fresh, result, sdir=sdir)
        if action == "ok":
            normalized = _normalize_rich_result(result)
            mid = str(normalized.get("message_id") or "") or None
            live = normalized.get("frame") or live
            if mid:
                fresh["rich_box"] = True
        elif action == "fallback":
            _append_rich_health(sdir, fresh, error_class)
            mid = _cold_start_plain_state(sdir, tg, fresh, error_class)
        else:
            fresh["rich_pending_open"] = True
    else:
        try:

            live = render_live(
                fresh["steps"], 0, now, {}, shown, panel=fresh.get("panel"))
            mid = tg.send(live, markdown=False)
        except Exception:
            mid = None
    if mid and not fresh.get("msgid"):
        fresh["msgid"] = mid


        box_send_record(sdir, now)
        fresh["last_live"] = live
        fresh["last_edit"] = now


        fresh["box_is_last"] = True
    save_state(sdir, fresh)
    spawn_animator(sdir)


def _registered_row_gate_ok(row, label_gate, exact=False):
    if not isinstance(row, dict) or not row.get("registered_by"):
        return True
    if not isinstance(label_gate, (int, float)) or isinstance(label_gate, bool):
        return exact
    started = _background_agent_started(row)
    return started is not None and started <= float(label_gate)


def _apply_notification_close(st, prompt_text, now=None, label_gate=None):
    results = dict(_terminal_notification_results(prompt_text))


    labels = _terminal_notification_agent_labels(prompt_text)

    agent_ids = _terminal_notification_agent_ids(prompt_text)
    if not st or (not results and not labels and not agent_ids):
        return False, False, [], []
    steps = st.get("steps")
    steps = steps if isinstance(steps, list) else []
    panel = st.get("panel")
    panel = panel if isinstance(panel, list) else []
    now = time.time() if now is None else now
    matched = False
    closed = False
    closed_steps = []
    inferred_corrections = []
    for step in steps:
        outcome = results.get(step.get("tuid"))


        if not (step.get("codex_bg") or step.get("bash_bg")) or not outcome:
            continue
        matched = True
        inferred = bool(step.get("done")
                        and (step.get("codex_inferred") or step.get("bash_inferred")))
        if step.get("done") and not inferred:
            continue
        closed = True
        is_bash = bool(step.get("bash_bg")) and not step.get("codex_bg")
        if not step.get("done"):
            step["done"] = True
            started = _bash_bg_started(step) if is_bash else _codex_bg_started(step)
            step["secs"] = max(0, now - started) if started is not None else 0
            closed_steps.append(step)
        else:
            inferred_corrections.append(step)
        if is_bash:
            step["outcome"] = outcome
            step.pop("bash_inferred", None)
        else:
            step["codex_terminal_status"] = outcome
            step.pop("codex_inferred", None)
        if outcome != "completed":
            suffix = " · " + outcome
            step["cap"] = short(step.get("cap")
                                or (BASH_BG_LABEL if is_bash else "Codex"),
                                80 - len(suffix)) + suffix
    for row in panel:
        if not isinstance(row, dict):
            continue
        outcome = results.get(row.get("tuid"))
        if not outcome:
            continue
        matched = True
        if row.get("done"):
            continue
        closed = True
        row["done"] = True
        row["secs"] = max(0, now - (row.get("t0") or now))
        row["outcome"] = outcome


    known = set()
    for row in panel:
        if isinstance(row, dict) and row.get("tuid"):
            known.add(row.get("tuid"))
    for step in steps:
        if isinstance(step, dict) and step.get("tuid")\
                and (step.get("codex_bg") or step.get("bash_bg")):
            known.add(step.get("tuid"))


    answered = set()
    for task_id, outcome, tuid, label in agent_ids:
        if tuid and tuid in known:
            continue
        if label_gate is False:
            continue
        eligible = [row for row in panel
                    if isinstance(row, dict) and row.get("background") is True
                    and row.get("close_by_agent_id") is True
                    and row.get("agent_id") == task_id]
        if isinstance(label_gate, (int, float)) and not isinstance(label_gate, bool):
            eligible = [row for row in eligible
                        if _background_agent_started(row) is not None
                        and _background_agent_started(row)
                        <= float(label_gate) + BG_NOTIFY_CLOCK_SLACK]
        eligible = [row for row in eligible
                    if _registered_row_gate_ok(row, label_gate, exact=True)]
        if not eligible:
            continue
        answered.add((label, tuid))
        matched = True
        row = next((r for r in eligible if not r.get("done")), None)
        if row is None:
            continue
        closed = True
        row["done"] = True
        row["secs"] = max(0, now - (row.get("t0") or now))
        row["outcome"] = outcome
    for label, outcome, tuid in labels:
        if tuid and tuid in known:
            continue
        if (label, tuid) in answered:
            continue


        if label_gate is False:


            continue
        stored = short(label, PANEL_LABEL_CAP)
        eligible = [row for row in panel
                    if isinstance(row, dict) and row.get("background") is True
                    and (row.get("lab") or "") == stored]
        if isinstance(label_gate, (int, float)) and not isinstance(label_gate, bool):


            eligible = [row for row in eligible
                        if _background_agent_started(row) is not None
                        and _background_agent_started(row)
                        <= float(label_gate) + BG_NOTIFY_CLOCK_SLACK]
        eligible = [row for row in eligible
                    if _registered_row_gate_ok(row, label_gate, exact=False)]
        row = next((r for r in eligible if not r.get("done")), None)
        if row is None:


            matched = matched or bool(eligible)
            continue
        matched = True
        closed = True
        row["done"] = True
        row["secs"] = max(0, now - (row.get("t0") or now))
        row["outcome"] = outcome
    return matched, closed, closed_steps, inferred_corrections


def _sweep_background_notifications(sdir, st, now=None, tail_bytes=None):
    try:
        if not isinstance(st, dict):
            return 0
        now = time.time() if now is None else now
        panel = st.get("panel")
        panel = panel if isinstance(panel, list) else []


        sweep_steps = st.get("steps")
        sweep_steps = sweep_steps if isinstance(sweep_steps, list) else []
        if not _open_background_agent_rows(panel, now)\
                and not _has_open_bash_bg(sweep_steps, now):
            return 0
        path = _bg_notify_transcript_path(st)
        if not path:
            return 0
        closed_count = 0
        seen = set()
        for stamp, text in _transcript_notifications(path, tail_bytes=tail_bytes):
            if text in seen:
                continue
            seen.add(text)
            gate = stamp if isinstance(stamp, float) else False
            try:
                _matched, closed, _steps, _inferred = _apply_notification_close(
                    st, text, now=now, label_gate=gate)
            except Exception:
                continue
            if closed:
                closed_count += 1
            if not _open_background_agent_rows(st.get("panel"), now)\
                    and not _has_open_bash_bg(st.get("steps"), now):
                break
        if closed_count and _agent_history_active(st, sdir):
            history_steps = st.get("steps")
            history_panel = st.get("panel")
            st["agent_history"] = _capture_closed_agents(
                st.get("agent_history"),
                history_panel if isinstance(history_panel, list) else [],
                history_steps if isinstance(history_steps, list) else [],
                st.get("rich_session_id") or "", now=now)
        return closed_count
    except Exception:
        return 0


def _close_codex_notification(sdir, st, prompt_text, tg=None):
    now = time.time()
    matched, closed, closed_steps, inferred_corrections = _apply_notification_close(
        st, prompt_text, now=now)
    if not matched:
        return None
    if not closed:
        return []
    steps = st.get("steps")
    steps = steps if isinstance(steps, list) else []
    panel = st.get("panel")
    panel = panel if isinstance(panel, list) else []

    deferred = []
    was_folded = bool(st.get("folded"))
    st["steps"] = steps
    st["panel"] = panel
    if _agent_history_active(st, sdir):
        st["agent_history"] = _capture_closed_agents(
            st.get("agent_history"), panel, steps,
            st.get("rich_session_id") or "", now=now)
    st["last_activity"] = now
    if _rich_state_present(st):
        tools = st.get("rich_tools")
        tools = list(tools) if isinstance(tools, list) else []
        for step in closed_steps:
            tools.append({"cap": short(step.get("cap") or "Codex",
                                        RICH_TOOL_LINE_CAP),
                          "elapsed": max(0, now - (st.get("proc_start") or now))})
        st["rich_tools"] = tools[-RICH_TOOL_LINES:]
        try:
            prior_tool_count = max(0, int(st.get("rich_tool_count") or 0))
        except Exception:
            prior_tool_count = len(tools) - len(closed_steps)
        st["rich_tool_count"] = prior_tool_count + len(closed_steps)
    msgid = st.get("msgid")
    msg_rich = _pointer_is_rich(st, msgid)
    if was_folded and inferred_corrections and not closed_steps:
        if msgid and not (msg_rich and _rich_failed_owner(st, msgid)):
            text, _ = _render_final_state(
                sdir, st, steps, current_mission(sdir, st), rich=msg_rich)
            if text:
                deferred.append(("rrecap" if msg_rich else "recap",
                                 msgid, text, True))
                st["last_live"] = text
        _tag_rich_deferred(st, deferred)
        save_state(sdir, st)
        return deferred
    st["typing_done"] = False
    st["noreply_grace_until"] = None
    if msgid and msg_rich and _rich_failed_owner(st, msgid) and not st.get("folded")\
            and tg is not None:


        _cold_start_plain_state(sdir, tg, st, "rich_failed")
        deferred.append(("typing", None, None, False))
        save_state(sdir, st)
        spawn_animator(sdir)
        return deferred
    if msgid and msg_rich and st.get("rich_failed")\
            and not _rich_failed_owner(st, msgid) and tg is not None\
            and rich_enabled(sdir):


        st["folded_above_msgid"] = msgid
        st["folded_above_rich"] = True
        st["msgid"] = None
        st["rich_box"] = False
        st["folded"] = False
        st["box_is_last"] = False
        _cold_start_plain_state(sdir, tg, st, "rich_failed")
        deferred.append(("typing", None, None, False))
        save_state(sdir, st)
        spawn_animator(sdir)
        return deferred
    if msgid and not (msg_rich and _rich_failed_owner(st, msgid)):
        st["folded"] = False
        frame = st.get("frame", 0) + 1
        if msg_rich:
            live = render_rich_live(
                sdir, steps, st.get("proc_start"), st.get("agents") or {},
                current_mission(sdir, st), st.get("panel"), st.get("rich_plan"),
                _rich_tools_payload(st), _effective_min_edit(sdir, st), frame=frame,
                state=st)
        elif _rich_state_present(st) and st.get("rich_failed"):
            live = render_rich_live(
                sdir, steps, st.get("proc_start"), st.get("agents") or {},
                current_mission(sdir, st), st.get("panel"), st.get("rich_plan"),
                _rich_tools_payload(st), _effective_min_edit(sdir, st), fallback=True,
                frame=frame, state=st)
        else:
            live = render_live(steps, frame, st.get("proc_start"),
                               st.get("agents") or {}, current_mission(sdir, st),
                               st.get("panel"))
        st["frame"] = frame
        st["last_edit"] = now
        if live != st.get("last_live"):
            deferred.append(("redit" if msg_rich else "edit",
                             msgid, live, False))
            if not msg_rich:
                st["last_live"] = live
        deferred.append(("typing", None, None, False))
    save_state(sdir, st)
    spawn_animator(sdir)
    return deferred


def on_prompt(sdir, tg, prompt_text="", session_id=None, transcript_path=None,
              project_dir=None):
    text = (prompt_text or "").strip()
    if not text or text.startswith("/"):
        return []
    st = load_state(sdir)
    ctx_pct, ctx_model, agent_history = None, None, None
    ctx_pending_path = None
    ctx_effort = None
    if rich_enabled(sdir):


        ctx_pct, ctx_model, ctx_pending_path = _turn_context_ex(
            sdir, transcript_path=transcript_path,
            session_id=session_id, project_dir=project_dir)


        ctx_effort = _turn_context_effort(sdir)
        agent_history = _normalize_agent_history(st.get("agent_history"))
        st["ctx_pct"] = ctx_pct
        st["ctx_model"] = ctx_model
        _store_context_effort(st, ctx_effort)
        st["agent_history"] = agent_history
    prompt_steps = st.get("steps")
    prompt_steps = prompt_steps if isinstance(prompt_steps, list) else []
    history_sink = st if _agent_history_active(st, sdir) else None
    _close_gone_codex_bg(prompt_steps, history_state=history_sink)
    _expire_codex_bg(prompt_steps, history_state=history_sink)
    _close_gone_bash_bg(prompt_steps)
    _expire_bash_bg(prompt_steps)
    prompt_panel = st.get("panel")
    prompt_panel = prompt_panel if isinstance(prompt_panel, list) else []
    _expire_background_agents(prompt_panel, history_state=history_sink)
    st["steps"] = prompt_steps
    st["panel"] = prompt_panel
    codex_terminal = _close_codex_notification(sdir, st, text, tg=tg)
    if codex_terminal is not None:
        return codex_terminal
    if rich_enabled(sdir):
        agent_history = _normalize_agent_history(st.get("agent_history"))
    carried_codex = _open_codex_bg_steps(st.get("steps"))
    carried_bash = _open_bash_bg_steps(st.get("steps"))
    carried_agents = _open_background_agent_rows(st.get("panel"))
    carried_agent_steps = _open_background_agent_steps(
        st.get("steps"), carried_agents)


    carried_plan = _boundary_plan(st, session_id)

    deferred, prev_mid, prev_text = _collapse_prior_ex(
        st, current_mission(sdir, st), sdir=sdir)
    prev_rich = _pointer_is_rich(st, prev_mid)
    _tag_rich_deferred(st, deferred)
    _open_fresh_box(sdir, tg, ghosts=[],
                    mission=("" if _is_notification_prompt(text)
                             else mission_from_prompt(text)),
                    prev_recap=(prev_mid, prev_text, prev_rich),
                    carried_steps=carried_codex + carried_bash + carried_agent_steps,
                    carried_panel=carried_agents,
                    rich_plan=carried_plan,
                    session_id=session_id, rich_op_state=st,
                    ctx_pct=ctx_pct, ctx_model=ctx_model,
                    agent_history=agent_history,
                    ctx_pending_path=ctx_pending_path,
                    ctx_effort=ctx_effort)
    return deferred


def on_react(sdir, tg):
    st = load_state(sdir)
    react_steps = st.get("steps")
    react_steps = react_steps if isinstance(react_steps, list) else []
    history_sink = st if _agent_history_active(st, sdir) else None
    _close_gone_codex_bg(react_steps, history_state=history_sink)
    _expire_codex_bg(react_steps, history_state=history_sink)
    _close_gone_bash_bg(react_steps)
    _expire_bash_bg(react_steps)
    react_panel = st.get("panel")
    react_panel = react_panel if isinstance(react_panel, list) else []
    _expire_background_agents(react_panel, history_state=history_sink)
    st["steps"] = react_steps
    st["panel"] = react_panel
    if _is_fresh_open_box(st):


        st["last_react"] = time.time()
        save_state(sdir, st)
        try:
            tg.typing()
        except Exception:
            pass
        return []
    carried_codex = _open_codex_bg_steps(st.get("steps"))
    carried_bash = _open_bash_bg_steps(st.get("steps"))
    carried_agents = _open_background_agent_rows(st.get("panel"))
    carried_agent_steps = _open_background_agent_steps(
        st.get("steps"), carried_agents)
    deferred, prev_mid, prev_text = _collapse_prior_ex(
        st, current_mission(sdir, st), sdir=sdir)
    prev_rich = _pointer_is_rich(st, prev_mid)
    _tag_rich_deferred(st, deferred)
    _open_fresh_box(sdir, tg, ghosts=[], prev_recap=(prev_mid, prev_text, prev_rich),
                    carried_steps=carried_codex + carried_bash + carried_agent_steps,
                    carried_panel=carried_agents,
                    rich_plan=_session_plan(st, None),
                    session_id=st.get("rich_session_id"), rich_op_state=st,
                    ctx_pct=st.get("ctx_pct"), ctx_model=st.get("ctx_model"),
                    agent_history=st.get("agent_history"),
                    ctx_effort=st.get("ctx_effort"))
    return deferred


def record_reply(sdir, tg, final=False):
    st = load_state(sdir)
    if not st:
        return []
    now = time.time()
    replied = _replied_this_turn(st)
    st["last_reply"] = now
    deferred = []
    msgid = st.get("msgid")
    above = st.get("folded_above_msgid")
    rich_state = _rich_state_present(st)
    msg_rich = _pointer_is_rich(st, msgid)
    above_rich = _pointer_is_rich(st, above)
    if not final:


        st["box_is_last"] = False
        st["replied_this_turn"] = True


        st["noreply_grace_until"] = None
        if msgid and not st.get("folded"):
            st["reanchor"] = True
        save_state(sdir, st)
        return deferred


    codex_steps = st.get("steps")
    codex_steps = codex_steps if isinstance(codex_steps, list) else []
    background_panel = st.get("panel")
    background_panel = background_panel if isinstance(background_panel, list) else []
    history_sink = st if _agent_history_active(st, sdir) else None
    _close_gone_codex_bg(codex_steps, now, history_state=history_sink)
    _expire_codex_bg(codex_steps, now, history_state=history_sink)
    _expire_background_agents(background_panel, now, history_state=history_sink)


    _close_gone_bash_bg(codex_steps, now)
    _expire_bash_bg(codex_steps, now)
    if _has_open_codex_bg(codex_steps, now)\
            or _has_open_background_agents(background_panel, now)\
            or _has_open_bash_bg(codex_steps, now):
        st["steps"] = codex_steps
        st["panel"] = background_panel
        st["typing_done"] = False
        st["noreply_grace_until"] = None
        save_state(sdir, st)
        spawn_animator(sdir)
        return [("typing", None, None, False)]


    orphans = st.get("rich_orphans")
    orphans = list(orphans) if isinstance(orphans, list) else []
    retained_orphans = []
    scheduled_orphans = False
    for orphan in orphans:
        if str(orphan) in (str(msgid), str(above)):
            retained_orphans.append(orphan)
        else:
            deferred.append(("delete", orphan, None, True))
            scheduled_orphans = True
    if rich_state and scheduled_orphans:
        st["rich_orphans"] = retained_orphans


    st["typing_done"] = True
    st["reanchor"] = False

    mission_now = current_mission(sdir, st)
    panel_now = st.get("panel")
    trailing_shape = bool(msgid and replied and st.get("box_is_last"))


    steps_now = st.get("steps")
    steps_now = steps_now if isinstance(steps_now, list) else []
    noreply_below = _noreply_below(st, steps_now)


    if _grace_due(st, steps_now, now):
        trailing, completed = _noreply_cleanup_inline(
            sdir, tg, st, steps_now, mission_now)
        if not completed:
            save_state(sdir, st)
            return deferred
        st["replied_this_turn"] = False
        if trailing:
            deferred.append(("delete", trailing, None, True))
        save_state(sdir, st)
        return deferred


    hold_position = bool(noreply_below or _grace_armed(st, steps_now))
    st["replied_this_turn"] = False
    st["box_is_last"] = hold_position

    if trailing_shape:


        deferred.append(("delete", msgid, None, True))
        st["msgid"] = None
        st["folded"] = True
        if above:
            if above_rich:
                text, _ = render_rich_final(
                    sdir, st.get("steps") or [], st.get("proc_start"),
                    len(st.get("agents") or {}), mission_now, panel_now,
                    st.get("rich_plan"), _effective_min_edit(sdir, st),
                    tools=_rich_tools_payload(st),
                    agent_history=st.get("agent_history"))
            else:
                text, _ = render_final(st.get("steps") or [], st.get("proc_start"),
                                       len(st.get("agents") or {}), mission_now, panel_now,
                                       plan=(st.get("rich_plan") if rich_state else None),
                                       tools=(_rich_tools_payload(st)
                                              if rich_state else None))
            if text:
                try:
                    if above_rich:
                        _invalidate_rich_targets(st, above)
                    if above_rich and not _rich_failed_owner(st, above):
                        rich_result = tg.edit_rich(above, text)
                        action, error_class = _rich_result_for_target(
                            st, above, rich_result, sdir=sdir)
                        edited = action == "ok"
                        if edited:
                            text = _normalize_rich_result(rich_result).get("frame") or text
                        if action == "fallback":
                            _append_rich_health(sdir, st, error_class)
                    elif not above_rich:
                        edited = tg.edit(above, text, markdown=True)
                    else:
                        edited = False
                    if edited:
                        st["last_live"] = text
                        _unghost(st, above)
                except Exception:
                    if above_rich and not _rich_failed_owner(st, above):
                        st.pop("rich_error_description", None)
                        _append_rich_health(sdir, st, "network")


            st["msgid"] = above
            if rich_state:
                st["rich_box"] = above_rich
            st["folded_above_msgid"] = None
            if rich_state:
                st["folded_above_rich"] = False
        save_state(sdir, st)
        return deferred


    if msgid and not st.get("folded"):
        if st.get("steps"):
            if msg_rich:
                text, _ = render_rich_final(
                    sdir, st.get("steps"), st.get("proc_start"),
                    len(st.get("agents") or {}), mission_now, panel_now,
                    st.get("rich_plan"), _effective_min_edit(sdir, st),
                    tools=_rich_tools_payload(st),
                    agent_history=st.get("agent_history"))
            else:
                text, _ = render_final(st.get("steps"), st.get("proc_start"),
                                       len(st.get("agents") or {}), mission_now, panel_now,
                                       plan=(st.get("rich_plan") if rich_state else None),
                                       tools=(_rich_tools_payload(st)
                                              if rich_state else None))
            if text:


                ok = False
                try:
                    if msg_rich:
                        _invalidate_rich_targets(st, msgid)
                    if msg_rich and not _rich_failed_owner(st, msgid):
                        rich_result = tg.edit_rich(msgid, text)
                        action, error_class = _rich_result_for_target(
                            st, msgid, rich_result, sdir=sdir)
                        ok = action == "ok"
                        if ok:
                            text = _normalize_rich_result(rich_result).get("frame") or text
                        if action == "fallback":
                            _append_rich_health(sdir, st, error_class)
                    elif not msg_rich:
                        ok = tg.edit(msgid, text, markdown=True)
                except Exception:
                    ok = False
                    if msg_rich and not _rich_failed_owner(st, msgid):
                        st.pop("rich_error_description", None)
                        _append_rich_health(sdir, st, "network")
                if ok:
                    st["folded"] = True
                    st["last_live"] = text
                    if rich_state:


                        st["rich_box"] = msg_rich
                    if noreply_below:


                        st["noreply_grace_until"] = now + NOREPLY_GRACE
                elif msg_rich and _rich_failed_owner(st, msgid):


                    deferred.append(("delete", msgid, None, True))
                    st["msgid"] = None
                    st["folded"] = True
                    if rich_state:
                        st["rich_box"] = False
                    st["box_is_last"] = False
                    st["rich_failed_msgid"] = str(msgid)
                    if above and str(above) == str(msgid):
                        above = None
                        st["folded_above_msgid"] = None
                        if rich_state:
                            st["folded_above_rich"] = False
        else:
            deferred.append(("delete", msgid, None, False))
            st["msgid"] = None
            if rich_state and msg_rich:
                st["rich_box"] = False
    if above:


        if not st.get("msgid"):
            st["msgid"] = above
            if rich_state:
                st["rich_box"] = above_rich
            st["folded"] = True
        elif above != st.get("msgid"):


            deferred.append(("delete", above, None, True))
        st["folded_above_msgid"] = None
        if rich_state:
            st["folded_above_rich"] = False
    save_state(sdir, st)
    return deferred


def handle_step(sdir, tg, mode, tool, tinput, tuid, agent_id, agent_type,
                tresponse=None, session_id=None):
    now = time.time()
    deferred = []
    codex_cap = _codex_bash_caption(tinput) if tool == "Bash" else ""
    codex_bg = bool(codex_cap and _is_background_codex(tinput))
    codex_key = _codex_process_key(tinput) if codex_bg else ""


    bash_bg_req = bool(tool == "Bash" and _is_background_bash(tinput))


    hidden = _is_mission_write(sdir, tool, tinput) and not codex_bg
    st = load_state(sdir)
    if not _rich_state_present(st) and not st.get("msgid") and\
            not st.get("proc_start") and rich_enabled(sdir):


        _seed_rich_turn(
            st, sdir, session_id=session_id,
            plan=_boundary_plan(st, session_id))
    rich_track = _rich_state_present(st)
    if rich_track:


        _refresh_pending_context(sdir, st)
    msgid = st.get("msgid")
    rich_box = _pointer_is_rich(st, msgid)


    if rich_track and _rich_failed_owner(st, msgid) and rich_box:
        orphans = st.get("rich_orphans")
        orphans = list(orphans) if isinstance(orphans, list) else []
        if msgid and str(msgid) not in [str(x) for x in orphans]:
            orphans.append(msgid)
        st["rich_orphans"] = orphans[-6:]
        msgid = None
        rich_box = False


    steps = st.get("steps")
    steps = steps if isinstance(steps, list) else []
    history_sink = st if _agent_history_active(st, sdir) else None
    _close_gone_codex_bg(
        steps, now, history_state=history_sink, session_id=session_id or "")
    _expire_codex_bg(
        steps, now, history_state=history_sink, session_id=session_id or "")
    _close_gone_bash_bg(steps, now)
    _expire_bash_bg(steps, now)
    had_steps = bool(steps)


    proc_start = st.get("proc_start")
    last_edit = st.get("last_edit", 0)
    last_activity = st.get("last_activity", 0)
    last_reply = st.get("last_reply", 0)
    last_react = st.get("last_react", 0)
    frame = st.get("frame", 0)
    last_spawn = st.get("last_spawn", 0)
    folded = st.get("folded", False)
    last_live = st.get("last_live")
    agents = st.get("agents") or {}
    next_n = st.get("next_agent_n", 1)
    ghosts = st.get("ghosts")
    ghosts = ghosts if isinstance(ghosts, list) else []
    reanchor = st.get("reanchor", False)
    box_is_last = st.get("box_is_last", False)
    folded_above = st.get("folded_above_msgid")


    replied_turn = st.get("replied_this_turn")


    mission = st.get("mission")
    mission = mission if isinstance(mission, str) else ""
    panel = st.get("panel")
    panel = panel if isinstance(panel, list) else []


    if _open_background_agent_rows(panel, now):
        st["steps"] = steps
        st["panel"] = panel
        _sweep_background_notifications(sdir, st, now)
    mission_now = current_mission(sdir, st)


    prev_recap_mid = st.get("prev_recap_msgid")
    prev_recap_text = st.get("prev_recap_text")
    prev_recap_text = prev_recap_text if isinstance(prev_recap_text, str) else None


    rich_plan = _session_plan(st, session_id)
    rich_tools = list(st.get("rich_tools")) if rich_track and\
        isinstance(st.get("rich_tools"), list) else []
    if rich_track:
        try:
            rich_tool_count = max(
                len(rich_tools), int(st.get("rich_tool_count") or 0))
        except Exception:
            rich_tool_count = len(rich_tools)
    else:
        rich_tool_count = 0
    folded_above_rich = bool(st.get("folded_above_rich"))
    prev_recap_rich = bool(st.get("prev_recap_rich"))
    healthy_rich_fallback = bool(
        rich_track and st.get("rich_failed") and msgid and rich_box
        and not _rich_failed_owner(st, msgid))


    if folded and msgid:


        folded = False
        last_edit = 0
    if healthy_rich_fallback:


        reanchor = True
        box_is_last = False

    if proc_start is None:
        proc_start = now
    folded = False

    fam = family_of(tool, agent_id)
    cap = codex_cap or caption_of(tool, tinput, agent_id)
    k = step_kind(tool, agent_id)
    if not hidden and agent_id and agent_id not in agents:
        agents[agent_id] = {"n": next_n, "type": agent_type or "agent"}
        next_n += 1


    if k == "subtitle":
        if mode != "pre":


            _panel_record_served_model(panel, tuid, tresponse)


            _panel_stamp_agent_id(panel, tuid, tresponse)
        if mode == "pre":
            panel = _panel_open(
                panel, tuid, tinput, now,
                session_id=(session_id or "") if rich_track else "")


            if rich_track and _claude_alias_family(
                    tinput.get("model") if isinstance(tinput, dict) else None):
                _models_cache_maybe_refresh(sdir, now)
        elif _panel_accept_background_launch(panel, tuid, tresponse):


            pass
        else:
            panel = _panel_close(
                panel, tuid, now, history_state=history_sink,
                session_id=session_id or "")
    elif mode != "pre" and not agent_id and not hidden:


        resumed_id, resumed_kind = _background_agent_signal(tresponse)
        if resumed_id:
            panel, _resumed_row = _panel_register_background_agent(
                panel, tuid, resumed_id, resumed_kind, now, tinput=tinput,
                session_id=(session_id or "") if rich_track else "",
                meta=lambda: _subagent_meta(session_id, resumed_id))

    frame += 1
    if hidden:


        pass
    elif mode == "pre":
        step = {"fam": fam, "cap": cap, "tuid": tuid or "", "done": False,
                "k": k, "aid": agent_id or ""}
        if rich_track:
            step["t0"] = now
            step["history_session_id"] = short(
                session_id or st.get("rich_session_id") or "",
                AGENT_HISTORY_SESSION_CAP)
        if codex_cap:
            step.update({"codex": True, "t0": now})


            codex_model = _codex_exec_model(
                tinput.get("command") if isinstance(tinput, dict) else None)
            if codex_model:
                step["codex_model"] = short(codex_model, PANEL_MODEL_STORE)
            if codex_bg:
                step["codex_bg"] = True
                if codex_key:
                    step["codex_key"] = codex_key
        elif bash_bg_req:


            step.update({"bash_bg_requested": True, "t0": now})
        steps.append(step)
    else:
        flipped = False
        finished_step = None
        if tuid:
            for s in steps:
                if not s.get("done") and s.get("tuid") == tuid:


                    launched = _bash_bg_accept_launch(s, tresponse)
                    if not s.get("codex_bg") and not launched:
                        s["done"] = True
                        if rich_track:
                            s["secs"] = max(0, now - (s.get("t0") or now))
                            finished_step = s
                    flipped = True
                    break
        else:
            for s in reversed(steps):
                if not s.get("done") and not s.get("tuid"):
                    launched = _bash_bg_accept_launch(s, tresponse)
                    if not s.get("codex_bg") and not launched:
                        s["done"] = True
                        if rich_track:
                            s["secs"] = max(0, now - (s.get("t0") or now))
                            finished_step = s
                    flipped = True
                    break

        if rich_track and finished_step is not None:
            rich_tools.append({"cap": short(
                                   finished_step.get("cap") or cap,
                                   RICH_TOOL_LINE_CAP),
                               "elapsed": max(0, now - (proc_start or now))})
            rich_tools = rich_tools[-RICH_TOOL_LINES:]
            rich_tool_count += 1
        rich_plan = _update_rich_plan(
            rich_plan, tool, tinput, tresponse, agent_id=agent_id)

    if len(steps) > MAX_STORED_STEPS:
        steps = steps[len(steps) - MAX_STORED_STEPS:]


    if reanchor and msgid and not folded and box_send_allowed(sdir, proc_start):
        old_mid = msgid
        old_rich = rich_box
        opened_live_text = None
        if old_rich and not st.get("rich_failed"):
            work_st = dict(st)


            work_st.update({"msgid": None, "steps": steps, "proc_start": proc_start,
                            "frame": frame, "agents": agents, "panel": panel,
                            "rich_box": False, "rich_plan": rich_plan,
                            "rich_tools": rich_tools, "rich_tool_count": rich_tool_count})
            live_text = render_rich_live(
                sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                {"items": rich_tools, "count": rich_tool_count},
                _effective_min_edit(sdir, work_st), frame=frame, state=work_st)
            opened_live_text = live_text
            new_mid, new_mode = _try_rich_send_state(sdir, tg, work_st, live_text)
            st.update({k: v for k, v in work_st.items()
                       if k.startswith("rich_") and k not in
                       ("rich_box", "rich_orphans")})
            if new_mode in ("rich", "plain"):
                opened_live_text = work_st.get("last_live") or live_text
        else:
            if rich_track and st.get("rich_failed"):
                live_text = render_rich_live(
                    sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                    {"items": rich_tools, "count": rich_tool_count},
                    _effective_min_edit(sdir, st), fallback=True, frame=frame, state=st)
            else:
                live_text = render_live(steps, frame, proc_start, agents, mission_now, panel)
            opened_live_text = live_text
            if rich_track and st.get("rich_failed") and not rich_enabled(sdir):
                new_mid, new_mode = None, "skip"
            else:
                new_mid = tg.send(live_text, markdown=False)
                new_mode = "plain"
        if new_mid:
            fold_text = None
            if had_steps:
                if old_rich:
                    fold_text, _ = render_rich_final(
                        sdir, steps, proc_start, len(agents), mission_now, panel,
                        rich_plan, _effective_min_edit(sdir, st),
                        tools={"items": rich_tools, "count": rich_tool_count},
                        agent_history=st.get("agent_history"))
                else:
                    fold_text, _ = render_final(steps, proc_start, len(agents),
                                                mission_now, panel,
                                                plan=(rich_plan if rich_track else None),
                                                tools=({"items": rich_tools,
                                                        "count": rich_tool_count}
                                                       if rich_track else None))
            if fold_text:
                if folded_above and folded_above != old_mid:
                    if folded_above_rich == old_rich:
                        deferred.append(("rrefold" if old_rich else "refold",
                                         folded_above, (old_mid, fold_text), True))
                    else:
                        deferred.append(("mixed_refold", folded_above,
                                         (old_mid, fold_text, old_rich,
                                          folded_above_rich), True))
                else:
                    deferred.append(("rfold" if old_rich else "edit",
                                     old_mid, fold_text, True))
                folded_above = old_mid
                folded_above_rich = old_rich
            else:


                deferred.append(("delete", old_mid, None, True))
            msgid = new_mid
            box_send_record(sdir, proc_start)
            rich_box = new_mode == "rich"
            box_is_last = True
            last_edit = now
            last_live = opened_live_text or live_text
            spawn_animator(sdir)
            last_spawn = now
            reanchor = False


    else:
        reanchor = False

    if not msgid:
        if steps:

            if rich_track and _rich_turn_active(sdir, st):
                work_st = dict(st)
                work_st.update({"msgid": None, "steps": steps, "proc_start": proc_start,
                                "frame": frame, "agents": agents, "panel": panel,
                                "rich_box": False, "rich_plan": rich_plan,
                                "rich_tools": rich_tools,
                                "rich_tool_count": rich_tool_count})
                live_text = render_rich_live(
                    sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                    {"items": rich_tools, "count": rich_tool_count},
                    _effective_min_edit(sdir, work_st), frame=frame, state=work_st)
                msgid, mode_used = _try_rich_send_state(sdir, tg, work_st, live_text)
                st.update({k: v for k, v in work_st.items() if k.startswith("rich_")})
                rich_box = mode_used == "rich" and bool(msgid)
                if mode_used == "plain":
                    last_live = work_st.get("last_live")
                    last_edit = work_st.get("last_edit", now)
                elif msgid:
                    last_edit = now
                    last_live = work_st.get("last_live") or live_text
            elif rich_track and st.get("rich_failed") and rich_enabled(sdir):
                live_text = render_rich_live(
                    sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                    {"items": rich_tools, "count": rich_tool_count},
                    _effective_min_edit(sdir, st), fallback=True, frame=frame, state=st)
                msgid = tg.send(live_text, markdown=False)
                last_edit = now
                last_live = live_text
            elif rich_track and st.get("rich_failed"):
                msgid = None
            else:
                live_text = render_live(steps, frame, proc_start, agents, mission_now, panel)
                msgid = tg.send(live_text, markdown=False)
                last_edit = now
                last_live = live_text
            if msgid:


                box_send_record(sdir, proc_start)
                box_is_last = True
                deferred.append(("typing", None, None, False))
                spawn_animator(sdir)
                last_spawn = now
    else:


        anim_alive = _animator_lock_held(sdir)
        if not anim_alive:
            deferred.append(("typing", None, None, False))


        if not hidden and (now - last_edit) >= _effective_min_edit(sdir, st):


            subagent_event = bool(agent_id)
            if _animator_will_repaint(sdir, st, last_edit, now, alive=anim_alive) or\
                    (subagent_event and anim_alive and last_edit):
                pass
            else:
                if subagent_event and not anim_alive and\
                        (now - last_spawn) >= _effective_anim_interval(sdir, st):


                    spawn_animator(sdir)
                    last_spawn = now
                if rich_box:
                    live_text = render_rich_live(
                        sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                        {"items": rich_tools, "count": rich_tool_count},
                        _effective_min_edit(sdir, st), frame=frame, state=st)
                elif rich_track and st.get("rich_failed"):
                    live_text = render_rich_live(
                        sdir, steps, proc_start, agents, mission_now, panel, rich_plan,
                        {"items": rich_tools, "count": rich_tool_count},
                        _effective_min_edit(sdir, st), fallback=True, frame=frame,
                        state=st)
                else:
                    live_text = render_live(steps, frame, proc_start, agents,
                                            mission_now, panel)
                if live_text != last_live:
                    deferred.append(("redit" if rich_box else "edit",
                                     msgid, live_text, False))
                    if not rich_box:
                        last_live = live_text
                last_edit = now
        if (now - last_spawn) > 12:
            spawn_animator(sdir)
            last_spawn = now

    next_state = {
        "msgid": msgid, "steps": steps, "proc_start": proc_start,
        "last_edit": last_edit, "last_activity": now, "last_reply": last_reply,
        "last_react": last_react, "frame": frame, "last_spawn": last_spawn,
        "folded": folded, "last_live": last_live, "agents": agents,
        "next_agent_n": next_n, "ghosts": ghosts, "typing_done": False,
        "reanchor": reanchor, "box_is_last": box_is_last,
        "folded_above_msgid": folded_above, "replied_this_turn": replied_turn,
        "mission": mission, "panel": panel,
        "prev_recap_msgid": prev_recap_mid, "prev_recap_text": prev_recap_text,


        "noreply_grace_until": None,
    }
    if rich_track:
        for key, value in st.items():
            if key.startswith("rich_"):
                next_state[key] = value
        next_state["ctx_pct"] = st.get("ctx_pct")
        next_state["ctx_model"] = _context_model_label(st.get("ctx_model"))


        _store_context_effort(next_state, st.get("ctx_effort"))


        if st.get("ctx_pending_path"):
            next_state["ctx_pending_path"] = st.get("ctx_pending_path")
        next_state["agent_history"] = _normalize_agent_history(
            st.get("agent_history"), now=now)
        next_state.update({
            "rich_box": bool(rich_box and msgid),
            "rich_plan": rich_plan,
            "rich_tools": rich_tools[-RICH_TOOL_LINES:],
            "rich_tool_count": rich_tool_count,
            "folded_above_rich": folded_above_rich,
            "prev_recap_rich": prev_recap_rich,
        })
        if session_id is not None:
            next_state["rich_session_id"] = str(session_id)
    else:


        for key in ("rich_op_seq", "rich_op_latest"):
            if key in st:
                next_state[key] = st[key]


        if rich_plan:
            next_state["rich_plan"] = _plan_copy(rich_plan)
            next_state["rich_session_id"] = str(
                session_id if session_id is not None
                else st.get("rich_session_id") or "")
    _tag_rich_deferred(next_state, deferred)
    save_state(sdir, next_state)
    return deferred


def _stash_ghost(sdir, mid):
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        st = load_state(sdir)
        if st is not None:
            g = st.get("ghosts") or []
            if mid not in g:
                g.append(mid)
            st["ghosts"] = g[-10:]
            save_state(sdir, st)
        fcntl.flock(blf, fcntl.LOCK_UN)
        blf.close()
    except Exception:
        pass


def _repoint_above(sdir, mid, expect, rich=None):
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        st = load_state(sdir)
        if st and str(st.get("folded_above_msgid")) == str(expect) and not st.get("folded"):
            st["folded_above_msgid"] = mid
            if rich is not None:
                st["folded_above_rich"] = bool(rich)
            _unghost(st, mid)
            save_state(sdir, st)
        fcntl.flock(blf, fcntl.LOCK_UN)
        blf.close()
    except Exception:
        pass


def _rich_compound_refold(sdir, tg, kind, stale_mid, payload,
                          generation=None, targets=None):
    try:
        new_mid, fold_text = payload[:2]
    except Exception:
        return False
    if kind == "rrefold":
        new_rich, stale_rich = True, True
    else:
        try:
            new_rich, stale_rich = bool(payload[2]), bool(payload[3])
        except Exception:
            return False


    blf = None
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        if not _rich_op_current(load_state(sdir), generation, targets):
            return False
    except Exception:
        return False
    finally:
        if blf is not None:
            try:
                fcntl.flock(blf, fcntl.LOCK_UN)
                blf.close()
            except Exception:
                pass


    try:
        gone = tg.delete(stale_mid)
        if not gone:
            gone = tg.delete(stale_mid)
    except Exception:
        gone = False
    ok = False
    stale_ok = None
    rich_error = None
    removed = None
    if gone:
        try:
            if new_rich:
                normalized = _normalize_rich_result(
                    tg.edit_rich(new_mid, fold_text))
                ok = bool(normalized.get("ok"))
                if not ok:
                    rich_error = normalized
            else:
                ok = bool(tg.edit(new_mid, fold_text, markdown=True))
                if not ok:
                    ok = bool(tg.edit(new_mid, fold_text, markdown=True))
        except Exception:
            ok = False
            if new_rich:
                rich_error = {"ok": False, "error_class": "network"}
    else:


        if kind == "rrefold":
            try:
                normalized = _normalize_rich_result(
                    tg.edit_rich(stale_mid, fold_text))
                stale_ok = bool(normalized.get("ok"))
                if not stale_ok:
                    rich_error = normalized
            except Exception:
                stale_ok = False
                rich_error = {"ok": False, "error_class": "network"}
        try:
            removed = tg.delete(new_mid)
            if not removed:
                removed = tg.delete(new_mid)
        except Exception:
            removed = False

    blf = None
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
        st = load_state(sdir)
        if not _rich_op_current(st, generation, targets):
            return False
        dirty = False
        if rich_error:
            st.pop("rich_error_description", None)
            description = _bounded_api_description(
                rich_error.get("error_description"))
            if description:
                st["rich_error_description"] = description
            _append_rich_health(
                sdir, st, rich_error.get("error_class") or "malformed")
            dirty = True
        if gone:
            if not ok:
                _stash_ghost_state(st, new_mid)
                dirty = True
            else:
                _unghost(st, new_mid)
        else:
            if kind == "rrefold":
                if not stale_ok:
                    _stash_ghost_state(st, stale_mid)
                else:
                    _unghost(st, stale_mid)
                dirty = True
            if str(st.get("folded_above_msgid")) == str(new_mid)\
                    and not st.get("folded"):
                st["folded_above_msgid"] = stale_mid
                st["folded_above_rich"] = stale_rich
                _unghost(st, stale_mid)
                dirty = True
            if not removed:
                _stash_ghost_state(st, new_mid)
                dirty = True
        if dirty:
            save_state(sdir, st)
        return True
    except Exception:
        return False
    finally:
        if blf is not None:
            try:
                fcntl.flock(blf, fcntl.LOCK_UN)
                blf.close()
            except Exception:
                pass


def _run_deferred(sdir, tg, deferred):
    for op in (deferred or []):
        try:
            kind, mid, text, md = op[:4]
            generation = op[4] if len(op) > 4 else None
            targets = op[5] if len(op) > 5 else None
            if kind in ("edit", "recap"):
                ok = tg.edit(mid, text, markdown=md)
                if not ok:
                    ok = tg.edit(mid, text, markdown=md)
                if not ok and md and kind == "edit":
                    _stash_ghost(sdir, mid)
            elif kind == "refold":
                new_mid, fold_text = text
                gone = tg.delete(mid)
                if not gone:
                    gone = tg.delete(mid)
                if gone:
                    ok = tg.edit(new_mid, fold_text, markdown=True)
                    if not ok:
                        ok = tg.edit(new_mid, fold_text, markdown=True)
                    if not ok:
                        _stash_ghost(sdir, new_mid)
                else:


                    if not tg.edit(mid, fold_text, markdown=True):
                        tg.edit(mid, fold_text, markdown=True)
                    _repoint_above(sdir, mid, new_mid)
                    if not tg.delete(new_mid):
                        if not tg.delete(new_mid):
                            _stash_ghost(sdir, new_mid)
            elif kind in ("redit", "rfold", "rrecap"):
                intent = "live" if kind == "redit" else\
                    ("recap" if kind == "rrecap" else "fold")
                _rich_deferred_edit(sdir, tg, mid, text, live_fallback=True,
                                    intent=intent, generation=generation,
                                    targets=targets)
            elif kind == "rrefold":
                _rich_compound_refold(
                    sdir, tg, kind, mid, text, generation, targets)
            elif kind == "mixed_refold":
                _rich_compound_refold(
                    sdir, tg, kind, mid, text, generation, targets)
            elif kind == "delete":
                ok = tg.delete(mid)
                if not ok:
                    ok = tg.delete(mid)
                if not ok and md:
                    _stash_ghost(sdir, mid)
            elif kind == "typing":
                current = load_state(sdir)
                typer = getattr(tg, "typing_once", None)\
                    if _rich_state_present(current) else None
                typer() if callable(typer) else tg.typing()
        except Exception:
            pass


def mission_nudge_text(path):
    return (
        "[work-streamer] FIRST action this turn: write ONE line - your own words, "
        "present tense, what you are doing for this message - to %s. Keep it under 60 "
        "characters so the whole line reads at a glance, and never leave it cut off "
        "mid-sentence. It renders as the \U0001F3AF line on the live Telegram box.\n"
        "Example: printf '%%s' 'Fixing the recap position bug' > %s\n"
        "Rewrite the same file if the goal genuinely shifts mid-turn. Writes to this "
        "path are hidden from the box, so they cost no visual noise."
    ) % (path, path)


def emit_mission_nudge(sdir, prompt_text):
    try:
        t = (prompt_text or "").strip()
        if not t or t.startswith("/"):
            return False
        paths = _mission_paths(sdir)
        if not paths:
            return False
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": mission_nudge_text(paths[0]),
            }
        }))
        sys.stdout.flush()
        return True
    except Exception:
        return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == MODELS_REFRESH_MODE:


        try:
            signal.signal(signal.SIGALRM, lambda *_args: os._exit(1))
            signal.alarm(int(MODELS_REFRESH_TIMEOUT + 15))
        except Exception:
            pass
        _models_refresh_main(sys.argv[2] if len(sys.argv) > 2 else "")
        hard_exit()

    state_dir = os.environ.get("TELEGRAM_STATE_DIR", "")
    if not state_dir or not os.path.isdir(state_dir):
        hard_exit()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or read_env_token(
        os.path.join(state_dir, ".env"))
    if not token:
        hard_exit()
    chat_id = read_chat_id(os.path.join(state_dir, "access.json"))
    if not chat_id:
        hard_exit()
    if os.path.exists(os.path.join(state_dir, "stream.off")):
        hard_exit()

    sdir = os.path.join(state_dir, "stream")
    try:
        os.makedirs(sdir, exist_ok=True)
    except Exception:
        hard_exit()


    tg = Telegram(token, chat_id, os.environ.get("STREAM_DRY") == "1", sdir=sdir)

    if mode == "animate":
        run_animator(sdir, tg)
        hard_exit()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}


    budget = None
    if mode in ("pre", "post"):
        _tool = data.get("tool_name", "")
        if isinstance(_tool, str) and _tool and not _tool.startswith(PLUGIN_PREFIX):
            budget = LOCK_WAIT_TOOL * (
                LOCK_WAIT_POST_FACTOR if mode == "post" else 1.0)
    lf, locked = _acquire_state_lock(os.path.join(sdir, "lock"), budget)
    if not locked:


        hard_exit()

    deferred = []
    try:
        if mode == "prompt":


            ptext = data.get("prompt")
            if ptext is None:
                ptext = data.get("user_prompt") or data.get("message") or ""
            if not isinstance(ptext, str):
                ptext = ""


            s = ptext.lstrip()
            if s.startswith("<channel"):
                gt = s.find(">")
                end = s.rfind("</channel>")
                if gt != -1 and end != -1 and end > gt:
                    ptext = s[gt + 1:end].strip()


            emit_mission_nudge(sdir, ptext)
            deferred = on_prompt(sdir, tg, prompt_text=ptext,
                                 session_id=data.get("session_id") or data.get("sessionId"),
                                 transcript_path=data.get("transcript_path"),
                                 project_dir=data.get("cwd") or data.get("project_dir"))
        elif mode == "stop":

            deferred = record_reply(sdir, tg, final=True)
        elif mode in ("pre", "post"):
            tool = data.get("tool_name", "")
            tuid = data.get("tool_use_id")
            agent_id = data.get("agent_id") or data.get("agentId")
            agent_type = data.get("agent_type") or data.get("agentType")
            if tool == REACT_TOOL and mode == "post":
                deferred = on_react(sdir, tg)
            elif tool == REPLY_TOOL and mode == "post":


                deferred = record_reply(sdir, tg, final=False)
            elif (not tool) or tool.startswith(PLUGIN_PREFIX):
                deferred = []
            else:
                deferred = handle_step(sdir, tg, mode, tool,
                                       data.get("tool_input") or {}, tuid, agent_id, agent_type,
                                       tresponse=data.get("tool_response"),
                                       session_id=data.get("session_id") or data.get("sessionId"))
    finally:
        if lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
                lf.close()
            except Exception:
                pass

    _run_deferred(sdir, tg, deferred)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    hard_exit()
