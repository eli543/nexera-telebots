#!/usr/bin/env python3
import subprocess
import json
import os
import re
import shutil
import sys
import tempfile
import time

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "plugin", "hooks", "tg-deliver-nudge.py")

HOME = tempfile.mkdtemp(prefix="tgnudge-home-")
os.makedirs(os.path.join(HOME, ".claude", "hooks"), exist_ok=True)
FLEET_HOME = os.path.join(HOME, "fleet")
os.makedirs(FLEET_HOME)
TEMPDIRS = [HOME]
CHAT = "4242"
OTHER_CHAT = "4343"

def make_state(ratelimit=None, access="normal", unreadable=False,
               rl_kind="file", rl_pad=0, access_fifo=False):
    """A throwaway TELEGRAM_STATE_DIR shaped like a real channel dir. rl_kind puts a
    fifo or a directory where the sidecar belongs; rl_pad inflates it past the cap."""
    d = tempfile.mkdtemp(prefix="tgnudge-state-")
    TEMPDIRS.append(d)
    os.makedirs(os.path.join(d, "stream"), exist_ok=True)
    if access == "normal":
        json.dump({"allowFrom": [CHAT]}, open(os.path.join(d, "access.json"), "w"))
    elif access == "corrupt":
        open(os.path.join(d, "access.json"), "w").write("{nope")
    if ratelimit is not None:
        rp = os.path.join(d, "stream", "ratelimit.json")
        if rl_kind == "fifo":
            os.mkfifo(rp)
        elif rl_kind == "dir":
            os.makedirs(rp)
        elif isinstance(ratelimit, str):
            open(rp, "w").write(ratelimit)
        else:
            if rl_pad:
                ratelimit = dict(ratelimit)
                ratelimit["_pad"] = "x" * rl_pad
            json.dump(ratelimit, open(rp, "w"))
        if unreadable and rl_kind == "file":
            os.chmod(rp, 0o000)
    if access_fifo:
        ap = os.path.join(d, "access.json")
        try:
            os.remove(ap)
        except Exception:
            pass
        os.mkfifo(ap)
    return d

def cooldown(chat=CHAT, offset=3600.0):
    return {"cooldown": {chat: {"until": time.time() + offset,
                                "retry_after": int(abs(offset)),
                                "stamp": "2026-09-02T21:00:00Z"}}}

def cleanup():
    for d in TEMPDIRS:
        try:
            for root, _dirs, files in os.walk(d):
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), 0o644)
                    except Exception:
                        pass
            shutil.rmtree(d)
        except Exception:
            pass

TG_DIR = make_state()

passes = 0
fails = 0

def run(stdin_obj, telegram=True, state=None):
    """Invoke the hook. Returns (exit_code, stdout, additionalContext_or_None)."""
    env = dict(os.environ)
    env.pop("TELEGRAM_STATE_DIR", None)
    env["HOME"] = HOME
    env["FLEET_HOME"] = FLEET_HOME
    if telegram:
        env["TELEGRAM_STATE_DIR"] = state or TG_DIR
    raw = stdin_obj if isinstance(stdin_obj, str) else json.dumps(stdin_obj)
    p = subprocess.run(
        ["/usr/bin/python3", HOOK],
        input=raw.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=10,
    )
    out = p.stdout.decode()
    ctx = None
    if out.strip():
        try:
            ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        except Exception:
            ctx = "<<INVALID_JSON_OUTPUT>>"
    return p.returncode, out, ctx

def check(name, cond):
    global passes, fails
    if cond:
        passes += 1
        print("  PASS  " + name)
    else:
        fails += 1
        print("  FAIL  " + name)

def write_evt(path, tool="Write", agent=False):
    e = {"tool_name": tool, "tool_input": {"file_path": path, "content": "x"}}
    if tool == "NotebookEdit":
        e["tool_input"] = {"notebook_path": path, "new_source": "x"}
    if agent:
        e["agent_id"] = "subagent-abc-123"
        e["agent_type"] = "Explore"
    return e

print("=== tg-deliver-nudge.test.py ===")

rc, out, ctx = run(write_evt("/tmp/proposal.pdf"))
check("1 pdf in tg session: exit 0", rc == 0)
check("1 pdf in tg session: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)
check("1 pdf in tg session: names file", ctx and "proposal.pdf" in ctx)
check("1 pdf in tg session: telegram+files wording", ctx and "files=[" in ctx)
check("1 pdf in tg session: mentions PDF", ctx and "PDF" in ctx)
check("1 pdf in tg session: valid json", ctx != "<<INVALID_JSON_OUTPUT>>")

rc, out, ctx = run(write_evt("/tmp/handoff-prompt.md"))
check("2 md handoff: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)
check("2 md handoff: copy-paste wording", ctx and "copy-paste" in ctx)

rc, out, ctx = run(write_evt(HOME + "/.claude/projects/x/memory/foo.md"))
check("3 ~/.claude memory: exit 0", rc == 0)
check("3 ~/.claude memory: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/Users/example/code/app/src/index.ts"))
check("4 code .ts: NO output", out.strip() == "")

rc, out, ctx = run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.md"}})
check("5 Read tool: NO output", out.strip() == "")

rc, out, ctx = run({"tool_name": "Bash", "tool_input": {"command": "ls > /tmp/a.txt"}})
check("6 Bash tool: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/tmp/report.md", tool="Edit"))
check("7 Edit tool: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/tmp/proposal.pdf", agent=True))
check("8 subagent write: NO output", out.strip() == "")

rc, out, ctx = run("{this is not json")
check("9 malformed stdin: exit 0", rc == 0)
check("9 malformed stdin: NO output", out.strip() == "")

rc, out, ctx = run("")
check("10 empty stdin: exit 0", rc == 0)
check("10 empty stdin: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/tmp/report.docx"), telegram=False)
check("11 docx non-tg: no output", out == "")
check("11 docx non-tg: exit 0", rc == 0)
check("11 docx non-tg: no context", ctx is None)

rc, out, ctx = run(write_evt("/Users/example/code/app/node_modules/pkg/readme.md"))
check("12 node_modules: NO output", out.strip() == "")

ks = os.path.join(FLEET_HOME, "deliver-nudge.off")
ks_existed = os.path.exists(ks)
try:
    open(ks, "w").close()
    rc, out, ctx = run(write_evt("/tmp/proposal.pdf"))
    check("13 kill switch: NO output", out.strip() == "")
    check("13 kill switch: exit 0", rc == 0)
finally:
    if not ks_existed and os.path.exists(ks):
        os.remove(ks)

rc, out, ctx = run(write_evt("/tmp/analysis.ipynb", tool="NotebookEdit"))
check("14 notebook: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)

rc, out, ctx = run(write_evt("/tmp/data.json"))
check("15 .json: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/Users/example/code/app/.git/COMMIT_EDITMSG.txt"))
check("16 .git path: NO output", out.strip() == "")

rc, out, ctx = run({"tool_name": "Write", "tool_input": {"content": "x"}})
check("17 no file_path: NO output", out.strip() == "")

st = make_state(ratelimit=cooldown())
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("18 live cooldown: exit 0", rc == 0)
check("18 live cooldown: no DELIVERABLE NUDGE", ctx and "DELIVERABLE NUDGE" not in ctx)
check("18 live cooldown: no send nudge wording", ctx and "files=[" not in ctx)
check("18 live cooldown: says cooldown", ctx and "under Telegram cooldown" in ctx)
check("18 live cooldown: says not nudging", ctx and "not nudging" in ctx)
check("18 live cooldown: names the file", ctx and "proposal.pdf" in ctx)
check("18 live cooldown: carries a clock time",
      bool(ctx) and bool(re.search(r"cooldown until (\w+ )?\d{1,2}:\d{2}(am|pm) \w+, not nudging", ctx)))
check("18 live cooldown: valid json envelope", ctx != "<<INVALID_JSON_OUTPUT>>")
check("18 live cooldown: no double-asterisk emphasis", ctx and "**" not in ctx)
check("18 live cooldown: one line", ctx and "\n" not in ctx)

st = make_state(ratelimit=cooldown(offset=-3600.0))
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("19 expired cooldown: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx and "files=[" in ctx)

st = make_state(ratelimit=cooldown(chat=OTHER_CHAT))
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("20 other chat cooldown: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx and "files=[" in ctx)

st = make_state(ratelimit="{not json at all")
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("21 corrupt sidecar: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx)

st = make_state(ratelimit={"cooldown": {CHAT: {"until": time.time() + 10 * 86400}}})
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("22 absurd until: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx)

st = make_state(ratelimit=cooldown(), access="missing")
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("23 no access.json: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx)

st = make_state(ratelimit=cooldown(), unreadable=True)
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("24 unreadable sidecar: normal nudge", ctx and "DELIVERABLE NUDGE" in ctx)

st = make_state(ratelimit=cooldown())
rc, out, ctx = run(write_evt(HOME + "/.claude/projects/x/memory/foo.md"), state=st)
check("25 cooldown + excluded path: NO output", out.strip() == "")

st = make_state(ratelimit=cooldown())
rc, out, ctx = run(write_evt("/Users/example/code/app/src/index.ts"), state=st)
check("26 cooldown + code file: NO output", out.strip() == "")

rc, out, ctx = run(write_evt("/tmp/report.docx"), telegram=False)
check("27 non-tg + cooldown code path: remains inert", out == "" and ctx is None)

st = make_state(ratelimit=cooldown())
rc, out, ctx = run(write_evt("/tmp/analysis.ipynb", tool="NotebookEdit"), state=st)
check("28 cooldown + notebook: note not nudge",
      ctx and "not nudging" in ctx and "DELIVERABLE NUDGE" not in ctx)

for label, off in (("30 30s remaining", 30.0), ("31 59s remaining", 59.0)):
    st = make_state(ratelimit=cooldown(offset=off))
    rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
    check(label + ": normal nudge, no suppression",
          bool(ctx) and "DELIVERABLE NUDGE" in ctx and "files=[" in ctx)

for label, off in (("32 61s remaining", 61.0), ("33 11h remaining", 11 * 3600.0)):
    st = make_state(ratelimit=cooldown(offset=off))
    rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
    check(label + ": suppressed with the note",
          bool(ctx) and "not nudging" in ctx and "DELIVERABLE NUDGE" not in ctx)

for label, kw in (("34 sidecar is a FIFO", {"rl_kind": "fifo"}),
                  ("35 sidecar is a DIRECTORY", {"rl_kind": "dir"}),
                  ("36 sidecar over the 1MB cap", {"rl_pad": 1100000}),
                  ("37 access.json is a FIFO", {"access_fifo": True})):
    st = make_state(ratelimit=cooldown(offset=11 * 3600.0), **kw)
    t0 = time.time()
    rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
    check(label + ": normal nudge, returned in %.2fs" % (time.time() - t0),
          bool(ctx) and "DELIVERABLE NUDGE" in ctx and (time.time() - t0) < 5.0)

st = make_state(ratelimit=cooldown(offset=11 * 3600.0), rl_pad=500000)
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"), state=st)
check("38 500KB sidecar: still suppressed", bool(ctx) and "not nudging" in ctx)

import calendar
import importlib.util
_spec = importlib.util.spec_from_file_location("hookmod_under_test", HOOK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
if hasattr(_mod, "_clock"):
    _now = time.time()
    _other = _mod._clock(_now + 25 * 3600)
    check("29 clock: other-day label carries a weekday (%s)" % _other,
          _other.split()[0] in calendar.day_name)
    if time.localtime(_now + 60).tm_yday == time.localtime(_now).tm_yday:
        check("29 clock: same-day label has no weekday", _mod._clock(_now + 60)[0].isdigit())
    check("29 clock: bad input degrades to an empty string", _mod._clock("not a number") == "")
else:
    check("29 clock helper present (hook under test is the pre-change copy)", False)

cleanup()

print()
print("RESULT: %d passed, %d failed" % (passes, fails))
sys.exit(1 if fails else 0)
