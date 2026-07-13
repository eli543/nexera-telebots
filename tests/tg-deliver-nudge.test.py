#!/usr/bin/env python3
# Offline test harness for tg-deliver-nudge.py
# Runs the REAL hook as a subprocess with mock stdin + env and asserts behavior.
# Run: python3 tests/tg-deliver-nudge.test.py   -- offline, no network, no bot token.
#
# The relocated kill switch ($FLEET_HOME/deliver-nudge.off) is exercised hermetically:
# every run sets FLEET_HOME to a throwaway temp dir, so the test never touches the real
# fleet home and the switch file lives under that temp dir.

import subprocess
import json
import os
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
HOOK = os.path.join(_root, "plugin", "hooks", "tg-deliver-nudge.py")
HOME = os.path.expanduser("~")

# Throwaway state dir + fleet home so nothing real is touched.
TG_DIR = tempfile.mkdtemp(prefix="fleet_deliver_state_")
FLEET_HOME = tempfile.mkdtemp(prefix="fleet_deliver_home_")

passes = 0
fails = 0


def run(stdin_obj, telegram=True):
    """Invoke the hook. Returns (exit_code, stdout, additionalContext_or_None)."""
    env = dict(os.environ)
    env.pop("TELEGRAM_STATE_DIR", None)
    env["FLEET_HOME"] = FLEET_HOME
    if telegram:
        env["TELEGRAM_STATE_DIR"] = TG_DIR
    raw = stdin_obj if isinstance(stdin_obj, str) else json.dumps(stdin_obj)
    p = subprocess.run(
        [sys.executable, HOOK],
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

# 1. Telegram: write a PDF deliverable -> nudge fires, telegram + PDF wording
rc, out, ctx = run(write_evt("/tmp/proposal.pdf"))
check("1 pdf in tg session: exit 0", rc == 0)
check("1 pdf in tg session: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)
check("1 pdf in tg session: names file", ctx and "proposal.pdf" in ctx)
check("1 pdf in tg session: telegram+files wording", ctx and "files=[" in ctx)
check("1 pdf in tg session: mentions PDF", ctx and "PDF" in ctx)
check("1 pdf in tg session: valid json", ctx != "<<INVALID_JSON_OUTPUT>>")

# 2. Telegram: write a markdown handoff -> nudge mentions copy-paste
rc, out, ctx = run(write_evt("/tmp/handoff-prompt.md"))
check("2 md handoff: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)
check("2 md handoff: copy-paste wording", ctx and "copy-paste" in ctx)

# 3. memory file under ~/.claude -> excluded, no output
rc, out, ctx = run(write_evt(HOME + "/.claude/projects/x/memory/foo.md"))
check("3 ~/.claude memory: exit 0", rc == 0)
check("3 ~/.claude memory: NO output", out.strip() == "")

# 4. code file (.ts) -> not a doc ext, no output
rc, out, ctx = run(write_evt("/tmp/app/src/index.ts"))
check("4 code .ts: NO output", out.strip() == "")

# 5. Read tool -> no output
rc, out, ctx = run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x.md"}})
check("5 Read tool: NO output", out.strip() == "")

# 6. Bash tool -> no output
rc, out, ctx = run({"tool_name": "Bash", "tool_input": {"command": "ls > /tmp/a.txt"}})
check("6 Bash tool: NO output", out.strip() == "")

# 7. Edit tool on a doc -> no output (we only fire on Write/NotebookEdit)
rc, out, ctx = run(write_evt("/tmp/report.md", tool="Edit"))
check("7 Edit tool: NO output", out.strip() == "")

# 8. Write inside a subagent -> no output
rc, out, ctx = run(write_evt("/tmp/proposal.pdf", agent=True))
check("8 subagent write: NO output", out.strip() == "")

# 9. Malformed stdin -> no output, exit 0
rc, out, ctx = run("{this is not json")
check("9 malformed stdin: exit 0", rc == 0)
check("9 malformed stdin: NO output", out.strip() == "")

# 10. Empty stdin -> no output, exit 0
rc, out, ctx = run("")
check("10 empty stdin: exit 0", rc == 0)
check("10 empty stdin: NO output", out.strip() == "")

# 11. Non-Telegram session: docx -> nudge fires with terminal/desktop wording
rc, out, ctx = run(write_evt("/tmp/report.docx"), telegram=False)
check("11 docx non-tg: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)
check("11 docx non-tg: terminal wording (open/print)", ctx and "open the file" in ctx)
check("11 docx non-tg: NOT telegram files wording", ctx and "files=[" not in ctx)

# 12. node_modules path -> excluded
rc, out, ctx = run(write_evt("/tmp/app/node_modules/pkg/readme.md"))
check("12 node_modules: NO output", out.strip() == "")

# 13. Kill switch present ($FLEET_HOME/deliver-nudge.off) -> no output (create then remove)
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

# 14. NotebookEdit on .ipynb -> nudge fires
rc, out, ctx = run(write_evt("/tmp/analysis.ipynb", tool="NotebookEdit"))
check("14 notebook: nudge present", ctx and "DELIVERABLE NUDGE" in ctx)

# 15. .json (config/data, not in allowlist) -> no output
rc, out, ctx = run(write_evt("/tmp/data.json"))
check("15 .json: NO output", out.strip() == "")

# 16. .git internal -> excluded
rc, out, ctx = run(write_evt("/tmp/app/.git/COMMIT_EDITMSG.txt"))
check("16 .git path: NO output", out.strip() == "")

# 17. missing file_path -> no output
rc, out, ctx = run({"tool_name": "Write", "tool_input": {"content": "x"}})
check("17 no file_path: NO output", out.strip() == "")

print()
print("RESULT: %d passed, %d failed" % (passes, fails))
sys.exit(1 if fails else 0)
