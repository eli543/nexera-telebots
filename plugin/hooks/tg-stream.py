#!/usr/bin/env python3
"""
Telegram live work-streamer hook for Claude Code channel bots.

ONE box per prompt. The box appears the INSTANT the user messages (on the
reaction) and stays a LIVE, MOVING "👾 Working" box - a sliding bar and a timer
that keeps climbing - the ENTIRE time work is happening. It never freezes to a
static state. Finished steps show ✓, the in-flight one ⏳, subagents render as a
numbered tree. The box leaves this moving state ONLY by FOLDING, in place, into
Telegram's expandable "📋 What I did · tap to expand" dropdown when the reply goes
out (or being deleted if the turn did no work). Native "typing…" stays on the
whole time too.

There are exactly TWO visible states: moving+counting while working, then the
folded tap-to-expand dropdown. No frozen "Finishing" in between (the owner
rejects paused timers), and it never keeps counting after the turn is over.

GUARANTEES: acts only inside a channel bot session (env-gated); zero stdout /
zero model tokens; never raises or blocks a turn; per-bot isolated; the
<state_dir>/stream.off kill switch stops new hook events AND any running
heartbeat within one tick.

Modes (argv[1]): "prompt" | "pre" | "post" | "stop" | "animate".
State: <TELEGRAM_STATE_DIR>/stream/state.json, guarded by a file lock.
"""

import sys
import os
import json
import time
# --- cross-platform file-lock shim ------------------------------------------
# POSIX/macOS (the supported target) import fcntl and get authoritative kernel
# advisory locks - behavior is byte-identical to the original hook. If fcntl is
# absent (Windows, EXPERIMENTAL / untested), fall back to a portable
# O_CREAT|O_EXCL lockfile that emulates the exact flock() subset this file uses:
# LOCK_EX (blocking), LOCK_EX|LOCK_NB (raise BlockingIOError when already held),
# and LOCK_UN. Each fcntl.flock(fileobj, ...) call keys off fileobj.name and
# manages a companion "<name>.winlock" file holding the owner PID; a stale lock is
# reclaimed only when its owner PID is gone (or, as a backstop, once it is older
# than _WINLOCK_STALE seconds - set above the animator's 2h max life so a live,
# long-lived hold is never stolen). The single-animator guarantee therefore holds
# as long as PID liveness can be determined, which it can on both POSIX and NT.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows experimental fallback path
    import errno

    class fcntl(object):  # noqa: N801 - deliberate drop-in for the fcntl module
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8
        _WINLOCK_STALE = 2.0 * 3600 + 300.0

        @staticmethod
        def _alive(pid):
            if pid <= 0:
                return False
            if os.name == "nt":
                try:
                    import ctypes
                    query = 0x1000            # PROCESS_QUERY_LIMITED_INFORMATION
                    still_active = 259        # STILL_ACTIVE
                    k = ctypes.windll.kernel32
                    h = k.OpenProcess(query, False, int(pid))
                    if not h:
                        return False
                    try:
                        code = ctypes.c_ulong()
                        if k.GetExitCodeProcess(h, ctypes.byref(code)):
                            return code.value == still_active
                        return True
                    finally:
                        k.CloseHandle(h)
                except Exception:
                    return True  # can't tell -> assume alive (never steal wrongly)
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except Exception:
                return True
            return True

        @classmethod
        def _reclaimable(cls, lockpath):
            try:
                with open(lockpath, "r") as f:
                    pid = int((f.read().strip() or "0"))
            except Exception:
                pid = 0
            if pid and not cls._alive(pid):
                return True
            try:
                return (time.time() - os.path.getmtime(lockpath)) > cls._WINLOCK_STALE
            except Exception:
                return False

        @classmethod
        def flock(cls, fileobj, flags):
            path = getattr(fileobj, "name", None)
            if not path:
                return
            lockpath = str(path) + ".winlock"
            if flags & cls.LOCK_UN:
                try:
                    os.unlink(lockpath)
                except OSError:
                    pass
                return
            nonblocking = bool(flags & cls.LOCK_NB)
            while True:
                try:
                    fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, str(os.getpid()).encode())
                    finally:
                        os.close(fd)
                    return
                except FileExistsError:
                    if cls._reclaimable(lockpath):
                        try:
                            os.unlink(lockpath)
                        except OSError:
                            pass
                        continue
                    if nonblocking:
                        raise BlockingIOError(errno.EWOULDBLOCK, "locked")
                    time.sleep(0.1)
import subprocess
from urllib import request, parse
from urllib.error import HTTPError

# ----- tunables (env-overridable) -----
WORKING_EMOJI = os.environ.get("STREAM_EMOJI", "\U0001F47E")  # 👾
THINKING_EMOJI = os.environ.get("STREAM_THINK_EMOJI", "\U0001F4AD")  # 💭 thought balloon
MIN_EDIT_INTERVAL = float(os.environ.get("STREAM_MIN_EDIT", "2.5"))
STALE_SECONDS = float(os.environ.get("STREAM_STALE", "240"))
ANIM_INTERVAL = float(os.environ.get("STREAM_ANIM", "3"))
ANIM_MAX_RUN = float(os.environ.get("STREAM_ANIM_MAX", "7200"))   # ultimate backstop fold (2 h)
ANIM_MAX_IDLE = float(os.environ.get("STREAM_ANIM_IDLE", "1800")) # ultimate backstop fold (30 min)
MAX_STORED_STEPS = 600
TG_HARD_LIMIT = 4000   # safe margin under Telegram's 4096 UTF-16 code-unit cap
LIVE_CAP = 100         # max chars per live line (phone width); the final summary spells things out
LIVE_STREAM = 8        # how many recent actions to show flowing in the live view
HTTP_TIMEOUT = 3   # bounded so a send under the lock can't approach the hook timeout
BAR_CELLS = 5
BAR_WIN = 1
BAR_FILL = "▪︎"    # small black square, text-presentation (locked small)
BAR_EMPTY = "▫︎"   # small white square, text-presentation
SUB = "  ↳ "            # indent + arrow for subagent tree lines

# INVARIANT: the sum of worst-case Telegram._call durations that EVER run while
# main() holds 'lock' must stay < the settings.json hook timeout (20s). The only
# under-lock network calls are on_react's tg.typing()+tg.send() (opening the box
# instantly), handle_step's cold-start tg.send(), handle_step's re-anchor tg.send()
# (one send that moves the live box to the bottom after an intermediate reply; these
# two handle_step sends are mutually exclusive - cold-start runs only when there is
# no msgid, re-anchor only when there is one), and record_reply's FINAL Done-fold (a
# single in-place tg.edit() of the box at its existing msgid, so the recap stays where
# the box was, ABOVE the answer, leaving the answer the last message). The intermediate
# record_reply edge (final=False) does NO network under the lock - it only flips flags.
# At most one of these events runs per hook invocation, and each call is bounded
# (HTTP_TIMEOUT + capped 429 sleep + retry ~= 8s worst case, well < 20s; the only
# two-call event is on_react's typing+send ~= 16s, still < 20s). Everything else
# (live edits, deletes, extra typing) is DEFERRED.

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
    """Length in Telegram's UTF-16 code units (emoji count as 2)."""
    try:
        return len(s.encode("utf-16-le")) // 2
    except Exception:
        return len(s)


def _utf16_clip(s, max_units):
    """Truncate s to at most max_units UTF-16 code units without splitting a
    surrogate pair or leaving a dangling MarkdownV2 escape backslash."""
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
    """The active line when NO tool is in-flight but the turn is still live: a
    thought-balloon + 'thinking' with dots that cycle each frame so the line
    itself visibly ANIMATES (on top of the header bar sliding and the timer
    climbing). It is a transient STATE, never a step - render_final never emits
    it, so it can't show up as a ✓ item in the folded dropdown."""
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


def caption_of(tool, tinput, agent_id):
    t = tool or ""
    ti = tinput if isinstance(tinput, dict) else {}
    if t in ("Task", "Agent") and not agent_id:
        return short(ti.get("description") or "a task", 240)
    if t == "Bash":
        desc = ti.get("description")
        return short(desc, 280) if desc else short(ti.get("command") or "running a command", 220)
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


# ----- state -----
def load_state(sdir):
    try:
        with open(os.path.join(sdir, "state.json"), "r") as f:
            return json.load(f)
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


def fresh_state(now, ghosts=None):
    """A brand new single-box process. `ghosts` carries forward any box ids whose
    history-collapse edit failed, so a later pass can retry them.

    `reanchor` is the flag that lets the live box TRACK to the bottom of the chat
    after an intermediate (non-final) reply. record_reply(final=False) sets it True
    and leaves the box live; the next handle_step consumes it by deleting the old
    box and re-sending a fresh live box at the bottom (below the intermediate reply)
    so the working box never strands ABOVE newer messages. It defaults False on every
    fresh box, so it never leaks across prompts."""
    return {
        "msgid": None, "steps": [], "proc_start": now,
        "last_edit": 0, "last_activity": 0, "last_reply": 0, "last_react": now,
        "last_spawn": now, "frame": 0, "folded": False, "last_live": None,
        "agents": {}, "next_agent_n": 1, "ghosts": ghosts or [], "typing_done": False,
        "reanchor": False,
    }


def all_steps_done(steps):
    return bool(steps) and all(s.get("done") for s in (steps or []))


# ----- rendering -----
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


def render_live(steps, frame, start, agents):
    """The single live box. It is ALWAYS the moving '👾 Working' state: a sliding
    bar and a LIVE, climbing timer (time.time()-start, recomputed every render) -
    it never freezes while the turn is live. ✓ marks finished steps as they stream,
    ⏳ the in-flight one, subagents render as a numbered tree. When no tool is
    in-flight (no steps yet at the start, or all steps done) the active line is an
    animated '💭 thinking…' line instead, so it visibly reads as thinking, never
    stuck; the hourglass tool line and the thinking line are mutually exclusive.
    The box only leaves this state by FOLDING to the dropdown (render_final) on the
    reply - the thinking line is a transient state and never appears there."""
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
                    short(s2.get("cap") or "working", LIVE_CAP)))
            if not order and spawns:
                for t in spawns[:4]:
                    rows.append("%s⏳ 🤖 %s" % (SUB, short(t, LIVE_CAP)))
                    shown_inflight = True
        else:
            done1 = s.get("done")
            if not done1:
                shown_inflight = True
            rows.append("%s %s" % ("✓" if done1 else "⏳", short(s.get("cap") or "working", LIVE_CAP)))
            k += 1
    if not shown_inflight:
        # No tool is in-flight in the visible window. A still-running step OUTSIDE
        # the window keeps the hourglass tool line; only when nothing at all is
        # running (no steps yet at the very start, or every step already done) does
        # the active line become the animated THINKING line. Hourglass-tool and
        # thinking are mutually exclusive - exactly one active line, always moving.
        oldest = next((s for s in steps if not s.get("done")), None)
        if oldest is not None:
            rows.append("⏳ " + short(oldest.get("cap") or "working", LIVE_CAP))
        else:
            rows.append(thinking_line(frame))
    return "\n".join(rows)


def _assemble_final(head_lines, detail_lines, header, omitted):
    body_lines = list(head_lines)
    if omitted:
        body_lines.append(escape_mdv2("…earlier omitted"))
    body_lines.extend(detail_lines)
    if not body_lines:
        body_lines = [escape_mdv2("…")]
    body = [("**>" if k == 0 else ">") + ln for k, ln in enumerate(body_lines)]
    body[-1] = body[-1] + "||"
    return header + "\n" + "\n".join(body)


def render_final(steps, start, n_agents=0):
    """The collapsed history form: '📋 What I did · M:SS · tap to expand' with an
    expandable body that spells out every step (each prefixed with a ✓), including
    each subagent's title AND activity, clamped to Telegram's cap (drop OLDEST).

    n_agents is the count of DISTINCT subagents this turn (len(state['agents']),
    which handle_step records per agent id and never prunes). Only the subagent
    tally in the summary line uses it; every other family tally is unchanged (they
    intentionally reflect main-thread tools only). It defaults to 0 so any caller
    that omits it falls back to today's subtitle/run-count heuristic - never a
    crash, never a wrong number for a no-subagent turn (the helpers branch only
    runs when helpers steps exist)."""
    if not steps:
        return None, 0
    order = _families_in_order(steps)
    parts = []
    for f in order:
        if f == "helpers":
            # ACCURATE distinct-subagent count. state['agents'] (n_agents) is the
            # source of truth: it is keyed by agent id and survives the steps[]
            # truncation, so it stays correct even when 600-step eviction has
            # dropped spawn markers or early subwork lines. Distinct non-empty
            # aids still present in steps and the subtitle-marker count are kept
            # as belt-and-suspenders; take the largest. Only if all three are 0
            # (degenerate: helpers-family steps with no aid, no subtitle, and an
            # empty agents dict) fall back to the contiguous-run heuristic so the
            # line never reads '0 subagents'.
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
            detail.append("✓ " + fam_get("helpers")[0] + " Subagents")
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
            detail.append("✓ " + fam_get(fam)[0] + " " + (s.get("cap") or fam_get(fam)[2]))
            i += 1

    head_lines = [escape_mdv2(summary)] if len(detail) > 1 else []
    esc_detail = [escape_mdv2(x) for x in detail]

    took = (" · " + fmt_elapsed(time.time() - start)) if start else ""
    header = escape_mdv2("\U0001F4CB What I did" + took + " · tap to expand")

    kept = list(esc_detail)
    omitted = False
    while True:
        msg = _assemble_final(head_lines, kept, header, omitted)
        if tg_len(msg) <= TG_HARD_LIMIT or not kept:
            break
        kept.pop(0)
        omitted = True

    msg = _assemble_final(head_lines, kept, header, omitted)
    if tg_len(msg) > TG_HARD_LIMIT:
        msg = _utf16_clip(msg, TG_HARD_LIMIT - 2)
        if not msg.endswith("||"):
            msg += "||"
    return msg, len(steps)


# ----- Telegram -----
class Telegram:
    def __init__(self, token, chat_id, dry):
        self.base = "https://api.telegram.org/bot%s/" % token
        self.chat = str(chat_id)
        self.dry = dry

    def _call(self, method, params, _retry=True):
        if self.dry:
            sys.stderr.write("[DRY] %s %s\n" % (method, json.dumps(params)[:700]))
            return {"ok": True, "result": {"message_id": 4242}}
        try:
            body = parse.urlencode(params).encode()
            req = request.Request(self.base + method, data=body)
            with request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            if e.code == 429 and _retry:
                try:
                    info = json.loads(e.read().decode())
                    ra = int(info.get("parameters", {}).get("retry_after", 1))
                except Exception:
                    ra = 1
                time.sleep(min(ra, 2))
                return self._call(method, params, _retry=False)
            return None
        except Exception:
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

    def edit(self, msgid, text, markdown=False):
        p = {"chat_id": self.chat, "message_id": str(msgid), "text": text}
        if markdown:
            p["parse_mode"] = "MarkdownV2"
        r = self._call("editMessageText", p)
        return bool(r and r.get("ok"))

    def delete(self, msgid):
        r = self._call("deleteMessage", {"chat_id": self.chat, "message_id": str(msgid)})
        return bool(r and r.get("ok"))

    def typing(self):
        self._call("sendChatAction", {"chat_id": self.chat, "action": "typing"})


# ----- heartbeat -----
def _animator_lock_held(sdir):
    """Best-effort probe: is an animator currently holding the exclusive anim.lock
    for this state dir? Tries a NON-BLOCKING LOCK_EX on the same anim.lock file the
    animator guards itself with; if it cannot be taken, an animator is live. The
    probe lock is released immediately (we only test, never hold). Returns True only
    when we positively observed the lock as held; on any error -> False so we fall
    through to spawn (the animator's own startup flock stays the authoritative guard,
    so a false 'not held' here can at worst spawn one extra process that will itself
    acquire-fail and exit - it can never produce two live animators)."""
    probe = None
    try:
        probe = open(os.path.join(sdir, "anim.lock"), "w")
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        # could not take it -> a live animator holds it (BlockingIOError) OR we
        # failed to open it; treat only a lock failure as "held". An open failure
        # also lands here, but then there is no anim.lock to contend on, so the
        # spawned animator will simply (re)create+lock it - still single-survivor.
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass
            return True
        return False
    # acquired -> NO animator holds it; release at once (we never hold the probe).
    try:
        fcntl.flock(probe, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        probe.close()
    except Exception:
        pass
    return False


def spawn_animator(sdir=None):
    if os.environ.get("STREAM_NO_SPAWN") == "1":
        return
    # Best-effort pre-check: if an animator already holds anim.lock for this state
    # dir, do NOT launch a second (doomed) process - it would only acquire-fail and
    # exit, churning processes and wasting work. This is advisory only; the
    # animator's own LOCK_EX|LOCK_NB on anim.lock at startup (run_animator) is the
    # AUTHORITATIVE guarantee that at most one animator ever runs per state dir.
    if sdir and _animator_lock_held(sdir):
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "animate"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _fold_to_done(sdir, tg):
    """Collapse the single box into its expandable '📋 What I did' dropdown. Only
    the animator's ultimate-backstop paths call this; the normal fold happens
    synchronously in record_reply on the reply edge."""
    try:
        blf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(blf, fcntl.LOCK_EX)
    except Exception:
        return
    try:
        st = load_state(sdir)
        if not st or st.get("folded"):
            return
        msgid = st.get("msgid")
        if not msgid:
            st["folded"] = True
            save_state(sdir, st)
            return
        text, _ = render_final(st.get("steps") or [], st.get("proc_start"),
                               len(st.get("agents") or {}))
        ok = False
        if text:
            try:
                ok = tg.edit(msgid, text, markdown=True)
            except Exception:
                ok = False
        if ok:
            st["folded"] = True
            st["last_live"] = text
            save_state(sdir, st)
    finally:
        try:
            fcntl.flock(blf, fcntl.LOCK_UN)
            blf.close()
        except Exception:
            pass


def run_animator(sdir, tg):
    off_path = os.path.join(os.path.dirname(sdir), "stream.off")
    # AUTHORITATIVE single-animator guard: take an EXCLUSIVE, NON-BLOCKING flock on a
    # dedicated anim.lock and HOLD it for this animator's entire life. If another
    # animator already holds it, LOCK_NB raises (BlockingIOError) and we exit
    # immediately and silently - so a second animator can NEVER run the loop
    # concurrently for the same state dir. The lock is on the open file description;
    # it is released automatically when this process dies (or via the finally below),
    # which frees the next turn to spawn a fresh animator. We open the lock file
    # first and ONLY treat a flock failure as "another animator owns it" so the guard
    # is unambiguous (an open() error is the rare exception and also just aborts).
    try:
        alf = open(os.path.join(sdir, "anim.lock"), "w")
    except Exception:
        return
    try:
        fcntl.flock(alf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        # another animator holds the lock (or locking is unavailable) -> this is the
        # second animator; exit at once so only the existing one survives.
        try:
            alf.close()
        except Exception:
            pass
        return
    started = time.time()
    killed = False
    try:
        while True:
            if os.path.exists(off_path):
                killed = True
                break
            st = load_state(sdir)
            if not st or st.get("folded"):
                break          # box folded to the dropdown -> done
            msgid = st.get("msgid")
            if not msgid:
                break          # box deleted / not opened -> nothing to animate
            now = time.time()
            la = st.get("last_activity", 0)
            lr = st.get("last_reply", 0)
            lrx = st.get("last_react", 0)
            last_touch = max(la, lr, lrx)
            if (now - started) > ANIM_MAX_RUN:
                _fold_to_done(sdir, tg)
                break
            if last_touch and (now - last_touch) > ANIM_MAX_IDLE:
                _fold_to_done(sdir, tg)
                break

            # CONTINUOUS native typing while the turn is live (no time cap) - stops
            # only once the reply is out (typing_done) or the box folds.
            if not st.get("typing_done"):
                try:
                    tg.typing()
                except Exception:
                    pass

            # tick the box every cycle: the bar slides and the timer climbs, so the
            # box is ALWAYS visibly moving while the turn is live (never frozen).
            do_edit = None
            try:
                blf = open(os.path.join(sdir, "lock"), "w")
                fcntl.flock(blf, fcntl.LOCK_EX)
                st2 = load_state(sdir)
                if st2.get("msgid") and not st2.get("folded") and \
                        (time.time() - st2.get("last_edit", 0)) >= MIN_EDIT_INTERVAL:
                    fr = st2.get("frame", 0) + 1
                    nl = render_live(st2.get("steps") or [], fr, st2.get("proc_start"),
                                     st2.get("agents") or {})
                    st2["frame"] = fr
                    st2["last_edit"] = time.time()
                    if nl != st2.get("last_live"):
                        do_edit = (st2["msgid"], nl)
                        st2["last_live"] = nl
                    save_state(sdir, st2)
                fcntl.flock(blf, fcntl.LOCK_UN)
                blf.close()
            except Exception:
                pass
            if do_edit:
                st3 = load_state(sdir)
                if st3.get("msgid") == do_edit[0] and not st3.get("folded"):
                    try:
                        tg.edit(do_edit[0], do_edit[1], markdown=False)
                    except Exception:
                        pass
            time.sleep(ANIM_INTERVAL)
    finally:
        try:
            if not killed:
                st = load_state(sdir)
                if st and st.get("msgid") and not st.get("folded") and st.get("steps"):
                    _fold_to_done(sdir, tg)
        except Exception:
            pass
        try:
            fcntl.flock(alf, fcntl.LOCK_UN)
            alf.close()
        except Exception:
            pass


# ----- lifecycle -----
def _collapse_ops(msgid, steps, start, n_agents=0):
    if not msgid:
        return []
    if steps:
        htext, _ = render_final(steps, start, n_agents)
        if htext:
            return [("edit", msgid, htext, True)]
    return [("delete", msgid, None, False)]


def _collapse_prior(st):
    """Deferred ops to leave the PREVIOUS prompt's box as a folded summary (or
    delete it if it had no work), plus cleanup of any failed-collapse ghosts. If
    the prior box was ALREADY folded (the normal Stop/reply fold already ran), there
    is nothing to collapse - just sweep ghosts. Shared by on_prompt and on_react."""
    deferred = []
    if not st.get("folded"):
        deferred = _collapse_ops(st.get("msgid"), st.get("steps") or [],
                                 st.get("proc_start"), len(st.get("agents") or {}))
    for g in (st.get("ghosts") or []):
        deferred.append(("delete", g, None, False))
    return deferred


def _is_fresh_open_box(st, now=None):
    """True when the current box is the one on_prompt JUST opened for this prompt:
    a live (un-folded) box with a msgid, zero steps yet, and a very recent
    proc_start. Used so on_react does NOT collapse+reopen the box on_prompt already
    opened (which would create a second box for the same prompt)."""
    if not st or not st.get("msgid") or st.get("folded"):
        return False
    if st.get("steps"):
        return False
    ps = st.get("proc_start")
    if not ps:
        return False
    now = time.time() if now is None else now
    return (now - ps) < STALE_SECONDS


def _open_fresh_box(sdir, tg, ghosts=None):
    """Open a brand-new live box RIGHT NOW: fresh state (proc_start = now), instant
    native typing, the moving '👾 Working · bar · 0:00' line (an empty box renders
    the thinking indicator), msgid stored, animator spawned. Shared by on_prompt
    (the primary per-prompt boundary) and on_react (its fallback open path)."""
    now = time.time()
    fresh = fresh_state(now, ghosts=ghosts)
    try:
        tg.typing()                 # instant native typing
    except Exception:
        pass
    mid = None
    try:
        live = render_live([], 0, now, {})    # "👾 Working · bar · 0:00" - empty, moving
        mid = tg.send(live, markdown=False)
    except Exception:
        mid = None
    if mid:
        fresh["msgid"] = mid
        fresh["last_live"] = live
        fresh["last_edit"] = now
    save_state(sdir, fresh)
    spawn_animator(sdir)


def on_prompt(sdir, tg, prompt_text=""):
    """UserPromptSubmit - the ONE event that fires exactly once per user message,
    BEFORE any tool or react. This is the deterministic per-prompt boundary that
    on_react / record_reply / the gap-heuristic only ever APPROXIMATED (and missed
    when the bot didn't react first AND a prior task kept the box 'active', merging
    two prompts into one ever-climbing box).

    On every (non-slash, non-empty) user prompt it:
      1. FINALIZES/FOLDS the previous prompt's box if one exists (reuse the same
         _collapse_ops history-collapse on_react uses), so a missed Stop/reply fold
         can never leave a prior box live - it is left as a folded summary dropdown.
      2. OPENS A FRESH BOX immediately (fresh proc_start = now, instant typing, the
         moving 0:00 Working line, animator), so every message gets its OWN box and
         OWN reset timer, and the box+typing appear the instant the prompt lands.

    Slash-commands (e.g. /fresh, which restarts the session) get NO box - it would
    be orphaned by the restart. Empty / missing prompt text is skipped the same way,
    tolerantly. Non-blocking, exit-0; mirrors the deferred-ops contract of pre/post/
    stop/react (the OLD box's collapse is deferred past the lock, the NEW box's
    send + typing run inline so they land on the same instant beat as the prompt)."""
    text = (prompt_text or "").strip()
    if not text or text.startswith("/"):
        return []   # slash-command or empty -> open nothing (avoid an orphaned box)
    st = load_state(sdir)
    deferred = _collapse_prior(st)
    _open_fresh_box(sdir, tg, ghosts=[])
    return deferred


def on_react(sdir, tg):
    """A reaction = a new prompt arrived. Collapse the PREVIOUS prompt's box into
    its history dropdown, then OPEN A FRESH BOX IMMEDIATELY (it must appear the
    instant the user messages, not wait for the first tool) and start native typing.
    The collapse of the OLD box is deferred past the lock; the NEW box send + the
    typing run inline so the box+typing land on the same instant beat as the react.

    IDEMPOTENT WITH on_prompt: on_prompt (UserPromptSubmit) now opens the box BEFORE
    the bot's react fires, so usually a fresh empty box already exists when this
    runs. If the current box is that freshly-opened one (live, zero steps, just-now
    proc_start), this react must NOT collapse+reopen it - that would orphan the fresh
    box and open a SECOND one (double box). In that case keep the box exactly as-is
    and only re-assert typing. If there is NO current box (UserPromptSubmit somehow
    didn't fire), on_react keeps its full fallback behavior: collapse whatever was
    there and open a fresh box - this open path stays as defense-in-depth."""
    st = load_state(sdir)
    if _is_fresh_open_box(st):
        # on_prompt already opened this prompt's box; don't double it. Keep native
        # typing warm and refresh last_react so the gap-heuristic stays calm.
        st["last_react"] = time.time()
        save_state(sdir, st)
        try:
            tg.typing()
        except Exception:
            pass
        return []
    deferred = _collapse_prior(st)
    _open_fresh_box(sdir, tg, ghosts=[])
    return deferred


def record_reply(sdir, tg, final=False):
    """A reply went out, or the turn stopped. This is called from TWO edges that look
    identical at the instant they fire but mean different things:

      * REPLY_TOOL PostToolUse  -> final=False. A reply was just sent. It could be an
        intermediate ack ("on it…") with more work still coming, OR it could be the
        final answer - we CANNOT tell yet. So we do NOT end the box: we leave it LIVE
        (no fold), leave native typing ON (do NOT set typing_done), and only arm a
        re-anchor so that when work resumes the box jumps to the bottom of the chat,
        below this reply. This is what stops the box stranding ABOVE later messages
        (symptom #3) and what keeps the chat from going dark during a post-reply
        thinking stretch (symptom #4): because we never fold here, the animator spawned
        at on_prompt keeps running uninterrupted - it keeps the box moving and typing
        alive straight through to the real end of the turn. last_reply=now also keeps
        the animator's idle backstop from firing during long post-reply thinking.

      * Stop hook                -> final=True. The turn is TRULY over. NOW we fold the
        box into its expandable '📋 What I did' dropdown and stop typing (typing_done).

    POSITIONING ON THE FINAL FOLD (final=True): the box was opened on UserPromptSubmit
    and has been tracking to the bottom on each resume, so by the time the final answer
    is sent it sits just ABOVE that answer (no tool fires after the final reply, so no
    further re-anchor moves it below). We fold IN PLACE: a single editMessageText at the
    box's existing msgid. The msgid does NOT change, so the folded summary stays exactly
    where the box was - just above the answer - leaving the answer the LAST message in
    the chat (so the chat-list preview and the line above the keyboard show the real
    answer, not 'What I did'). A no-work turn (no steps) just deletes its empty box.

    Folding/arming is synchronous under main's lock so the next prompt can't race it."""
    st = load_state(sdir)
    if not st:
        return []
    now = time.time()
    st["last_reply"] = now
    deferred = []
    msgid = st.get("msgid")

    if not final:
        # Intermediate reply: keep the box LIVE and typing ON. Arm a re-anchor so the
        # next tool moves the box below this reply. Do nothing if there's no live box
        # (a no-work intermediate reply with no box just leaves nothing to track).
        if msgid and not st.get("folded"):
            st["reanchor"] = True
        save_state(sdir, st)
        return deferred

    # final=True: the turn is over. Stop typing and fold (or delete an empty box).
    st["typing_done"] = True
    st["reanchor"] = False
    if msgid and not st.get("folded"):
        if st.get("steps"):
            text, _ = render_final(st.get("steps"), st.get("proc_start"),
                                   len(st.get("agents") or {}))
            if text:
                # Fold IN PLACE: edit the existing box at its current msgid. The
                # msgid is unchanged so the recap stays above the already-sent
                # answer, leaving the answer the last message in the chat.
                ok = False
                try:
                    ok = tg.edit(msgid, text, markdown=True)
                except Exception:
                    ok = False
                if ok:
                    st["folded"] = True
                    st["last_live"] = text
        else:
            deferred.append(("delete", msgid, None, False))
            st["msgid"] = None
    save_state(sdir, st)
    return deferred


def handle_step(sdir, tg, mode, tool, tinput, tuid, agent_id, agent_type):
    now = time.time()
    deferred = []
    st = load_state(sdir)
    msgid = st.get("msgid")
    steps = st.get("steps") or []
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
    ghosts = st.get("ghosts") or []
    reanchor = st.get("reanchor", False)

    # NO gap-based collapse+reopen here. The deterministic per-prompt boundary is
    # on_prompt (UserPromptSubmit), which folds the prior box and opens a fresh one
    # on every real user message. A long turn (subagent runs, long shell commands,
    # deep thinking) routinely has multi-minute gaps WITHIN a single turn; a gap
    # check would shatter that one turn into several "What I did" boxes. The only
    # thing we do on a resumed event is REVIVE a box an animator backstop folded -
    # in place, same msgid, never a new box. (last_activity/last_react are still
    # loaded above because they are carried forward in save_state; they just no
    # longer drive a fold here.)
    if folded and msgid:
        # box was folded by an animator backstop but work RESUMED in the same
        # prompt -> un-fold and continue in the SAME box. last_edit=0 forces the
        # first resumed render to fire immediately (not throttled by
        # MIN_EDIT_INTERVAL), so the box flips back from the folded dropdown to the
        # live moving form on the very next tool event.
        folded = False
        last_edit = 0

    if proc_start is None:
        proc_start = now
    folded = False

    fam = family_of(tool, agent_id)
    cap = caption_of(tool, tinput, agent_id)
    k = step_kind(tool, agent_id)
    if agent_id and agent_id not in agents:
        agents[agent_id] = {"n": next_n, "type": agent_type or "agent"}
        next_n += 1

    frame += 1
    if mode == "pre":
        steps.append({"fam": fam, "cap": cap, "tuid": tuid or "", "done": False,
                      "k": k, "aid": agent_id or ""})
    else:
        flipped = False
        if tuid:
            for s in steps:
                if not s.get("done") and s.get("tuid") == tuid:
                    s["done"] = True
                    flipped = True
                    break
        else:
            for s in reversed(steps):
                if not s.get("done") and not s.get("tuid"):
                    s["done"] = True
                    flipped = True
                    break
        # unmatched post (unknown/stale id) is dropped - never steal another step's check

    if len(steps) > MAX_STORED_STEPS:
        steps = steps[len(steps) - MAX_STORED_STEPS:]

    # RE-ANCHOR: an intermediate reply went out since the box was last shown
    # (record_reply set reanchor=True without folding). Work has now resumed, so the
    # live box - still pinned where it opened, now ABOVE that reply - must jump to the
    # BOTTOM of the chat. Send a FRESH live box at its new position (one inline send,
    # honoring the <20s under-lock budget) and DEFER-delete the old one so the old box
    # vanishes after the lock releases. The new send reflects the step we just recorded.
    # The animator's reload-guard (it re-checks msgid before every edit) makes it follow
    # the new msgid and harmlessly skip the old one. Only re-anchor a live (un-folded)
    # box that actually exists; clear the flag either way so it fires exactly once.
    if reanchor and msgid and not folded:
        old_mid = msgid
        live_text = render_live(steps, frame, proc_start, agents)
        new_mid = tg.send(live_text, markdown=False)
        if new_mid:
            deferred.append(("delete", old_mid, None, False))
            msgid = new_mid
            last_edit = now
            last_live = live_text
            spawn_animator(sdir)        # ensure the animator is alive for the new box
            last_spawn = now
            reanchor = False            # consumed -> fire exactly once on success
        # if the send failed: keep the OLD box (never strand a deleted msgid) and leave
        # reanchor=True so the NEXT tool retries the re-anchor.
    else:
        reanchor = False                # no live box to move (or already folded) -> drop it

    if not msgid:
        if steps:
            # Cold start (a tool fired without a preceding react opening the box).
            live_text = render_live(steps, frame, proc_start, agents)
            msgid = tg.send(live_text, markdown=False)
            last_edit = now
            last_live = live_text
            if msgid:
                deferred.append(("typing", None, None, False))
                spawn_animator(sdir)
                last_spawn = now
    else:
        deferred.append(("typing", None, None, False))   # keep native typing fresh on every tool
        if (now - last_edit) >= MIN_EDIT_INTERVAL:
            live_text = render_live(steps, frame, proc_start, agents)
            if live_text != last_live:
                deferred.append(("edit", msgid, live_text, False))
                last_live = live_text
            last_edit = now
        if (now - last_spawn) > 12:
            spawn_animator(sdir)
            last_spawn = now

    save_state(sdir, {
        "msgid": msgid, "steps": steps, "proc_start": proc_start,
        "last_edit": last_edit, "last_activity": now, "last_reply": last_reply,
        "last_react": last_react, "frame": frame, "last_spawn": last_spawn,
        "folded": folded, "last_live": last_live, "agents": agents,
        "next_agent_n": next_n, "ghosts": ghosts, "typing_done": False,
        "reanchor": reanchor,
    })
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


def _run_deferred(sdir, tg, deferred):
    for kind, mid, text, md in (deferred or []):
        try:
            if kind == "edit":
                ok = tg.edit(mid, text, markdown=md)
                if not ok:
                    ok = tg.edit(mid, text, markdown=md)
                if not ok and md:
                    _stash_ghost(sdir, mid)
            elif kind == "delete":
                ok = tg.delete(mid)
                if not ok:
                    tg.delete(mid)
            elif kind == "typing":
                tg.typing()
        except Exception:
            pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

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

    tg = Telegram(token, chat_id, os.environ.get("STREAM_DRY") == "1")

    if mode == "animate":
        run_animator(sdir, tg)
        hard_exit()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    lf = None
    try:
        lf = open(os.path.join(sdir, "lock"), "w")
        fcntl.flock(lf, fcntl.LOCK_EX)
    except Exception:
        lf = None

    deferred = []
    try:
        if mode == "prompt":
            # UserPromptSubmit: the deterministic per-prompt boundary. Read the
            # prompt text from the event JSON (tolerant of a missing field) so we
            # can skip slash-commands / empty prompts inside on_prompt.
            ptext = data.get("prompt")
            if ptext is None:
                ptext = data.get("user_prompt") or data.get("message") or ""
            if not isinstance(ptext, str):
                ptext = ""
            # The channel plugin wraps the user's message as <channel ...>BODY
            # </channel>; pull BODY out so on_prompt's slash-command skip sees the
            # real text (e.g. /fresh) instead of the wrapper (which always starts
            # with "<channel" and would never be detected as a slash command).
            s = ptext.lstrip()
            if s.startswith("<channel"):
                gt = s.find(">")
                end = s.rfind("</channel>")
                if gt != -1 and end != -1 and end > gt:
                    ptext = s[gt + 1:end].strip()
            deferred = on_prompt(sdir, tg, prompt_text=ptext)
        elif mode == "stop":
            # Stop hook = the turn is TRULY over -> fold the box in place and stop typing.
            deferred = record_reply(sdir, tg, final=True)
        elif mode in ("pre", "post"):
            tool = data.get("tool_name", "")
            tuid = data.get("tool_use_id")
            agent_id = data.get("agent_id") or data.get("agentId")
            agent_type = data.get("agent_type") or data.get("agentType")
            if tool == REACT_TOOL and mode == "post":
                deferred = on_react(sdir, tg)
            elif tool == REPLY_TOOL and mode == "post":
                # A reply was sent, but we can't yet tell if it's an intermediate ack or
                # the final answer -> don't fold; keep the box live + typing on and arm a
                # re-anchor so it tracks below this reply when work resumes. The Stop hook
                # (final=True) is the deterministic end that actually folds the box.
                deferred = record_reply(sdir, tg, final=False)
            elif (not tool) or tool.startswith(PLUGIN_PREFIX):
                deferred = []
            else:
                deferred = handle_step(sdir, tg, mode, tool,
                                       data.get("tool_input") or {}, tuid, agent_id, agent_type)
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
