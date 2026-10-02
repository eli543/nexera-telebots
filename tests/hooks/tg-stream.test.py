#!/usr/bin/env python3
"""Offline streamer regression suite with synthetic fixtures and fenced I/O."""

import os
import sys
import ast
import builtins
import contextlib
import io
import hashlib
import json
import re
import time
import shutil
import tempfile
import threading
import stat
import types
import importlib.util
from importlib.machinery import SourceFileLoader

MOD_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugin", "hooks", "tg-stream.py")
if not os.path.exists(MOD_PATH):
    print("FAIL: module not found at %s" % MOD_PATH)
    sys.exit(2)

SANDBOX_ROOT = os.path.realpath(os.environ.get("SANDBOX_ROOT", ""))
if not os.environ.get("SANDBOX_ROOT") or os.environ.get("TELEBOTS_TEST") != "1":
    raise SystemExit("Run this suite through tests/sandbox/sbenv with TELEBOTS_TEST=1")
if "TELEGRAM_BOT_TOKEN" in os.environ:
    raise SystemExit("The test environment must not contain TELEGRAM_BOT_TOKEN")
for _sandbox_key in ("HOME", "TMPDIR", "FLEET_HOME"):
    _sandbox_path = os.path.realpath(os.environ.get(_sandbox_key, ""))
    if os.path.commonpath((SANDBOX_ROOT, _sandbox_path)) != SANDBOX_ROOT:
        raise SystemExit("Sandbox path required for " + _sandbox_key)

STATE_DIR = tempfile.mkdtemp(prefix="tgstream-test-")
SDIR = os.path.join(STATE_DIR, "stream")
os.makedirs(SDIR, exist_ok=True)

with open(os.path.join(STATE_DIR, ".env"), "w") as f:
    f.write('TELEGRAM_BOT_TOKEN="123:FAKE"\n')
with open(os.path.join(STATE_DIR, "access.json"), "w") as f:
    json.dump({"allowFrom": ["999"]}, f)

os.environ["TELEGRAM_STATE_DIR"] = STATE_DIR
os.environ["STREAM_DRY"] = "1"
os.environ["STREAM_NO_SPAWN"] = "1"

os.environ["STREAM_MIN_EDIT"] = "2.5"
os.environ["STREAM_STALE"] = "240"

os.environ["CODEX_HOME"] = os.path.join(STATE_DIR, "no-codex-home")
FLEET_HOME = os.path.join(STATE_DIR, "fleet")
os.makedirs(FLEET_HOME)
os.environ["FLEET_HOME"] = FLEET_HOME
FLEET_CONFIG = os.path.join(FLEET_HOME, "fleet.json")


def _write_fleet_config(bots=None, judge=False):
    with open(FLEET_CONFIG, "w") as handle:
        json.dump({"version": 2, "bots": bots or [],
                   "options": {"tldr_judge": judge}}, handle)


_write_fleet_config()

spec = importlib.util.spec_from_loader(
    "tgstream_under_test", SourceFileLoader("tgstream_under_test", MOD_PATH))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

_ORIGINAL_SUBPROCESS_POPEN = mod.subprocess.Popen
_PGREP_TEST_STATE = {"global": True, "paths": {}, "calls": []}


def _deny_external(*args, **kwargs):
    raise AssertionError("Unexpected external I/O in offline streamer test")


mod.request.urlopen = _deny_external
mod.subprocess.Popen = _deny_external


def _isolated_main(argv, **kwargs):
    """Fresh module constants and hook entry point, without child processes or signals."""
    saved_env, saved_argv, saved_stdin = dict(os.environ), sys.argv, sys.stdin
    supplied = kwargs.get("input") or b""
    if isinstance(supplied, bytes):
        supplied = supplied.decode("utf-8", "replace")
    stdout, stderr = io.StringIO(), io.StringIO()
    result_code = 0
    try:
        os.environ.clear()
        os.environ.update(kwargs.get("env") or saved_env)
        sys.argv = list(argv[1:])
        sys.stdin = io.StringIO(supplied)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            child_spec = importlib.util.spec_from_loader(
                "tgstream_isolated_main", SourceFileLoader("tgstream_isolated_main", MOD_PATH))
            child = importlib.util.module_from_spec(child_spec)
            child_spec.loader.exec_module(child)
            child.signal = types.SimpleNamespace(
                SIGALRM=14, signal=lambda *args: None, alarm=lambda *args: None)
            try:
                child.main()
            except SystemExit as exc:
                result_code = exc.code or 0
            except Exception:
                # The shipped __main__ wrapper is deliberately fail-silent.
                pass
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.stdin = saved_argv, saved_stdin
    return types.SimpleNamespace(returncode=result_code,
                                 stdout=stdout.getvalue().encode(),
                                 stderr=stderr.getvalue().encode())


def _safe_subprocess_run(argv, *args, **kwargs):
    if isinstance(argv, (tuple, list)) and argv:
        if (str(argv[0]) == sys.executable and len(argv) > 1
                and os.path.realpath(str(argv[1])) == os.path.realpath(MOD_PATH)):
            return _isolated_main(argv, **kwargs)
        if str(argv[0]) == "/usr/bin/grep":
            # The suite needs the actual POSIX ERE engine used by process probes.
            # This read-only stdin filter has no timeout or signal path.
            process = _ORIGINAL_SUBPROCESS_POPEN(
                argv, stdin=mod.subprocess.PIPE,
                stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"))
            stdout, stderr = process.communicate(kwargs.get("input"))
            return types.SimpleNamespace(returncode=process.returncode,
                                         stdout=stdout, stderr=stderr)
    return _deny_external(argv, *args, **kwargs)


_REAL_SUBPROCESS_RUN = _safe_subprocess_run

class _PgrepTestResult:
    def __init__(self, alive):
        self.stdout = b"3301\n" if alive is True else b""
        self.returncode = 0 if alive is True else 1 if alive is False else 3

def _test_subprocess_run(argv, *args, **kwargs):
    if isinstance(argv, (list, tuple)) and list(argv[:2]) == ["/usr/bin/pgrep", "-f"]:
        pattern = str(argv[-1])
        _PGREP_TEST_STATE["calls"].append(tuple(argv))
        alive = _PGREP_TEST_STATE["paths"].get(
            pattern, _PGREP_TEST_STATE["global"] if pattern == "codex exec" else True)
        return _PgrepTestResult(alive)
    return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)

mod.subprocess.run = _test_subprocess_run

_MID_SEQ = [1000]

class RecTG:

    def __init__(self, fail_send=False):
        self.calls = []
        self.fail_send = fail_send

    def send(self, text, markdown=False):
        if self.fail_send:
            self.calls.append(("send_FAIL", text, markdown))
            return None
        _MID_SEQ[0] += 1
        mid = str(_MID_SEQ[0])
        self.calls.append(("send", mid, text, markdown))
        return mid

    def edit(self, msgid, text, markdown=False):
        self.calls.append(("edit", str(msgid), text, markdown))
        return True

    def delete(self, msgid):
        self.calls.append(("delete", str(msgid)))
        return True

    def typing(self):
        self.calls.append(("typing",))

    def ops(self, kind):
        return [c for c in self.calls if c[0] == kind]

FAILS = []
SKIPS = []
CHECK_COUNT = 0

def check(label, cond, detail=""):
    global CHECK_COUNT
    CHECK_COUNT += 1
    status = "PASS" if cond else "FAIL"
    line = "[%s] %s" % (status, label)
    if detail:
        line += "  ::  " + detail
    print(line.replace("**", "").replace("\u2014", "-"))
    if not cond:
        FAILS.append(label)

def _behavior_hash(node):
    class StripDocstrings(ast.NodeTransformer):
        def visit(self, item):
            item = super().visit(item)
            # Empty declaration metadata was added by Python 3.12.
            if hasattr(item, "type_params") and not item.type_params:
                item._fields = tuple(field for field in item._fields
                                     if field != "type_params")
            if isinstance(item, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (item.body and isinstance(item.body[0], ast.Expr)
                        and isinstance(item.body[0].value, ast.Constant)
                        and isinstance(item.body[0].value.value, str)):
                    item.body = item.body[1:]
            return item
    return hashlib.sha256(ast.dump(StripDocstrings().visit(node),
                                  include_attributes=False).encode()).hexdigest()


def reset_state():
    for fn in ("state.json", "state.json.tmp", "lock", "anim.lock"):
        try:
            os.remove(os.path.join(SDIR, fn))
        except OSError:
            pass

def _et_today(hour, minute=0, second=0):

    _n = mod.datetime.fromtimestamp(mod.time.time(), mod.ET_ZONE)
    return _n.replace(hour=hour, minute=minute, second=second,
                      microsecond=0).timestamp()

def load():
    return mod.load_state(SDIR)

def run_step(tg, mode, tool, tinput=None, tuid=None, agent_id=None, agent_type=None):

    deferred = mod.handle_step(SDIR, tg, mode, tool, tinput or {}, tuid,
                               agent_id, agent_type)
    mod._run_deferred(SDIR, tg, deferred)
    return deferred

def run_reply(tg, final):
    deferred = mod.record_reply(SDIR, tg, final=final)
    mod._run_deferred(SDIR, tg, deferred)
    return deferred

print("\n===== #1 subagent count =====")

def build_helper_steps(n_distinct_aids, n_subwork_each=3, with_subtitles=False):

    steps = []
    if with_subtitles:
        steps.append({"fam": "helpers", "cap": "a task", "tuid": "", "done": True,
                      "k": "subtitle", "aid": ""})
    for a in range(n_distinct_aids):
        aid = "agent-%d" % a
        for _ in range(n_subwork_each):
            steps.append({"fam": "helpers", "cap": "doing subwork", "tuid": "",
                          "done": True, "k": "subwork", "aid": aid})
    return steps

steps5 = build_helper_steps(5, n_subwork_each=4, with_subtitles=False)
msg5, _ = mod.render_final(steps5, time.time() - 30, n_agents=5)
check("5 distinct agents, 0 subtitles -> '5 subagents'",
      "5 subagents" in msg5,
      "summary head: " + msg5.splitlines()[1] if len(msg5.splitlines()) > 1 else msg5[:120])

steps_trunc = build_helper_steps(2, n_subwork_each=2, with_subtitles=False)
msg_tr, _ = mod.render_final(steps_trunc, time.time() - 30, n_agents=8)
check("truncated steps (2 aids) but n_agents=8 -> '8 subagents'",
      "8 subagents" in msg_tr,
      msg_tr.splitlines()[1] if len(msg_tr.splitlines()) > 1 else msg_tr[:120])

steps_sub = [{"fam": "helpers", "cap": "task A", "tuid": "", "done": True,
              "k": "subtitle", "aid": ""},
             {"fam": "helpers", "cap": "task B", "tuid": "", "done": True,
              "k": "subtitle", "aid": ""},
             {"fam": "helpers", "cap": "task C", "tuid": "", "done": True,
              "k": "subtitle", "aid": ""}]
msg_subt, _ = mod.render_final(steps_sub, time.time() - 10, n_agents=0)
check("n_agents=0, 3 subtitle markers -> '3 subagents' (back-compat)",
      "3 subagents" in msg_subt,
      msg_subt.splitlines()[1] if len(msg_subt.splitlines()) > 1 else msg_subt[:120])

steps0 = [{"fam": "files", "cap": "reading a.py", "tuid": "t1", "done": True, "k": "", "aid": ""},
          {"fam": "files", "cap": "reading b.py", "tuid": "t2", "done": True, "k": "", "aid": ""},
          {"fam": "write", "cap": "editing c.py", "tuid": "t3", "done": True, "k": "", "aid": ""},
          {"fam": "run",   "cap": "ran tests",   "tuid": "t4", "done": True, "k": "", "aid": ""}]
msg0, _ = mod.render_final(steps0, time.time() - 12, n_agents=0)
check("0-subagent turn -> no 'subagent' token in summary",
      "subagent" not in msg0,
      msg0.splitlines()[1] if len(msg0.splitlines()) > 1 else msg0[:120])
check("0-subagent turn -> still reports files + edits + commands",
      ("2 files" in msg0) and ("1 edit" in msg0) and ("1 command" in msg0),
      msg0.splitlines()[1] if len(msg0.splitlines()) > 1 else msg0[:120])

steps_mix = (
    [{"fam": "files", "cap": "reading a", "tuid": "t1", "done": True, "k": "", "aid": ""},
     {"fam": "files", "cap": "reading b", "tuid": "t2", "done": True, "k": "", "aid": ""},
     {"fam": "files", "cap": "reading c", "tuid": "t3", "done": True, "k": "", "aid": ""},
     {"fam": "files", "cap": "reading d", "tuid": "t4", "done": True, "k": "", "aid": ""},
     {"fam": "files", "cap": "reading e", "tuid": "t5", "done": True, "k": "", "aid": ""},
     {"fam": "run", "cap": "ran 1", "tuid": "b1", "done": True, "k": "", "aid": ""},
     {"fam": "run", "cap": "ran 2", "tuid": "b2", "done": True, "k": "", "aid": ""},
     {"fam": "run", "cap": "ran 3", "tuid": "b3", "done": True, "k": "", "aid": ""},
     {"fam": "web", "cap": "search 1", "tuid": "w1", "done": True, "k": "", "aid": ""},
     {"fam": "web", "cap": "search 2", "tuid": "w2", "done": True, "k": "", "aid": ""}]
    + build_helper_steps(4, n_subwork_each=2, with_subtitles=True))
msg_mix, _ = mod.render_final(steps_mix, time.time() - 60, n_agents=4)
head_mix = msg_mix.splitlines()[1] if len(msg_mix.splitlines()) > 1 else msg_mix
check("mixed turn -> '5 files'", "5 files" in msg_mix, head_mix)
check("mixed turn -> '3 commands'", "3 commands" in msg_mix, head_mix)
check("mixed turn -> '2 web searches'", "2 web searches" in msg_mix, head_mix)
check("mixed turn -> '4 subagents' (only this number reflects n_agents)",
      "4 subagents" in msg_mix, head_mix)

steps1 = build_helper_steps(1, n_subwork_each=5, with_subtitles=True)
msg1, _ = mod.render_final(steps1, time.time() - 10, n_agents=1)
check("1 subagent -> '1 subagent' (singular, no regression)",
      "1 subagent" in msg1 and "1 subagents" not in msg1,
      msg1.splitlines()[1] if len(msg1.splitlines()) > 1 else msg1[:120])

print("\n===== #1b end-to-end count through the real fold path =====")
reset_state()
tg = RecTG()

mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="please fan out work"))

run_step(tg, "pre", "Task", {"description": "fan out"})
run_step(tg, "post", "Task", {"description": "fan out"})
for i in range(5):
    aid = "agent-%d" % i
    run_step(tg, "pre", "Read", {"file_path": "/x/%d" % i}, tuid="tu%d" % i, agent_id=aid, agent_type="agent")
    run_step(tg, "post", "Read", {"file_path": "/x/%d" % i}, tuid="tu%d" % i, agent_id=aid, agent_type="agent")
st_before_stop = load()
n_agents_recorded = len(st_before_stop.get("agents") or {})
check("handle_step recorded 5 distinct agents in state['agents']",
      n_agents_recorded == 5, "agents=%d" % n_agents_recorded)

run_reply(tg, final=False)

tg.calls.clear()
run_reply(tg, final=True)
fold_edits = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
check("Stop fold edit emitted (markdown=True)", len(fold_edits) >= 1,
      "edits(md)=%d" % len(fold_edits))
fold_text = fold_edits[-1][2] if fold_edits else ""
check("end-to-end fold text says '5 subagents'",
      "5 subagents" in fold_text,
      fold_text.splitlines()[1] if len(fold_text.splitlines()) > 1 else fold_text[:140])

print("\n===== #2 no mid-turn fragmentation =====")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="long running turn"))
st0 = load()
box_id_open = st0.get("msgid")
check("on_prompt opened a box", bool(box_id_open), "msgid=%s" % box_id_open)

run_step(tg, "pre", "Bash", {"command": "step one"}, tuid="g1")
run_step(tg, "post", "Bash", {"command": "step one"}, tuid="g1")
st1 = load()
msgid_after_first = st1.get("msgid")
steps_after_first = len(st1.get("steps") or [])

st1["last_activity"] = time.time() - 100000
st1["last_react"] = time.time() - 100000
st1["last_reply"] = time.time() - 100000
mod.save_state(SDIR, st1)

tg.calls.clear()

run_step(tg, "pre", "Bash", {"command": "step two after long gap"}, tuid="g2")
run_step(tg, "post", "Bash", {"command": "step two after long gap"}, tuid="g2")
st2 = load()
msgid_after_gap = st2.get("msgid")
steps_after_gap = len(st2.get("steps") or [])

check("post-gap msgid unchanged (one continuous box, NOT reset)",
      msgid_after_gap == msgid_after_first and msgid_after_first is not None,
      "before=%s after=%s" % (msgid_after_first, msgid_after_gap))
check("post-gap steps ACCUMULATE (not reset to a fresh box)",
      steps_after_gap > steps_after_first,
      "before=%d after=%d" % (steps_after_first, steps_after_gap))

check("post-gap box is NOT folded mid-turn", not st2.get("folded"),
      "folded=%s" % st2.get("folded"))

gap_sends = [c for c in tg.calls if c[0] == "send"]
gap_fold_edits = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
check("post-gap emitted NO new box send (no second box)",
      len(gap_sends) == 0, "sends=%d %r" % (len(gap_sends), gap_sends))
check("post-gap emitted NO fold edit (markdown=True)",
      len(gap_fold_edits) == 0, "foldedits=%d" % len(gap_fold_edits))

print("\n===== #3 intermediate reply -> reanchor; Stop -> fold in place =====")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="ack then work"))

run_step(tg, "pre", "Read", {"file_path": "/a"}, tuid="r1")
run_step(tg, "post", "Read", {"file_path": "/a"}, tuid="r1")
st_pre_reply = load()
msgid_before_reply = st_pre_reply.get("msgid")
folded_before = st_pre_reply.get("folded")

tg.calls.clear()
run_reply(tg, final=False)
st_after_reply = load()
check("intermediate reply does NOT fold the box",
      not st_after_reply.get("folded"),
      "folded=%s" % st_after_reply.get("folded"))
check("intermediate reply SETS reanchor=True",
      st_after_reply.get("reanchor") is True,
      "reanchor=%s" % st_after_reply.get("reanchor"))
check("intermediate reply msgid unchanged (box not moved YET, just armed)",
      st_after_reply.get("msgid") == msgid_before_reply,
      "before=%s after=%s" % (msgid_before_reply, st_after_reply.get("msgid")))
inter_reply_sends = [c for c in tg.calls if c[0] == "send"]
inter_reply_edits_md = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
check("intermediate reply did NO network fold (no send, no md edit under lock)",
      len(inter_reply_sends) == 0 and len(inter_reply_edits_md) == 0,
      "sends=%d md_edits=%d" % (len(inter_reply_sends), len(inter_reply_edits_md)))

tg.calls.clear()
run_step(tg, "pre", "Bash", {"command": "resumed work"}, tuid="b9")
st_after_resume = load()
new_msgid = st_after_resume.get("msgid")
check("resumed handle_step re-anchored to a NEW bottom msgid",
      new_msgid is not None and new_msgid != msgid_before_reply,
      "old=%s new=%s" % (msgid_before_reply, new_msgid))
resume_sends = [c for c in tg.calls if c[0] == "send"]
resume_deletes = [c for c in tg.calls if c[0] == "delete"]
resume_md_edits = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
check("resumed handle_step SENT a fresh live box (new position)",
      len(resume_sends) >= 1, "sends=%r" % resume_sends)
check("v20: resumed handle_step FOLDED the old box in place (did NOT delete it)",
      any(e[1] == str(msgid_before_reply) for e in resume_md_edits)
      and not any(d[1] == str(msgid_before_reply) for d in resume_deletes),
      "mdedits=%r deletes=%r old=%s" % (resume_md_edits, resume_deletes, msgid_before_reply))
check("v20: the folded old box is recorded as folded_above_msgid",
      st_after_resume.get("folded_above_msgid") == msgid_before_reply,
      "folded_above=%s old=%s" % (st_after_resume.get("folded_above_msgid"), msgid_before_reply))
check("reanchor flag CLEARED after consumption",
      not st_after_resume.get("reanchor"),
      "reanchor=%s" % st_after_resume.get("reanchor"))

run_step(tg, "post", "Bash", {"command": "resumed work"}, tuid="b9")

st_before_stop3 = load()
live_msgid_before_stop = st_before_stop3.get("msgid")
fold_above_before_stop = st_before_stop3.get("folded_above_msgid")
tg.calls.clear()
run_reply(tg, final=True)
st_after_stop3 = load()
stop_edits_md = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
stop_sends = [c for c in tg.calls if c[0] == "send"]
stop_deletes = [c for c in tg.calls if c[0] == "delete"]
check("Stop folds the box (folded=True)", st_after_stop3.get("folded") is True,
      "folded=%s" % st_after_stop3.get("folded"))
check("v20: Stop wrote the recap into the message ABOVE the reply",
      any(e[1] == str(fold_above_before_stop) for e in stop_edits_md),
      "edits=%r above=%s" % (stop_edits_md, fold_above_before_stop))
check("v20: Stop DELETED the trailing box that sat below the reply",
      any(d[1] == str(live_msgid_before_stop) for d in stop_deletes),
      "deletes=%r trailing=%s" % (stop_deletes, live_msgid_before_stop))
check("Stop fold did NOT relocate (no new box send)",
      len(stop_sends) == 0, "sends=%r" % stop_sends)
check("v20: state msgid now points at the surviving recap above the reply",
      st_after_stop3.get("msgid") == fold_above_before_stop
      and not st_after_stop3.get("folded_above_msgid"),
      "msgid=%s above=%s" % (st_after_stop3.get("msgid"),
                             st_after_stop3.get("folded_above_msgid")))

print("\n===== #4 intermediate reply keeps typing alive =====")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="ack keeps typing"))
run_step(tg, "pre", "Read", {"file_path": "/z"}, tuid="z1")
run_step(tg, "post", "Read", {"file_path": "/z"}, tuid="z1")

st_work = load()
check("during work typing_done is False (animator keeps typing)",
      not st_work.get("typing_done"), "typing_done=%s" % st_work.get("typing_done"))

run_reply(tg, final=False)
st_inter = load()
check("intermediate reply does NOT set typing_done (box stays live/typing)",
      not st_inter.get("typing_done"),
      "typing_done=%s" % st_inter.get("typing_done"))

run_step(tg, "pre", "Bash", {"command": "more"}, tuid="z2")
run_step(tg, "post", "Bash", {"command": "more"}, tuid="z2")
run_reply(tg, final=True)
st_final = load()
check("FINAL reply (Stop) DOES set typing_done (typing stops at true end)",
      st_final.get("typing_done") is True,
      "typing_done=%s" % st_final.get("typing_done"))

print("\n===== #3b reanchor send failure keeps old box + retries =====")
reset_state()
tgok = RecTG()
mod._run_deferred(SDIR, tgok, mod.on_prompt(SDIR, tgok, prompt_text="fail then retry"))
run_step(tgok, "pre", "Read", {"file_path": "/q"}, tuid="q1")
run_step(tgok, "post", "Read", {"file_path": "/q"}, tuid="q1")
st_q = load()
old_box = st_q.get("msgid")
run_reply(tgok, final=False)

tgfail = RecTG(fail_send=True)
run_step(tgfail, "pre", "Bash", {"command": "resume but send fails"}, tuid="q2")
st_failed = load()
check("reanchor send failure: msgid stays the OLD box (no stranded deleted msgid)",
      st_failed.get("msgid") == old_box,
      "old=%s now=%s" % (old_box, st_failed.get("msgid")))
check("reanchor send failure: reanchor stays armed True (will retry)",
      st_failed.get("reanchor") is True,
      "reanchor=%s" % st_failed.get("reanchor"))
fail_deletes = [c for c in tgfail.calls if c[0] == "delete"]
check("reanchor send failure: NO delete of the old box (never strand a deleted msgid)",
      len(fail_deletes) == 0, "deletes=%r" % fail_deletes)

tgretry = RecTG()
run_step(tgretry, "post", "Bash", {"command": "resume but send fails"}, tuid="q2")
st_retry = load()
check("reanchor retry: now re-anchored to a NEW msgid",
      st_retry.get("msgid") is not None and st_retry.get("msgid") != old_box,
      "old=%s new=%s" % (old_box, st_retry.get("msgid")))
check("reanchor retry: reanchor cleared after success",
      not st_retry.get("reanchor"), "reanchor=%s" % st_retry.get("reanchor"))

print("\n===== #3c reanchor never leaks across a new prompt =====")

tgp = RecTG()
mod._run_deferred(SDIR, tgp, mod.on_prompt(SDIR, tgp, prompt_text="brand new message"))
st_newprompt = load()
check("new prompt -> reanchor reset False (no leak)",
      not st_newprompt.get("reanchor"), "reanchor=%s" % st_newprompt.get("reanchor"))
check("new prompt -> folded False, steps empty (fresh box)",
      (not st_newprompt.get("folded")) and not (st_newprompt.get("steps")),
      "folded=%s steps=%d" % (st_newprompt.get("folded"), len(st_newprompt.get("steps") or [])))

print("\n===== #5 robustness smoke (malformed state, never raise, exit 0) =====")
import subprocess as _sp

def drive_main(mode, stdin_obj, extra_env=None):
    env = dict(os.environ)
    env["TELEGRAM_STATE_DIR"] = STATE_DIR
    env["STREAM_DRY"] = "1"
    env["STREAM_NO_SPAWN"] = "1"
    if extra_env:
        env.update(extra_env)
    p = _sp.run([sys.executable, MOD_PATH, mode],
                input=json.dumps(stdin_obj).encode(),
                stdout=_sp.PIPE, stderr=_sp.PIPE, env=env, timeout=30)
    return p

def strip_nudges(raw):
    try:
        s = raw.decode("utf-8")
    except Exception:
        return raw
    dec = json.JSONDecoder()
    while True:
        i = s.find('{"hookSpecificOutput"')
        if i < 0:
            break
        try:
            obj, end = dec.raw_decode(s[i:])
        except Exception:
            break
        hso = obj.get("hookSpecificOutput") if isinstance(obj, dict) else None
        if not isinstance(hso, dict) or hso.get("hookEventName") != "UserPromptSubmit"\
                or not isinstance(hso.get("additionalContext"), str)\
                or set(obj.keys()) != {"hookSpecificOutput"}:
            break
        s = s[:i] + s[i + end:]
    return s.encode("utf-8")

with open(os.path.join(SDIR, "state.json"), "w") as f:
    f.write("{ not valid json ]]]")
ok_all = True
out_all = b""
for mode, payload in [
        ("prompt", {"prompt": "<channel source=\"telegram\">hello there</channel>"}),
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/x"}, "tool_use_id": "m1"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/x"}, "tool_use_id": "m1"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("prompt", {"prompt": "/fresh"}),
        ("pre", {"tool_name": "", "tool_input": {}}),
]:
    p = drive_main(mode, payload)
    if p.returncode != 0:
        ok_all = False
    out_all += p.stdout
check("all argv modes exit 0 even with garbage state", ok_all)

check("all argv modes produce ZERO stdout", strip_nudges(out_all) == b"",
      "stdout=%r" % strip_nudges(out_all)[:120])

with open(os.path.join(SDIR, "state.json"), "w") as f:
    json.dump({"msgid": "5", "steps": [{"fam": "helpers", "cap": "x", "k": "subwork",
               "aid": "a1", "done": True}], "agents": None, "proc_start": time.time()}, f)
crashed = False
try:
    mod.render_final([{"fam": "helpers", "cap": "x", "k": "subwork", "aid": "a1", "done": True}],
                     time.time(), n_agents=len({} or {}))
except Exception as e:
    crashed = True
check("render_final tolerates degenerate agents (no crash)", not crashed)
p = drive_main("stop", {})
check("main() Stop with agents=null exits 0", p.returncode == 0)

print("\n===== #6 (v20) the answer must be the LAST message, in every turn shape =====")

def reply_lands():

    _MID_SEQ[0] += 1
    return _MID_SEQ[0]

def surviving(tg):
    gone = set(c[1] for c in tg.calls if c[0] == "delete")
    return [c[1] for c in tg.calls if c[0] == "send" and c[1] not in gone]

def md_edits(tg, mid=None):
    return [c for c in tg.calls if c[0] == "edit" and c[3] is True
            and (mid is None or str(c[1]) == str(mid))]

def below(mids, boundary):
    return [m for m in mids if int(m) > boundary]

def do_tool(tg, tool, tinput, tuid):
    run_step(tg, "pre", tool, tinput, tuid=tuid)
    run_step(tg, "post", tool, tinput, tuid=tuid)

print("\n----- 6a trailing tools after the final reply (the reported bug) -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="answer me then file it"))
first_box = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/a.md"}, "s1")
do_tool(tg, "Bash", {"command": "build"}, "s2")
answer6a = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Read", {"file_path": "/notes/2026-07-24.md"}, "s3")
do_tool(tg, "Write", {"file_path": "/notes/log.md"}, "s4")
run_reply(tg, final=True)
st6a = load()
alive6a = surviving(tg)
recap6a = md_edits(tg, first_box)
check("6a NOTHING the streamer sent survives below the final answer",
      len(below(alive6a, answer6a)) == 0,
      "alive=%r answer_mid=%d" % (alive6a, answer6a))
check("6a exactly ONE streamer message survives the turn",
      len(alive6a) == 1, "alive=%r" % alive6a)
check("6a the survivor is the box opened before the answer (so it is above it)",
      alive6a == [str(first_box)], "alive=%r first_box=%s" % (alive6a, first_box))
check("6a the survivor was written as the folded recap",
      bool(recap6a) and "What I did" in recap6a[-1][2],
      "md_edits_at_survivor=%d" % len(recap6a))
check("6a the recap covers the WHOLE turn, trailing steps included",
      bool(recap6a) and all(x in recap6a[-1][2]
                            for x in ("2 files", "1 command", "1 edit")),
      recap6a[-1][2].splitlines()[1] if recap6a else "no recap")
check("6a state: folded, msgid = the surviving recap, no dangling fold-above",
      st6a.get("folded") is True and str(st6a.get("msgid")) == str(first_box)
      and not st6a.get("folded_above_msgid"),
      "folded=%s msgid=%s above=%s" % (st6a.get("folded"), st6a.get("msgid"),
                                       st6a.get("folded_above_msgid")))

print("\n----- 6b reply as the last action -> v19 fold-in-place fast path -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="just answer"))
box6b = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/b.md"}, "t1")
answer6b = reply_lands()
run_reply(tg, final=False)
mark = len(tg.calls)
run_reply(tg, final=True)
tail6b = tg.calls[mark:]
st6b = load()
check("6b Stop costs exactly ONE Telegram call and it is a markdown edit",
      len(tail6b) == 1 and tail6b[0][0] == "edit" and tail6b[0][3] is True,
      "tail=%r" % (tail6b,))
check("6b the fold is IN PLACE at the box's own msgid (unchanged)",
      bool(tail6b) and str(tail6b[0][1]) == str(box6b) and st6b.get("msgid") == box6b,
      "tail=%r box=%s msgid=%s" % (tail6b[:1], box6b, st6b.get("msgid")))
check("6b no send and no delete on the common path",
      not [c for c in tail6b if c[0] in ("send", "delete")], "tail=%r" % (tail6b,))
check("6b the recap sits ABOVE the answer, nothing below it",
      int(box6b) < answer6b and len(below(surviving(tg), answer6b)) == 0,
      "box=%s answer=%d alive=%r" % (box6b, answer6b, surviving(tg)))
check("6b folded + typing stopped",
      st6b.get("folded") is True and st6b.get("typing_done") is True,
      "folded=%s typing_done=%s" % (st6b.get("folded"), st6b.get("typing_done")))

print("\n----- 6c instant-ack pattern (reply before any tool, then work, then answer) -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="on it"))
ack_box = load().get("msgid")
ack6c = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Read", {"file_path": "/c.md"}, "c1")
do_tool(tg, "Bash", {"command": "go"}, "c2")
answer6c = reply_lands()
run_reply(tg, final=False)
run_reply(tg, final=True)
st6c = load()
alive6c = surviving(tg)
check("6c the EMPTY pre-ack box was deleted (same single-box look as v19)",
      any(c[0] == "delete" and str(c[1]) == str(ack_box) for c in tg.calls),
      "deletes=%r ack_box=%s" % ([c for c in tg.calls if c[0] == "delete"], ack_box))
check("6c exactly one box survives", len(alive6c) == 1, "alive=%r" % alive6c)
check("6c the survivor sits between the ack and the answer (recap above the answer)",
      len(alive6c) == 1 and ack6c < int(alive6c[0]) < answer6c,
      "ack=%d alive=%r answer=%d" % (ack6c, alive6c, answer6c))
check("6c the survivor is folded into the recap",
      len(alive6c) == 1 and bool(md_edits(tg, alive6c[0])),
      "md_edits=%r" % md_edits(tg, alive6c[0] if alive6c else None))
check("6c nothing survives below the answer",
      len(below(alive6c, answer6c)) == 0, "alive=%r answer=%d" % (alive6c, answer6c))

print("\n----- 6d multi-reply turn with trailing work -> ONE recap above the answer -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="update me, answer, then file"))
box_a = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/d1"}, "d1")
r1_6d = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Bash", {"command": "work more"}, "d2")
box_b = load().get("msgid")
r2_6d = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Write", {"file_path": "/d3"}, "d3")
run_reply(tg, final=True)
st6d = load()
alive6d = surviving(tg)
final_md6d = md_edits(tg, alive6d[0]) if alive6d else []
check("6d exactly ONE folded recap survives the whole turn",
      len(alive6d) == 1, "alive=%r" % alive6d)
check("6d that recap sits ABOVE the final answer",
      len(alive6d) == 1 and int(alive6d[0]) < r2_6d,
      "alive=%r answer=%d" % (alive6d, r2_6d))
check("6d the survivor is the box that was live between the two replies",
      alive6d == [str(box_b)], "alive=%r box_b=%s box_a=%s" % (alive6d, box_b, box_a))
check("6d the recap carries the FULL tally across all three work families",
      bool(final_md6d) and all(x in final_md6d[-1][2]
                               for x in ("1 file", "1 command", "1 edit")),
      final_md6d[-1][2].splitlines()[1] if final_md6d else "no recap")
check("6d no leftover fold-above in state",
      not st6d.get("folded_above_msgid"), "above=%s" % st6d.get("folded_above_msgid"))

print("\n----- 6e documented residual: all work happened after the only reply -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="ack then only work"))
ack6e = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Read", {"file_path": "/e1"}, "e1")
run_reply(tg, final=True)
st6e = load()
alive6e = surviving(tg)
check("6e nothing survives below the reply (the answer stays last)",
      len(below(alive6e, ack6e)) == 0, "alive=%r reply=%d" % (alive6e, ack6e))
check("6e residual as designed: the recap is dropped, never parked below the answer",
      alive6e == [], "alive=%r" % alive6e)
check("6e state is clean (folded, no box, no fold-above)",
      st6e.get("folded") is True and not st6e.get("msgid")
      and not st6e.get("folded_above_msgid"),
      "folded=%s msgid=%s above=%s" % (st6e.get("folded"), st6e.get("msgid"),
                                       st6e.get("folded_above_msgid")))

print("\n----- 6f a v19-shaped state file (no v20 keys) upgrades mid-turn -----")
V19_KEYS = ("msgid steps proc_start last_edit last_activity last_reply last_react "
            "last_spawn frame folded last_live agents next_agent_n ghosts typing_done "
            "reanchor").split()

def write_v19_state(**over):
    now = time.time()
    st = {"msgid": "7001",
          "steps": [{"fam": "files", "cap": "reading old.md", "tuid": "v1",
                     "done": True, "k": "", "aid": ""}],
          "proc_start": now - 42, "last_edit": now - 3, "last_activity": now - 3,
          "last_reply": now - 2, "last_react": now - 40, "last_spawn": now - 40,
          "frame": 9, "folded": False, "last_live": "old live text", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False}
    st.update(over)
    mod.save_state(SDIR, st)
    return st

reset_state()
write_v19_state()
check("6f the fixture really is v19-shaped (no v20 keys present)",
      set(load().keys()) == set(V19_KEYS), "keys=%r" % sorted(load().keys()))
tgf = RecTG()
run_reply(tgf, final=True)
st6f = load()
check("6f v19 state + Stop -> folds IN PLACE at the same msgid (v19 behavior, no crash)",
      len(md_edits(tgf, "7001")) == 1 and st6f.get("msgid") == "7001"
      and st6f.get("folded") is True,
      "edits=%r msgid=%s folded=%s" % (md_edits(tgf, "7001"), st6f.get("msgid"),
                                       st6f.get("folded")))

reset_state()
write_v19_state(reanchor=True)
tgf2 = RecTG()
run_step(tgf2, "pre", "Bash", {"command": "resume on upgraded code"}, tuid="v2")
st6f2 = load()
check("6f v19 state + armed reanchor -> fresh box below, old box folded above, no crash",
      st6f2.get("msgid") not in (None, "7001")
      and st6f2.get("folded_above_msgid") == "7001"
      and st6f2.get("box_is_last") is True,
      "msgid=%s above=%s last=%s" % (st6f2.get("msgid"),
                                     st6f2.get("folded_above_msgid"),
                                     st6f2.get("box_is_last")))

reset_state()
write_v19_state(reanchor=True)
ok6f, out6f = True, b""
for mode, payload in [
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("prompt", {"prompt": "<channel source=\"telegram\">next message</channel>"}),
]:
    p = drive_main(mode, payload)
    if p.returncode != 0:
        ok6f = False
    out6f += p.stdout
check("6f every hook edge over a v19-shaped state exits 0", ok6f)

check("6f every hook edge over a v19-shaped state prints nothing", strip_nudges(out6f) == b"",
      "stdout=%r" % strip_nudges(out6f)[:120])

print("\n----- 6g the animator backstop fold respects the position rule -----")

def write_backstop_state(**over):
    now = time.time()
    st = {"msgid": "8002", "steps": [{"fam": "run", "cap": "long job", "tuid": "g1",
                                      "done": False, "k": "", "aid": ""}],
          "proc_start": now - 4000, "last_edit": now, "last_activity": now - 3000,
          "last_reply": now - 3500, "last_react": now - 4000, "last_spawn": now,
          "frame": 3, "folded": False, "last_live": "live", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
          "box_is_last": True, "folded_above_msgid": "8001"}
    st.update(over)
    mod.save_state(SDIR, st)
    return st

reset_state()
write_backstop_state()
tgg = RecTG()
mod._fold_to_done(SDIR, tgg)
stg = load()
check("6g1 backstop wrote the recap into the message ABOVE the reply",
      any(c[0] == "edit" and str(c[1]) == "8001" and c[3] is True for c in tgg.calls),
      "calls=%r" % (tgg.calls,))
check("6g1 backstop deleted the trailing box that sat below the reply",
      any(c[0] == "delete" and str(c[1]) == "8002" for c in tgg.calls),
      "calls=%r" % (tgg.calls,))
check("6g1 state follows the surviving recap",
      stg.get("msgid") == "8001" and stg.get("folded") is True
      and not stg.get("folded_above_msgid") and not stg.get("box_is_last"),
      "msgid=%s folded=%s above=%s last=%s" % (stg.get("msgid"), stg.get("folded"),
                                               stg.get("folded_above_msgid"),
                                               stg.get("box_is_last")))
tgg3 = RecTG()
run_reply(tgg3, final=True)
stg3 = load()
check("6g1 a later Stop leaves that recap exactly where it is (no second move)",
      stg3.get("msgid") == "8001" and not [c for c in tgg3.calls
                                           if c[0] in ("send", "delete")],
      "msgid=%s calls=%r" % (stg3.get("msgid"), tgg3.calls))

reset_state()
write_backstop_state(msgid="8010", folded_above_msgid=None)
tgg2 = RecTG()
mod._fold_to_done(SDIR, tgg2)
stg2 = load()

check("6g2 no carried recap: the backstop takes the below-answer box OUT, never folds it there",
      any(c[0] == "delete" and str(c[1]) == "8010" for c in tgg2.calls)
      and not [c for c in tgg2.calls if c[0] == "edit"]
      and stg2.get("msgid") is None and stg2.get("folded") is True,
      "calls=%r msgid=%s folded=%s" % (tgg2.calls, stg2.get("msgid"), stg2.get("folded")))

reset_state()
write_backstop_state(msgid="8011", folded_above_msgid=None, box_is_last=False)
tgg2b = RecTG()
mod._fold_to_done(SDIR, tgg2b)
stg2b = load()
check("6g2 a box that is NOT below the answer still folds IN PLACE (v19 parity, no delete)",
      any(c[0] == "edit" and str(c[1]) == "8011" for c in tgg2b.calls)
      and not [c for c in tgg2b.calls if c[0] == "delete"]
      and stg2b.get("msgid") == "8011" and stg2b.get("folded") is True,
      "calls=%r msgid=%s folded=%s" % (tgg2b.calls, stg2b.get("msgid"),
                                       stg2b.get("folded")))

print("\n----- 6h v20 spends no more network under the lock than v19 -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="budget check"))
do_tool(tg, "Read", {"file_path": "/h1"}, "h1")
reply_lands()
run_reply(tg, final=False)
tg.calls.clear()
deferred_h = mod.handle_step(SDIR, tg, "pre", "Bash", {"command": "resume"}, "h2", None, None)
inline_h = list(tg.calls)
mod._run_deferred(SDIR, tg, deferred_h)
check("6h re-anchor makes exactly ONE call under the lock (the box send), as v19",
      len(inline_h) == 1 and inline_h[0][0] == "send", "inline=%r" % (inline_h,))
check("6h the fold-above edit is DEFERRED past the lock",
      "edit" in [d[0] for d in deferred_h], "deferred=%r" % ([d[0] for d in deferred_h],))
tg.calls.clear()
deferred_s = mod.record_reply(SDIR, tg, final=True)
inline_s = list(tg.calls)
mod._run_deferred(SDIR, tg, deferred_s)
check("6h Stop in the trailing shape: ONE under-lock edit, the delete deferred",
      len(inline_s) == 1 and inline_s[0][0] == "edit"
      and [d[0] for d in deferred_s] == ["delete"],
      "inline=%r deferred=%r" % (inline_s, [d[0] for d in deferred_s]))

print("\n----- 6i a zero-tool turn still just deletes its empty box -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="hey"))
empty_box = load().get("msgid")
reply_lands()
run_reply(tg, final=False)
run_reply(tg, final=True)
st6i = load()
check("6i the empty box is deleted and nothing is left in the chat",
      surviving(tg) == []
      and any(c[0] == "delete" and str(c[1]) == str(empty_box) for c in tg.calls),
      "alive=%r calls=%r" % (surviving(tg), tg.calls))
check("6i no recap is ever written for a turn that did no work",
      md_edits(tg) == [], "md_edits=%r" % md_edits(tg))
check("6i state carries no box afterwards", not st6i.get("msgid"),
      "msgid=%s" % st6i.get("msgid"))

print("\n----- 6j an interrupted turn collapses ABOVE the answer on the next prompt -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="first message"))
old_box6j = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/j1"}, "j1")
answer6j = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Bash", {"command": "still working"}, "j2")
mid_box6j = load().get("msgid")

mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="second message"))
st6j = load()
alive6j = surviving(tg)
deleted6j = [str(c[1]) for c in tg.calls if c[0] == "delete"]
recap6j = md_edits(tg, old_box6j)
check("6j the trailing box that sat BELOW the answer is deleted, never folded there",
      str(mid_box6j) in deleted6j and str(mid_box6j) not in alive6j,
      "deleted=%r alive=%r mid=%s" % (deleted6j, alive6j, mid_box6j))
check("6j the recap ABOVE the answer survives and carries the whole abandoned turn",
      str(old_box6j) in alive6j and bool(recap6j)
      and all(x in recap6j[-1][2] for x in ("1 file", "1 command")),
      "alive=%r old=%s recap=%r" % (alive6j, old_box6j,
                                    recap6j[-1][2].splitlines()[1] if recap6j else None))
check("6j the only streamer message below that answer is the NEW turn's live box",
      below(alive6j, answer6j) == [str(st6j.get("msgid"))],
      "below=%r new_box=%s" % (below(alive6j, answer6j), st6j.get("msgid")))
check("6j exactly two messages remain: the old turn's recap + the new turn's live box",
      len(alive6j) == 2, "alive=%r" % alive6j)
check("6j the new prompt opened a fresh box and reset the position bookkeeping",
      st6j.get("msgid") not in (None, mid_box6j, old_box6j)
      and not st6j.get("folded_above_msgid") and st6j.get("box_is_last") is True
      and st6j.get("replied_this_turn") is False,
      "msgid=%s above=%s last=%s replied=%s" % (
          st6j.get("msgid"), st6j.get("folded_above_msgid"),
          st6j.get("box_is_last"), st6j.get("replied_this_turn")))

print("\n===== #7 (v21) a second Stop for the same turn is harmless =====")

def prior_turn(tg, text="the previous question"):

    mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=text))
    rec = load().get("msgid")
    do_tool(tg, "Read", {"file_path": "/prev/context.md"}, "P1")
    ans = reply_lands()
    run_reply(tg, final=False)
    run_reply(tg, final=True)
    return rec, ans

def grace_armed():
    return load().get("noreply_grace_until")

def burn_fuse(tg):

    st = load()
    if st and st.get("noreply_grace_until") is not None:
        st["noreply_grace_until"] = 0
        mod.save_state(SDIR, st)
    return mod._noreply_grace_sweep(SDIR, tg)

print("\n----- 7a Stop, guard blocks, Stop again -> the recap is untouched -----")
reset_state()
tg = RecTG()
recap7a, answer7a = prior_turn(tg)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="work then answer in the terminal"))
box7a = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/k1"}, "k1")
do_tool(tg, "Bash", {"command": "ls"}, "k2")
run_reply(tg, final=True)
st7a1 = load()
mark7a = len(tg.calls)
run_reply(tg, final=True)
tail7a = tg.calls[mark7a:]
st7a2 = load()

check("7a Stop #1 folds the box IN PLACE and arms the fuse - it deletes nothing",
      st7a1.get("folded") is True and str(st7a1.get("msgid")) == str(box7a)
      and str(box7a) in surviving(tg) and bool(md_edits(tg, box7a))
      and not [c for c in tg.calls if c[0] == "delete"]
      and st7a1.get("noreply_grace_until") is not None,
      "folded=%s msgid=%s grace=%s" % (st7a1.get("folded"), st7a1.get("msgid"),
                                       st7a1.get("noreply_grace_until")))

check("7a Stop #1 consumed the reply flag and KEPT the position fact for the cleanup",
      st7a1.get("replied_this_turn") is False and st7a1.get("box_is_last") is True,
      "replied=%s last=%s" % (st7a1.get("replied_this_turn"), st7a1.get("box_is_last")))
check("7a Stop #2 makes NO Telegram call at all", tail7a == [], "tail=%r" % (tail7a,))
check("7a Stop #2 leaves the box and its fuse exactly as Stop #1 left them",
      str(st7a2.get("msgid")) == str(box7a) and st7a2.get("folded") is True
      and st7a2.get("noreply_grace_until") == st7a1.get("noreply_grace_until"),
      "msgid=%s grace=%s" % (st7a2.get("msgid"), st7a2.get("noreply_grace_until")))

burn_fuse(tg)
st7a3 = load()

check("7a the carried recap is still in the chat and still holds the merged work",
      surviving(tg) == [str(recap7a)]
      and "continued" in md_edits(tg, recap7a)[-1][2]
      and "ls" in md_edits(tg, recap7a)[-1][2]
      and str(st7a3.get("prev_recap_msgid")) == str(recap7a),
      "alive=%r prev=%s" % (surviving(tg), st7a3.get("prev_recap_msgid")))
check("7a nothing the streamer sent survives below the previous answer",
      below(surviving(tg), answer7a) == [],
      "alive=%r answer=%d" % (surviving(tg), answer7a))

print("\n----- 7b Stop, guard blocks, more work, Stop again -> folds in place -----")
reset_state()
tg = RecTG()
recap7b, answer7b = prior_turn(tg)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="keep going"))
box7b = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/l1"}, "l1")
run_reply(tg, final=True)
do_tool(tg, "Read", {"file_path": "/l2"}, "l2")

box7b2 = load().get("msgid")
check("7b the resumed tool revives the SAME box and defuses the fuse",
      str(box7b2) == str(box7b) and load().get("folded") is False
      and load().get("noreply_grace_until") is None
      and not [c for c in tg.calls if c[0] == "delete"],
      "msgid=%s folded=%s grace=%s" % (box7b2, load().get("folded"),
                                       load().get("noreply_grace_until")))
run_reply(tg, final=True)
st7b = load()
check("7b Stop #2 folds the resumed box in place and re-arms the fuse",
      st7b.get("folded") is True and str(st7b.get("msgid")) == str(box7b)
      and st7b.get("noreply_grace_until") is not None,
      "msgid=%s grace=%s" % (st7b.get("msgid"), st7b.get("noreply_grace_until")))
burn_fuse(tg)
st7b2 = load()
check("7b once the fuse burns down the box goes, leaving nothing below the answer",
      st7b2.get("folded") is True and st7b2.get("msgid") is None
      and below(surviving(tg), answer7b) == [],
      "msgid=%s alive=%r" % (st7b2.get("msgid"), surviving(tg)))

_r7b = md_edits(tg, recap7b)[-1][2] if md_edits(tg, recap7b) else ""
check("7b the carried recap covers BOTH work chunks",
      "l1" in _r7b and "l2" in _r7b and _r7b.count("continued") == 1,
      "continued=%d body=%r" % (_r7b.count("continued"), _r7b.splitlines()[1:3]))
check("7b one message in the chat: the carried recap",
      surviving(tg) == [str(recap7b)], "alive=%r" % surviving(tg))

print("\n----- 7c Stop, guard blocks, the model replies + files, Stop again -----")
reset_state()
tg = RecTG()
recap7c, prev_answer7c = prior_turn(tg)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="answer me properly"))
box7c = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/m1"}, "m1")
run_reply(tg, final=True)
answer7c = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Write", {"file_path": "/notes/m.md"}, "m2")
run_reply(tg, final=True)
st7c = load()
alive7c = surviving(tg)
check("7c nothing the streamer sent survives below the answer",
      below(alive7c, answer7c) == [], "alive=%r answer=%d" % (alive7c, answer7c))

check("7c this turn keeps its OWN recap, folded above the answer",
      str(box7c) in alive7c and int(box7c) < answer7c
      and str(st7c.get("msgid")) == str(box7c) and st7c.get("folded") is True,
      "alive=%r box=%s answer=%d" % (alive7c, box7c, answer7c))
check("7c the previous turn's recap is untouched by any of it",
      str(recap7c) in alive7c and len(md_edits(tg, recap7c)) == 1,
      "alive=%r edits_at_prev=%d" % (alive7c, len(md_edits(tg, recap7c))))

_r7c = md_edits(tg, box7c)[-1][2] if md_edits(tg, box7c) else ""
check("7c BOTH halves survive: the pre-block work and the post-reply filing",
      "reading m1" in _r7c and "editing m\\.md" in _r7c and _r7c.endswith("||"),
      "%r" % (_r7c.splitlines()[1:4] if _r7c else "no recap"))
check("7c no fuse is left armed on a turn that replied",
      load().get("noreply_grace_until") is None,
      "grace=%s" % load().get("noreply_grace_until"))

print("\n----- 7d a slash-command turn never deletes the previous turn's recap -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="first message"))
box7d = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/n1"}, "n1")

reply_lands()
run_reply(tg, final=False)
run_reply(tg, final=True)
sends_before7d = len(tg.ops("send"))
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="/model opus"))
check("7d the slash prompt opens no box and collapses nothing",
      len(tg.ops("send")) == sends_before7d
      and not [c for c in tg.calls if c[0] == "delete"]
      and str(load().get("msgid")) == str(box7d),
      "sends=%d deletes=%r msgid=%s" % (len(tg.ops("send")),
                                        [c for c in tg.calls if c[0] == "delete"],
                                        load().get("msgid")))
do_tool(tg, "Bash", {"command": "switch the model"}, "n2")
run_reply(tg, final=True)
st7d = load()
check("7d the recap is still in the chat after the slash turn ends",
      surviving(tg) == [str(box7d)] and st7d.get("folded") is True,
      "alive=%r folded=%s" % (surviving(tg), st7d.get("folded")))

print("\n===== #8 (v21) react mid-turn, and residue that refuses to be deleted =====")

class FlakyTG(RecTG):

    def __init__(self, fail_delete=(), fail_edit=()):
        RecTG.__init__(self)
        self.fail_delete = set(str(x) for x in fail_delete)
        self.fail_edit = set(str(x) for x in fail_edit)

    def delete(self, msgid):
        if str(msgid) in self.fail_delete:
            self.calls.append(("delete_FAIL", str(msgid)))
            return False
        return RecTG.delete(self, msgid)

    def edit(self, msgid, text, markdown=False):
        if str(msgid) in self.fail_edit:
            self.calls.append(("edit_FAIL", str(msgid), text, markdown))
            return False
        return RecTG.edit(self, msgid, text, markdown)

print("\n----- 8a a react landing during trailing work collapses ABOVE the answer -----")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="answer then file it"))
box8a = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/p1"}, "p1")
answer8a = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Write", {"file_path": "/notes/p.md"}, "p2")
trailing8a = load().get("msgid")
mod._run_deferred(SDIR, tg, mod.on_react(SDIR, tg))
st8a = load()
alive8a = surviving(tg)
check("8a the react deleted the trailing box instead of folding it below the answer",
      str(trailing8a) not in alive8a and bool(md_edits(tg, box8a)),
      "alive=%r trailing=%s" % (alive8a, trailing8a))
check("8a the abandoned turn's recap survives ABOVE the answer",
      str(box8a) in alive8a and int(box8a) < answer8a,
      "alive=%r box=%s answer=%d" % (alive8a, box8a, answer8a))
check("8a the only streamer message below the answer is the react's fresh box",
      below(alive8a, answer8a) == [str(st8a.get("msgid"))],
      "below=%r new=%s" % (below(alive8a, answer8a), st8a.get("msgid")))

print("\n----- 8b a trailing box that refuses to delete becomes a tracked ghost -----")
reset_state()
tg = FlakyTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="answer then file it"))
box8b = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/q1"}, "q1")
answer8b = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Write", {"file_path": "/notes/q.md"}, "q2")
trailing8b = load().get("msgid")
tg.fail_delete.add(str(trailing8b))
run_reply(tg, final=True)
st8b = load()
check("8b the recap still landed in the message above the answer",
      bool(md_edits(tg, box8b)) and str(st8b.get("msgid")) == str(box8b),
      "msgid=%s md=%d" % (st8b.get("msgid"), len(md_edits(tg, box8b))))
check("8b the surviving trailing box is tracked as a ghost (not permanent residue)",
      str(trailing8b) in [str(g) for g in (st8b.get("ghosts") or [])],
      "ghosts=%r trailing=%s" % (st8b.get("ghosts"), trailing8b))
tg.fail_delete.discard(str(trailing8b))
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="next message"))
check("8b the next prompt's ghost sweep removes it",
      str(trailing8b) not in surviving(tg),
      "alive=%r trailing=%s" % (surviving(tg), trailing8b))

print("\n----- 8c a stale recap that refuses to delete keeps the ONE-recap rule -----")
reset_state()
tg = FlakyTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="update, answer, then file"))
box_a8c = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/r1"}, "r1")
r1_8c = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Bash", {"command": "more work"}, "r2")
box_b8c = load().get("msgid")
r2_8c = reply_lands()
run_reply(tg, final=False)
tg.fail_delete.add(str(box_a8c))
do_tool(tg, "Write", {"file_path": "/notes/r.md"}, "r3")
st8c_mid = load()
check("8c the refold retired the vacated box instead, keeping ONE recap",
      str(box_b8c) not in surviving(tg) and str(box_a8c) in surviving(tg),
      "alive=%r a=%s b=%s" % (surviving(tg), box_a8c, box_b8c))
check("8c state repointed the fold-above at the message that really holds the recap",
      str(st8c_mid.get("folded_above_msgid")) == str(box_a8c),
      "above=%s a=%s" % (st8c_mid.get("folded_above_msgid"), box_a8c))
run_reply(tg, final=True)
st8c = load()
alive8c = surviving(tg)
check("8c exactly one message survives the turn and it is that recap",
      alive8c == [str(box_a8c)] and str(st8c.get("msgid")) == str(box_a8c),
      "alive=%r msgid=%s" % (alive8c, st8c.get("msgid")))
check("8c nothing survives below the final answer",
      below(alive8c, r2_8c) == [], "alive=%r answer=%d" % (alive8c, r2_8c))
check("8c that recap carries the FULL turn (all three families)",
      all(x in md_edits(tg, box_a8c)[-1][2] for x in ("1 file", "1 command", "1 edit")),
      md_edits(tg, box_a8c)[-1][2].splitlines()[1] if md_edits(tg, box_a8c) else "none")

print("\n===== #9 (v22) the mission line =====")

MISSION_PATH = os.path.join(STATE_DIR, "mission.txt")

def clear_mission():
    try:
        os.remove(MISSION_PATH)
    except OSError:
        pass

def write_mission(text, mtime=None):
    with open(MISSION_PATH, "w") as f:
        f.write(text)
    if mtime is not None:
        os.utime(MISSION_PATH, (mtime, mtime))

def force_edit_window():

    st = load()
    st["last_edit"] = 0
    mod.save_state(SDIR, st)

print("\n----- 9a the mission derived from the user's prompt body -----")
clear_mission()
check("9a a normal body becomes the mission verbatim",
      mod.mission_from_prompt("fix the fold bug in the streamer")
      == "fix the fold bug in the streamer")
check("9a an empty body reads as 'working', never a blank line",
      mod.mission_from_prompt("") == mod.MISSION_EMPTY,
      "got=%r" % mod.mission_from_prompt(""))
check("9a a whitespace-only body reads as 'working'",
      mod.mission_from_prompt("   \n\t  ") == mod.MISSION_EMPTY)
check("9a '(voice message)' reads as 'voice note'",
      mod.mission_from_prompt("(voice message)") == mod.MISSION_VOICE,
      "got=%r" % mod.mission_from_prompt("(voice message)"))
check("9a 'Voice Message' (no parens, mixed case) also reads as 'voice note'",
      mod.mission_from_prompt("Voice Message") == mod.MISSION_VOICE)
_m500 = mod.mission_from_prompt("x" * 500)
check("9a a 500-char body is clipped to the cap with an ellipsis",
      len(_m500) == mod.MISSION_CAP and _m500.endswith("…"),
      "len=%d tail=%r" % (len(_m500), _m500[-3:]))
check("9a newlines collapse - the mission is always ONE line",
      "\n" not in mod.mission_from_prompt("line one\nline two\n\n  line three"),
      "got=%r" % mod.mission_from_prompt("line one\nline two\n\n  line three"))

print("\n----- 9b the line is in the very first frame, above the unchanged header -----")
reset_state()
clear_mission()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="ship the agent panel"))
open_frame = tg.ops("send")[-1]
frame_lines = open_frame[2].splitlines()

check("9b line 1 of the box is the mission",
      frame_lines[0] == "🎯 " + mod.MISSION_DEFAULT, "line1=%r" % frame_lines[0])
check("9b the box NEVER opens on an echo of the prompt body (v24)",
      "ship the agent panel" not in open_frame[2], "frame=%r" % open_frame[2][:80])
check("9b line 2 is still the v21 👾 header (bar + timer), unchanged",
      frame_lines[1].startswith("👾 Working · ") and frame_lines[1].endswith(" · 0:00"),
      "line2=%r" % frame_lines[1])
check("9b the live box is still sent as PLAIN TEXT (markdown False)",
      open_frame[3] is False, "markdown=%r" % (open_frame[3],))
check("9b on_prompt stored the derived mission in state",
      load().get("mission") == "ship the agent panel", "mission=%r" % load().get("mission"))
check("9b a fresh box starts with an EMPTY agent panel",
      load().get("panel") == [], "panel=%r" % load().get("panel"))

print("\n----- 9c mission.txt: fresh wins, stale is ignored, first line only -----")
_ps = load().get("proc_start")
write_mission("rebuilding the fold logic\nsecond line must be ignored\n", mtime=_ps + 1)
check("9c a mission.txt written DURING the turn wins over the prompt-derived one",
      mod.current_mission(SDIR, load()) == "rebuilding the fold logic",
      "got=%r" % mod.current_mission(SDIR, load()))
check("9c only the FIRST line of the file is used",
      "second line" not in mod.current_mission(SDIR, load()))
write_mission("stale text left over from the previous turn", mtime=_ps - 5)

check("9c a mission.txt older than this turn's proc_start is IGNORED (no leak)",
      mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT,
      "got=%r" % mod.current_mission(SDIR, load()))
write_mission("y" * 400, mtime=_ps + 1)
check("9c an over-long override line is clipped to the cap like any other mission",
      len(mod.current_mission(SDIR, load())) == mod.MISSION_CAP)
check("9c the override is NEVER written back into state (removing the file reverts)",
      load().get("mission") == "ship the agent panel")
clear_mission()

check("9c with the file gone the placeholder is used again (never the prompt)",
      mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT
      and "ship the agent panel" not in mod.current_mission(SDIR, load()))

run_step(tg, "pre", "Read", {"file_path": "/mm1"}, tuid="mm1")
write_mission("mid-turn: now auditing the diff", mtime=time.time())
force_edit_window()
tg.calls.clear()
run_step(tg, "post", "Read", {"file_path": "/mm1"}, tuid="mm1")
live_edits9 = [c for c in tg.calls if c[0] == "edit" and c[3] is False]
check("9c a mission.txt appearing MID-TURN shows on the next render",
      bool(live_edits9)
      and live_edits9[-1][2].splitlines()[0] == "🎯 mid-turn: now auditing the diff",
      "edits=%d line1=%r" % (len(live_edits9),
                             live_edits9[-1][2].splitlines()[0] if live_edits9 else None))

print("\n----- 9d the folded recap carries the mission as its first body line -----")

run_reply(tg, final=False)
tg.calls.clear()
run_reply(tg, final=True)
fold9 = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
fold9_lines = fold9[-1][2].split("\n") if fold9 else []
check("9d the recap header is byte-identical to v21",
      bool(fold9_lines) and fold9_lines[0].startswith("📋 What I did · ")
      and fold9_lines[0].endswith(" · tap to expand"),
      "header=%r" % (fold9_lines[0] if fold9_lines else None))
check("9d the mission is the FIRST body line inside the expandable",
      len(fold9_lines) > 1 and fold9_lines[1] == "**>🎯 mid\\-turn: now auditing the diff",
      "body1=%r" % (fold9_lines[1] if len(fold9_lines) > 1 else None))
check("9d the existing step detail is untouched below the new zone",
      any(ln.startswith(">✓ 🔍 reading mm1") for ln in fold9_lines),
      "lines=%r" % fold9_lines[:4])
clear_mission()

print("\n----- 9e a new prompt resets the mission and the panel -----")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="second message entirely"))
st9e = load()
check("9e the new turn carries THIS prompt's mission",
      st9e.get("mission") == "second message entirely", "mission=%r" % st9e.get("mission"))
check("9e the new turn's panel is empty (no agent rows leak across prompts)",
      st9e.get("panel") == [], "panel=%r" % st9e.get("panel"))

print("\n===== #10 (v22) the agent fleet panel =====")

AG_IN = {"description": "fix streamer fold bug", "subagent_type": "opus-executor",
         "model": "claude-opus-5"}

print("\n----- 10a a row is born on the Agent dispatch, under the unchanged header -----")
reset_state()
clear_mission()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="fan out the work"))
check("10a a turn that dispatched nothing renders NO panel at all",
      "🤖 Agents" not in tg.ops("send")[-1][2])
run_step(tg, "pre", "Agent", AG_IN, tuid="A1")
panel10 = load().get("panel") or []
check("10a the Agent PreToolUse created exactly one row, keyed by its tool_use id",
      len(panel10) == 1 and panel10[0].get("tuid") == "A1", "panel=%r" % panel10)
check("10a the row captured description, subagent_type and the model VERBATIM",
      panel10[0].get("lab") == "fix streamer fold bug"
      and panel10[0].get("typ") == "opus-executor"
      and panel10[0].get("mdl") == "claude-opus-5", "row=%r" % panel10[0])
check("10a the row starts ACTIVE (not done)", panel10[0].get("done") is False)
st10 = load()
live10 = mod.render_live(st10["steps"], st10["frame"], st10["proc_start"],
                         st10["agents"], mod.current_mission(SDIR, st10), st10["panel"])
l10 = live10.splitlines()
check("10a the panel block sits directly UNDER the 👾 header line",
      l10[1].startswith("👾 Working · ") and l10[2].startswith("🤖 Agents · "),
      "lines=%r" % l10[:4])
check("10a the row renders status, type, model, label and a clock",
      l10[3] == "  ↳ ⏳ opus-executor · opus-5 · fix streamer fold bug · 0:00",
      "row=%r" % l10[3])
check("10a the v21 inline subagent tree is untouched (still its own numbered line)",
      any("🤖 1 opus-executor" in x for x in
          mod.render_live([{"fam": "helpers", "cap": "doing work", "tuid": "", "done": False,
                            "k": "subwork", "aid": "z1"}], 1, time.time() - 5,
                          {"z1": {"n": 1, "type": "opus-executor"}}).splitlines()))

print("\n----- 10b PostToolUse closes the row and FREEZES its clock -----")
st10 = load()
st10["panel"][0]["t0"] = time.time() - 723
mod.save_state(SDIR, st10)
run_step(tg, "post", "Agent", AG_IN, tuid="A1")
row10 = (load().get("panel") or [{}])[0]
check("10b the matching PostToolUse marked the row done",
      row10.get("done") is True, "row=%r" % row10)
check("10b the runtime was frozen at the elapsed time, not zeroed",
      720 <= row10.get("secs", 0) <= 727, "secs=%r" % row10.get("secs"))
_T = time.time()
check("10b a DONE row's clock does NOT climb on later ticks",
      mod.panel_lines(load().get("panel"), now=_T)
      == mod.panel_lines(load().get("panel"), now=_T + 600),
      "t=%r  t+600=%r" % (mod.panel_lines(load().get("panel"), now=_T),
                          mod.panel_lines(load().get("panel"), now=_T + 600)))
_active_only = [{"tuid": "x", "lab": "running", "typ": "opus-executor", "mdl": "opus-5",
                 "t0": _T - 10, "done": False, "secs": 0}]
check("10b an ACTIVE row's clock DOES climb on every tick, like the header timer",
      mod.panel_lines(_active_only, now=_T)[1].endswith("0:10")
      and mod.panel_lines(_active_only, now=_T + 113)[1].endswith("2:03"),
      "%r / %r" % (mod.panel_lines(_active_only, now=_T)[1],
                   mod.panel_lines(_active_only, now=_T + 113)[1]))

print("\n----- 10c the count line -----")
_mixed = [{"tuid": "a", "lab": "one", "typ": "opus-executor", "mdl": "claude-opus-5",
           "t0": _T - 723, "done": False, "secs": 0},
          {"tuid": "b", "lab": "two", "typ": "opus-executor", "mdl": "claude-opus-5",
           "t0": _T - 900, "done": False, "secs": 0},
          {"tuid": "c", "lab": "three", "typ": "opus-executor", "mdl": "claude-opus-5",
           "t0": _T - 1500, "done": True, "secs": 1180}]
_ml = mod.panel_lines(_mixed, now=_T)
check("10c the count line reads '🤖 Agents · 2 active · 1 done'",
      _ml[0] == "🤖 Agents · 2 active · 1 done", "got=%r" % _ml[0])
check("10c active rows are listed FIRST, the done row after them",
      _ml[1].startswith("  ↳ ⏳") and _ml[2].startswith("  ↳ ⏳")
      and _ml[3].startswith("  ↳ ✓"), "rows=%r" % _ml[1:])
check("10c a zero count is omitted rather than printed",
      mod.panel_lines(_mixed[:2], now=_T)[0] == "🤖 Agents · 2 active"
      and mod.panel_lines(_mixed[2:], now=_T)[0] == "🤖 Agents · 1 done",
      "%r / %r" % (mod.panel_lines(_mixed[:2], now=_T)[0],
                   mod.panel_lines(_mixed[2:], now=_T)[0]))

print("\n----- 10d the 6-row cap and the '+N more' overflow line -----")
_panel12 = []
for i in range(12):
    _panel12.append({"tuid": "t%d" % i, "lab": "agent job number %d" % i,
                     "typ": "opus-executor", "mdl": "claude-opus-5",
                     "t0": _T - (100 + i), "done": (i % 3 == 0), "secs": 90 + i})
_l12 = mod.panel_lines(_panel12, now=_T)
check("10d the live panel shows a count line + 6 rows + one overflow line",
      len(_l12) == 8, "lines=%d %r" % (len(_l12), _l12))
check("10d the overflow line names the exact hidden count",
      _l12[-1] == "  ↳ … +6 more", "got=%r" % _l12[-1])
check("10d the counts still report the WHOLE fleet, not just the shown rows",
      _l12[0] == "🤖 Agents · 8 active · 4 done", "got=%r" % _l12[0])
_r12 = mod.panel_recap_lines(_panel12, now=_T)
check("10d the recap caps at 6 rows plus its own '+6 more'",
      len(_r12) == 7 and _r12[-1] == "+6 more", "lines=%d last=%r" % (len(_r12), _r12[-1]))
check("10d the recap lists rows in DISPATCH order (a stable roster)",
      _r12[0].endswith("agent job number 0 · 1:30")
      and _r12[5].startswith("🤖 opus-executor"), "first=%r" % _r12[0])

print("\n----- 10e the model column: verbatim in state, cleaned on screen -----")
check("10e 'claude-opus-5' displays as 'opus-5'", mod.model_label("claude-opus-5") == "opus-5")
check("10e a model with no claude- prefix is shown as-is",
      mod.model_label("sonnet") == "sonnet")
check("10e an ABSENT model shows the placeholder, never an invented name",
      mod.model_label("") == mod.PANEL_NO_MODEL and mod.model_label(None) == mod.PANEL_NO_MODEL
      and mod.PANEL_NO_MODEL == "session")
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="dispatch with no model named"))
run_step(tg, "pre", "Agent", {"description": "no model given", "subagent_type": "general-purpose"},
         tuid="NM1")
_nm = (load().get("panel") or [{}])[0]
check("10e a dispatch that named no model stores an empty model and renders 'session'",
      _nm.get("mdl") == "" and " · session · " in mod.panel_lines(load().get("panel"))[1],
      "row=%r line=%r" % (_nm, mod.panel_lines(load().get("panel"))[1]))
check("10e a dispatch with no subagent_type falls back to 'agent' (never blank)",
      mod._panel_open([], "z", {"description": "d"}, _T)[0]["typ"] == "agent")

print("\n===== #11 (v22) escaping torture and the 4096 UTF-16 cap =====")

TORTURE = "_*[]()~`>#+-=|{}.!\\ 🤖 <b>x</b> 100% \"q\" 'q'"

def mdv2_fully_escaped(msg):

    def walk(s):
        i = 0
        while i < len(s):
            c = s[i]
            if c == "\\":
                if i + 1 >= len(s) or s[i + 1] not in mod.MDV2_SPECIAL:
                    return False
                i += 2
                continue
            if c in mod.MDV2_SPECIAL:
                return False
            i += 1
        return True

    lines = msg.split("\n")
    if not walk(lines[0]):
        return False
    body = lines[1:]
    for k, ln in enumerate(body):
        if k == 0:
            if not ln.startswith("**>"):
                return False
            ln = ln[3:]
        else:
            if not ln.startswith(">"):
                return False
            ln = ln[1:]
        if k == len(body) - 1:
            if not ln.endswith("||"):
                return False
            ln = ln[:-2]
        if not walk(ln):
            return False
    return True

check("11a the validator itself rejects an unescaped special char (sanity)",
      not mdv2_fully_escaped("h\n**>a_b||"))
check("11a the validator accepts a properly escaped body (sanity)",
      mdv2_fully_escaped("h\n**>a\\_b||"))

reset_state()
clear_mission()
tg = RecTG()

write_mission(TORTURE, mtime=time.time() + 5)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=TORTURE))
open_t = tg.ops("send")[-1]
check("11a the torture mission reaches the LIVE box VERBATIM (never escaped)",
      open_t[2].splitlines()[0] == "🎯 " + TORTURE and "\\_" not in open_t[2],
      "line1=%r" % open_t[2].splitlines()[0])
run_step(tg, "pre", "Agent", {"description": TORTURE, "subagent_type": "opus_executor[1]",
                              "model": "claude-opus-5[1m]"}, tuid="TT1")
run_step(tg, "post", "Agent", {"description": TORTURE, "subagent_type": "opus_executor[1]",
                               "model": "claude-opus-5[1m]"}, tuid="TT1")
run_step(tg, "pre", "Read", {"file_path": "/x/weird_[1](2).md"}, tuid="TT2")
run_step(tg, "post", "Read", {"file_path": "/x/weird_[1](2).md"}, tuid="TT2")
st11 = load()
live11 = mod.render_live(st11["steps"], st11["frame"], st11["proc_start"], st11["agents"],
                         mod.current_mission(SDIR, st11), st11["panel"])
_live_rows11 = [x for x in live11.splitlines() if x.startswith(mod.SUB)]
check("11a the torture agent row is verbatim in the live box, with no escapes anywhere",
      bool(_live_rows11) and mod.short(TORTURE, mod.PANEL_LABEL_CAP) in _live_rows11[0]
      and "\\_" not in live11 and "\\[" not in live11 and "\\." not in live11,
      "panelrow=%r" % (_live_rows11[0] if _live_rows11 else None))
check("11a every live frame this turn was sent/edited with markdown=False",
      all(c[3] is False for c in tg.calls if c[0] in ("send", "edit")),
      "md_flags=%r" % [c[3] for c in tg.calls if c[0] in ("send", "edit")])

run_reply(tg, final=False)
tg.calls.clear()
run_reply(tg, final=True)
fold11 = [c for c in tg.calls if c[0] == "edit" and c[3] is True]
recap11 = fold11[-1][2] if fold11 else ""
check("11b the folded recap is FULLY escaped, valid MarkdownV2",
      bool(recap11) and mdv2_fully_escaped(recap11),
      "recap head=%r" % recap11.split("\n")[1:3])
check("11b the torture text really is in there (escaped, not stripped)",
      "\\_\\*\\[\\]\\(\\)" in recap11, "recap=%r" % recap11[:200])
check("11b the mission line inside the recap is escaped too",
      "**>🎯 \\_\\*\\[\\]" in recap11, "line=%r" % recap11.split("\n")[1][:60])
check("11b a real-shaped editMessageText payload stays under Telegram's 4096 cap",
      mod.tg_len(recap11) <= 4096 and mod.tg_len(recap11) <= mod.TG_HARD_LIMIT,
      "utf16=%d" % mod.tg_len(recap11))
clear_mission()

print("\n----- 11c the worst case: 300 steps + 12 agents + a max-length mission -----")
_big_steps = []
for i in range(300):
    _big_steps.append({"fam": "files", "cap": "reading %s_%d[x].md" % (TORTURE, i),
                       "tuid": "b%d" % i, "done": True, "k": "", "aid": ""})
_big_mission = mod.mission_from_prompt(TORTURE * 40)
_big_recap, _ = mod.render_final(_big_steps, time.time() - 900, 12, _big_mission, _panel12)
check("11c the clamped recap is still valid MarkdownV2",
      mdv2_fully_escaped(_big_recap), "head=%r" % _big_recap.split("\n")[1][:80])
check("11c the clamped recap is under the wire cap",
      mod.tg_len(_big_recap) <= mod.TG_HARD_LIMIT, "utf16=%d" % mod.tg_len(_big_recap))
check("11c the new zones survived the clamp (steps are dropped first, not the zones)",
      "🎯" in _big_recap and "🤖 opus" in _big_recap and "earlier omitted" in _big_recap)
_big_live = mod.render_live(_big_steps, 7, time.time() - 900,
                            {"a": {"n": 1, "type": "opus-executor"}}, _big_mission, _panel12)
check("11c the live box with the same load is under the cap",
      mod.tg_len(_big_live) <= mod.TG_HARD_LIMIT, "utf16=%d" % mod.tg_len(_big_live))

print("\n----- 11d under a squeezed budget the NEW zones give way first -----")
_small_steps = [{"fam": "files", "cap": "reading a.py", "tuid": "s1", "done": True,
                 "k": "", "aid": ""}]
_saved_limit = mod.TG_HARD_LIMIT
try:
    mod.TG_HARD_LIMIT = 10
    _squeezed = mod.render_live(_small_steps, 2, _T - 30, {}, "a mission line", _panel12)
finally:
    mod.TG_HARD_LIMIT = _saved_limit
_v21_shape = mod.render_live(_small_steps, 2, _T - 30, {})
check("11d fully squeezed, the box is byte-identical to the v21 box",
      _squeezed == _v21_shape, "squeezed=%r" % _squeezed)
check("11d with no mission and no panel the box is the v21 box, unconditionally",
      mod.render_live(_small_steps, 2, _T - 30, {}, "", None) == _v21_shape)
check("11d and the un-squeezed box really did carry both zones (the test means something)",
      mod.render_live(_small_steps, 2, _T - 30, {}, "a mission line", _panel12)
      != _v21_shape)

print("\n===== #12 (v22) a v21-shaped state file upgrades mid-turn =====")
V21_KEYS = V19_KEYS + ["box_is_last", "folded_above_msgid", "replied_this_turn"]

def write_v21_state(**over):
    now = time.time()
    st = {"msgid": "9001",
          "steps": [{"fam": "files", "cap": "reading mid-turn.md", "tuid": "w1",
                     "done": True, "k": "", "aid": ""}],
          "proc_start": now - 30, "last_edit": 0, "last_activity": now - 2,
          "last_reply": now - 2, "last_react": now - 30, "last_spawn": now - 30,
          "frame": 4, "folded": False, "last_live": "old live text", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
          "box_is_last": False, "folded_above_msgid": None, "replied_this_turn": False}
    st.update(over)
    mod.save_state(SDIR, st)
    return st

reset_state()
clear_mission()
write_v21_state()
check("12 the fixture really is v21-shaped (no mission, no panel key)",
      set(load().keys()) == set(V21_KEYS), "keys=%r" % sorted(load().keys()))
tg12 = RecTG()
run_step(tg12, "pre", "Bash", {"command": "keep working on v21 state"}, tuid="w2")
st12 = load()
rendered12 = [c[2] for c in tg12.calls if c[0] in ("send", "edit")]

check("12 a tool event over v21 state renders the PLACEHOLDER mission and NO panel",
      all("🤖 Agents" not in t for t in rendered12)
      and all(ln == "🎯 " + mod.MISSION_DEFAULT
              for t in rendered12 for ln in t.splitlines() if ln.startswith("🎯")),
      "rendered=%r" % [t.splitlines()[0] for t in rendered12])
check("12 the in-flight box keeps its msgid and its steps (nothing reset)",
      st12.get("msgid") == "9001" and len(st12.get("steps") or []) == 2,
      "msgid=%s steps=%d" % (st12.get("msgid"), len(st12.get("steps") or [])))
check("12 the rewrite initialized the two new keys with safe defaults",
      st12.get("mission") == "" and st12.get("panel") == [],
      "mission=%r panel=%r" % (st12.get("mission"), st12.get("panel")))
run_step(tg12, "post", "Bash", {"command": "keep working on v21 state"}, tuid="w2")
run_reply(tg12, final=True)
fold12 = [c for c in tg12.calls if c[0] == "edit" and c[3] is True]

check("12 Stop over upgraded state still folds in place, with no new zones in the recap",
      bool(fold12) and "**>🎯 " + mod.MISSION_DEFAULT in fold12[-1][2]
      and "🤖 opus" not in fold12[-1][2]
      and load().get("folded") is True,
      "folded=%s" % load().get("folded"))

reset_state()
write_v21_state(reanchor=True, box_is_last=True, replied_this_turn=True,
                folded_above_msgid="9000")
ok12, out12 = True, b""
for mode, payload in [
        ("pre", {"tool_name": "Agent", "tool_input": {"description": "go", "subagent_type": "x"},
                 "tool_use_id": "v1"}),
        ("post", {"tool_name": "Agent", "tool_input": {"description": "go", "subagent_type": "x"},
                  "tool_use_id": "v1"}),
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("prompt", {"prompt": "<channel source=\"telegram\">next message</channel>"}),
        ("prompt", {"prompt": "/fresh"}),
]:
    p = drive_main(mode, payload)
    if p.returncode != 0:
        ok12 = False
    out12 += p.stdout
check("12 every hook edge over a v21-shaped state exits 0 (incl. an Agent dispatch)", ok12)

check("12 every hook edge over a v21-shaped state prints nothing", strip_nudges(out12) == b"",
      "stdout=%r" % strip_nudges(out12)[:120])
check("12 a degenerate panel value (null / wrong type) never crashes a render",
      mod.panel_lines(None) == [] and mod.panel_lines([]) == []
      and mod.render_final([{"fam": "files", "cap": "x", "tuid": "", "done": True,
                             "k": "", "aid": ""}], time.time(), 0, "", None)[0] is not None)

print("\n===== #13 (v22) position invariant with a mission + agents + trailing work =====")
reset_state()
clear_mission()
tg = RecTG()

write_mission("fan out then file the result", mtime=time.time() + 5)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="fan out then file the result"))
box13 = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/main/a.md"}, "D0")
run_step(tg, "pre", "Agent", AG_IN, tuid="D1")
run_step(tg, "pre", "Read", {"file_path": "/sub/a"}, tuid="D1a", agent_id="ag-1",
         agent_type="opus-executor")
run_step(tg, "post", "Read", {"file_path": "/sub/a"}, tuid="D1a", agent_id="ag-1",
         agent_type="opus-executor")
run_step(tg, "post", "Agent", AG_IN, tuid="D1")
do_tool(tg, "Bash", {"command": "build"}, "D2")
answer13 = reply_lands()
run_reply(tg, final=False)
do_tool(tg, "Write", {"file_path": "/notes/13.md"}, "D3")
run_reply(tg, final=True)
st13 = load()
alive13 = surviving(tg)
recap13 = md_edits(tg, box13)
check("13 nothing the streamer sent survives below the final answer",
      len(below(alive13, answer13)) == 0, "alive=%r answer=%d" % (alive13, answer13))
check("13 exactly ONE streamer message survives and it is above the answer",
      alive13 == [str(box13)] and int(box13) < answer13, "alive=%r" % alive13)
check("13 the recap carries the mission as its first body line",
      bool(recap13) and recap13[-1][2].split("\n")[1] == "**>🎯 fan out then file the result",
      "body1=%r" % (recap13[-1][2].split("\n")[1] if recap13 else None))
check("13 the recap carries the agent roster line (escaped, as MarkdownV2 needs)",
      bool(recap13)
      and ">🤖 opus\\-executor · opus\\-5 · fix streamer fold bug · " in recap13[-1][2],
      "recap=%r" % (recap13[-1][2].split("\n")[2] if recap13 else None))
check("13 the v21 tallies are unchanged and complete (files, subagents, commands, edits)",
      bool(recap13) and all(x in recap13[-1][2]
                            for x in ("1 file", "1 subagent", "1 command", "1 edit")),
      recap13[-1][2].splitlines()[3] if recap13 else "no recap")
check("13 the recap is valid MarkdownV2 end to end",
      bool(recap13) and mdv2_fully_escaped(recap13[-1][2]))
check("13 state is clean: folded, no fold-above, panel + mission still there for the record",
      st13.get("folded") is True and not st13.get("folded_above_msgid")
      and len(st13.get("panel") or []) == 1 and st13.get("mission"),
      "folded=%s above=%s panel=%d" % (st13.get("folded"), st13.get("folded_above_msgid"),
                                       len(st13.get("panel") or [])))
clear_mission()

print("\n===== #14 (v23) mission.txt that is not a regular file =====")


class _StreamBlocked(BaseException):
    pass

def bounded(fn, *a, **kw):
    """Reject a blocking special-file open in process, without arming a signal."""
    kw.pop("limit", 3.0)
    prior_open = getattr(mod, "open", None)
    def guarded_open(path, *args, **kwargs):
        try:
            mode = os.stat(path).st_mode
        except (OSError, TypeError):
            mode = 0
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            raise _StreamBlocked("special-file read would block")
        return builtins.open(path, *args, **kwargs)
    mod.open = guarded_open
    t0 = time.perf_counter()
    try:
        try:
            return fn(*a, **kw), time.perf_counter() - t0, False
        except _StreamBlocked:
            return None, time.perf_counter() - t0, True
    finally:
        if prior_open is None:
            del mod.open
        else:
            mod.open = prior_open

def nuke_mission():

    try:
        if os.path.islink(MISSION_PATH):
            os.remove(MISSION_PATH)
        elif os.path.isdir(MISSION_PATH):
            shutil.rmtree(MISSION_PATH)
        elif os.path.exists(MISSION_PATH):
            os.remove(MISSION_PATH)
    except OSError:
        pass

reset_state()
nuke_mission()
DERIVED14 = "harden the streamer"
tg14 = RecTG()
mod._run_deferred(SDIR, tg14, mod.on_prompt(SDIR, tg14, prompt_text=DERIVED14))
check("14 baseline: the derived mission is in state before any special file exists",
      load().get("mission") == DERIVED14, "mission=%r" % load().get("mission"))

SPECIALS = []

def make_fifo():
    os.mkfifo(MISSION_PATH)

    os.utime(MISSION_PATH, (time.time() + 5, time.time() + 5))

def make_dir():
    os.makedirs(MISSION_PATH)
    os.utime(MISSION_PATH, (time.time() + 5, time.time() + 5))

def make_dangling_symlink():
    os.symlink(os.path.join(STATE_DIR, "no-such-target-9f3a"), MISSION_PATH)

SPECIALS = [("FIFO", make_fifo), ("directory", make_dir),
            ("dangling symlink", make_dangling_symlink)]

for label, make in SPECIALS:
    nuke_mission()
    make()
    val, dt, blocked = bounded(mod.current_mission, SDIR, load())
    check("14 mission.txt = %-17s the render does NOT block" % label, not blocked,
          "elapsed=%.2fs" % dt)

    check("14 mission.txt = %-17s falls back to the DERIVED mission" % label,
          val == mod.MISSION_DEFAULT, "got=%r" % (val,))
    check("14 mission.txt = %-17s returns immediately (<1s)" % label, dt < 1.0,
          "elapsed=%.3fs" % dt)

nuke_mission()
make_fifo()
force_edit_window()
tg14.calls.clear()
_, dt14, blocked14 = bounded(run_step, tg14, "pre", "Read", {"file_path": "/f/x.py"}, "F14")
live14 = [c for c in tg14.calls if c[0] in ("send", "edit") and c[3] is False]
check("14 a full tool edge over a FIFO mission.txt completes", not blocked14,
      "elapsed=%.2fs" % dt14)

check("14 that edge's live frame carries the derived mission, not a blank zone",
      bool(live14) and live14[-1][2].splitlines()[0] == "🎯 " + mod.MISSION_DEFAULT,
      "line1=%r" % (live14[-1][2].splitlines()[0] if live14 else None))

nuke_mission()
_real = os.path.join(STATE_DIR, "real-mission-target.txt")
with open(_real, "w") as f:
    f.write("via a symlink to a real file\nsecond line ignored\n")
os.utime(_real, (time.time() + 5, time.time() + 5))
os.symlink(_real, MISSION_PATH)
val14s, dt14s, blocked14s = bounded(mod.current_mission, SDIR, load())
check("14 a symlink TO A REAL FILE is still read (the guard is not over-broad)",
      (not blocked14s) and val14s == "via a symlink to a real file", "got=%r" % (val14s,))
nuke_mission()
os.remove(_real)
write_mission("a perfectly ordinary override", mtime=time.time() + 5)
val14r, _, blocked14r = bounded(mod.current_mission, SDIR, load())
check("14 a plain regular mission.txt is read exactly as before (no regression)",
      (not blocked14r) and val14r == "a perfectly ordinary override", "got=%r" % (val14r,))
write_mission("stale from the previous turn", mtime=load().get("proc_start") - 5)
val14t, _, _ = bounded(mod.current_mission, SDIR, load())

check("14 the staleness rule still works through the new stat (no regression)",
      val14t == mod.MISSION_DEFAULT, "got=%r" % (val14t,))
nuke_mission()

print("\n===== #15 (v23) a wrong-type state value cannot wedge the prompt boundary =====")

nuke_mission()

def write_corrupt_state(**over):

    now = time.time()
    st = {"msgid": "7700",
          "steps": [{"fam": "files", "cap": "reading before.md", "tuid": "c0",
                     "done": True, "k": "", "aid": ""}],
          "proc_start": now - 30, "last_edit": 0, "last_activity": now - 2,
          "last_reply": 0, "last_react": now - 30, "last_spawn": now - 30,
          "frame": 4, "folded": False, "last_live": "old live text", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
          "box_is_last": True, "folded_above_msgid": None, "replied_this_turn": False,
          "mission": "the corrupted turn", "panel": []}
    st.update(over)
    with open(os.path.join(SDIR, "state.json"), "w") as f:
        json.dump(st, f)
    return st

def no_raise(fn, *a, **kw):

    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)

CORRUPT_KEYS = [
    ("panel", "not a list"), ("panel", 7), ("panel", {"a": 1}),
    ("steps", "not a list"), ("steps", 12345),
    ("ghosts", "not a list"), ("ghosts", 42),
    ("mission", 12345), ("mission", ["a", "b"]), ("mission", {"m": 1}),
]
PRE15 = {"tool_name": "Read", "tool_input": {"file_path": "/c/x.py"}, "tool_use_id": "K1"}

for key, bad in CORRUPT_KEYS:
    tag = "%s = %r" % (key, bad)

    reset_state()
    write_corrupt_state(**{key: bad})
    p15 = drive_main("pre", PRE15)
    check("15 %-24s the corrupted-turn edge exits 0, silent, no traceback" % tag,
          p15.returncode == 0 and p15.stdout == b""
          and b"Traceback" not in p15.stderr,
          "rc=%d stdout=%r" % (p15.returncode, p15.stdout[:80]))
    st_after = load()
    check("15 %-24s that one edge already re-typed every guarded key" % tag,
          isinstance(st_after.get("steps"), list)
          and isinstance(st_after.get("panel"), list)
          and isinstance(st_after.get("ghosts"), list)
          and isinstance(st_after.get("mission"), str),
          "steps=%s panel=%s ghosts=%s mission=%s"
          % (type(st_after.get("steps")).__name__, type(st_after.get("panel")).__name__,
             type(st_after.get("ghosts")).__name__, type(st_after.get("mission")).__name__))

    reset_state()
    write_corrupt_state(**{key: bad})
    tg15 = RecTG()
    _ops15, _err15 = no_raise(mod.on_prompt, SDIR, tg15, prompt_text="a fresh question")
    check("15 %-24s the NEXT prompt does not raise at the boundary" % tag,
          _err15 is None, "raised=%s" % _err15)
    if _err15 is None:
        mod._run_deferred(SDIR, tg15, _ops15)
    st15 = load()
    check("15 %-24s the NEXT prompt opens a fresh box" % tag,
          bool(tg15.ops("send")) and st15.get("msgid") not in (None, "7700"),
          "sends=%d msgid=%r" % (len(tg15.ops("send")), st15.get("msgid")))
    check("15 %-24s and the fresh state is clean on every guarded key" % tag,
          st15.get("steps") == [] and st15.get("panel") == []
          and st15.get("ghosts") == [] and st15.get("mission") == "a fresh question",
          "steps=%r panel=%r ghosts=%r mission=%r"
          % (st15.get("steps"), st15.get("panel"), st15.get("ghosts"), st15.get("mission")))

reset_state()
write_corrupt_state(panel="nope", steps="nope", ghosts=7, mission=["m"])
ok15, out15 = True, b""
for mode, payload in [
        ("pre", {"tool_name": "Agent", "tool_use_id": "A15",
                 "tool_input": {"description": "go", "subagent_type": "opus-executor"}}),
        ("post", {"tool_name": "Agent", "tool_use_id": "A15", "tool_input": {}}),
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/z"}, "tool_use_id": "R15"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/z"}, "tool_use_id": "R15"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("prompt", {"prompt": "<channel source=\"telegram\">back to normal</channel>"}),
]:
    p = drive_main(mode, payload)
    if p.returncode != 0 or b"Traceback" in p.stderr:
        ok15 = False
    out15 += p.stdout
st15all = load()
check("15 all four keys corrupted at once: every edge still exits 0, no traceback", ok15)

check("15 all four keys corrupted at once: every edge still prints nothing",
      strip_nudges(out15) == b"", "stdout=%r" % strip_nudges(out15)[:120])
check("15 all four keys corrupted at once: the prompt edge STILL opened a fresh box",
      bool(st15all.get("msgid")) and st15all.get("msgid") != "7700"
      and st15all.get("mission") == "back to normal" and st15all.get("panel") == []
      and st15all.get("steps") == [],
      "msgid=%r mission=%r" % (st15all.get("msgid"), st15all.get("mission")))

_pl_ok, _pl_err = no_raise(
    lambda: all(mod.panel_lines(v) == [] and mod.panel_recap_lines(v) == []
                for v in ("not a list", 7, {"a": 1}, None, "")))
check("15 panel_lines / panel_recap_lines yield NO rows for any wrong-type panel",
      _pl_err is None and _pl_ok is True, "raised=%s ok=%r" % (_pl_err, _pl_ok))
_row15 = [{"tuid": "a", "lab": "do a thing", "typ": "opus-executor", "mdl": "claude-opus-5",
           "t0": time.time() - 5, "done": False, "secs": 0}]
check("15 a WELL-FORMED panel still renders its count line + row (guard is not an off-switch)",
      len(mod.panel_lines(_row15)) == 2 and len(mod.panel_recap_lines(_row15)) == 1,
      "live=%r" % (mod.panel_lines(_row15),))

_cm_ok, _cm_err = no_raise(
    lambda: all(mod.current_mission(SDIR, {"proc_start": time.time(), "mission": v})
                == mod.MISSION_DEFAULT
                for v in (12345, ["a"], {"m": 1}, None)))
check("15 current_mission ignores a non-string stored mission",
      _cm_err is None and _cm_ok is True, "raised=%s ok=%r" % (_cm_err, _cm_ok))

check("15 current_mission ignores a WELL-FORMED stored mission too (the echo is dead)",
      mod.current_mission(SDIR, {"proc_start": time.time(),
                                 "mission": "a real mission"}) == mod.MISSION_DEFAULT)
_ops_all, _err_all = no_raise(mod._collapse_prior,
                              {"msgid": "1", "steps": "nope", "ghosts": 5,
                               "panel": "nope", "mission": 3})
check("15 _collapse_prior survives every guarded key corrupted at once and returns ops",
      _err_all is None and isinstance(_ops_all, list),
      "raised=%s ops=%r" % (_err_all, _ops_all))
_ops_del, _err_del = no_raise(mod._collapse_prior,
                              {"msgid": "1", "steps": "nope", "ghosts": [], "panel": []})
check("15 a wrong-type steps collapses to 'no work' (delete), never a half-rendered recap",
      _err_del is None and _ops_del == [("delete", "1", None, False)],
      "raised=%s ops=%r" % (_err_del, _ops_del))
check("15 a well-formed prior turn STILL folds to a recap (no collapse-path regression)",
      mod._collapse_prior({"msgid": "1", "proc_start": time.time() - 5, "ghosts": [],
                           "panel": [], "steps": [{"fam": "files", "cap": "reading a.py",
                                                   "tuid": "t", "done": True, "k": "",
                                                   "aid": ""}]})[0][0] == "edit")

_inner_raised = False
try:
    mod._collapse_prior({"msgid": "1", "proc_start": time.time() - 5, "steps": [None],
                         "ghosts": [], "panel": []})
except Exception:
    _inner_raised = True
check("15 element-level corruption inside a well-typed list STILL raises (v21 parity, "
      "deliberately out of the guard's scope)", _inner_raised)

reset_state()
nuke_mission()

print("\n===== #16 (v24) the mission nudge on the prompt edge =====")

CHANNEL_PROMPT = {"prompt": "<channel source=\"telegram\" chat_id=\"999\">"
                            "fix the fold bug please</channel>"}

def nudge_of(p):
    try:
        return json.loads(p.stdout.decode("utf-8"))
    except Exception:
        return None

reset_state()
nuke_mission()
p16 = drive_main("prompt", CHANNEL_PROMPT)
n16 = nudge_of(p16)
check("16 the prompt edge exits 0 (never 2 - exit 2 is what BLOCKS a prompt)",
      p16.returncode == 0, "rc=%s stderr=%r" % (p16.returncode, p16.stderr[:120]))
check("16 stdout is exactly ONE valid JSON object, nothing before or after it",
      isinstance(n16, dict), "stdout=%r" % p16.stdout[:160])
check("16 the envelope has exactly one top-level key: hookSpecificOutput",
      isinstance(n16, dict) and set(n16.keys()) == {"hookSpecificOutput"},
      "keys=%r" % (sorted(n16.keys()) if isinstance(n16, dict) else None))
_hso16 = (n16 or {}).get("hookSpecificOutput") or {}
check("16 hookEventName is the literal 'UserPromptSubmit' (anything else is DROPPED)",
      _hso16.get("hookEventName") == "UserPromptSubmit",
      "got=%r" % _hso16.get("hookEventName"))
check("16 additionalContext is a non-empty string",
      isinstance(_hso16.get("additionalContext"), str) and _hso16.get("additionalContext"),
      "type=%r" % type(_hso16.get("additionalContext")))
check("16 hookSpecificOutput carries ONLY the two fields it needs",
      set(_hso16.keys()) == {"hookEventName", "additionalContext"},
      "keys=%r" % sorted(_hso16.keys()))

check("16 the payload contains NO blocking field (decision / continue / stopReason)",
      not any(k in p16.stdout.decode("utf-8", "replace")
              for k in ('"decision"', '"continue"', '"stopReason"')),
      "stdout=%r" % p16.stdout[:200])

check("16 no traceback on stderr on the success path", b"Traceback" not in p16.stderr,
      "stderr=%r" % p16.stderr[:160])

_ctx16 = _hso16.get("additionalContext") or ""
_want16 = mod._mission_paths(SDIR)[0]
check("16 the nudge names the RESOLVED absolute mission.txt path, not $TELEGRAM_STATE_DIR",
      _want16 in _ctx16 and os.path.isabs(_want16) and "$TELEGRAM_STATE_DIR" not in _ctx16,
      "want=%r" % _want16)
check("16 the path is the one _mission_override actually reads",
      _want16 == os.path.join(os.path.dirname(SDIR), mod.MISSION_FILE),
      "want=%r" % _want16)
check("16 it identifies its sender so the model knows what is talking to it",
      "[work-streamer]" in _ctx16, "ctx=%r" % _ctx16[:80])
check("16 it asks for ONE line, in the model's own words, present tense",
      "ONE line" in _ctx16 and "your own words" in _ctx16 and "present tense" in _ctx16,
      "ctx=%r" % _ctx16[:200])
check("16 it states the length rule the owner asked for (readable whole, never cut off)",
      "60" in _ctx16 and "cut off" in _ctx16, "ctx=%r" % _ctx16)
check("16 it says the write is hidden, so the model knows it costs no box noise",
      "hidden" in _ctx16, "ctx=%r" % _ctx16)
check("16 it covers the mid-turn update case",
      "mid-turn" in _ctx16, "ctx=%r" % _ctx16)
check("16 it stays short - this rides in EVERY turn on six bots",
      len(mod.mission_nudge_text(
          "/Users/example/.claude/channels/telegram6/mission.txt")) < 700,
      "fixture_chars=%d production_chars=%d" % (
          len(_ctx16), len(mod.mission_nudge_text(
              "/Users/example/.claude/channels/telegram6/mission.txt"))))
check("16 and far under the 10k harness cap where output gets spilled to disk",
      len(_ctx16) < 10000, "chars=%d" % len(_ctx16))
check("16 the nudge is exactly what mission_nudge_text() builds (one source of truth)",
      _ctx16 == mod.mission_nudge_text(_want16))
check("16 it survives a JSON round trip byte-for-byte (json.dumps, never hand-built)",
      json.loads(json.dumps(n16)) == n16)

print("\n----- 16b no box, no nudge: every skip path is silent -----")
_gate_cases = [
    ("a slash command", {"prompt": "/fresh"}, None, None),
    ("a bare slash command in a channel wrapper",
     {"prompt": "<channel source=\"telegram\">/fresh</channel>"}, None, None),
    ("another slash command", {"prompt": "/model opus"}, None, None),
    ("an empty body", {"prompt": "<channel source=\"telegram\"></channel>"}, None, None),
    ("a whitespace-only body",
     {"prompt": "<channel source=\"telegram\">   \n\t </channel>"}, None, None),
    ("a missing prompt field", {}, None, None),
    ("a null prompt field", {"prompt": None}, None, None),
    ("a non-string prompt field", {"prompt": 12345}, None, None),
]
for label, payload, mk, rm in _gate_cases:
    reset_state()
    pg = drive_main("prompt", payload)
    check("16b %-38s emits NOTHING" % label, pg.stdout == b"",
          "stdout=%r" % pg.stdout[:120])
    check("16b %-38s still exits 0" % label, pg.returncode == 0, "rc=%s" % pg.returncode)

reset_state()
_off = os.path.join(STATE_DIR, "stream.off")
open(_off, "w").close()
p_off = drive_main("prompt", CHANNEL_PROMPT)
os.remove(_off)
check("16b the stream.off kill switch silences the nudge too", p_off.stdout == b"",
      "stdout=%r" % p_off.stdout[:120])
check("16b and that edge still exits 0", p_off.returncode == 0, "rc=%s" % p_off.returncode)

reset_state()
p_env = drive_main("prompt", CHANNEL_PROMPT, extra_env={"TELEGRAM_STATE_DIR": ""})
check("16b with TELEGRAM_STATE_DIR unset there is no nudge (not a channel session)",
      p_env.stdout == b"", "stdout=%r" % p_env.stdout[:120])
check("16b and that edge still exits 0", p_env.returncode == 0, "rc=%s" % p_env.returncode)

reset_state()
p_bad = drive_main("prompt", CHANNEL_PROMPT,
                   extra_env={"TELEGRAM_STATE_DIR": os.path.join(STATE_DIR, "no-such-dir")})
check("16b a TELEGRAM_STATE_DIR that is not a directory emits nothing",
      p_bad.stdout == b"", "stdout=%r" % p_bad.stdout[:120])

reset_state()
_other = b""
for mode, payload in [
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/n"}, "tool_use_id": "N1"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/n"}, "tool_use_id": "N1"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("animate", {}),
        ("", {}),
]:
    _other += drive_main(mode, payload).stdout
check("16b no other hook edge emits a single byte", _other == b"",
      "stdout=%r" % _other[:160])

print("\n----- 16c malformed input and broken state never produce broken output -----")
reset_state()
with open(os.path.join(SDIR, "state.json"), "w") as f:
    f.write("{ not valid json ]]]")
p16c = drive_main("prompt", CHANNEL_PROMPT)
check("16c a corrupt state.json does NOT suppress the nudge (it needs no state)",
      nudge_of(p16c) is not None and p16c.returncode == 0,
      "rc=%s stdout=%r" % (p16c.returncode, p16c.stdout[:120]))

def drive_raw(mode, raw_bytes, extra_env=None):
    env = dict(os.environ)
    env["TELEGRAM_STATE_DIR"] = STATE_DIR
    env["STREAM_DRY"] = "1"
    env["STREAM_NO_SPAWN"] = "1"
    if extra_env:
        env.update(extra_env)
    return _sp.run([sys.executable, MOD_PATH, mode], input=raw_bytes,
                   stdout=_sp.PIPE, stderr=_sp.PIPE, env=env, timeout=30)

for label, raw in [("unparseable stdin", b"{ nope ]]]"),
                   ("empty stdin", b""),
                   ("a JSON list instead of an object", b"[1,2,3]"),
                   ("a bare JSON string", b'"hello"'),
                   ("a NUL byte", b"\x00\x00\x00")]:
    reset_state()
    pr = drive_raw("prompt", raw)
    check("16c %-34s -> no stdout, exit 0" % label,
          pr.stdout == b"" and pr.returncode == 0,
          "rc=%s stdout=%r" % (pr.returncode, pr.stdout[:120]))

check("16c a degenerate state dir yields no path, so nothing is emitted",
      mod._mission_paths("/stream") == [] and mod._mission_paths("stream") == []
      and mod.emit_mission_nudge("/stream", "a real body") is False,
      "paths=%r" % (mod._mission_paths("/stream"),))
check("16c emit_mission_nudge returns False (not raises) on hostile input",
      mod.emit_mission_nudge(SDIR, None) is False
      and mod.emit_mission_nudge(SDIR, "") is False
      and mod.emit_mission_nudge(SDIR, "/fresh") is False)

print("\n----- 16d the nudge is stdout, not network: transport counts are unchanged -----")
reset_state()
nuke_mission()
tg16 = RecTG()
mod._run_deferred(SDIR, tg16, mod.on_prompt(SDIR, tg16, prompt_text="a normal message"))
_calls16 = [c[0] for c in tg16.calls]
check("16d a v24 prompt boundary still costs exactly one typing + one send",
      _calls16.count("typing") == 1 and _calls16.count("send") == 1
      and _calls16.count("edit") == 0 and _calls16.count("delete") == 0,
      "calls=%r" % _calls16)
check("16d emit_mission_nudge itself touches the transport zero times",
      (lambda t: (mod.emit_mission_nudge(SDIR, "x"), len(t.calls) == 0)[1])(RecTG()))

print("\n===== #17 (v24) the prompt echo can never render again =====")

ECHO17 = "my laptop is plugged into the monitor and closed lid can I keep it running"
reset_state()
nuke_mission()
tg17 = RecTG()
mod._run_deferred(SDIR, tg17, mod.on_prompt(SDIR, tg17, prompt_text=ECHO17))
open17 = tg17.ops("send")[-1][2]
check("17 the box opens on the placeholder, never on the user's words",
      open17.splitlines()[0] == "🎯 " + mod.MISSION_DEFAULT, "line1=%r" % open17.splitlines()[0])
check("17 no fragment of the prompt body is anywhere in the opening frame",
      "laptop" not in open17 and "monitor" not in open17, "frame=%r" % open17[:120])
check("17 the placeholder is short enough to read whole - the point of the change",
      len(mod.MISSION_DEFAULT) < 20 and not mod.MISSION_DEFAULT.endswith("…"),
      "default=%r" % mod.MISSION_DEFAULT)
check("17 on_prompt still records the prompt in state (schema unchanged from v22/v23)",
      load().get("mission") == mod.mission_from_prompt(ECHO17),
      "mission=%r" % load().get("mission"))
check("17 but current_mission does not read it - that is the whole guard",
      mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT,
      "got=%r" % mod.current_mission(SDIR, load()))

_long17 = "please go and " + ("check every single one of the six bots " * 4)
reset_state()
tg17b = RecTG()
mod._run_deferred(SDIR, tg17b, mod.on_prompt(SDIR, tg17b, prompt_text=_long17))
_f17b = tg17b.ops("send")[-1][2].splitlines()[0]
check("17 a long prompt no longer produces a clipped, cut-off 🎯 line",
      _f17b == "🎯 " + mod.MISSION_DEFAULT and "…" not in _f17b, "line1=%r" % _f17b)

print("\n----- 17b the model's own line renders complete, and reverts cleanly -----")
reset_state()
nuke_mission()
tg17c = RecTG()
write_mission("Fixing the recap position bug", mtime=time.time() + 5)
mod._run_deferred(SDIR, tg17c, mod.on_prompt(SDIR, tg17c, prompt_text=ECHO17))
_o17c = tg17c.ops("send")[-1][2].splitlines()[0]
check("17b a mission already written this turn is on the box from the first frame",
      _o17c == "🎯 Fixing the recap position bug", "line1=%r" % _o17c)
check("17b it renders COMPLETE - no ellipsis, nothing clipped",
      "…" not in _o17c and len(_o17c) < 2 + mod.MISSION_CAP, "line1=%r" % _o17c)
_exact60 = "x" * mod.MISSION_CAP
write_mission(_exact60, mtime=time.time() + 5)
check("17b a mission at exactly the cap is still rendered whole",
      mod.current_mission(SDIR, load()) == _exact60)
clear_mission()
check("17b deleting the file reverts to the placeholder, never to the prompt",
      mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT
      and "laptop" not in mod.current_mission(SDIR, load()))

print("\n----- 17c a v22/v23 state carrying a stored ECHO must not render it -----")
_ECHO_IN_STATE = "can you look at the thing I sent you yesterday about the invoices"
for shape, extra in (("v22/v23", {}), ("v22/v23 + panel", {"panel": []})):
    reset_state()
    nuke_mission()
    _now17 = time.time()
    _st17 = {"msgid": "7301",
             "steps": [{"fam": "files", "cap": "reading old.md", "tuid": "o1",
                        "done": True, "k": "", "aid": ""}],
             "proc_start": _now17 - 30, "last_edit": 0, "last_activity": _now17 - 2,
             "last_reply": 0, "last_react": _now17 - 30, "last_spawn": _now17 - 30,
             "frame": 3, "folded": False, "last_live": "old", "agents": {},
             "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
             "box_is_last": False, "folded_above_msgid": None, "replied_this_turn": False,
             "mission": _ECHO_IN_STATE, "panel": []}
    _st17.update(extra)
    mod.save_state(SDIR, _st17)
    check("17c %-16s the fixture really does hold an echo mission" % shape,
          load().get("mission") == _ECHO_IN_STATE)
    check("17c %-16s current_mission refuses it on the very first read" % shape,
          mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT,
          "got=%r" % mod.current_mission(SDIR, load()))
    tg17d = RecTG()
    run_step(tg17d, "pre", "Bash", {"command": "carry on with the in-flight turn"}, tuid="U1")
    _rendered17 = [c[2] for c in tg17d.calls if c[0] in ("send", "edit")]
    check("17c %-16s no in-flight render leaks the stored echo" % shape,
          all("invoices" not in t and "yesterday" not in t for t in _rendered17),
          "rendered=%r" % [t.splitlines()[0] for t in _rendered17])
    run_step(tg17d, "post", "Bash", {"command": "carry on with the in-flight turn"}, tuid="U1")
    tg17d.calls.clear()
    run_reply(tg17d, final=True)
    _recap17 = [c for c in tg17d.calls if c[0] == "edit" and c[3] is True]
    check("17c %-16s and the turn's RECAP does not leak it either" % shape,
          bool(_recap17) and "invoices" not in _recap17[-1][2]
          and "**>🎯 " + mod.MISSION_DEFAULT in _recap17[-1][2],
          "body1=%r" % (_recap17[-1][2].split("\n")[1] if _recap17 else None))

reset_state()
nuke_mission()
mod.save_state(SDIR, dict(_st17, msgid="7350", mission=_ECHO_IN_STATE))
tg17e = RecTG()
mod._run_deferred(SDIR, tg17e, mod.on_prompt(SDIR, tg17e, prompt_text="a brand new question"))
_prior17 = [c for c in tg17e.calls if c[0] == "edit" and c[3] is True]
check("17c the PRIOR turn's recap, folded by the new prompt, is echo-free too",
      bool(_prior17) and "invoices" not in _prior17[-1][2]
      and "**>🎯 " + mod.MISSION_DEFAULT in _prior17[-1][2],
      "body1=%r" % (_prior17[-1][2].split("\n")[1] if _prior17 else None))

print("\n----- 17d the recap's first body line follows the same rule -----")
reset_state()
nuke_mission()
tg17f = RecTG()
write_mission("Auditing the streamer diff", mtime=time.time() + 5)
mod._run_deferred(SDIR, tg17f, mod.on_prompt(SDIR, tg17f, prompt_text=ECHO17))
do_tool(tg17f, "Read", {"file_path": "/a/b.md"}, "R17")

run_reply(tg17f, final=False)
tg17f.calls.clear()
run_reply(tg17f, final=True)
_f17 = [c for c in tg17f.calls if c[0] == "edit" and c[3] is True]
check("17d with a mission written, the recap leads with the MODEL's line",
      bool(_f17) and _f17[-1][2].split("\n")[1] == "**>🎯 Auditing the streamer diff",
      "body1=%r" % (_f17[-1][2].split("\n")[1] if _f17 else None))
clear_mission()
reset_state()
tg17g = RecTG()
mod._run_deferred(SDIR, tg17g, mod.on_prompt(SDIR, tg17g, prompt_text=ECHO17))
do_tool(tg17g, "Read", {"file_path": "/a/b.md"}, "R18")
run_reply(tg17g, final=False)
tg17g.calls.clear()
run_reply(tg17g, final=True)
_g17 = [c for c in tg17g.calls if c[0] == "edit" and c[3] is True]
check("17d with none written, it leads with the placeholder - never the prompt",
      bool(_g17) and _g17[-1][2].split("\n")[1] == "**>🎯 " + mod.MISSION_DEFAULT
      and "laptop" not in _g17[-1][2],
      "body1=%r" % (_g17[-1][2].split("\n")[1] if _g17 else None))

print("\n===== #18 (v24) the mission write never shows up as work =====")

MPATH18 = mod._mission_paths(SDIR)[0]

def fresh_turn18(prompt="do the thing"):
    reset_state()
    nuke_mission()
    t = RecTG()
    mod._run_deferred(SDIR, t, mod.on_prompt(SDIR, t, prompt_text=prompt))
    t.calls.clear()
    return t

print("\n----- 18a the Bash shape and the Write shape are both hidden -----")
_shapes18 = [
    ("Bash / printf redirect", "Bash", {"command": "printf '%s' 'Fixing the fold bug' > " + MPATH18}),
    ("Bash / heredoc",         "Bash", {"command": "cat > %s <<'EOF'\nDoing a thing\nEOF" % MPATH18}),
    ("Bash / with description", "Bash", {"command": "echo hi > " + MPATH18,
                                         "description": "write the mission"}),
    ("Write",                  "Write", {"file_path": MPATH18, "content": "Fixing the fold bug"}),
    ("Edit",                   "Edit",  {"file_path": MPATH18, "old_string": "a",
                                         "new_string": "b"}),
    ("Bash / realpath spelling", "Bash", {"command": "printf x > " + os.path.realpath(MPATH18)}),
]
for label, tool, tin in _shapes18:
    tg18 = fresh_turn18()
    _steps_before = len(load().get("steps") or [])
    _frame_before = load().get("frame", 0)
    run_step(tg18, "pre", tool, tin, tuid="H_" + label[:4])
    run_step(tg18, "post", tool, tin, tuid="H_" + label[:4])
    st18 = load()
    check("18a %-26s records NO step" % label,
          len(st18.get("steps") or []) == _steps_before,
          "steps=%r" % (st18.get("steps"),))
    check("18a %-26s pushes NO live edit" % label,
          not [c for c in tg18.calls if c[0] in ("send", "edit")],
          "calls=%r" % [c[0] for c in tg18.calls])
    check("18a %-26s still advances the frame (the bar keeps sliding)" % label,
          st18.get("frame", 0) > _frame_before,
          "frame %s -> %s" % (_frame_before, st18.get("frame")))
    check("18a %-26s still refreshes native typing" % label,
          any(c[0] == "typing" for c in tg18.calls),
          "calls=%r" % [c[0] for c in tg18.calls])
    tg18.calls.clear()
    run_reply(tg18, final=True)
    _fold18 = [c for c in tg18.calls if c[0] == "edit" and c[3] is True]
    check("18a %-26s leaves NOTHING in the recap (an empty turn is deleted)" % label,
          not _fold18 and any(c[0] == "delete" for c in tg18.calls),
          "calls=%r" % [c[0] for c in tg18.calls])

print("\n----- 18b a hidden write never reaches a tally or a step line -----")
tg18b = fresh_turn18()
do_tool(tg18b, "Read", {"file_path": "/real/one.md"}, "V1")
run_step(tg18b, "pre", "Bash", {"command": "printf '%s' 'on the case' > " + MPATH18}, tuid="HB")
run_step(tg18b, "post", "Bash", {"command": "printf '%s' 'on the case' > " + MPATH18}, tuid="HB")
do_tool(tg18b, "Write", {"file_path": "/real/two.md"}, "V2")
st18b = load()
check("18b only the two REAL tools are in state",
      len(st18b.get("steps") or []) == 2
      and all("mission.txt" not in (s.get("cap") or "") for s in st18b["steps"]),
      "steps=%r" % [s.get("cap") for s in (st18b.get("steps") or [])])

run_reply(tg18b, final=False)
tg18b.calls.clear()
run_reply(tg18b, final=True)
_r18b = [c for c in tg18b.calls if c[0] == "edit" and c[3] is True][-1][2]
check("18b the recap tally counts 1 file and 1 edit, not 2 edits",
      "1 file" in _r18b and "1 edit" in _r18b and "2 edit" not in _r18b,
      "tally=%r" % _r18b.splitlines()[2])
check("18b the word mission.txt appears nowhere in the recap",
      "mission" not in _r18b, "recap=%r" % _r18b[:200])

print("\n----- 18c an unrelated mission.txt is still ordinary work -----")
_others18 = [
    ("another project's file", "Write", {"file_path": "/Users/example/code/proj/mission.txt"}),
    ("a bare relative name",   "Write", {"file_path": "mission.txt"}),
    ("another bot's state dir", "Write",
     {"file_path": os.path.join(os.path.dirname(SDIR) + "-other", "mission.txt")}),
    ("a similarly-named file", "Write", {"file_path": os.path.dirname(SDIR) + "/mission.txt.bak"}),
    ("a Bash cat of someone else's", "Bash", {"command": "cat /etc/mission.txt"}),
    ("a Bash mention of the basename", "Bash", {"command": "grep -r mission.txt ."}),
]
for label, tool, tin in _others18:
    check("18c %-32s is NOT suppressed" % label,
          mod._is_mission_write(SDIR, tool, tin) is False,
          "matched=%r" % mod._is_mission_write(SDIR, tool, tin))
check("18c the real path in either spelling IS suppressed",
      mod._is_mission_write(SDIR, "Write", {"file_path": MPATH18}) is True
      and mod._is_mission_write(SDIR, "Bash", {"command": "x > " + MPATH18}) is True)
check("18c a non-write tool aimed at the same path is untouched (Read stays visible)",
      mod._is_mission_write(SDIR, "Read", {"file_path": MPATH18}) is False
      and mod._is_mission_write(SDIR, "Grep", {"pattern": MPATH18}) is False)
check("18c degenerate tool_input never raises and never matches",
      mod._is_mission_write(SDIR, "Write", None) is False
      and mod._is_mission_write(SDIR, "Write", {"file_path": 12}) is False
      and mod._is_mission_write(SDIR, "Bash", {"command": None}) is False
      and mod._is_mission_write(SDIR, None, {}) is False)

tg18c = fresh_turn18()
do_tool(tg18c, "Read", {"file_path": MPATH18}, "RD")
check("18c a Read of mission.txt is recorded like any other read",
      len(load().get("steps") or []) == 1, "steps=%r" % (load().get("steps"),))

print("\n----- 18d the position / re-anchor machinery is untouched by a hidden event -----")
reset_state()
nuke_mission()
tg18d = RecTG()
mod._run_deferred(SDIR, tg18d, mod.on_prompt(SDIR, tg18d, prompt_text="ack then work"))
box18 = load().get("msgid")
do_tool(tg18d, "Read", {"file_path": "/w/1.md"}, "P1")
ack18 = reply_lands()
run_reply(tg18d, final=False)
check("18d the re-anchor is armed before the hidden event", load().get("reanchor") is True)
run_step(tg18d, "pre", "Bash", {"command": "printf '%s' 'still going' > " + MPATH18}, tuid="HR")
st18d = load()
check("18d a HIDDEN event still consumes the re-anchor and moves the box",
      st18d.get("reanchor") is False and st18d.get("msgid") != box18
      and int(st18d.get("msgid")) > ack18,
      "reanchor=%s msgid=%s box=%s" % (st18d.get("reanchor"), st18d.get("msgid"), box18))
check("18d the box it re-anchored to carries the earlier REAL step, not the hidden one",
      len(st18d.get("steps") or []) == 1
      and "mission" not in (st18d["steps"][0].get("cap") or ""),
      "steps=%r" % [s.get("cap") for s in (st18d.get("steps") or [])])
check("18d the old box was folded ABOVE the reply, exactly as for a visible tool",
      st18d.get("folded_above_msgid") == box18,
      "above=%r" % st18d.get("folded_above_msgid"))
run_step(tg18d, "post", "Bash", {"command": "printf '%s' 'still going' > " + MPATH18}, tuid="HR")
do_tool(tg18d, "Write", {"file_path": "/notes/note.md"}, "P2")
answer18 = reply_lands()
run_reply(tg18d, final=True)
check("18d the answer is still the LAST message after a turn with a hidden write",
      len(below(surviving(tg18d), answer18)) == 0,
      "alive=%r answer=%d" % (surviving(tg18d), answer18))

print("\n----- 18e the whole point: the line flips to the model's own words -----")
tg18e = fresh_turn18("look into the streamer")
check("18e the box starts on the placeholder",
      mod.current_mission(SDIR, load()) == mod.MISSION_DEFAULT)
write_mission("Fixing the recap position bug")
run_step(tg18e, "pre", "Bash",
         {"command": "printf '%s' 'Fixing the recap position bug' > " + MPATH18}, tuid="E1")
run_step(tg18e, "post", "Bash",
         {"command": "printf '%s' 'Fixing the recap position bug' > " + MPATH18}, tuid="E1")
check("18e the write itself left no trace in the box",
      not [c for c in tg18e.calls if c[0] in ("send", "edit")]
      and (load().get("steps") or []) == [],
      "calls=%r steps=%r" % ([c[0] for c in tg18e.calls], load().get("steps")))
force_edit_window()
tg18e.calls.clear()
do_tool(tg18e, "Read", {"file_path": "/s/x.py"}, "E2")
_live18 = [c for c in tg18e.calls if c[0] in ("send", "edit") and c[3] is False]
check("18e the very next real step renders the model's own mission",
      bool(_live18) and _live18[-1][2].splitlines()[0] == "🎯 Fixing the recap position bug",
      "line1=%r" % (_live18[-1][2].splitlines()[0] if _live18 else None))
check("18e and that line is complete - no ellipsis, nothing cut off",
      bool(_live18) and "…" not in _live18[-1][2].splitlines()[0])

run_reply(tg18e, final=False)
tg18e.calls.clear()
run_reply(tg18e, final=True)
_rec18 = [c for c in tg18e.calls if c[0] == "edit" and c[3] is True]
check("18e the recap leads with it too, and shows only the real step",
      bool(_rec18) and _rec18[-1][2].split("\n")[1] == "**>🎯 Fixing the recap position bug"
      and ">✓ 🔍 reading x\\.py" in _rec18[-1][2]
      and "edit" not in _rec18[-1][2] and "mission" not in _rec18[-1][2],
      "recap=%r" % (_rec18[-1][2][:160] if _rec18 else None))
clear_mission()

reset_state()
nuke_mission()

print("\n===== #19 (v25) a turn that sends NO reply never leaves its box below the answer =====")

NOTIF = ("<task-notification> <task-id>a0000000000000003</task-id> "
         "<o>worker 2 finished</o></task-notification>")

def notif_turn(tg, steps=(("Bash", "check worker 2 state"),), reply=False, fuse=True):

    mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
    box = load().get("msgid")
    for i, (tool, cap) in enumerate(steps):
        do_tool(tg, tool, {"command": cap} if tool == "Bash" else {"file_path": cap},
                "N%d" % i)
    ans = None
    if reply:
        ans = reply_lands()
        run_reply(tg, final=False)
    run_reply(tg, final=True)
    if fuse and not reply:
        burn_fuse(tg)
    return box, ans

print("\n----- 19a the notification turn with a carried recap: work goes UP -----")
reset_state()
tg = RecTG()
recap19a, answer19a = prior_turn(tg, "what is the worker doing")

box19a, _ = notif_turn(tg, fuse=False)
st19a_mid = load()
check("19a Stop itself only folds in place and arms the fuse (the v24 render, kept)",
      st19a_mid.get("folded") is True and str(st19a_mid.get("msgid")) == str(box19a)
      and str(box19a) in surviving(tg)
      and st19a_mid.get("noreply_grace_until") is not None
      and not [c for c in tg.calls if c[0] == "delete"],
      "msgid=%s grace=%s" % (st19a_mid.get("msgid"),
                             st19a_mid.get("noreply_grace_until")))
check("19a while the fuse is armed the box is still the folded recap the owner can read",
      (st19a_mid.get("last_live") or "").startswith(mod.RECAP_MARK),
      "last_live=%r" % (st19a_mid.get("last_live") or "")[:40])
burn_fuse(tg)
st19a = load()
alive19a = surviving(tg)
merged19a = md_edits(tg, recap19a)[-1][2] if md_edits(tg, recap19a) else ""
check("19a the answer is still the LAST message - nothing survives below it",
      below(alive19a, answer19a) == [], "alive=%r answer=%d" % (alive19a, answer19a))
check("19a the live box is deleted, never folded where it stood",
      any(c[0] == "delete" and str(c[1]) == str(box19a) for c in tg.calls)
      and str(box19a) not in alive19a,
      "alive=%r box=%s" % (alive19a, box19a))
check("19a exactly one streamer message survives: the carried recap, above the answer",
      alive19a == [str(recap19a)] and int(recap19a) < answer19a,
      "alive=%r recap=%s answer=%d" % (alive19a, recap19a, answer19a))
check("19a the carried recap now holds this turn's work as a continuation block",
      "continued" in merged19a and "check worker 2 state" in merged19a
      and merged19a.startswith("📋 What I did"),
      "body=%r" % merged19a.splitlines()[1:4])
check("19a state follows the merged recap and keeps NO msgid",
      st19a.get("folded") is True and st19a.get("msgid") is None
      and str(st19a.get("prev_recap_msgid")) == str(recap19a)
      and st19a.get("prev_recap_text") == merged19a,
      "msgid=%s prev=%s" % (st19a.get("msgid"), st19a.get("prev_recap_msgid")))
check("19a the work MOVED: the box's steps are cleared, so nothing is reported twice",
      (st19a.get("steps") or []) == [], "steps=%r" % st19a.get("steps"))

print("\n----- 19b the same turn with nothing to carry into: the box still goes -----")
reset_state()
tg = RecTG()
answer19b = reply_lands()
box19b, _ = notif_turn(tg)
st19b = load()

check("19b nothing the streamer sent survives at all (recap dropped, never parked)",
      surviving(tg) == []
      and all(str(c[1]) == str(box19b) for c in md_edits(tg)),
      "alive=%r md_edits=%r" % (surviving(tg), [c[1] for c in md_edits(tg)]))
check("19b the box was deleted",
      any(c[0] == "delete" and str(c[1]) == str(box19b) for c in tg.calls),
      "calls=%r" % [c[0] for c in tg.calls])
check("19b state is clean: folded, no box, no carried recap invented",
      st19b.get("folded") is True and st19b.get("msgid") is None
      and st19b.get("prev_recap_msgid") is None,
      "msgid=%s prev=%s" % (st19b.get("msgid"), st19b.get("prev_recap_msgid")))
check("19b the dropped turn's steps are KEPT (nothing moved, so nothing is discarded)",
      len(st19b.get("steps") or []) == 1, "steps=%r" % st19b.get("steps"))

print("\n----- 19c a notification turn that DOES reply keeps the v21-v24 path -----")
reset_state()
tg = RecTG()
recap19c, prev_ans19c = prior_turn(tg, "first")
box19c, answer19c = notif_turn(tg, reply=True)
alive19c = surviving(tg)
check("19c it folds IN PLACE at its own msgid, exactly as v24 did",
      str(box19c) in alive19c and bool(md_edits(tg, box19c))
      and load().get("msgid") == box19c and load().get("folded") is True,
      "alive=%r box=%s msgid=%s" % (alive19c, box19c, load().get("msgid")))
check("19c the carried recap is NOT touched by a turn that replied",
      len(md_edits(tg, recap19c)) == 1,
      "edits_at_prev=%d" % len(md_edits(tg, recap19c)))
check("19c the answer is last and the recap sits above it",
      below(alive19c, answer19c) == [] and int(box19c) < answer19c,
      "alive=%r answer=%d" % (alive19c, answer19c))
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="and now something else"))
check("19c the next turn carries THIS turn's own recap forward as its target",
      str(load().get("prev_recap_msgid")) == str(box19c)
      and (load().get("prev_recap_text") or "").startswith(mod.RECAP_MARK),
      "prev=%s" % load().get("prev_recap_msgid"))

print("\n----- 19d reply-shaped turns are byte-identical to v24 -----")
nuke_mission()
V24_PATH = os.environ.get("TG_STREAM_V24_REFERENCE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "stream-v24-reference.py")

class CmpTG:

    def __init__(self):
        self.calls = []
        self.seq = [7000]

    def send(self, text, markdown=False):
        self.seq[0] += 1
        self.calls.append(("send", str(self.seq[0]), text, markdown))
        return str(self.seq[0])

    def edit(self, msgid, text, markdown=False):
        self.calls.append(("edit", str(msgid), text, markdown))
        return True

    def delete(self, msgid):
        self.calls.append(("delete", str(msgid)))
        return True

    def typing(self):
        self.calls.append(("typing",))

def _norm(calls):

    import re
    out = []
    for c in calls:
        c = list(c)
        for i, v in enumerate(c):
            if isinstance(v, str):
                c[i] = re.sub(r"\d+:\d\d", "M:SS", v)
        out.append(tuple(c))
    return out

def drive(m, sd, tg, script):
    for kind, a, b in script:
        if kind == "prompt":
            m._run_deferred(sd, tg, m.on_prompt(sd, tg, prompt_text=a))
        elif kind == "tool":
            m._run_deferred(sd, tg, m.handle_step(sd, tg, "pre", a, b, a + "1", None, None))
            m._run_deferred(sd, tg, m.handle_step(sd, tg, "post", a, b, a + "1", None, None))
        elif kind == "reply":
            m._run_deferred(sd, tg, m.record_reply(sd, tg, final=False))
        elif kind == "stop":
            m._run_deferred(sd, tg, m.record_reply(sd, tg, final=True))
        elif kind == "react":
            m._run_deferred(sd, tg, m.on_react(sd, tg))

SCRIPTS = {
    "reply is the last action": [("prompt", "q1", None), ("tool", "Read", {"file_path": "/a"}),
                                 ("reply", None, None), ("stop", None, None)],
    "trailing work after the answer": [("prompt", "q2", None), ("tool", "Read", {"file_path": "/a"}),
                                       ("reply", None, None), ("tool", "Write", {"file_path": "/v"}),
                                       ("stop", None, None)],
    "instant ack then work": [("prompt", "q3", None), ("reply", None, None),
                              ("tool", "Bash", {"command": "go"}), ("reply", None, None),
                              ("stop", None, None)],
    "zero-tool turn": [("prompt", "q4", None), ("reply", None, None), ("stop", None, None)],
    "two turns back to back": [("prompt", "q5", None), ("tool", "Read", {"file_path": "/a"}),
                               ("reply", None, None), ("stop", None, None),
                               ("prompt", "q6", None), ("tool", "Bash", {"command": "b"}),
                               ("reply", None, None), ("stop", None, None)],
    "react mid-turn": [("prompt", "q7", None), ("tool", "Read", {"file_path": "/a"}),
                       ("reply", None, None), ("react", None, None), ("stop", None, None)],

    "guard block, tools, then the answer": [
        ("prompt", "q8", None), ("tool", "Read", {"file_path": "/a"}),
        ("stop", None, None),
        ("tool", "Bash", {"command": "finish up"}),
        ("reply", None, None), ("stop", None, None)],
    "guard block, then the answer with no tools": [
        ("prompt", "q9", None), ("tool", "Read", {"file_path": "/a"}),
        ("stop", None, None),
        ("reply", None, None), ("stop", None, None)],
}

if not os.path.exists(V24_PATH):
    print("[SKIP] 19d: optional pre-v25 reference is absent; no private snapshot ships")
    SKIPS.append("19d optional reference absent")
else:
    SD24 = tempfile.mkdtemp(prefix="tgstream-v24ref-")

    _v24_py = os.path.join(SD24, "v24_ref.py")
    shutil.copyfile(V24_PATH, _v24_py)
    _spec24 = importlib.util.spec_from_file_location("tgstream_v24_ref", _v24_py)
    mod24 = importlib.util.module_from_spec(_spec24)
    _spec24.loader.exec_module(mod24)
    SDIR24 = os.path.join(SD24, "stream")
    os.makedirs(SDIR24, exist_ok=True)
    for label, script in SCRIPTS.items():
        reset_state()
        try:
            os.remove(os.path.join(SDIR24, "state.json"))
        except OSError:
            pass
        a, b = CmpTG(), CmpTG()
        drive(mod24, SDIR24, a, script)
        drive(mod, SDIR, b, script)
        if label == "react mid-turn":
            check("19d react cutoff differs from v24 only by the required interruption record",
                  len(a.calls) == len(b.calls)
                  and any(call[0] == "edit" and "Interrupted by user" in call[2]
                          for call in b.calls)
                  and not any(call[0] == "edit" and "Interrupted by user" in call[2]
                              for call in a.calls),
                  "first_diff=%r" % [
                      (x, y) for x, y in zip(_norm(a.calls), _norm(b.calls))
                      if x != y][:1])
        else:
            check("19d byte-identical to v24: %s" % label,
                  _norm(a.calls) == _norm(b.calls),
                  "first_diff=%r" % [
                      (x, y) for x, y in zip(_norm(a.calls), _norm(b.calls))
                      if x != y][:1])

    reset_state()
    try:
        os.remove(os.path.join(SDIR24, "state.json"))
    except OSError:
        pass
    a, b = CmpTG(), CmpTG()
    drive(mod24, SDIR24, a, SCRIPTS["trailing work after the answer"])
    drive(mod, SDIR, b, SCRIPTS["trailing work after the answer"])
    s24 = mod24.load_state(SDIR24)

    s25 = {k: v for k, v in load().items()
           if k not in ("prev_recap_msgid", "prev_recap_text", "noreply_grace_until")}
    check("19d a reply-shaped turn leaves no fuse armed",
          load().get("noreply_grace_until") is None,
          "grace=%r" % load().get("noreply_grace_until"))
    for k in ("proc_start", "last_edit", "last_activity", "last_reply", "last_react",
              "last_spawn", "last_live"):
        s24.pop(k, None)
        s25.pop(k, None)
    check("19d the state a reply-shaped turn leaves is the v24 state plus the two new keys",
          s24 == s25, "v24=%r\nv25=%r" % (sorted(s24.items()), sorted(s25.items())))
    shutil.rmtree(SD24, ignore_errors=True)

print("\n----- 19e an aborted no-reply turn swept by the NEXT prompt -----")
reset_state()
tg = RecTG()
recap19e, answer19e = prior_turn(tg, "the question before it died")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
box19e = load().get("msgid")
do_tool(tg, "Bash", {"command": "inventory worker 2 progress"}, "E1")

mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="are you there"))
st19e = load()
alive19e = surviving(tg)
merged19e = md_edits(tg, recap19e)[-1][2] if md_edits(tg, recap19e) else ""
check("19e the abandoned box is deleted, not folded under the answer",
      any(c[0] == "delete" and str(c[1]) == str(box19e) for c in tg.calls)
      and str(box19e) not in alive19e, "alive=%r box=%s" % (alive19e, box19e))
check("19e its work went UP into the carried recap",
      "Interrupted by user" in merged19e
      and "inventory worker 2 progress" in merged19e,
      "merged=%r" % merged19e[:120])
check("19e the only streamer message below the answer is the NEW turn's live box",
      below(alive19e, answer19e) == [str(st19e.get("msgid"))],
      "below=%r new=%s" % (below(alive19e, answer19e), st19e.get("msgid")))
check("19e the new turn carries the merged recap forward",
      str(st19e.get("prev_recap_msgid")) == str(recap19e)
      and st19e.get("prev_recap_text") == merged19e,
      "prev=%s" % st19e.get("prev_recap_msgid"))

print("\n----- 19f the animator backstop obeys the same rule -----")
reset_state()
tg = RecTG()
recap19f, answer19f = prior_turn(tg, "before the worker notification")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
box19f = load().get("msgid")
do_tool(tg, "Bash", {"command": "audit worker 2 partial progress"}, "F1")
run_step(tg, "pre", "Bash", {"command": "capture the resolved model"}, tuid="F2")
st_pre19f = load()
check("19f the fixture reproduces Delta's end-state shape exactly",
      st_pre19f.get("box_is_last") is True and st_pre19f.get("replied_this_turn") is False
      and st_pre19f.get("last_reply") == 0 and st_pre19f.get("typing_done") is False
      and st_pre19f.get("folded") is False and len(st_pre19f.get("steps") or []) == 2,
      "box_is_last=%s replied=%s last_reply=%s" % (
          st_pre19f.get("box_is_last"), st_pre19f.get("replied_this_turn"),
          st_pre19f.get("last_reply")))
mod._fold_to_done(SDIR, tg)
st19f = load()
alive19f = surviving(tg)
check("19f the backstop deletes the box instead of folding it below the answer",
      any(c[0] == "delete" and str(c[1]) == str(box19f) for c in tg.calls)
      and str(box19f) not in alive19f, "alive=%r box=%s" % (alive19f, box19f))
check("19f the answer is the last message again",
      below(alive19f, answer19f) == [], "below=%r" % below(alive19f, answer19f))
check("19f the work is in the carried recap, including the step still in flight",
      "continued" in md_edits(tg, recap19f)[-1][2]
      and "capture the resolved model" in md_edits(tg, recap19f)[-1][2],
      "merged=%r" % md_edits(tg, recap19f)[-1][2][:120])
check("19f state keeps no msgid, so a turn that wakes up cold-starts a NEW box",
      st19f.get("folded") is True and st19f.get("msgid") is None
      and st19f.get("box_is_last") is False,
      "msgid=%s box_is_last=%s" % (st19f.get("msgid"), st19f.get("box_is_last")))

do_tool(tg, "Read", {"file_path": "/after/the/backstop.md"}, "F3")
st19f2 = load()
check("19f a resumed tool opens a fresh box and never touches the carried recap",
      st19f2.get("msgid") not in (None, recap19f, box19f)
      and len(md_edits(tg, recap19f)) == 2,
      "msgid=%s edits_at_recap=%d" % (st19f2.get("msgid"), len(md_edits(tg, recap19f))))
check("19f the resumed box starts from an empty step list (the work moved, not copied)",
      [s.get("cap") for s in (st19f2.get("steps") or [])] == ["reading backstop.md"],
      "steps=%r" % [s.get("cap") for s in (st19f2.get("steps") or [])])

print("\n----- 19g the carried recap across several consecutive turns -----")
reset_state()
tg = RecTG()
recap19g, answer19g = prior_turn(tg, "the one real question")
for i in range(3):
    notif_turn(tg, steps=(("Bash", "notification pass %d" % i),))
st19g = load()
alive19g = surviving(tg)
chain = md_edits(tg, recap19g)[-1][2]
check("19g three consecutive no-reply turns leave EXACTLY one visible recap",
      alive19g == [str(recap19g)], "alive=%r" % alive19g)
check("19g every turn's work is in it, in order, one continuation block each",
      chain.count("continued") == 3
      and all("notification pass %d" % i in chain for i in range(3))
      and chain.index("pass 0") < chain.index("pass 1") < chain.index("pass 2"),
      "continued=%d" % chain.count("continued"))
check("19g the chat still ends with the answer",
      below(alive19g, answer19g) == [], "below=%r" % below(alive19g, answer19g))
check("19g the recap is still ONE expandable: a single opener and a single closer",
      chain.count("**>") == 1 and chain.count("||") == 1 and chain.endswith("||"),
      "openers=%d closers=%d" % (chain.count("**>"), chain.count("||")))
check("19g the folded header the owner reads is untouched by every merge",
      chain.splitlines()[0] == md_edits(tg, recap19g)[0][2].splitlines()[0],
      "header=%r" % chain.splitlines()[0])
check("19g the merged recap is valid MarkdownV2 end to end",
      mdv2_fully_escaped(chain))
check("19g state carries the latest merged text forward verbatim",
      st19g.get("prev_recap_text") == chain
      and str(st19g.get("prev_recap_msgid")) == str(recap19g))

_BIGSTEPS = [{"fam": "run", "cap": "a step with a fairly long caption number %d" % i,
              "tuid": str(i), "done": True, "k": "", "aid": ""} for i in range(120)]
_base = mod.render_final(_BIGSTEPS[:40], time.time() - 30, 0, "the base recap", None)[0]
_merged = mod.merge_recap(_base, _BIGSTEPS, time.time() - 5, 0, "the continuation", None)
check("19g a merge that would overflow drops OLDEST continuation detail, keeps the frame",
      _merged is not None and mod.tg_len(_merged) <= mod.TG_HARD_LIMIT
      and _merged.endswith("||") and _merged.count("**>") == 1
      and "earlier omitted" in _merged and _merged.startswith(mod.RECAP_MARK),
      "utf16=%d" % (mod.tg_len(_merged or "")))
check("19g the base recap is never trimmed to make room",
      _merged is not None and _merged.startswith(_base.rstrip()[:-2]),
      "base_intact=%s" % (bool(_merged) and _merged.startswith(_base.rstrip()[:-2])))
_tiny = mod.merge_recap(_base, _BIGSTEPS, time.time() - 5, 0, "x", None,
                        limit=mod.tg_len(_base) + 4)
check("19g a merge that cannot fit at all returns None (the base is left untouched)",
      _tiny is None, "got=%r" % (_tiny or "")[:60])
check("19g a base that is not a recap is never appended to",
      mod.merge_recap("👾 Working · ▪︎▫︎▫︎▫︎▫︎ · 0:12", _BIGSTEPS[:2], time.time(), 0,
                      "m", None) is None
      and mod.merge_recap(None, _BIGSTEPS[:2], time.time(), 0, "m", None) is None
      and mod.merge_recap(_base, [], time.time(), 0, "m", None) is None)

print("\n----- 19h a v24-shaped state (no prev_recap_* keys) through every edge -----")
V24_KEYS = V21_KEYS + ["mission", "panel"]

def write_v24_state(**over):
    now = time.time()
    st = {"msgid": "9500",
          "steps": [{"fam": "run", "cap": "mid-flight when v25 landed", "tuid": "z1",
                     "done": True, "k": "", "aid": ""}],
          "proc_start": now - 30, "last_edit": 0, "last_activity": now - 2,
          "last_reply": 0, "last_react": now - 30, "last_spawn": now - 30,
          "frame": 4, "folded": False, "last_live": "old live text", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
          "box_is_last": True, "folded_above_msgid": None, "replied_this_turn": False,
          "mission": "an echo from v22", "panel": []}
    st.update(over)
    mod.save_state(SDIR, st)
    return st

reset_state()
write_v24_state()
check("19h the fixture really is v24-shaped (no v25 keys present)",
      set(load().keys()) == set(V24_KEYS), "keys=%r" % sorted(load().keys()))
tg19h = RecTG()
run_step(tg19h, "pre", "Read", {"file_path": "/still/running.md"}, tuid="z2")
st19h = load()
check("19h a tool event initializes both new keys with the safe default",
      "prev_recap_msgid" in st19h and st19h.get("prev_recap_msgid") is None
      and st19h.get("prev_recap_text") is None,
      "prev=%r/%r" % (st19h.get("prev_recap_msgid"), st19h.get("prev_recap_text")))
check("19h the in-flight box keeps its msgid and its steps",
      st19h.get("msgid") == "9500" and len(st19h.get("steps") or []) == 2,
      "msgid=%s steps=%d" % (st19h.get("msgid"), len(st19h.get("steps") or [])))
run_reply(tg19h, final=True)
st19h1 = load()

check("19h Stop over v24 state folds in place and arms the fuse on a state that lacked it",
      st19h1.get("folded") is True and str(st19h1.get("msgid")) == "9500"
      and st19h1.get("noreply_grace_until") is not None,
      "msgid=%s grace=%s" % (st19h1.get("msgid"), st19h1.get("noreply_grace_until")))
burn_fuse(tg19h)
st19h2 = load()
check("19h Stop over v24 state takes the drop path (no known recap) and never folds below",
      st19h2.get("msgid") is None and st19h2.get("folded") is True
      and any(c[0] == "delete" and str(c[1]) == "9500" for c in tg19h.calls)
      and all(str(c[1]) == "9500"
              for c in tg19h.calls if c[0] == "edit" and c[3] is True),
      "msgid=%s calls=%r" % (st19h2.get("msgid"), [c[0] for c in tg19h.calls]))
reset_state()
write_v24_state()
ok19h, out19h = True, b""
for mode, payload in [
        ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
        ("post", {"tool_name": mod.REPLY_TOOL}),
        ("stop", {}),
        ("prompt", {"prompt": "<channel source=\"telegram\">next message</channel>"}),
        ("prompt", {"prompt": NOTIF}),
        ("stop", {}),
]:
    p = drive_main(mode, payload)
    if p.returncode != 0:
        ok19h = False
    out19h += p.stdout
check("19h every hook edge over a v24-shaped state exits 0", ok19h)
check("19h every hook edge over a v24-shaped state prints nothing but the nudge",
      strip_nudges(out19h) == b"", "stdout=%r" % strip_nudges(out19h)[:120])
reset_state()
write_v24_state(prev_recap_msgid=12, prev_recap_text=["not", "a", "string"])
tg19h3 = RecTG()
run_reply(tg19h3, final=True)
burn_fuse(tg19h3)

check("19h a wrong-TYPE prev_recap_text is ignored (drop path, no crash, no edit)",
      load().get("msgid") is None
      and not [c for c in tg19h3.calls if c[0] == "edit" and str(c[1]) == "12"],
      "calls=%r" % [(c[0], c[1] if len(c) > 1 else None) for c in tg19h3.calls])

print("\n----- 19i what the merge writes -----")
reset_state()
tg = RecTG()
clear_mission()
recap19i, answer19i = prior_turn(tg, "the first question")
write_mission("Checking worker 2 state after session restart", mtime=time.time() + 5)
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
run_step(tg, "pre", "Agent", AG_IN, tuid="I1")
run_step(tg, "post", "Agent", AG_IN, tuid="I1")
do_tool(tg, "Read", {"file_path": "/worker2/state.json"}, "I2")
run_reply(tg, final=True)
burn_fuse(tg)
_m19i = md_edits(tg, recap19i)[-1][2]
_lines19i = _m19i.split("\n")
check("19i the continuation opens with a divider and its own header",
      mod.escape_mdv2(mod.CONT_DIVIDER) in _m19i and ">" + mod.escape_mdv2(mod.CONT_MARK)
      in _m19i.replace("\\", "\\"),
      "tail=%r" % _lines19i[-6:])
check("19i it carries THIS turn's mission line, the model's own words",
      ">🎯 Checking worker 2 state after session restart" in _m19i,
      "tail=%r" % _lines19i[-6:])
check("19i it carries the agent roster and the step lines",
      ">🤖 opus\\-executor" in _m19i and "reading state\\.json" in _m19i,
      "tail=%r" % _lines19i[-4:])
check("19i every appended line is inside the blockquote frame",
      _lines19i[1].startswith("**>")
      and all(ln.startswith(">") for ln in _lines19i[2:])
      and _m19i.endswith("||") and _m19i.count("**>") == 1,
      "line1=%r last=%r" % (_lines19i[1][:30], _lines19i[-1][-20:]))
check("19i the merged message is valid MarkdownV2 and inside the wire budget",
      mdv2_fully_escaped(_m19i) and mod.tg_len(_m19i) <= mod.TG_HARD_LIMIT,
      "utf16=%d" % mod.tg_len(_m19i))
clear_mission()

print("\n----- 19j the under-lock network budget is unchanged -----")
reset_state()
tg = RecTG()
prior_turn(tg, "budget check")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
do_tool(tg, "Bash", {"command": "one step"}, "J1")
tg.calls.clear()
deferred_j = mod.record_reply(SDIR, tg, final=True)
inline_j = list(tg.calls)
mod._run_deferred(SDIR, tg, deferred_j)

check("19j the Stop fold is ONE under-lock call and defers nothing",
      len(inline_j) == 1 and inline_j[0][0] == "edit" and inline_j[0][3] is True
      and [d[0] for d in deferred_j] == [],
      "inline=%r deferred=%r" % (inline_j, [d[0] for d in deferred_j]))
tg.calls.clear()
st_j = load()
st_j["noreply_grace_until"] = 0
mod.save_state(SDIR, st_j)
mod._noreply_grace_sweep(SDIR, tg)
check("19j the fuse spends ONE edit (the merge) plus the box's delete, in that order",
      [c[0] for c in tg.calls] == ["edit", "delete"] and tg.calls[0][3] is True,
      "calls=%r" % [c[0] for c in tg.calls])
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
do_tool(tg, "Bash", {"command": "one step"}, "J2")
tg.calls.clear()
deferred_j2 = mod.record_reply(SDIR, tg, final=True)
check("19j with no target the Stop still just folds, and defers nothing",
      [c[0] for c in tg.calls] == ["edit"] and [d[0] for d in deferred_j2] == [],
      "inline=%r deferred=%r" % ([c[0] for c in tg.calls],
                                 [d[0] for d in deferred_j2]))
tg.calls.clear()
burn_fuse(tg)
check("19j with no target the fuse spends ZERO edits and only the delete",
      [c[0] for c in tg.calls] == ["delete"], "calls=%r" % [c[0] for c in tg.calls])
reset_state()
tg = RecTG()
prior_turn(tg, "boundary budget")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
do_tool(tg, "Bash", {"command": "one step"}, "J3")
tg.calls.clear()
ops_j3 = mod.on_prompt(SDIR, tg, prompt_text="a new question")
inline_j3 = [c for c in tg.calls if c[0] in ("send", "edit", "delete")]
check("19j the BOUNDARY still defers everything: only the new box's send runs inline",
      [c[0] for c in inline_j3] == ["send"]
      and sorted(d[0] for d in ops_j3) == ["delete", "recap"],
      "inline=%r deferred=%r" % ([c[0] for c in inline_j3], [d[0] for d in ops_j3]))

print("\n----- 19k the notification prompt is recognised conservatively -----")
check("19k a real task notification is recognised",
      mod._is_notification_prompt(NOTIF)
      and mod._is_notification_prompt("   \n" + NOTIF))
check("19k a message that merely mentions the tag is NOT",
      not mod._is_notification_prompt("what does <task-notification> mean?")
      and not mod._is_notification_prompt("")
      and not mod._is_notification_prompt(None)
      and not mod._is_notification_prompt("<channel source=\"telegram\">hi</channel>"))
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=NOTIF))
check("19k the notification fragment is NOT stored in the mission key",
      load().get("mission") == "", "mission=%r" % load().get("mission"))
check("19k the box still opens for it, with the normal placeholder line",
      load().get("msgid") is not None
      and [c for c in tg.calls if c[0] == "send"][-1][2].splitlines()[0]
      == "🎯 " + mod.MISSION_DEFAULT,
      "line1=%r" % [c for c in tg.calls if c[0] == "send"][-1][2].splitlines()[0])
reset_state()
tg = RecTG()
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="a real message"))
check("19k a real message still records its derived mission, exactly as v22-v24",
      load().get("mission") == "a real message", "mission=%r" % load().get("mission"))
p19k = drive_main("prompt", {"prompt": NOTIF})
check("19k the prompt edge still emits exactly one nudge for a notification turn",
      p19k.returncode == 0 and strip_nudges(p19k.stdout) == b""
      and b"work-streamer" in p19k.stdout,
      "rc=%d stdout=%r" % (p19k.returncode, p19k.stdout[:80]))

print("\n----- 19l a recap already parked below the answer is left alone -----")
reset_state()
_now19l = time.time()
mod.save_state(SDIR, {
    "msgid": "3192", "steps": [{"fam": "run", "cap": "capture the resolved model",
                                "tuid": "j1", "done": False, "k": "", "aid": ""}],
    "proc_start": _now19l - 1980, "last_edit": _now19l - 3, "last_activity": _now19l - 1800,
    "last_reply": 0, "last_react": _now19l - 1980, "last_spawn": _now19l - 1980,
    "frame": 490, "folded": True,
    "last_live": "📋 What I did · 33:07 · tap to expand\n**>🎯 Checking worker 2 state\n>✓ ⚙️ ran it||",
    "agents": {}, "next_agent_n": 1, "ghosts": [], "typing_done": False,
    "reanchor": False, "box_is_last": True, "folded_above_msgid": None,
    "replied_this_turn": False, "mission": "<task-notification> <task-id>a228…",
    "panel": []})
tg19l = RecTG()
ops19l = mod.on_prompt(SDIR, tg19l, prompt_text="what happened with the worker")
mod._run_deferred(SDIR, tg19l, ops19l)
st19l = load()
check("19l the parked recap is not edited, not deleted, not moved",
      not [c for c in tg19l.calls if c[0] in ("edit", "delete")],
      "calls=%r" % [c[0] for c in tg19l.calls])
check("19l and it is NOT carried forward as a merge target (it is below the answer)",
      st19l.get("prev_recap_msgid") is None and st19l.get("prev_recap_text") is None,
      "prev=%r" % st19l.get("prev_recap_msgid"))
check("19l the new turn just opens its own box, as always",
      st19l.get("msgid") not in (None, "3192") and st19l.get("box_is_last") is True,
      "msgid=%s" % st19l.get("msgid"))
reset_state()

print("\n===== #20 (v26) the no-reply cleanup is deferred, never instant =====")

print("\n----- 20a the guard-block sequence: block, tools, answer -----")
reset_state()
tg = RecTG()
recap20a, prev_ans20a = prior_turn(tg, "the question before the blocked one")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="do the work"))
box20a = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/pre/block.md"}, "A1")
run_reply(tg, final=True)
st20a1 = load()
check("20a Stop #1 folds the box IN PLACE - it is still in the chat, showing the recap",
      str(box20a) in surviving(tg) and st20a1.get("folded") is True
      and str(st20a1.get("msgid")) == str(box20a)
      and (st20a1.get("last_live") or "").startswith(mod.RECAP_MARK)
      and not [c for c in tg.calls if c[0] == "delete"],
      "alive=%r msgid=%s" % (surviving(tg), st20a1.get("msgid")))
check("20a Stop #1 arms the fuse and keeps the position fact",
      grace_armed() is not None and st20a1.get("box_is_last") is True
      and st20a1.get("replied_this_turn") is False,
      "grace=%s box_is_last=%s" % (grace_armed(), st20a1.get("box_is_last")))
check("20a the carried recap is untouched at this point",
      len(md_edits(tg, recap20a)) == 1, "edits=%d" % len(md_edits(tg, recap20a)))
do_tool(tg, "Bash", {"command": "deliver the answer"}, "A2")
st20a2 = load()
check("20a the continuation REVIVES the same box and defuses the fuse",
      str(st20a2.get("msgid")) == str(box20a) and st20a2.get("folded") is False
      and grace_armed() is None
      and (st20a2.get("last_live") or "").startswith("🎯"),
      "msgid=%s folded=%s grace=%s" % (st20a2.get("msgid"), st20a2.get("folded"),
                                       grace_armed()))
answer20a = reply_lands()
run_reply(tg, final=False)
run_reply(tg, final=True)
st20a3 = load()
alive20a = surviving(tg)
check("20a the box folds ABOVE the reply and the ANSWER IS LAST",
      below(alive20a, answer20a) == [] and int(box20a) < answer20a
      and str(box20a) in alive20a and st20a3.get("folded") is True,
      "alive=%r answer=%d" % (alive20a, answer20a))
_r20a = md_edits(tg, box20a)[-1][2]
check("20a the recap holds the work from BOTH sides of the block",
      "reading block\\.md" in _r20a and "deliver the answer" in _r20a,
      "%r" % _r20a.splitlines()[1:5])
check("20a no fuse survives a turn that ended with a reply",
      grace_armed() is None, "grace=%s" % grace_armed())
check("20a the box was never deleted at any point in the sequence",
      not [c for c in tg.calls if c[0] == "delete" and str(c[1]) == str(box20a)],
      "deletes=%r" % [c[1] for c in tg.calls if c[0] == "delete"])

print("\n----- 20b the guard-block sequence with NO tools in the continuation -----")
reset_state()
tg = RecTG()
recap20b, _ = prior_turn(tg, "and the one before that")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="just answer"))
box20b = load().get("msgid")
do_tool(tg, "Read", {"file_path": "/only/work.md"}, "B1")
run_reply(tg, final=True)
mark20b = len(tg.calls)
answer20b = reply_lands()
run_reply(tg, final=False)
st20b1 = load()
check("20b the reply defuses the fuse without touching the box",
      grace_armed() is None and str(st20b1.get("msgid")) == str(box20b)
      and st20b1.get("folded") is True and st20b1.get("box_is_last") is False
      and tg.calls[mark20b:] == [],
      "grace=%s calls_since=%r" % (grace_armed(), tg.calls[mark20b:]))
run_reply(tg, final=True)
st20b2 = load()
check("20b Stop #2 makes no call at all - the box is already folded where it belongs",
      tg.calls[mark20b:] == [], "calls=%r" % (tg.calls[mark20b:],))
check("20b the folded box sits ABOVE the answer, and the answer is last",
      str(box20b) in surviving(tg) and int(box20b) < answer20b
      and below(surviving(tg), answer20b) == [],
      "alive=%r answer=%d" % (surviving(tg), answer20b))
check("20b the recap the owner reads is this turn's own, folded, with its work in it",
      "reading work\\.md" in (st20b2.get("last_live") or "")
      and (st20b2.get("last_live") or "").startswith(mod.RECAP_MARK),
      "last_live=%r" % (st20b2.get("last_live") or "")[:80])

print("\n----- 20c a turn that really is dead: the fuse burns down -----")
reset_state()
tg = RecTG()
recap20c, answer20c = prior_turn(tg, "what is the worker up to")
box20c, _ = notif_turn(tg, steps=(("Bash", "poll worker 3"),), fuse=False)
check("20c before the fuse: the box is still there, folded, below the answer",
      str(box20c) in surviving(tg) and below(surviving(tg), answer20c) == [str(box20c)]
      and grace_armed() is not None,
      "alive=%r below=%r" % (surviving(tg), below(surviving(tg), answer20c)))
fired20c = burn_fuse(tg)
st20c = load()
check("20c the fuse fires and the box is taken out", fired20c is True
      and any(c[0] == "delete" and str(c[1]) == str(box20c) for c in tg.calls)
      and str(box20c) not in surviving(tg), "alive=%r" % surviving(tg))
check("20c v25's guarantee, 90 seconds later: nothing below the previous answer",
      below(surviving(tg), answer20c) == [] and surviving(tg) == [str(recap20c)],
      "alive=%r answer=%d" % (surviving(tg), answer20c))
check("20c the work went UP into the carried recap as a continuation block",
      "continued" in md_edits(tg, recap20c)[-1][2]
      and "poll worker 3" in md_edits(tg, recap20c)[-1][2],
      "merged=%r" % md_edits(tg, recap20c)[-1][2][:90])
check("20c state after the fuse is exactly what v25's Stop merge left",
      st20c.get("folded") is True and st20c.get("msgid") is None
      and st20c.get("box_is_last") is False and (st20c.get("steps") or []) == []
      and st20c.get("noreply_grace_until") is None
      and str(st20c.get("prev_recap_msgid")) == str(recap20c),
      "st=%r" % {k: st20c.get(k) for k in ("folded", "msgid", "box_is_last")})
check("20c a second sweep is a no-op (the fuse is gone, nothing to fire on)",
      mod._noreply_grace_sweep(SDIR, tg) is False)

print("\n----- 20d each of the three things that defuse it -----")

def armed_turn(tg, cap="work that never got answered"):
    mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="go"))
    box = load().get("msgid")
    do_tool(tg, "Bash", {"command": cap}, "D%d" % len(tg.calls))
    run_reply(tg, final=True)
    return box

for label, fire in (
        ("a tool event (revive)", lambda t: do_tool(t, "Read", {"file_path": "/x.md"}, "DR")),
        ("a hidden mission write", lambda t: run_step(
            t, "pre", "Write", {"file_path": os.path.join(STATE_DIR, "mission.txt")},
            tuid="DM")),
        ("a reply", lambda t: (reply_lands(), run_reply(t, final=False))),
        ("a new prompt", lambda t: mod._run_deferred(
            SDIR, t, mod.on_prompt(SDIR, t, prompt_text="something else entirely"))),
):
    reset_state()
    tgd = RecTG()
    prior_turn(tgd, "prior")
    armed_turn(tgd)
    was = grace_armed()
    fire(tgd)
    check("20d %s clears the fuse" % label,
          was is not None and grace_armed() is None,
          "before=%s after=%s" % (was, grace_armed()))
    check("20d %s: a later sweep then does nothing" % label,
          mod._noreply_grace_sweep(SDIR, tgd) is False)

print("\n----- 20e when no animator is running, a later edge does the cleanup -----")

reset_state()
tg = RecTG()
recap20e, answer20e = prior_turn(tg, "before the silent turn")
box20e = armed_turn(tg, "the last thing it did")
_st = load()
_st["noreply_grace_until"] = time.time() - 1
mod.save_state(SDIR, _st)
d20e = mod.record_reply(SDIR, tg, final=True)
mod._run_deferred(SDIR, tg, d20e)
check("20e a later Stop performs the cleanup itself once the deadline has passed",
      str(box20e) not in surviving(tg) and load().get("msgid") is None
      and below(surviving(tg), answer20e) == []
      and "continued" in md_edits(tg, recap20e)[-1][2],
      "alive=%r deferred=%r" % (surviving(tg), [x[0] for x in d20e]))
check("20e that Stop keeps the budget: one edit under the lock, the delete deferred",
      [x[0] for x in d20e] == ["delete"], "deferred=%r" % [x[0] for x in d20e])
reset_state()
tg = RecTG()
recap20e2, answer20e2 = prior_turn(tg, "before the other silent turn")
box20e2 = armed_turn(tg, "and this was its last step")
ops20e2 = mod.on_prompt(SDIR, tg, prompt_text="are you still there")
mod._run_deferred(SDIR, tg, ops20e2)
check("20e the next prompt cleans it immediately, without waiting for the deadline",
      str(box20e2) not in surviving(tg)
      and "continued" in md_edits(tg, recap20e2)[-1][2]
      and below(surviving(tg), answer20e2) == [str(load().get("msgid"))],
      "alive=%r below=%r" % (surviving(tg), below(surviving(tg), answer20e2)))
check("20e the boundary defers all of it: only the new box's send runs inline",
      sorted(x[0] for x in ops20e2) == ["delete", "recap"],
      "ops=%r" % [x[0] for x in ops20e2])
check("20e the new turn inherits the merged recap as its own carry target",
      str(load().get("prev_recap_msgid")) == str(recap20e2)
      and (load().get("prev_recap_text") or "").startswith(mod.RECAP_MARK)
      and load().get("noreply_grace_until") is None,
      "prev=%s" % load().get("prev_recap_msgid"))

print("\n----- 20f the animator really does keep ticking until the fuse burns -----")

_ANIM, _GRACE = mod.ANIM_INTERVAL, mod.NOREPLY_GRACE
try:
    mod.ANIM_INTERVAL, mod.NOREPLY_GRACE = 0.05, 0.35
    reset_state()
    tg = RecTG()
    recap20f, answer20f = prior_turn(tg, "the turn before the animator test")
    box20f = armed_turn(tg, "one last poll")
    mark20f = len(tg.calls)
    t0 = time.time()
    mod.run_animator(SDIR, tg)
    dt20f = time.time() - t0
    tail20f = tg.calls[mark20f:]
    check("20f the animator waited for the fuse instead of exiting on the fold",
          dt20f >= 0.3, "waited=%.2fs" % dt20f)
    check("20f it fired exactly once it burned down, and then exited",
          dt20f < 5 and load().get("msgid") is None
          and str(box20f) not in surviving(tg), "waited=%.2fs" % dt20f)
    check("20f it spent NOTHING while waiting: only the merge edit and the delete",
          [c[0] for c in tail20f] == ["edit", "delete"]
          and not [c for c in tail20f if c[0] == "typing"],
          "calls=%r" % [c[0] for c in tail20f])
    check("20f and the answer is last again",
          below(surviving(tg), answer20f) == [] and surviving(tg) == [str(recap20f)],
          "alive=%r" % surviving(tg))

    reset_state()
    tg = RecTG()
    prior_turn(tg, "x")
    mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="q"))
    do_tool(tg, "Read", {"file_path": "/a.md"}, "F9")
    reply_lands()
    run_reply(tg, final=False)
    run_reply(tg, final=True)
    mark20f2 = len(tg.calls)
    t0 = time.time()
    mod.run_animator(SDIR, tg)
    check("20f a folded box with no fuse still exits immediately and silently",
          (time.time() - t0) < 0.5 and tg.calls[mark20f2:] == [],
          "took=%.2fs calls=%r" % (time.time() - t0, tg.calls[mark20f2:]))

    reset_state()
    tg = RecTG()
    prior_turn(tg, "y")
    box20f3 = armed_turn(tg, "poll")
    _st = load()
    _st["noreply_grace_until"] = time.time() + 86400
    mod.save_state(SDIR, _st)
    mark20f3 = len(tg.calls)
    t0 = time.time()
    mod.run_animator(SDIR, tg)
    check("20f a far-future deadline makes it give up, not hang - and it fires nothing",
          (time.time() - t0) < 3 and tg.calls[mark20f3:] == []
          and str(box20f3) in surviving(tg),
          "took=%.2fs calls=%r" % (time.time() - t0, tg.calls[mark20f3:]))
finally:
    mod.ANIM_INTERVAL, mod.NOREPLY_GRACE = _ANIM, _GRACE

print("\n----- 20g the boundary, and the turn that is still running when it lands -----")

reset_state()
tg = RecTG()
recap20g, answer20g = prior_turn(tg, "the first thing")
mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text="start something long"))
box20g = load().get("msgid")
do_tool(tg, "Bash", {"command": "still running when the next message lands"}, "G1")
ops20g = mod.on_prompt(SDIR, tg, prompt_text="actually, also do this")
mod._run_deferred(SDIR, tg, ops20g)
check("20g a LIVE box at the boundary keeps its v25 collapse (up, never parked below)",
      str(box20g) not in surviving(tg)
      and "still running when the next message lands" in md_edits(tg, recap20g)[-1][2]
      and below(surviving(tg), answer20g) == [str(load().get("msgid"))],
      "alive=%r" % surviving(tg))
check("20g the new turn's box is the only streamer message below the answer",
      load().get("msgid") is not None and load().get("box_is_last") is True
      and load().get("noreply_grace_until") is None)

print("\n----- 20h v25-shaped and v24-shaped state through every edge -----")
V25_KEYS = V24_KEYS + ["prev_recap_msgid", "prev_recap_text"]

def write_v25_state(**over):
    now = time.time()
    st = {"msgid": "9700",
          "steps": [{"fam": "run", "cap": "mid-flight when v26 landed", "tuid": "y1",
                     "done": True, "k": "", "aid": ""}],
          "proc_start": now - 40, "last_edit": 0, "last_activity": now - 2,
          "last_reply": 0, "last_react": now - 40, "last_spawn": now - 40,
          "frame": 6, "folded": False, "last_live": "old live text", "agents": {},
          "next_agent_n": 1, "ghosts": [], "typing_done": False, "reanchor": False,
          "box_is_last": True, "folded_above_msgid": None, "replied_this_turn": False,
          "mission": "", "panel": [],
          "prev_recap_msgid": "9600",
          "prev_recap_text": mod.RECAP_MARK + " · 2:00 · tap to expand\n**>🎯 on it\n"
                             ">✓ 🔍 reading earlier\\.md||"}
    st.update(over)
    mod.save_state(SDIR, st)
    return st

reset_state()
write_v25_state()
check("20h the fixture really is v25-shaped (no fuse key present)",
      set(load().keys()) == set(V25_KEYS) and "noreply_grace_until" not in load(),
      "keys=%r" % sorted(load().keys()))
tg20h = RecTG()
run_step(tg20h, "pre", "Read", {"file_path": "/still/going.md"}, tuid="y2")
check("20h a tool event over v25 state initializes the fuse key to the safe default",
      "noreply_grace_until" in load() and load().get("noreply_grace_until") is None,
      "grace=%r" % load().get("noreply_grace_until"))
run_reply(tg20h, final=True)
st20h = load()
check("20h Stop over v25 state folds in place and arms, exactly as over fresh state",
      st20h.get("folded") is True and str(st20h.get("msgid")) == "9700"
      and st20h.get("noreply_grace_until") is not None,
      "msgid=%s grace=%s" % (st20h.get("msgid"), st20h.get("noreply_grace_until")))
burn_fuse(tg20h)
check("20h and the fuse then carries the work into the v25-shaped carried recap",
      load().get("msgid") is None
      and any(c[0] == "edit" and str(c[1]) == "9600" and "continued" in c[2]
              for c in tg20h.calls)
      and any(c[0] == "delete" and str(c[1]) == "9700" for c in tg20h.calls),
      "calls=%r" % [(c[0], c[1]) for c in tg20h.calls if c[0] in ("edit", "delete")])

ok20h, out20h = True, b""
for shape, writer in (("v25", write_v25_state), ("v24", write_v24_state)):
    reset_state()
    writer()
    for mode, payload in [
            ("pre", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
            ("post", {"tool_name": "Read", "tool_input": {"file_path": "/u"}, "tool_use_id": "u1"}),
            ("stop", {}),
            ("stop", {}),
            ("post", {"tool_name": mod.REPLY_TOOL}),
            ("stop", {}),
            ("prompt", {"prompt": NOTIF}),
            ("stop", {}),
    ]:
        p = drive_main(mode, payload)
        if p.returncode != 0:
            ok20h = False
        out20h += p.stdout
check("20h every hook edge over v25- and v24-shaped state exits 0", ok20h)
check("20h every hook edge over v25- and v24-shaped state prints nothing but the nudge",
      strip_nudges(out20h) == b"", "stdout=%r" % strip_nudges(out20h)[:120])

print("\n----- 20i synthetic previous-turn state fixtures -----")

DELTA_1721 = json.loads('{"msgid": null, "steps": [], "proc_start": 1785014479.635082, "last_edit": 1785014510.3662689, "last_activity": 1785014495.978155, "last_reply": 1785014511.239655, "last_react": 1785014479.635082, "frame": 9, "last_spawn": 1785014495.4021251, "folded": false, "last_live": "🎯 Reviewing fixture configuration\\n🤖 Working · 1:02\\n✓ Subagents:\\n  ↳ ✓ 🤖 1 worker: reading the test fixture\\n💭 thinking.", "agents": {}, "next_agent_n": 1, "ghosts": [], "typing_done": true, "reanchor": false, "box_is_last": false, "folded_above_msgid": null, "replied_this_turn": false, "mission": "Reviewing the example configuration", "panel": [], "prev_recap_msgid": "3198", "prev_recap_text": "📋 What I did · 4:24 · tap to expand\\n**>🎯 Reviewing fixture configuration\\n>✓ ⚙️ Checked the example settings\\n>✓ ✏️ Updated the sample note||"}')
DELTA_1753 = json.loads('{"msgid": "3220", "steps": [{"fam": "helpers", "cap": "reading the fixture file", "tuid": "toolu_fixture000000000000000009", "done": true, "k": "subwork", "aid": "a0000000000000001"}], "proc_start": 1785016356.511848, "last_edit": 1785016418.938316, "last_activity": 1785016379.731381, "last_reply": 0, "last_react": 1785016356.511848, "frame": 17, "last_spawn": 1785016379.1065679, "folded": false, "last_live": "🎯 Reviewing fixture configuration\\n🤖 Working · 1:02\\n✓ Subagents:\\n  ↳ ✓ 🤖 1 worker: reading the test fixture\\n💭 thinking.", "agents": {"a0000000000000001": {"n": 1, "type": "opus-executor"}}, "next_agent_n": 2, "ghosts": [], "typing_done": false, "reanchor": false, "box_is_last": true, "folded_above_msgid": null, "replied_this_turn": false, "mission": "", "panel": [], "prev_recap_msgid": "3217", "prev_recap_text": "📋 What I did · 4:24 · tap to expand\\n**>🎯 Reviewing fixture configuration\\n>✓ ⚙️ Checked the example settings\\n>✓ ✏️ Updated the sample note||"}')

reset_state()
mod.save_state(SDIR, dict(DELTA_1721))
tg20i = RecTG()
ops20i = mod.on_prompt(SDIR, tg20i, prompt_text="ok what happened there")
mod._run_deferred(SDIR, tg20i, ops20i)
st20i = load()
check("20i the boxless fixture state queues NO cleanup ops at all",
      ops20i == [], "ops=%r" % (ops20i,))
check("20i it just opens a normal fresh box, and nothing is edited or deleted",
      st20i.get("msgid") is not None and st20i.get("box_is_last") is True
      and st20i.get("folded") is False and (st20i.get("steps") or []) == []
      and not [c for c in tg20i.calls if c[0] in ("edit", "delete")],
      "msgid=%s calls=%r" % (st20i.get("msgid"), [c[0] for c in tg20i.calls]))
check("20i the carried recap (3198) rides forward as the next turn's merge target",
      str(st20i.get("prev_recap_msgid")) == "3198"
      and (st20i.get("prev_recap_text") or "").startswith(mod.RECAP_MARK)
      and st20i.get("noreply_grace_until") is None,
      "prev=%s" % st20i.get("prev_recap_msgid"))
check("20i the fresh box renders the normal placeholder line, no leak from the fixture state",
      [c for c in tg20i.calls if c[0] == "send"][-1][2].splitlines()[0]
      == "🎯 " + mod.MISSION_DEFAULT,
      "line1=%r" % [c for c in tg20i.calls if c[0] == "send"][-1][2].splitlines()[0])
reset_state()
mod.save_state(SDIR, dict(DELTA_1753))
tg20i2 = RecTG()
ops20i2 = mod.on_prompt(SDIR, tg20i2, prompt_text="and now this")
mod._run_deferred(SDIR, tg20i2, ops20i2)
st20i2 = load()
check("20i the active fixture state collapses UP into 3217 and opens a fresh box",
      sorted(x[0] for x in ops20i2) == ["delete", "recap"]
      and any(c[0] == "edit" and str(c[1]) == "3217"
              and "Interrupted by user" in c[2]
              for c in tg20i2.calls)
      and any(c[0] == "delete" and str(c[1]) == "3220" for c in tg20i2.calls)
      and st20i2.get("msgid") not in (None, "3220"),
      "ops=%r msgid=%s" % ([x[0] for x in ops20i2], st20i2.get("msgid")))
check("20i that merge is valid MarkdownV2 with one expandable, built on the synthetic text",
      mdv2_fully_escaped([c for c in tg20i2.calls
                          if c[0] == "edit" and str(c[1]) == "3217"][-1][2]))
for label, fixture in (("boxless", DELTA_1721), ("active", DELTA_1753)):
    reset_state()
    mod.save_state(SDIR, dict(fixture))
    okj, outj = True, b""
    for mode, payload in [
            ("pre", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "j1"}),
            ("post", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "j1"}),
            ("stop", {}),
            ("stop", {}),
            ("prompt", {"prompt": "<channel source=\"telegram\">next</channel>"}),
    ]:
        p = drive_main(mode, payload)
        if p.returncode != 0:
            okj = False
        outj += p.stdout
    check("20i every hook edge over the %s fixture state exits 0 and stays silent" % label,
          okj and strip_nudges(outj) == b"", "stdout=%r" % strip_nudges(outj)[:80])

reset_state()
mod.save_state(SDIR, dict(DELTA_1721))
p20i = drive_main("animate", {})
check("20i the animate edge over the boxless fixture exits 0 at once and prints nothing",
      p20i.returncode == 0 and p20i.stdout == b"",
      "rc=%d stdout=%r" % (p20i.returncode, p20i.stdout[:80]))

print("\n----- 20j the shapes the fuse must never fire on -----")

reset_state()
_now20j = time.time()
_PARKED = {
    "msgid": "3192", "steps": [{"fam": "run", "cap": "capture the resolved model",
                                "tuid": "j1", "done": False, "k": "", "aid": ""}],
    "proc_start": _now20j - 1980, "last_edit": _now20j - 3,
    "last_activity": _now20j - 1800, "last_reply": 0, "last_react": _now20j - 1980,
    "last_spawn": _now20j - 1980, "frame": 490, "folded": True,
    "last_live": "📋 What I did · 33:07 · tap to expand\n**>🎯 Checking worker 2\n>✓ ⚙️ ran it||",
    "agents": {}, "next_agent_n": 1, "ghosts": [], "typing_done": False,
    "reanchor": False, "box_is_last": True, "folded_above_msgid": None,
    "replied_this_turn": False, "mission": "", "panel": [],
    "prev_recap_msgid": "3100", "prev_recap_text": mod.RECAP_MARK + " · 1:00 · tap to expand\n**>🎯 x||"}
mod.save_state(SDIR, dict(_PARKED))
tg20j = RecTG()
check("20j a v25-parked recap (the shape, but no fuse) is never swept",
      mod._noreply_grace_sweep(SDIR, tg20j) is False and tg20j.calls == []
      and load().get("msgid") == "3192", "calls=%r" % tg20j.calls)
ops20j = mod.on_prompt(SDIR, tg20j, prompt_text="a new question")
mod._run_deferred(SDIR, tg20j, ops20j)
check("20j and the boundary leaves it alone too, exactly as v25 documented",
      not [c for c in tg20j.calls if c[0] in ("edit", "delete")]
      and str(load().get("prev_recap_msgid")) != "3192"
      and str(load().get("prev_recap_msgid")) == "3100",
      "calls=%r prev=%s" % ([c[0] for c in tg20j.calls], load().get("prev_recap_msgid")))
for label, bad in (("a string", "soon"), ("a list", [1]), ("True", True),
                   ("None", None), ("a dict", {})):
    reset_state()
    mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=bad))
    tgb = RecTG()
    check("20j a %s deadline reads as no fuse at all" % label,
          mod._grace_deadline(load()) is None
          and mod._noreply_grace_sweep(SDIR, tgb) is False and tgb.calls == [],
          "deadline=%r" % mod._grace_deadline(load()))
reset_state()
mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=0, box_is_last=False))
tg20j2 = RecTG()
check("20j a burned fuse on a box that is NOT below the answer never fires",
      mod._noreply_grace_sweep(SDIR, tg20j2) is False and tg20j2.calls == [])
reset_state()
mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=0, replied_this_turn=True))
tg20j3 = RecTG()
check("20j a burned fuse on a turn that DID reply never fires",
      mod._noreply_grace_sweep(SDIR, tg20j3) is False and tg20j3.calls == [])
reset_state()
mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=0, steps=[]))
tg20j4 = RecTG()
check("20j a burned fuse on an empty box never fires",
      mod._noreply_grace_sweep(SDIR, tg20j4) is False and tg20j4.calls == [])
reset_state()
mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=0, folded=False))
tg20j5 = RecTG()
check("20j a burned fuse on a box that is still LIVE never fires (it is not folded yet)",
      mod._noreply_grace_sweep(SDIR, tg20j5) is False and tg20j5.calls == [])
reset_state()
mod.save_state(SDIR, dict(_PARKED, noreply_grace_until=0,
                          prev_recap_msgid="3192",
                          prev_recap_text=_PARKED["last_live"]))
tg20j6 = RecTG()
mod._noreply_grace_sweep(SDIR, tg20j6)
check("20j the fuse never merges into the very message it is about to delete",
      not [c for c in tg20j6.calls if c[0] == "edit"]
      and [c[0] for c in tg20j6.calls] == ["delete"],
      "calls=%r" % [c[0] for c in tg20j6.calls])
reset_state()

print("\n===== #21 Codex exec visibility =====")

_cases21 = [
    ({"command": "codex exec -m gpt-5.6-sol --sandbox read-only "
                 "-o /private/tmp/lane-research-report.md -",
      "description": "run the external reviewer"},
     "GPT-5.6 Sol · lane-research"),
    ({"command": "/opt/homebrew/bin/codex exec -m gpt-5.6-terra "
                 "-o '/private/tmp/architecture-report.md' prompt"},
     "GPT-5.6 Terra · architecture"),
    ({"command": "codex exec --sandbox read-only prompt"},
     "Codex"),
]
_got21 = [mod.caption_of("Bash", tin, None) for tin, _ in _cases21]
_want21 = [want for _, want in _cases21]
check("21a Sol, Terra and no-model Codex commands get the dedicated label",
      _got21 == _want21, "got=%r" % (_got21,))

check("21b a non-Codex Bash description is byte-for-byte unchanged",
      mod.caption_of("Bash",
                     {"command": "printf hello", "description": "format release notes"},
                     None) == "format release notes")

reset_state()
tg21 = RecTG()
mod._run_deferred(SDIR, tg21, mod.on_prompt(SDIR, tg21, prompt_text="run the review"))
tg21.calls.clear()
_cmd21 = _cases21[0][0]
run_step(tg21, "pre", "Bash", _cmd21, tuid="CX21")
_st21 = load()
_step21 = _st21["steps"][-1]
_has_start21 = (isinstance(_step21.get("t0"), (int, float))
                and not isinstance(_step21.get("t0"), bool))
_real_time21 = mod.time.time
_base21 = _real_time21()
_step21["t0"] = _base21 - 245
try:
    mod.time.time = lambda: _base21
    _live21 = mod.render_live(_st21["steps"], _st21["frame"], _st21["proc_start"],
                              _st21["agents"], mod.MISSION_DEFAULT, _st21["panel"])
finally:
    mod.time.time = _real_time21

_lines21 = [ln for ln in _live21.splitlines() if "GPT-5.6" in ln]
check("21c the in-flight Codex line reuses the live M:SS clock",
      _has_start21 and _lines21 == ["⏳ GPT-5.6 Sol · lane-research · 4:05"],
      "t0=%r lines=%r" % (_step21.get("t0"), _lines21))

mod.save_state(SDIR, _st21)
run_step(tg21, "post", "Bash", _cmd21, tuid="CX21")
_done21 = load()
_live_done21 = mod.render_live(_done21["steps"], _done21["frame"],
                               _done21["proc_start"], _done21["agents"],
                               mod.MISSION_DEFAULT, _done21["panel"])

_done_lines21 = [ln for ln in _live_done21.splitlines() if "GPT-5.6" in ln]
check("21d completion keeps the normal done flip and removes only the live clock",
      _done21["steps"][-1].get("done") is True
      and _done21["steps"][-1].get("fam") == "run"
      and _done_lines21 == ["✓ GPT-5.6 Sol · lane-research"],
      "step=%r lines=%r" % (_done21["steps"][-1], _done_lines21))
reset_state()

print("\n===== #22 background Codex lanes stay live =====")

def _bg_cmd22(slug, model="gpt-5.6-sol"):
    scratch = "/private/tmp/tgstream-bg-%s" % slug
    return {
        "command": (
            'S="%s"\n'
            "codex exec -m %s -c model_reasoning_effort=max --sandbox read-only "
            '--skip-git-repo-check --color never -o "$S/%s-report.md" '
            '"$(cat "$S/%s-brief.txt")" > "$S/%s-run.log" 2>&1'
        ) % (scratch, model, slug, slug, slug),
        "description": "Launch background Codex lane %s" % slug,
        "run_in_background": True,
    }

def _done_notification22(tuid, task_id="bgtask22"):
    return (
        "<task-notification>\n"
        "<task-id>%s</task-id>\n"
        "<tool-use-id>%s</tool-use-id>\n"
        "<output-file>/private/tmp/tasks/%s.output</output-file>\n"
        "<status>completed</status>\n"
        '<summary>Background command "Codex lane" completed (exit code 0)</summary>\n'
        "</task-notification>"
    ) % (task_id, tuid, task_id)

reset_state()
tg22 = RecTG()
mod._run_deferred(SDIR, tg22, mod.on_prompt(SDIR, tg22, prompt_text="run Codex"))
_box22 = load().get("msgid")
_cmd22 = _bg_cmd22("persistent")
run_step(tg22, "pre", "Bash", _cmd22, tuid="CXBG22")
_pre22 = load()["steps"][-1]
run_step(tg22, "post", "Bash", _cmd22, tuid="CXBG22")
_post22 = load()
check("22a the real boolean run_in_background key marks a Codex lane",
      _pre22.get("codex_bg") is True,
      "step=%r" % _pre22)
check("22a the launch PostToolUse leaves the background lane OPEN",
      _post22["steps"][-1].get("done") is False,
      "step=%r" % _post22["steps"][-1])
run_reply(tg22, final=True)
_stop22 = load()
check("22a Stop defers while the background lane is open",
      _stop22.get("msgid") == _box22
      and _stop22.get("folded") is False
      and _stop22.get("typing_done") is False
      and _stop22["steps"][-1].get("done") is False,
      "msgid=%r folded=%r typing_done=%r step=%r" % (
          _stop22.get("msgid"), _stop22.get("folded"),
          _stop22.get("typing_done"), _stop22["steps"][-1]))

_real_time22 = mod.time.time
_base22 = _real_time22()
_stop22["steps"][-1]["t0"] = _base22 - 60
try:
    mod.time.time = lambda: _base22
    _tick22a = mod.render_live(
        _stop22["steps"], _stop22["frame"], _stop22["proc_start"],
        _stop22["agents"], mod.MISSION_DEFAULT, _stop22["panel"])
    mod.time.time = lambda: _base22 + 65
    _tick22b = mod.render_live(
        _stop22["steps"], _stop22["frame"] + 1, _stop22["proc_start"],
        _stop22["agents"], mod.MISSION_DEFAULT, _stop22["panel"])
finally:
    mod.time.time = _real_time22

_line22a = [x for x in _tick22a.splitlines() if "GPT-5.6" in x]
_line22b = [x for x in _tick22b.splitlines() if "GPT-5.6" in x]
check("22b subsequent repaints advance the persistent Codex clock",
      _line22a == ["⏳ GPT-5.6 Sol · persistent · 1:00"]
      and _line22b == ["⏳ GPT-5.6 Sol · persistent · 2:05"],
      "first=%r second=%r" % (_line22a, _line22b))
mod.save_state(SDIR, _stop22)

_before_notify_mid22 = load().get("msgid")
_notify_ops22 = mod.on_prompt(
    SDIR, tg22, prompt_text=_done_notification22("CXBG22"))
mod._run_deferred(SDIR, tg22, _notify_ops22)
_closed22 = load()
_closed_steps22 = _closed22.get("steps") or []
_closed_step22 = _closed_steps22[-1] if _closed_steps22 else {}
_closed_live22 = mod.render_live(
    _closed_steps22, _closed22.get("frame", 0), _closed22.get("proc_start"),
    _closed22.get("agents") or {}, mod.MISSION_DEFAULT, _closed22.get("panel"))

_closed_line22 = [x for x in _closed_live22.splitlines() if "GPT-5.6" in x]
check("22c completion closes the matching lane in the same box",
      _closed22.get("msgid") == _before_notify_mid22
      and _closed_step22.get("done") is True
      and _closed_line22 == ["✓ GPT-5.6 Sol · persistent"],
      "msgid=%r step=%r line=%r" % (
          _closed22.get("msgid"), _closed_step22, _closed_line22))
check("22c a matched notification does not open a replacement box",
      not [c for c in tg22.calls if c[0] == "send" and c[1] != str(_box22)],
      "sends=%r" % tg22.ops("send"))

reset_state()
tg22d = RecTG()
mod._run_deferred(SDIR, tg22d, mod.on_prompt(SDIR, tg22d, prompt_text="fan out"))
for _tuid22, _slug22, _model22 in (
        ("CXA22", "alpha", "gpt-5.6-sol"),
        ("CXB22", "beta", "gpt-5.6-terra")):
    _input22 = _bg_cmd22(_slug22, _model22)
    run_step(tg22d, "pre", "Bash", _input22, tuid=_tuid22)
    run_step(tg22d, "post", "Bash", _input22, tuid=_tuid22)
_ops22d = mod.on_prompt(SDIR, tg22d, prompt_text=_done_notification22("CXA22", "task-a"))
mod._run_deferred(SDIR, tg22d, _ops22d)
_lanes22d = [s for s in load().get("steps", []) if s.get("codex_bg")]
check("22d one completion closes only its matching lane",
      len(_lanes22d) == 2
      and _lanes22d[0].get("done") is True
      and _lanes22d[1].get("done") is False,
      "lanes=%r" % _lanes22d)
_live22d = mod.render_live(
    load()["steps"], load()["frame"], load()["proc_start"],
    load()["agents"], mod.MISSION_DEFAULT, load()["panel"])

check("22d simultaneous lanes render as separate done and open lines",
      len([x for x in _live22d.splitlines() if "GPT-5.6" in x]) == 2
      and "✓ GPT-5.6 Sol · alpha" in _live22d
      and "⏳ GPT-5.6 Terra · beta" in _live22d,
      "lines=%r" % [x for x in _live22d.splitlines() if "GPT-5.6" in x])

class _Clock22:
    def __init__(self, now):
        self.now = float(now)
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

_time_module22 = mod.time
_anim22 = mod.ANIM_INTERVAL
_maxrun22 = mod.ANIM_MAX_RUN
_maxidle22 = mod.ANIM_MAX_IDLE
_minedit22 = mod.MIN_EDIT_INTERVAL
_maxage22 = getattr(mod, "CODEX_BG_MAX_AGE", None)
try:
    _clock22 = _Clock22(1000)
    mod.time = _clock22
    mod.ANIM_INTERVAL = 3
    mod.ANIM_MAX_RUN = 60
    mod.ANIM_MAX_IDLE = 60
    mod.MIN_EDIT_INTERVAL = 0
    mod.CODEX_BG_MAX_AGE = 12
    reset_state()
    tg22e = RecTG()
    mod._run_deferred(SDIR, tg22e, mod.on_prompt(SDIR, tg22e, prompt_text="long lane"))
    _cmd22e = _bg_cmd22("aging")
    run_step(tg22e, "pre", "Bash", _cmd22e, tuid="CXSTALE22")
    run_step(tg22e, "post", "Bash", _cmd22e, tuid="CXSTALE22")
    run_reply(tg22e, final=True)
    tg22e.calls.clear()
    mod.run_animator(SDIR, tg22e)
    _after_anim22 = load()
    _stale_step22 = _after_anim22["steps"][-1]
    _typing22 = len(tg22e.ops("typing"))

    check("22e the normal three-second animator cadence sustains typing while open",
          _typing22 == 2 and _clock22.sleeps == [3, 3, 3, 3]
          and mod.TYPING_MIN_INTERVAL == 4.0
          and mod.TYPING_MIN_INTERVAL < 5.0,
          "typing=%d sleeps=%r floor=%r" % (
              _typing22, _clock22.sleeps, mod.TYPING_MIN_INTERVAL))
    check("22e a lane at the two-hour-equivalent cutoff self-closes as stale",
          _stale_step22.get("done") is True
          and _stale_step22.get("codex_stale") is True
          and "stale" in (_stale_step22.get("cap") or "")
          and _after_anim22.get("typing_done") is True
          and _after_anim22.get("folded") is True,
          "step=%r folded=%r typing_done=%r" % (
              _stale_step22, _after_anim22.get("folded"),
              _after_anim22.get("typing_done")))
    check("22e the stale render is visible in the final panel edit",
          any(c[0] == "edit" and "stale" in c[2] for c in tg22e.calls),
          "edits=%r" % [c[:2] for c in tg22e.ops("edit")])
finally:
    mod.time = _time_module22
    mod.ANIM_INTERVAL = _anim22
    mod.ANIM_MAX_RUN = _maxrun22
    mod.ANIM_MAX_IDLE = _maxidle22
    mod.MIN_EDIT_INTERVAL = _minedit22
    if _maxage22 is None:
        try:
            delattr(mod, "CODEX_BG_MAX_AGE")
        except AttributeError:
            pass
    else:
        mod.CODEX_BG_MAX_AGE = _maxage22
reset_state()

print("\n===== #23 live box holds the bottom while work continues =====")

class _OneTickClock23:
    def __init__(self, now, off_path):
        self.now = float(now)
        self.off_path = off_path
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        with open(self.off_path, "w") as f:
            f.write("")

def animator_tick23(tg):
    off_path = os.path.join(STATE_DIR, "stream.off")
    try:
        os.remove(off_path)
    except OSError:
        pass
    real_time = mod.time
    real_interval = mod.ANIM_INTERVAL
    real_max_run = mod.ANIM_MAX_RUN
    real_max_idle = mod.ANIM_MAX_IDLE
    real_min_edit = mod.MIN_EDIT_INTERVAL
    st = load()
    now = max(time.time(), float(st.get("proc_start") or 0) + 1)
    clock = _OneTickClock23(now, off_path)
    try:
        mod.time = clock
        mod.ANIM_INTERVAL = 3
        mod.ANIM_MAX_RUN = 100000
        mod.ANIM_MAX_IDLE = 100000

        mod.MIN_EDIT_INTERVAL = 100000
        mod.run_animator(SDIR, tg)
    finally:
        mod.time = real_time
        mod.ANIM_INTERVAL = real_interval
        mod.ANIM_MAX_RUN = real_max_run
        mod.ANIM_MAX_IDLE = real_max_idle
        mod.MIN_EDIT_INTERVAL = real_min_edit
        try:
            os.remove(off_path)
        except OSError:
            pass
    return clock

reset_state()
tg23a = RecTG()
mod._run_deferred(SDIR, tg23a, mod.on_prompt(
    SDIR, tg23a, prompt_text="keep the live box visible"))
mission_path23 = os.path.join(STATE_DIR, mod.MISSION_FILE)
with open(mission_path23, "w") as f:
    f.write("Holding the work box at the bottom")
_mission_state23 = load()
os.utime(mission_path23, (
    _mission_state23["proc_start"] + 1,
    _mission_state23["proc_start"] + 1))

for _tuid23, _desc23, _type23 in (
        ("AG23A", "position audit row", "auditor"),
        ("AG23B", "lane watch row", "worker")):
    _agent_input23 = {
        "description": _desc23,
        "subagent_type": _type23,
        "model": "claude-opus-5",
    }
    run_step(tg23a, "pre", "Agent", _agent_input23, tuid=_tuid23)
    run_step(tg23a, "post", "Agent", _agent_input23, tuid=_tuid23)

for _tuid23, _slug23, _model23 in (
        ("CX23A", "hold-alpha", "gpt-5.6-sol"),
        ("CX23B", "hold-beta", "gpt-5.6-terra")):
    _codex_input23 = _bg_cmd22(_slug23, _model23)
    run_step(tg23a, "pre", "Bash", _codex_input23, tuid=_tuid23)
    run_step(tg23a, "post", "Bash", _codex_input23, tuid=_tuid23)
do_tool(tg23a, "Read", {"file_path": "/tmp/bottom-position.md"}, "R23")

_before_reply23 = load()
_old_box23 = _before_reply23.get("msgid")
_step_count23 = len(_before_reply23.get("steps") or [])
_message_below23 = reply_lands()
run_reply(tg23a, final=False)
_send_count23 = len(tg23a.ops("send"))
_clock23 = animator_tick23(tg23a)
_held23 = load()
_reposts23 = tg23a.ops("send")[_send_count23:]
_repost_text23 = _reposts23[-1][2] if _reposts23 else ""

check("23a a live turn re-anchors below the new message without waiting for another tool",
      _held23.get("msgid") != _old_box23
      and int(_held23.get("msgid") or 0) > _message_below23
      and _held23.get("box_is_last") is True
      and _held23.get("reanchor") is False,
      "old=%r message=%r new=%r last=%r reanchor=%r" % (
          _old_box23, _message_below23, _held23.get("msgid"),
          _held23.get("box_is_last"), _held23.get("reanchor")))
check("23a the animator reposts exactly once on the displaced tick",
      len(_reposts23) == 1 and _clock23.sleeps == [3],
      "reposts=%r sleeps=%r" % (_reposts23, _clock23.sleeps))

check("23a the repost preserves mission, steps, agent rows and both Codex lanes",
      all(token in _repost_text23 for token in (
          "🎯 Holding the work box at the bottom",
          "position audit row", "lane watch row",
          "GPT-5.6 Sol · hold-alpha", "GPT-5.6 Terra · hold-beta",
          "reading bottom-position.md"))
      and len(_held23.get("steps") or []) == _step_count23
      and len(_held23.get("panel") or []) == 2
      and len([s for s in (_held23.get("steps") or [])
               if s.get("codex_bg") and not s.get("done")]) == 2,
      "text=%r steps=%d panel=%d" % (
          _repost_text23, len(_held23.get("steps") or []),
          len(_held23.get("panel") or [])))
try:
    os.remove(mission_path23)
except OSError:
    pass
reset_state()

tg23b = RecTG()
mod._run_deferred(SDIR, tg23b, mod.on_prompt(
    SDIR, tg23b, prompt_text="stay on the same message"))
do_tool(tg23b, "Read", {"file_path": "/tmp/already-last.md"}, "R23B")
_same_box23 = load().get("msgid")
_sends_before23 = len(tg23b.ops("send"))
animator_tick23(tg23b)
_same_state23 = load()
check("23b the animator does not repost when the live box is already last",
      len(tg23b.ops("send")) == _sends_before23
      and _same_state23.get("msgid") == _same_box23
      and _same_state23.get("box_is_last") is True,
      "sends=%d->%d msgid=%r->%r" % (
          _sends_before23, len(tg23b.ops("send")),
          _same_box23, _same_state23.get("msgid")))
reset_state()

tg23c = RecTG()
mod._run_deferred(SDIR, tg23c, mod.on_prompt(
    SDIR, tg23c, prompt_text="work, update, then answer"))
do_tool(tg23c, "Read", {"file_path": "/tmp/final-position.md"}, "R23C")
_interim23 = reply_lands()
run_reply(tg23c, final=False)
animator_tick23(tg23c)
_final_answer23 = reply_lands()
run_reply(tg23c, final=False)
run_reply(tg23c, final=True)
_alive23c = surviving(tg23c)
_done23c = load()
check("23c Stop restores answer-last with one recap above the final reply",
      len(_alive23c) == 1
      and int(_alive23c[0]) < _final_answer23
      and below(_alive23c, _final_answer23) == []
      and _done23c.get("folded") is True
      and str(_done23c.get("msgid")) == _alive23c[0],
      "alive=%r interim=%r answer=%r state=%r" % (
          _alive23c, _interim23, _final_answer23,
          {k: _done23c.get(k) for k in ("msgid", "folded", "box_is_last")}))
reset_state()

tg23d = RecTG()
_recap23d, _answer23d = prior_turn(tg23d, "answer before silent work")
mod._run_deferred(SDIR, tg23d, mod.on_prompt(
    SDIR, tg23d, prompt_text="silent notification work"))
do_tool(tg23d, "Read", {"file_path": "/tmp/no-reply-23.md"}, "R23D")
_silent_box23 = load().get("msgid")
run_reply(tg23d, final=True)
_armed23d = load()
check("23d a no-reply Stop still folds in place and arms the v26 fuse",
      _armed23d.get("msgid") == _silent_box23
      and _armed23d.get("folded") is True
      and mod._grace_deadline(_armed23d) is not None,
      "box=%r state=%r" % (
          _silent_box23,
          {k: _armed23d.get(k) for k in (
              "msgid", "folded", "box_is_last", "noreply_grace_until")}))
burn_fuse(tg23d)
_after_fuse23d = load()
check("23d a silent turn still carries up and leaves the previous answer last",
      below(surviving(tg23d), _answer23d) == []
      and str(_recap23d) in surviving(tg23d)
      and _after_fuse23d.get("msgid") is None
      and _after_fuse23d.get("box_is_last") is False,
      "alive=%r answer=%r state=%r" % (
          surviving(tg23d), _answer23d,
          {k: _after_fuse23d.get(k) for k in ("msgid", "box_is_last")}))
reset_state()

tg23e = RecTG()
mod._run_deferred(SDIR, tg23e, mod.on_prompt(
    SDIR, tg23e, prompt_text="fail open if Telegram refuses"))
do_tool(tg23e, "Read", {"file_path": "/tmp/fail-open-23.md"}, "R23E")
_old23e = load().get("msgid")
reply_lands()
run_reply(tg23e, final=False)
tg23e.fail_send = True
_deletes23e = len(tg23e.ops("delete"))
animator_tick23(tg23e)
_failed23e = load()
check("23e a refused repost keeps the prior box live and the re-anchor armed",
      _failed23e.get("msgid") == _old23e
      and _failed23e.get("folded") is False
      and _failed23e.get("reanchor") is True
      and len(tg23e.ops("delete")) == _deletes23e,
      "old=%r state=%r deletes=%d->%d" % (
          _old23e,
          {k: _failed23e.get(k) for k in ("msgid", "folded", "reanchor")},
          _deletes23e, len(tg23e.ops("delete"))))
reset_state()

print("\n===== #24 (v28) background Codex outranks compound mission suppression =====")

reset_state()
tg24 = RecTG()
mod._run_deferred(SDIR, tg24, mod.on_prompt(
    SDIR, tg24, prompt_text="launch the audited Codex lane"))
_compound24 = {
    "command": (
        "printf '%s' 'Fix wave running - two Codex lanes' > " + MPATH18 + "\n"
        "/opt/homebrew/bin/codex exec -m gpt-5.6-sol "
        "-c model_reasoning_effort=max --sandbox read-only "
        "--skip-git-repo-check --color never "
        "-o /private/tmp/missioncodex-report.md 'audit the streamer'"
    ),
    "description": "Launch background Codex lane missioncodex",
    "run_in_background": True,
}
run_step(tg24, "pre", "Bash", _compound24, tuid="CXBG24")
_compound_steps24 = load().get("steps") or []
check("24a a compound mission write plus background Codex launch creates one open lane",
      len(_compound_steps24) == 1
      and _compound_steps24[0].get("codex_bg") is True
      and _compound_steps24[0].get("done") is False
      and _compound_steps24[0].get("tuid") == "CXBG24",
      "steps=%r" % _compound_steps24)

reset_state()
tg24b = RecTG()
mod._run_deferred(SDIR, tg24b, mod.on_prompt(
    SDIR, tg24b, prompt_text="write only the mission"))
_mission_only24 = {
    "command": "printf '%s' 'Writing the mission only' > " + MPATH18,
    "run_in_background": True,
}
run_step(tg24b, "pre", "Bash", _mission_only24, tuid="MISSION24")
check("24b a mission-only command stays hidden even with run_in_background true",
      (load().get("steps") or []) == [],
      "steps=%r" % (load().get("steps") or []))
reset_state()

print("\n===== #25 (v29) terminal Codex notifications close exact lanes =====")

def _terminal_notification25(tuid, status, task_id="task25"):
    tool_tag = ("<tool-use-id>%s</tool-use-id>\n" % tuid) if tuid else ""
    return (
        "<task-notification>\n"
        "<task-id>%s</task-id>\n"
        "%s"
        "<status>%s</status>\n"
        "<summary>No completion record was found for the recovered task</summary>\n"
        "</task-notification>"
    ) % (task_id, tool_tag, status)

def _lane25(tuid):
    return next((s for s in (load().get("steps") or [])
                 if s.get("tuid") == tuid), {})

def _open_single_lane25(tuid, slug):
    reset_state()
    tg = RecTG()
    mod._run_deferred(SDIR, tg, mod.on_prompt(
        SDIR, tg, prompt_text="open terminal matrix lane"))
    command = _bg_cmd22(slug)
    run_step(tg, "pre", "Bash", command, tuid=tuid)
    run_step(tg, "post", "Bash", command, tuid=tuid)
    return tg

_B25 = "toolu_fixture000000000000000007"
_C25 = "toolu_fixture000000000000000008"
reset_state()
tg25 = RecTG()
mod._run_deferred(SDIR, tg25, mod.on_prompt(
    SDIR, tg25, prompt_text="run and retry the audited Codex lane"))
_same_caption25 = _bg_cmd22("brief-O")
run_step(tg25, "pre", "Bash", _same_caption25, tuid=_B25)
run_step(tg25, "post", "Bash", _same_caption25, tuid=_B25)

_ops25a = mod.on_prompt(SDIR, tg25, prompt_text=_terminal_notification25(
    _B25, "stopped", task_id="bydpcwhrc"))
mod._run_deferred(SDIR, tg25, _ops25a)
_b25a = _lane25(_B25)

check("25a stopped closes B with frozen seconds, stored outcome and honest caption",
      _b25a.get("done") is True
      and isinstance(_b25a.get("secs"), (int, float))
      and not isinstance(_b25a.get("secs"), bool)
      and _b25a.get("codex_terminal_status") == "stopped"
      and _b25a.get("cap") == "GPT-5.6 Sol · brief-O · stopped",
      "B=%r" % _b25a)

_b_frozen25 = dict(_b25a)
run_step(tg25, "pre", "Bash", _same_caption25, tuid=_C25)
run_step(tg25, "post", "Bash", _same_caption25, tuid=_C25)
_ops25b = mod.on_prompt(SDIR, tg25, prompt_text=_terminal_notification25(
    _C25, "completed", task_id="bh3phujjz"))
mod._run_deferred(SDIR, tg25, _ops25b)
_b25b = _lane25(_B25)
_c25b = _lane25(_C25)

check("25b same-caption C completes unsuffixed and leaves stopped B byte-for-byte alone",
      _c25b.get("done") is True
      and _c25b.get("codex_terminal_status") == "completed"
      and _c25b.get("cap") == "GPT-5.6 Sol · brief-O"
      and _b25b == _b_frozen25,
      "B-before=%r B-after=%r C=%r" % (_b_frozen25, _b25b, _c25b))

reset_state()
tg25c = RecTG()
mod._run_deferred(SDIR, tg25c, mod.on_prompt(
    SDIR, tg25c, prompt_text="open failed and killed lanes"))
for _tuid25, _slug25 in (("FAIL25", "failed-lane"), ("KILL25", "killed-lane")):
    _command25 = _bg_cmd22(_slug25)
    run_step(tg25c, "pre", "Bash", _command25, tuid=_tuid25)
    run_step(tg25c, "post", "Bash", _command25, tuid=_tuid25)
_matrix_ops25 = mod.on_prompt(
    SDIR, tg25c,
    prompt_text=(
        _terminal_notification25("FAIL25", "FAILED", task_id="failed25") + "\n" +
        _terminal_notification25("KILL25", "killed", task_id="killed25")))
mod._run_deferred(SDIR, tg25c, _matrix_ops25)
_failed25 = _lane25("FAIL25")
_killed25 = _lane25("KILL25")

check("25c failed closes with a normalized failed outcome and suffix",
      _failed25.get("done") is True
      and _failed25.get("codex_terminal_status") == "failed"
      and _failed25.get("cap") == "GPT-5.6 Sol · failed-lane · failed",
      "step=%r" % _failed25)
check("25c killed closes with a killed outcome and suffix",
      _killed25.get("done") is True
      and _killed25.get("codex_terminal_status") == "killed"
      and _killed25.get("cap") == "GPT-5.6 Sol · killed-lane · killed",
      "step=%r" % _killed25)

tg25long = _open_single_lane25("LONG25", "long-" + ("x" * 120))
_long_ops25 = mod.on_prompt(SDIR, tg25long, prompt_text=_terminal_notification25(
    "LONG25", "failed", task_id="long25"))
mod._run_deferred(SDIR, tg25long, _long_ops25)
_long25 = _lane25("LONG25")
check("25c a shortened caption keeps the complete terminal suffix within 80 characters",
      _long25.get("done") is True
      and len(_long25.get("cap") or "") <= 80
      and (_long25.get("cap") or "").endswith(" · failed"),
      "cap=%r len=%d" % (
          _long25.get("cap"), len(_long25.get("cap") or "")))

tg25paused = _open_single_lane25("PAUSE25", "paused-lane")
_paused_ops25 = mod.on_prompt(SDIR, tg25paused, prompt_text=_terminal_notification25(
    "PAUSE25", "paused", task_id="paused25"))
mod._run_deferred(SDIR, tg25paused, _paused_ops25)
_paused25 = _lane25("PAUSE25")
check("25c unknown paused status leaves the matching lane open",
      _paused25.get("done") is False
      and "codex_terminal_status" not in _paused25,
      "step=%r" % _paused25)

tg25wrong = _open_single_lane25("RIGHT25", "wrong-id-lane")
_wrong_ops25 = mod.on_prompt(SDIR, tg25wrong, prompt_text=_terminal_notification25(
    "WRONG25", "completed", task_id="wrong25"))
mod._run_deferred(SDIR, tg25wrong, _wrong_ops25)
_wrong25 = _lane25("RIGHT25")
check("25c terminal status with the wrong tool-use id leaves the lane open",
      _wrong25.get("done") is False
      and "codex_terminal_status" not in _wrong25,
      "step=%r" % _wrong25)

tg25missing = _open_single_lane25("MISSING25", "missing-id-lane")
_missing_ops25 = mod.on_prompt(SDIR, tg25missing, prompt_text=_terminal_notification25(
    None, "stopped", task_id="missing25"))
mod._run_deferred(SDIR, tg25missing, _missing_ops25)
_missing25 = _lane25("MISSING25")
check("25c terminal status without a tool-use id leaves the lane open",
      _missing25.get("done") is False
      and "codex_terminal_status" not in _missing25,
      "step=%r" % _missing25)

tg25dup = _open_single_lane25("DUP25", "duplicate-lane")
_dup_prompt25 = _terminal_notification25("DUP25", "completed", task_id="dup25")
_dup_close25 = mod.on_prompt(SDIR, tg25dup, prompt_text=_dup_prompt25)
mod._run_deferred(SDIR, tg25dup, _dup_close25)
_dup_before25 = load()
_dup_calls25 = list(tg25dup.calls)
_dup_again25 = mod.on_prompt(SDIR, tg25dup, prompt_text=_dup_prompt25)
_dup_after25 = load()
check("25c duplicate terminal envelope for an already-closed lane is a no-op",
      _dup_again25 == []
      and _dup_after25 == _dup_before25
      and tg25dup.calls == _dup_calls25,
      "ops=%r before=%r after=%r" % (
          _dup_again25, _dup_before25, _dup_after25))

reset_state()
tg25non = RecTG()
mod._run_deferred(SDIR, tg25non, mod.on_prompt(
    SDIR, tg25non, prompt_text="open a normal non-Codex row"))
run_step(tg25non, "pre", "Read", {"file_path": "/tmp/non-codex-25.md"},
         tuid="NONCODEX25")
_non_before25 = load()
_non_result25 = mod._close_codex_notification(
    SDIR, _non_before25, _terminal_notification25(
        "NONCODEX25", "completed", task_id="noncodex25"))
_non_after25 = load()
_non_step25 = next((s for s in (_non_after25.get("steps") or [])
                    if s.get("tuid") == "NONCODEX25"), {})
check("25c a terminal notification never closes a non-codex_bg row",
      _non_result25 is None
      and _non_after25 == _non_before25
      and _non_step25.get("done") is False
      and "codex_terminal_status" not in _non_step25,
      "result=%r step=%r" % (_non_result25, _non_step25))
reset_state()

print("\n===== #26 (v30) rich streaming =====")
check("26a v30 exposes the rich-stream feature gate",
      callable(getattr(mod, "rich_enabled", None)))

_MISSING26 = object()
_BOLD26 = "*" * 2
_FENCE26 = "`" * 3
_NETWORK26 = []
_REAL_URLOPEN26 = mod.request.urlopen

def _deny_network26(*args, **kwargs):
    _NETWORK26.append((args, kwargs))
    raise AssertionError("v30 test attempted network access")

def _call26(name, *args, **kwargs):
    fn = getattr(mod, name, None)
    if not callable(fn):
        return _MISSING26, "missing %s" % name
    try:
        return fn(*args, **kwargs), ""
    except Exception as exc:
        return _MISSING26, "%s: %s" % (type(exc).__name__, str(exc)[:180])

def _text26(value):
    if isinstance(value, tuple):
        value = value[0] if value else ""
    return value if isinstance(value, str) else ""

def _identity_text26(value):
    if isinstance(value, dict):
        return " ".join(str(value.get(k) or "") for k in ("name", "emoji", "icon"))
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value) if value is not _MISSING26 else ""

def _row_id26(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("id") or row.get("task_id") or row.get("taskId") or "")

def _row_subject26(row):
    if not isinstance(row, dict):
        return ""
    return str(row.get("subject") or row.get("title") or "")

def _row_status26(row):
    return str(row.get("status") or "") if isinstance(row, dict) else ""

def _row26(plan, task_id):
    return next((r for r in (plan or []) if _row_id26(r) == str(task_id)), {})

def _state_plan26(st):
    if not isinstance(st, dict):
        return []
    plan = st.get("plan")
    if not isinstance(plan, list):
        plan = st.get("rich_plan")
    return plan if isinstance(plan, list) else []

def _state_is_rich26(st):
    return isinstance(st, dict) and (
        st.get("msg_transport") == "rich" or st.get("rich_box") is True)

def _plan_call26(plan, tool, tinput, tresponse, agent_id=None):
    value, err = _call26("_update_rich_plan", plan, tool, tinput, tresponse,
                         agent_id=agent_id)
    return (value if isinstance(value, list) else []), err

def _render_live26(sdir, steps, start, agents, mission="", panel=None,
                   plan=None, tools=None, cadence=1.5, fallback=False,
                   state=None):
    return _call26(
        "render_rich_live", sdir, steps, start, agents, mission=mission,
        panel=panel, plan=plan, tools=tools, cadence=cadence,
        fallback=fallback, state=state)

def _render_final26(sdir, steps, start, n_agents=0, mission="", panel=None,
                    plan=None, cadence=1.5, outcome="done", tools=None):
    return _call26(
        "render_rich_final", sdir, steps, start, n_agents=n_agents,
        mission=mission, panel=panel, plan=plan, cadence=cadence,
        outcome=outcome, tools=tools)

class RichRecTG(RecTG):
    def __init__(self, rich_results=None, fail_send=False):
        super().__init__(fail_send=fail_send)
        self.rich_results = list(rich_results or [])

    def _result(self, default_mid=None):
        if self.rich_results:
            result = dict(self.rich_results.pop(0))
        else:
            result = {"ok": True}
        if result.get("ok") and default_mid is not None:
            result.setdefault("message_id", str(default_mid))
        return result

    def send_rich(self, markdown):
        _MID_SEQ[0] += 1
        mid = str(_MID_SEQ[0])
        result = self._result(default_mid=mid)
        self.calls.append(("send_rich", result.get("message_id"), markdown, dict(result)))
        return result

    def edit_rich(self, msgid, markdown):
        result = self._result()
        self.calls.append(("edit_rich", str(msgid), markdown, dict(result)))
        return result

class WireTG(mod.Telegram):
    def __init__(self, responses=None):
        super().__init__("TOKEN26", "999", False)
        self.wire = []
        self.responses = list(responses or [])

    def _call(self, method, params, _retry=True, _meta=None):
        del _retry
        self.wire.append((method, json.loads(json.dumps(params))))
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True, "result": {"message_id": 2601}}

def _prompt26(tg, text, session_id=None):
    fn = mod.on_prompt
    names = getattr(getattr(fn, "__code__", None), "co_varnames", ())
    try:
        if "session_id" in names:
            deferred = fn(SDIR, tg, prompt_text=text, session_id=session_id)
        else:
            deferred = fn(SDIR, tg, prompt_text=text)
        mod._run_deferred(SDIR, tg, deferred)
        return deferred, ""
    except Exception as exc:
        return [], "%s: %s" % (type(exc).__name__, str(exc)[:180])

def _step26(tg, mode, tool, tinput=None, tuid=None, tresponse=None,
            session_id=None, agent_id=None, agent_type=None):
    fn = mod.handle_step
    names = getattr(getattr(fn, "__code__", None), "co_varnames", ())
    kwargs = {}
    if "tresponse" in names:
        kwargs["tresponse"] = tresponse
    if "session_id" in names:
        kwargs["session_id"] = session_id
    try:
        deferred = fn(SDIR, tg, mode, tool, tinput or {}, tuid,
                      agent_id, agent_type, **kwargs)
        mod._run_deferred(SDIR, tg, deferred)
        return deferred, ""
    except Exception as exc:
        return [], "%s: %s" % (type(exc).__name__, str(exc)[:180])

def _reset26():
    reset_state()
    for base in (STATE_DIR, SDIR):
        for fn in ("rich-stream", "stream.off", "rich-health.log", "rich-health.jsonl"):
            try:
                os.remove(os.path.join(base, fn))
            except OSError:
                pass

def _rich_ops26(tg, kind):
    return [c for c in tg.calls if c[0] == kind]

def _detail26(value):
    return repr(value).replace(_BOLD26, "<bold>").replace(
        _FENCE26, "<code-fence>")[:800]

def _rich_content26(payload):
    if not isinstance(payload, dict):
        return {}
    content = payload.get("rich_message")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return {}
    return content if isinstance(content, dict) else {}

mod.request.urlopen = _deny_network26

_required26 = (
    "rich_enabled", "bot_identity", "sanitize_rich", "rich_bar",
    "_update_rich_plan", "render_rich_live", "render_rich_final",
    "_effective_min_edit", "_effective_anim_interval",
)
for _api26 in _required26[1:]:
    check("26a v30 exposes %s" % _api26,
          callable(getattr(mod, _api26, None)))
check("26a Telegram exposes send_rich and edit_rich",
      callable(getattr(mod.Telegram, "send_rich", None))
      and callable(getattr(mod.Telegram, "edit_rich", None)))

_gate_root26 = os.path.join(STATE_DIR, "gate26")
_gate_sdir26 = os.path.join(_gate_root26, "stream")
os.makedirs(_gate_sdir26, exist_ok=True)
_gate_off26, _gate_off_err26 = _call26("rich_enabled", _gate_sdir26)
check("26a no flag keeps the stream plain",
      not _gate_off_err26 and _gate_off26 is False,
      _gate_off_err26)
with open(os.path.join(_gate_sdir26, "rich-stream"), "w"):
    pass
_gate_wrong26, _gate_wrong_err26 = _call26("rich_enabled", _gate_sdir26)
check("26a a nested stream/rich-stream flag is ignored",
      not _gate_wrong_err26 and _gate_wrong26 is False,
      _gate_wrong_err26)
os.remove(os.path.join(_gate_sdir26, "rich-stream"))
with open(os.path.join(_gate_root26, "rich-stream"), "w"):
    pass
_gate_on26, _gate_on_err26 = _call26("rich_enabled", _gate_sdir26)
check("26a only the parent-state rich-stream flag enables rich mode",
      not _gate_on_err26 and _gate_on26 is True,
      _gate_on_err26)

_bots26 = (
    ("telegram", "Atlas", "💻"),
    ("telegram2", "Beacon", "🧿"),
    ("telegram3", "Cedar", "🌵"),
    ("telegram4", "Delta", "🔸"),
    ("telegram5", "Echo", "🫟"),
    ("telegram6", "Foxtrot", "🪩"),
)
_write_fleet_config([
    {"key": dirname, "name": name, "emoji": emoji,
     "state_dir": os.path.join(STATE_DIR, "identities26", dirname)}
    for dirname, name, emoji in _bots26
])
for _dirname26, _name26, _emoji26 in _bots26:
    _bot_sdir26 = os.path.join(STATE_DIR, "identities26", _dirname26, "stream")
    os.makedirs(_bot_sdir26, exist_ok=True)
    _identity26, _identity_err26 = _call26("bot_identity", _bot_sdir26)
    _identity_text_value26 = _identity_text26(_identity26)
    check("26a %s identity is exact" % _name26,
          not _identity_err26
          and _name26 in _identity_text_value26
          and _emoji26 in _identity_text_value26,
          _identity_err26 or _detail26(_identity_text_value26))
_unknown_identity26, _unknown_identity_err26 = _call26(
    "bot_identity", os.path.join(STATE_DIR, "identities26", "unknown", "stream"))
_unknown_text26 = _identity_text26(_unknown_identity26)
check("26a an unknown state directory gets a neutral nonempty identity",
      not _unknown_identity_err26 and bool(_unknown_text26.strip())
      and not any(name in _unknown_text26 for _, name, _ in _bots26),
      _unknown_identity_err26 or _detail26(_unknown_text26))

_config_identity_path = os.path.join(STATE_DIR, "configured-bot", "stream")
_write_fleet_config([{"key": "new-name", "name": "  Example Bot  ", "emoji": " ✨ ",
                      "state_dir": os.path.dirname(_config_identity_path)}])
check("26a a new arbitrary bot key gets its configured name and emoji",
      mod.bot_identity(_config_identity_path) == ("Example Bot", "✨"))
_write_fleet_config([{"key": "new-name", "name": "", "emoji": None,
                      "state_dir": os.path.dirname(_config_identity_path)}])
check("26a missing display values use the neutral fallback",
      mod.bot_identity(_config_identity_path) == ("Claude", "🤖"))
_write_fleet_config([{"key": "tilde", "name": "Home Bot", "emoji": "🏠",
                      "state_dir": "~/fixture-state"}])
check("26a configured state paths expand the sandbox home",
      mod.bot_identity(os.path.join(os.environ["HOME"], "fixture-state", "stream"))
      == ("Home Bot", "🏠"))
with open(FLEET_CONFIG, "w") as _bad_config26:
    _bad_config26.write("{broken")
check("26a malformed fleet configuration fails to the neutral identity",
      mod.bot_identity(_config_identity_path) == ("Claude", "🤖"))
os.remove(FLEET_CONFIG)
check("26a absent fleet configuration fails to the neutral identity",
      mod.bot_identity(_config_identity_path) == ("Claude", "🤖"))
_write_fleet_config()

_bar_cases26 = (
    (0, 4, "▱▱▱▱▱▱▱▱"),
    (2, 4, "▰▰▰▰▱▱▱▱"),
    (4, 4, "▰▰▰▰▰▰▰▰"),
    (9, 4, "▰▰▰▰▰▰▰▰"),
    (0, 0, "▱▱▱▱▱▱▱▱"),
)
for _done26, _total26, _want_bar26 in _bar_cases26:
    _bar26, _bar_err26 = _call26("rich_bar", _done26, _total26)
    check("26b rich bar %d of %d is an exact eight-cell rail" % (_done26, _total26),
          not _bar_err26 and _bar26 == _want_bar26,
          _bar_err26 or _detail26(_bar26))

_hostile26 = (
    "Visible " + _BOLD26 + "bold" + _BOLD26
    + " <details open><summary>trap</summary><mark>paint</mark>"
    + "<tg-thinking>secret</tg-thinking></details> ==loud=="
)
_safe26, _safe_err26 = _call26("sanitize_rich", _hostile26)
check("26b sanitizer preserves visible words without rich control delimiters",
      not _safe_err26 and all(w in _safe26 for w in ("Visible", "bold", "trap", "paint"))
      and _BOLD26 not in _safe26,
      _safe_err26)
check("26b sanitizer blocks details, mark and thinking injection",
      not _safe_err26
      and not any(x in _safe26.lower() for x in (
          "<details", "</details", "<summary", "<mark", "<tg-thinking", "==")),
      _safe_err26)
_table26, _table_err26 = _call26(
    "sanitize_rich", "one\r\ntwo\t| three", table=True)
check("26b table sanitizer collapses line breaks, tabs and raw pipes",
      not _table_err26
      and not any(x in _table26 for x in ("\r", "\n", "\t", "|"))
      and all(w in _table26 for w in ("one", "two", "three")),
      _table_err26 or _detail26(_table26))
_cap26, _cap_err26 = _call26("sanitize_rich", "é" * 80, cap=24)
check("26b sanitizer applies a bounded caller cap without raising",
      not _cap_err26 and isinstance(_cap26, str) and len(_cap26) <= 24,
      _cap_err26 or _detail26(_cap26))
_obj26, _obj_err26 = _call26("sanitize_rich", {"safe": 26})
check("26b sanitizer safely normalizes non-string values",
      not _obj_err26 and isinstance(_obj26, str) and "safe" in _obj26,
      _obj_err26 or _detail26(_obj26))
_nul26, _nul_err26 = _call26("sanitize_rich", "left\x00right")
check("26b sanitizer removes NUL without dropping adjacent text",
      not _nul_err26 and "\x00" not in _nul26
      and "left" in _nul26 and "right" in _nul26,
      _nul_err26 or _detail26(_nul26))
_ticks26, _ticks_err26 = _call26(
    "sanitize_rich", "before `inline` and ```fenced``` after")
check("26b sanitizer neutralizes inline and fenced backticks but keeps their words",
      not _ticks_err26 and "`" not in _ticks26
      and all(word in _ticks26 for word in ("before", "inline", "fenced", "after")),
      _ticks_err26 or _detail26(_ticks26))
_pipe_case26, _pipe_case_err26 = _call26(
    "sanitize_rich", "left | middle || right", table=True)
check("26b sanitizer neutralizes single and repeated table pipes",
      not _pipe_case_err26 and "|" not in _pipe_case26
      and all(word in _pipe_case26 for word in ("left", "middle", "right")),
      _pipe_case_err26 or _detail26(_pipe_case26))
_details_case26, _details_case_err26 = _call26(
    "sanitize_rich", "<details open><summary>fake</summary>visible</details>")
check("26b sanitizer makes fake details markup inert while retaining visible text",
      not _details_case_err26
      and "<" not in _details_case26 and ">" not in _details_case26
      and "fake" in _details_case26 and "visible" in _details_case26,
      _details_case_err26 or _detail26(_details_case26))
_long_subject26 = "subject-" + ("q" * 492)
_long_subject_safe26, _long_subject_err26 = _call26(
    "sanitize_rich", _long_subject26, cap=180)
check("26b a 500-character subject is bounded cleanly at the plan-field cap",
      not _long_subject_err26 and len(_long_subject_safe26) <= 180
      and _long_subject_safe26.startswith("subject-")
      and _long_subject_safe26.endswith("…"),
      _long_subject_err26 or _detail26(_long_subject_safe26))
_emoji_safe26, _emoji_err26 = _call26(
    "sanitize_rich", "Status 🧿 🌵 🔸 🫟 🪩 complete")
check("26b sanitizer preserves meaningful fleet emoji and adjacent words",
      not _emoji_err26
      and all(token in _emoji_safe26 for token in (
          "Status", "🧿", "🌵", "🔸", "🫟", "🪩", "complete")),
      _emoji_err26 or _detail26(_emoji_safe26))
_bidi_controls26 = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_bidi_safe26, _bidi_err26 = _call26(
    "sanitize_rich", "alpha\u202eevil\u202c مرحبا")
check("26b sanitizer removes RTL and bidi controls without dropping readable text",
      not _bidi_err26
      and not any(ch in _bidi_safe26 for ch in _bidi_controls26)
      and "alpha" in _bidi_safe26 and "evil" in _bidi_safe26
      and "مرحبا" in _bidi_safe26,
      _bidi_err26 or _detail26(_bidi_safe26))

_plan26 = []
_pre_plan26, _pre_plan_err26 = _plan_call26(
    _plan26, "TaskCreate",
    {"subject": "Map transport", "description": "Read the transport"}, None)
check("26c TaskCreate without a successful response never fabricates an id",
      not _pre_plan_err26 and _pre_plan26 == [],
      _pre_plan_err26 or _detail26(_pre_plan26))

_create_input26 = {
    "subject": "Map transport",
    "description": "Read the complete transport path",
    "activeForm": "Mapping transport",
    "metadata": {"scope": "v30"},
}
_create_response26 = {"task": {"id": "task-26-a", "subject": "Map transport"}}
_plan26, _create_err26 = _plan_call26(
    [], "TaskCreate", _create_input26, _create_response26)
_created26 = _row26(_plan26, "task-26-a")
check("26c successful TaskCreate uses the response id and subject",
      not _create_err26 and _row_subject26(_created26) == "Map transport",
      _create_err26 or _detail26(_plan26))
check("26c a created task starts pending and retains activeForm",
      not _create_err26 and _row_status26(_created26) == "pending"
      and str(_created26.get("active_form") or _created26.get("activeForm") or "")
      == "Mapping transport",
      _create_err26 or _detail26(_created26))

_plan26, _create_b_err26 = _plan_call26(
    _plan26, "TaskCreate",
    {"subject": "Implement patch", "description": "Patch only the streamer",
     "activeForm": "Implementing patch"},
    {"task": {"id": "task-26-b", "subject": "Implement patch"}})
check("26c multiple creates preserve response-id insertion order",
      not _create_b_err26
      and [_row_id26(r) for r in _plan26] == ["task-26-a", "task-26-b"],
      _create_b_err26 or _detail26(_plan26))

_plan_before_failed_create26 = json.loads(json.dumps(_plan26))
_failed_create26, _failed_create_err26 = _plan_call26(
    _plan26, "TaskCreate", {"subject": "Must not appear"},
    {"success": False, "error": "denied"})
check("26c failed TaskCreate leaves the established plan unchanged",
      not _failed_create_err26 and _failed_create26 == _plan_before_failed_create26,
      _failed_create_err26 or _detail26(_failed_create26))

_plan26, _progress_err26 = _plan_call26(
    _plan26, "TaskUpdate", {"taskId": "task-26-a", "status": "in_progress"},
    {"success": True, "taskId": "task-26-a", "updatedFields": ["status"],
     "statusChange": {"from": "pending", "to": "in_progress"}})
check("26c successful in_progress update selects the exact task",
      not _progress_err26 and _row_status26(_row26(_plan26, "task-26-a")) == "in_progress"
      and _row_status26(_row26(_plan26, "task-26-b")) == "pending",
      _progress_err26 or _detail26(_plan26))

_plan26, _fields_err26 = _plan_call26(
    _plan26, "TaskUpdate",
    {"taskId": "task-26-a", "subject": "Map rich transport",
     "description": "Updated description", "activeForm": "Mapping rich transport"},
    {"success": True, "taskId": "task-26-a",
     "updatedFields": ["subject", "description", "activeForm"]})
_fields_row26 = _row26(_plan26, "task-26-a")
check("26c TaskUpdate mutates only supplied descriptive fields",
      not _fields_err26
      and _row_subject26(_fields_row26) == "Map rich transport"
      and str(_fields_row26.get("active_form") or _fields_row26.get("activeForm") or "")
      == "Mapping rich transport"
      and _row_status26(_fields_row26) == "in_progress",
      _fields_err26 or _detail26(_fields_row26))

_plan_before_block26 = json.loads(json.dumps(_plan26))
_blocked26, _blocked_err26 = _plan_call26(
    _plan26, "TaskUpdate", {"taskId": "task-26-b", "addBlockedBy": ["task-26-a"]},
    {"success": True, "taskId": "task-26-b", "updatedFields": ["blockedBy"]})
check("26c blocking-only update does not invent a status transition",
      not _blocked_err26
      and _row_status26(_row26(_blocked26, "task-26-b"))
      == _row_status26(_row26(_plan_before_block26, "task-26-b")),
      _blocked_err26 or _detail26(_blocked26))

_plan26, _complete_err26 = _plan_call26(
    _blocked26, "TaskUpdate", {"taskId": "task-26-a", "status": "completed"},
    {"success": True, "taskId": "task-26-a", "updatedFields": ["status"],
     "statusChange": {"from": "in_progress", "to": "completed"}})
check("26c completed update checks only the addressed task",
      not _complete_err26 and _row_status26(_row26(_plan26, "task-26-a")) == "completed"
      and _row_status26(_row26(_plan26, "task-26-b")) == "pending",
      _complete_err26 or _detail26(_plan26))

_plan26, _delete_err26 = _plan_call26(
    _plan26, "TaskUpdate", {"taskId": "task-26-b", "status": "deleted"},
    {"success": True, "taskId": "task-26-b", "updatedFields": ["status"],
     "statusChange": {"from": "pending", "to": "deleted"}})
check("26c deleted task is retained as an explicit cancelled row",
      not _delete_err26
      and _row_status26(_row26(_plan26, "task-26-b")) in ("deleted", "cancelled"),
      _delete_err26 or _detail26(_plan26))

_plan_before_bad_updates26 = json.loads(json.dumps(_plan26))
_unknown26, _unknown_err26 = _plan_call26(
    _plan26, "TaskUpdate", {"taskId": "unknown-26", "status": "completed"},
    {"success": True, "taskId": "unknown-26", "updatedFields": ["status"]})
check("26c an unknown TaskUpdate id cannot mutate another row",
      not _unknown_err26 and _unknown26 == _plan_before_bad_updates26,
      _unknown_err26 or _detail26(_unknown26))
_failed_update26, _failed_update_err26 = _plan_call26(
    _plan26, "TaskUpdate", {"taskId": "task-26-a", "status": "pending"},
    {"success": False, "taskId": "task-26-a", "error": "denied"})
check("26c a failed TaskUpdate cannot roll back a completed row",
      not _failed_update_err26 and _failed_update26 == _plan_before_bad_updates26,
      _failed_update_err26 or _detail26(_failed_update26))
_subagent_plan26, _subagent_plan_err26 = _plan_call26(
    _plan26, "TaskCreate", {"subject": "Subagent private task"},
    {"task": {"id": "subagent-26", "subject": "Subagent private task"}},
    agent_id="agent-26")
check("26c subagent task events never contaminate the parent plan",
      not _subagent_plan_err26 and _subagent_plan26 == _plan_before_bad_updates26,
      _subagent_plan_err26 or _detail26(_subagent_plan26))

_long_meta26 = "m" * 500
_bounded_plan26, _bounded_create_err26 = _plan_call26(
    [], "TaskCreate",
    {"subject": "Bound stored metadata", "description": _long_meta26,
     "activeForm": _long_meta26},
    {"task": {"id": "bounded-26", "subject": "Bound stored metadata"}})
_bounded_row26 = _row26(_bounded_plan26, "bounded-26")
check("26c TaskCreate bounds stored description and activeForm to 200 characters",
      not _bounded_create_err26
      and len(str(_bounded_row26.get("description") or "")) == 200
      and len(str(_bounded_row26.get("activeForm") or "")) == 200,
      _bounded_create_err26 or _detail26(_bounded_row26))
_bounded_update26, _bounded_update_err26 = _plan_call26(
    _bounded_plan26, "TaskUpdate",
    {"taskId": "bounded-26", "description": "d" * 500,
     "activeForm": "a" * 500},
    {"success": True, "taskId": "bounded-26",
     "updatedFields": ["description", "activeForm"]})
_bounded_update_row26 = _row26(_bounded_update26, "bounded-26")
check("26c TaskUpdate bounds confirmed description and activeForm updates",
      not _bounded_update_err26
      and len(str(_bounded_update_row26.get("description") or "")) == 200
      and len(str(_bounded_update_row26.get("activeForm") or "")) == 200,
      _bounded_update_err26 or _detail26(_bounded_update_row26))

_frame_steps26 = [
    {"fam": "files", "cap": "Read source", "tuid": "S26-1", "done": True,
     "k": "", "aid": ""},
    {"fam": "write", "cap": "Patch rich stream", "tuid": "S26-2", "done": False,
     "k": "", "aid": ""},
    {"fam": "run", "cap": "Verify suite", "tuid": "S26-3", "done": False,
     "k": "", "aid": ""},
]
_frame_plan26 = [
    {"id": "P26-1", "subject": "Read source", "status": "completed",
     "active_form": "Reading source"},
    {"id": "P26-2", "subject": "Patch rich stream", "status": "in_progress",
     "active_form": "Patching rich stream"},
    {"id": "P26-3", "subject": "Verify suite", "status": "pending",
     "active_form": "Verifying suite"},
]
_frame_panel26 = [
    {"tuid": "L26-1", "lab": "Explorer sweep", "typ": "explorer", "mdl": "sol",
     "t0": 970.0, "done": False, "secs": 0},
    {"tuid": "L26-2", "lab": "Worker patch", "typ": "worker", "mdl": "sol",
     "t0": 980.0, "done": True, "secs": 12.0},
]
_frame_tools26 = [
    {"cap": "Read tool record", "secs": 7.0},
    {"cap": "Write tool record", "secs": 9.0},
]
_real_time26 = mod.time.time
try:
    mod.time.time = lambda: 1065.0
    _live_value26, _live_err26 = _render_live26(
        SDIR, _frame_steps26, 1000.0, {}, mission="Ship rich streaming",
        panel=_frame_panel26, plan=_frame_plan26, tools=_frame_tools26, cadence=1.5)
    _live26 = _text26(_live_value26)
    mod.time.time = lambda: 1075.0
    _live_later_value26, _live_later_err26 = _render_live26(
        SDIR, _frame_steps26, 1000.0, {}, mission="Ship rich streaming",
        panel=_frame_panel26, plan=_frame_plan26, tools=_frame_tools26, cadence=1.5)
    _live_later26 = _text26(_live_later_value26)
    _fallback_value26, _fallback_err26 = _render_live26(
        SDIR, _frame_steps26, 1000.0, {}, mission="Ship rich streaming",
        panel=_frame_panel26, plan=_frame_plan26, tools=_frame_tools26,
        cadence=1.5, fallback=True)
    _fallback26 = _text26(_fallback_value26)
    _final_value26, _final_err26 = _render_final26(
        SDIR, _frame_steps26, 1000.0, mission="Ship rich streaming",
        panel=_frame_panel26, plan=_frame_plan26, cadence=1.5, outcome="done")
    _final26 = _text26(_final_value26)
finally:
    mod.time.time = _real_time26

check("26d live renderer returns a nonempty bounded string",
      not _live_err26 and bool(_live26) and len(_live26.encode("utf-8")) <= 32768,
      _live_err26)
_identity_live26, _identity_live_err26 = _call26("bot_identity", SDIR)
_identity_parts26 = [p for p in _identity_text26(_identity_live26).split() if p]
_top26 = _live26.split("\n\n", 1)[0]
check("26d live frame carries NO bot identity and no heading (v30.3)",
      not _live_err26 and not _identity_live_err26
      and "at the workbench" not in _live26
      and not any(line.lstrip().startswith("#") for line in _live26.splitlines()),
      _live_err26 or _identity_live_err26)
check("26d target emoji leads the top line, mission unbolded (v30.3)",
      not _live_err26 and _live26.startswith("🎯 ")
      and not _live26.startswith("🎯 " + _BOLD26)
      and "⚡" not in _live26
      and "Ship rich streaming" in _top26,
      _live_err26)

check("26d Plan and Tools are the live box's only dropdowns, in approved order",
      not _live_err26
      and "📋 Plan" in _live26
      and _live26.index("📋 Plan") < _live26.index("🔧 Tools")
      and "🤖 Agents" not in _live26
      and "| Agent |" not in _live26
      and "| Lane |" not in _live26,
      _live_err26)
check("26d an empty plan reports 0/0, never borrowed step counts (v30.3)",
      (lambda f: "📋 Plan · 0/0 done" in f and "nothing logged yet" in f)(
          mod.render_rich_live(SDIR, _frame_steps26, 1000.0, {},
                               mission="no tasks logged", plan=[],
                               tools=_frame_tools26, cadence=1.5, frame=0)),
      "")
check("26d v29 unpadded elapsed clock, never zero-padded (v30.3)",
      not _live_err26 and "1:05" in _top26 and "01:05" not in _live26,
      _live_err26)
check("26d top block is the v29 heartbeat, not a progress rail (v30.3)",
      not _live_err26
      and (_live26.count("▰") + _live26.count("▱")) == 0
      and (_top26.count(mod.BAR_FILL) + _top26.count(mod.BAR_EMPTY)) == mod.BAR_CELLS
      and "👾 Working · " in _top26
      and "1/3" not in _top26,
      _live_err26)
check("26d task tally lives in the plan dropdown summary (v30.3)",
      not _live_err26 and "Plan · 1/3 done" in _live26,
      _live_err26)
check("26d activity line names the LATEST in-flight step, no NOW pointer (v30.3)",
      not _live_err26 and "⏳ Verify suite" in _top26
      and "▸" not in _top26,
      _live_err26)
check("26d idle turn swaps the activity line for animated thinking (v30.3)",
      mod._rich_activity([], 0).startswith("💭 thinking")
      and mod._rich_activity([], 1) != mod._rich_activity([], 2)
      and not mod._rich_activity(
          [{"cap": "Reading", "done": False, "k": "", "aid": ""}], 0
      ).startswith("💭"),
      "")
check("26d heartbeat slides with the frame and ignores task counts (v30.3)",
      mod.activity_bar(0) != mod.activity_bar(1)
      and mod.activity_bar(0) == mod.activity_bar(mod.BAR_CELLS),
      "")
check("26d mission is capped to one phone line so nothing shifts (v30.3)",
      len(mod.sanitize_rich("x" * 400, cap=mod.MISSION_CAP)) <= mod.MISSION_CAP,
      "")
check("26d top block lines join with the proven hard break (v30.3)",
      not _live_err26 and _top26.count(mod.RICH_BR) == 3
      and len(_top26.split("\n")) == 4
      and _top26.split("\n")[2].lstrip().startswith("🧠 "),
      _live_err26)
_plan_segment26 = ""
if "<details" in _live26 and "</details>" in _live26:
    _plan_segment26 = _live26.split("<details", 1)[1].split("</details>", 1)[0]
check("26d closed plan retains completed, active and pending rows in order",
      not _live_err26
      and all(x in _plan_segment26 for x in (
          "Read source", "Patch rich stream", "Verify suite"))
      and _plan_segment26.index("Read source")
      < _plan_segment26.index("Patch rich stream")
      < _plan_segment26.index("Verify suite")
      and "<details" in _live26 and "open" not in _live26.split("<details", 1)[1].split(">", 1)[0],
      _live_err26)
check("26d plan uses checked and unchecked task states",
      not _live_err26
      and any(x in _live26 for x in ("[x]", "☑", "✓"))
      and any(x in _live26 for x in ("[ ]", "☐", "○")),
      _live_err26)

check("26d the mid-box table retains dispatch order with honest states",
      not _live_err26 and "Explorer sweep" in _live26 and "Worker patch" in _live26
      and _live26.index("Explorer sweep") < _live26.index("Worker patch")
      and "| ◉ Explorer sweep (Sol) | 01:35 |" in _live26
      and "| ✓ Worker patch (Sol) | 00:12 |" in _live26
      and "🤖 Agents" not in _live26,
      _live_err26)
check("26d background agent launch does not stamp a launch-time duration (v30.3)",
      mod._is_async_agent_launch(
          {"result": "Async agent launched successfully. agentId: aXYZ"}) is True
      and mod._is_async_agent_launch({"result": "Rule 1. PASS"}) is False
      and mod._is_async_agent_launch(None) is False,
      "")
check("26d bash caption never renders a heredoc blob (v30.3)",
      (lambda c: "\n" not in c and len(c) <= 60 and "EOF" not in c)(
          mod.caption_of("Bash", {"command": "python3 - <<'EOF'\nimport re\nEOF"}, None))
      and mod.caption_of(
          "Bash", {"command": "x", "description": "Run the suite"}, None) == "Run the suite",
      "")
check("26d no <footer> element - one font size throughout (v30.3)",
      not _live_err26 and "<footer>" not in _live26,
      _live_err26)
check("26d active lane advances while completed lane duration stays frozen",
      not _live_err26 and not _live_later_err26
      and "| ✓ Worker patch (Sol) | 00:12 |" in _live26
      and "| ✓ Worker patch (Sol) | 00:12 |" in _live_later26
      and "| ◉ Explorer sweep (Sol) | 01:35 |" in _live26
      and "| ◉ Explorer sweep (Sol) | 01:45 |" in _live_later26,
      _live_err26 or _live_later_err26)
check("26d tools retain eligible completed calls in chronological order",
      not _live_err26
      and "Read tool record" in _live26 and "Write tool record" in _live26
      and _live26.index("Read tool record") < _live26.index("Write tool record"),
      _live_err26)
check("26d live frame excludes headings, highlighting, thinking and code fences",
      not _live_err26
      and not any(line.lstrip().startswith("#") for line in _live26.splitlines())
      and "==" not in _live26 and "<mark" not in _live26.lower()
      and "<tg-thinking" not in _live26.lower() and _FENCE26 not in _live26,
      _live_err26)
check("26d plain fallback strips rich syntax but preserves all information",
      not _fallback_err26
      and all(x in _fallback26 for x in (
          "Ship rich streaming", "Read source", "Patch rich stream", "Verify suite",
          "Explorer sweep", "Worker patch"))
      and _BOLD26 not in _fallback26 and "<details" not in _fallback26.lower()
      and "</details" not in _fallback26.lower() and _FENCE26 not in _fallback26,
      _fallback_err26)
check("26d final fold is nonempty and keeps mission, lanes and the full plan",
      not _final_err26 and bool(_final26)
      and all(x in _final26 for x in (
          "Ship rich streaming", "Explorer sweep", "Worker patch",
          "Read source", "Patch rich stream", "Verify suite")),
      _final_err26)
check("26d final fold is closed and contains no live-thinking syntax",
      not _final_err26 and "<details" in _final26
      and "open" not in _final26.split("<details", 1)[1].split(">", 1)[0]
      and "<tg-thinking" not in _final26.lower() and "==" not in _final26,
      _final_err26)

_wire26 = WireTG()
_wire_send26 = _MISSING26
_wire_send_err26 = ""
try:
    if callable(getattr(_wire26, "send_rich", None)):
        _wire_send26 = _wire26.send_rich(_live26)
    else:
        _wire_send_err26 = "missing send_rich"
except Exception as exc:
    _wire_send_err26 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
_wire_edit26 = _MISSING26
_wire_edit_err26 = ""
try:
    if callable(getattr(_wire26, "edit_rich", None)):
        _wire_edit26 = _wire26.edit_rich("2601", _final26)
    else:
        _wire_edit_err26 = "missing edit_rich"
except Exception as exc:
    _wire_edit_err26 = "%s: %s" % (type(exc).__name__, str(exc)[:180])

_wire_send_call26 = _wire26.wire[0] if len(_wire26.wire) > 0 else ("", {})
_wire_edit_call26 = _wire26.wire[1] if len(_wire26.wire) > 1 else ("", {})
_wire_send_payload26 = _wire_send_call26[1] if len(_wire_send_call26) > 1 else {}
_wire_edit_payload26 = _wire_edit_call26[1] if len(_wire_edit_call26) > 1 else {}
_wire_send_content26 = _rich_content26(_wire_send_payload26)
_wire_edit_content26 = _rich_content26(_wire_edit_payload26)
check("26e send_rich uses sendRichMessage",
      not _wire_send_err26 and _wire_send_call26[0] == "sendRichMessage",
      _wire_send_err26 or _detail26(_wire_send_call26))
check("26e send_rich is silent and emits markdown plus entity suppression",
      not _wire_send_err26
      and str(_wire_send_payload26.get("chat_id")) == "999"
      and _wire_send_payload26.get("disable_notification") == "true"
      and set(_wire_send_content26.keys())
      == {"markdown", "skip_entity_detection"}
      and _wire_send_content26["markdown"] == _live26
      and _wire_send_content26["skip_entity_detection"] is True
      and "text" not in _wire_send_payload26,
      _wire_send_err26 or _detail26(_wire_send_payload26))
check("26e send_rich returns the structured message id",
      not _wire_send_err26 and isinstance(_wire_send26, dict)
      and _wire_send26.get("ok") is True
      and str(_wire_send26.get("message_id")) == "2601",
      _wire_send_err26 or _detail26(_wire_send26))
check("26e edit_rich uses editMessageText on the same message id",
      not _wire_edit_err26 and _wire_edit_call26[0] == "editMessageText"
      and str(_wire_edit_payload26.get("message_id")) == "2601",
      _wire_edit_err26 or _detail26(_wire_edit_call26))
check("26e edit_rich cannot accidentally demote through a plain text field",
      not _wire_edit_err26
      and set(_wire_edit_content26.keys())
      == {"markdown", "skip_entity_detection"}
      and _wire_edit_content26["markdown"] == _final26
      and _wire_edit_content26["skip_entity_detection"] is True
      and "text" not in _wire_edit_payload26,
      _wire_edit_err26 or _detail26(_wire_edit_payload26))

class _Clock26:
    def __init__(self, now):
        self.now = float(now)

    def time(self):
        return self.now

    def advance(self, seconds=3.0):
        self.now += float(seconds)

def _enable_rich26():
    with open(os.path.join(STATE_DIR, "rich-stream"), "w"):
        pass

def _ok26(mid):
    return {"ok": True, "message_id": str(mid)}

def _fail26(error_class, code=None, description="fixture failure"):
    out = {"ok": False, "error_class": error_class, "description": description}
    if code is not None:
        out["error_code"] = code

    if error_class == "api_400":
        out["api_400_retried"] = True
    return out

def _health26():
    paths = []
    chunks = []
    for root, _, files in os.walk(STATE_DIR):
        for name in files:
            low = name.lower()
            if "rich" not in low or not ("health" in low or low.endswith(".log")
                                         or low.endswith(".jsonl")):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r") as f:
                    chunks.append(f.read())
                paths.append(path)
            except OSError:
                pass
    return paths, "\n".join(chunks)

_clock26 = _Clock26(26000.0)
_real_time_lifecycle26 = mod.time.time
try:
    mod.time.time = _clock26.time

    _reset26()
    _plain_tg26 = RichRecTG()
    _, _plain_prompt_err26 = _prompt26(
        _plain_tg26, "plain compatibility turn", session_id="plain-session-26")
    _plain_state26 = load()
    check("26f flag-off prompt creates only the legacy plain box",
          not _plain_prompt_err26
          and len(_plain_tg26.ops("send")) == 1
          and not _rich_ops26(_plain_tg26, "send_rich")
          and not _state_is_rich26(_plain_state26),
          _plain_prompt_err26 or _detail26(_plain_tg26.calls))

    _reset26()
    _enable_rich26()
    with open(os.path.join(STATE_DIR, "stream.off"), "w"):
        pass
    _off_proc26 = drive_main("prompt", {
        "session_id": "off-session-26", "prompt": "must stay fully disabled"})
    check("26f stream.off overrides the rich-stream flag before any transport",
          _off_proc26.returncode == 0
          and _off_proc26.stdout == b"" and _off_proc26.stderr == b""
          and not os.path.exists(os.path.join(SDIR, "state.json")),
          "rc=%d stdout=%r stderr=%r" % (
              _off_proc26.returncode, _off_proc26.stdout, _off_proc26.stderr))

    _reset26()
    _enable_rich26()
    _rich_tg26 = RichRecTG([_ok26("26101"), _ok26("26101"), _ok26("26101")])
    _, _rich_prompt_err26 = _prompt26(
        _rich_tg26, "rich lifecycle turn", session_id="rich-session-26")
    _rich_open_state26 = load()
    check("26f enabled prompt creates one rich box and persists its transport",
          not _rich_prompt_err26
          and len(_rich_ops26(_rich_tg26, "send_rich")) == 1
          and not _rich_tg26.ops("send")
          and str(_rich_open_state26.get("msgid")) == "26101"
          and _state_is_rich26(_rich_open_state26),
          _rich_prompt_err26 or _detail26(_rich_tg26.calls))
    _clock26.advance()
    _, _rich_step_err26 = _step26(
        _rich_tg26, "pre", "Read", {"file_path": "/tmp/v30-rich.txt"},
        tuid="RICH26", session_id="rich-session-26")
    _first_rich_edits26 = _rich_ops26(_rich_tg26, "edit_rich")
    check("26f active rich work edits the same rich message id",
          not _rich_step_err26 and len(_first_rich_edits26) == 1
          and _first_rich_edits26[0][1] == "26101"
          and not _rich_tg26.ops("edit"),
          _rich_step_err26 or _detail26(_rich_tg26.calls))
    os.remove(os.path.join(STATE_DIR, "rich-stream"))
    _clock26.advance()
    _, _rich_flag_removed_err26 = _step26(
        _rich_tg26, "post", "Read", {"file_path": "/tmp/v30-rich.txt"},
        tuid="RICH26", session_id="rich-session-26",
        tresponse={"ok": True})
    check("26f removing the flag mid-turn cannot plain-edit a rich-created id",
          not _rich_flag_removed_err26
          and len(_rich_ops26(_rich_tg26, "edit_rich")) >= 2
          and not _rich_tg26.ops("edit")
          and _state_is_rich26(load()),
          _rich_flag_removed_err26 or _detail26(_rich_tg26.calls))

    _reset26()
    _plain_then_flag_tg26 = RichRecTG()
    _, _plain_then_flag_prompt_err26 = _prompt26(
        _plain_then_flag_tg26, "plain then flag", session_id="plain-then-flag-26")
    _plain_mid26 = str(load().get("msgid"))
    _enable_rich26()
    _clock26.advance()
    _, _plain_then_flag_step_err26 = _step26(
        _plain_then_flag_tg26, "pre", "Read", {"file_path": "/tmp/plain-26.txt"},
        tuid="PLAIN26", session_id="plain-then-flag-26")
    check("26f adding the flag mid-turn cannot rich-edit a plain-created id",
          not _plain_then_flag_prompt_err26 and not _plain_then_flag_step_err26
          and bool(_plain_mid26) and not _rich_ops26(_plain_then_flag_tg26, "edit_rich")
          and not _state_is_rich26(load()),
          _plain_then_flag_prompt_err26 or _plain_then_flag_step_err26
          or _detail26(_plain_then_flag_tg26.calls))

    _reset26()
    _enable_rich26()
    _secret26 = "SECRET_PAYLOAD_26"
    _terminal_tg26 = RichRecTG([
        _ok26("26201"),
        _fail26("api_400", 400,
                "bad request TOKEN26 %s %s" % (_secret26, "x" * 800)),
    ])
    _, _terminal_prompt_err26 = _prompt26(
        _terminal_tg26, _secret26, session_id="terminal-session-26")
    _clock26.advance()
    _, _terminal_step_err26 = _step26(
        _terminal_tg26, "pre", "Read", {"file_path": "/tmp/terminal-26.txt"},
        tuid="TERM26", session_id="terminal-session-26")
    _terminal_state26 = load()
    check("26f api_400 on an active rich edit cold-starts exactly one plain box",
          not _terminal_prompt_err26 and not _terminal_step_err26
          and len(_rich_ops26(_terminal_tg26, "send_rich")) == 1
          and len(_rich_ops26(_terminal_tg26, "edit_rich")) == 1
          and len(_terminal_tg26.ops("send")) == 1
          and not _terminal_tg26.ops("edit"),
          _terminal_prompt_err26 or _terminal_step_err26
          or _detail26(_terminal_tg26.calls))
    check("26f terminal downgrade repoints state and prevents later rich retries",
          _terminal_state26.get("rich_failed") is True
          and not _state_is_rich26(_terminal_state26)
          and str(_terminal_state26.get("msgid"))
          == str(_terminal_tg26.ops("send")[0][1] if _terminal_tg26.ops("send") else "")
          and not _terminal_state26.get("rich_pending_open"),
          _detail26(_terminal_state26))
    _health_paths26, _health_text_value26 = _health26()
    check("26f terminal downgrade writes one local rich health record",
          bool(_health_paths26) and "api_400" in _health_text_value26,
          "paths=%r" % _health_paths26)
    check("26f health record omits token, payload and unbounded descriptions",
          bool(_health_paths26)
          and "TOKEN26" not in _health_text_value26
          and _secret26 not in _health_text_value26
          and ("x" * 200) not in _health_text_value26
          and all(len(line) <= 512 for line in _health_text_value26.splitlines()),
          "paths=%r lengths=%r" % (
              _health_paths26, [len(line) for line in _health_text_value26.splitlines()]))
    _clock26.advance()
    _terminal_rich_calls_before26 = len(_rich_ops26(_terminal_tg26, "edit_rich"))
    _, _terminal_later_err26 = _step26(
        _terminal_tg26, "post", "Read", {"file_path": "/tmp/terminal-26.txt"},
        tuid="TERM26", tresponse={"ok": True}, session_id="terminal-session-26")
    check("26f a terminally downgraded turn stays plain for later events",
          _terminal_state26.get("rich_failed") is True
          and not _terminal_later_err26
          and len(_rich_ops26(_terminal_tg26, "edit_rich"))
          == _terminal_rich_calls_before26,
          _terminal_later_err26 or _detail26(_terminal_tg26.calls))

    _reset26()
    _enable_rich26()
    _rate_tg26 = RichRecTG([
        _ok26("26301"),
        _fail26("rate_limit", 429, "retry later"),
        _fail26("rate_limit", 429, "retry later again"),
        _fail26("rate_limit", 429, "still limited while slow"),
        _fail26("rate_limit", 429, "persistently limited while slow"),
    ])
    _prompt26(_rate_tg26, "rate-limit turn", session_id="rate-session-26")
    _clock26.advance()
    _, _rate_first_err26 = _step26(
        _rate_tg26, "pre", "Read", {"file_path": "/tmp/rate-26.txt"},
        tuid="RATE26", session_id="rate-session-26")
    _rate_first_state26 = load()
    check("26f first 429 skips the frame without plain demotion",
          not _rate_first_err26 and not _rate_tg26.ops("send")
          and _state_is_rich26(_rate_first_state26)
          and int(_rate_first_state26.get("rich_429_streak") or 0) == 1,
          _rate_first_err26 or _detail26(_rate_first_state26))
    _clock26.advance()
    _, _rate_second_err26 = _step26(
        _rate_tg26, "post", "Read", {"file_path": "/tmp/rate-26.txt"},
        tuid="RATE26", tresponse={"ok": True}, session_id="rate-session-26")
    _rate_second_state26 = load()
    check("26f second consecutive 429 slows to the rich_slow cadence "
          "without falling back",
          not _rate_second_err26 and not _rate_tg26.ops("send")
          and _state_is_rich26(_rate_second_state26)
          and _rate_second_state26.get("rich_slow") is True
          and _rate_second_state26.get("rich_failed") is not True,
          _rate_second_err26 or _detail26(_rate_second_state26))
    _rate_min26, _rate_min_err26 = _call26(
        "_effective_min_edit", SDIR, _rate_second_state26)
    _rate_anim26, _rate_anim_err26 = _call26(
        "_effective_anim_interval", SDIR, _rate_second_state26)
    check("26f slow 429 cadence controls both edits and animation",
          not _rate_min_err26 and not _rate_anim_err26
          and float(_rate_min26) == mod.RICH_SLOW_INTERVAL
          and float(_rate_anim26) == mod.RICH_SLOW_INTERVAL,
          _rate_min_err26 or _rate_anim_err26)

    _clock26.advance(mod.RICH_SLOW_INTERVAL)
    _, _rate_third_err26 = _step26(
        _rate_tg26, "pre", "Write", {"file_path": "/tmp/rate-3-26.txt"},
        tuid="RATE-3-26", session_id="rate-session-26")
    _rate_third_state26 = load()
    check("26f first slow-cadence 429 still skips without fallback",
          not _rate_third_err26 and not _rate_tg26.ops("send")
          and _state_is_rich26(_rate_third_state26)
          and _rate_third_state26.get("rich_slow") is True
          and _rate_third_state26.get("rich_failed") is not True,
          _rate_third_err26 or _detail26(_rate_third_state26))
    _clock26.advance(mod.RICH_SLOW_INTERVAL)
    _, _rate_fourth_err26 = _step26(
        _rate_tg26, "pre", "Bash", {"command": "true"},
        tuid="RATE-4-26", session_id="rate-session-26")
    _rate_fourth_state26 = load()
    check("26f continued slow-cadence 429 keeps the box rich",
          not _rate_fourth_err26 and not _rate_tg26.ops("send")
          and _state_is_rich26(_rate_fourth_state26)
          and _rate_fourth_state26.get("rich_slow") is True
          and _rate_fourth_state26.get("rich_failed") is not True
          and int(_rate_fourth_state26.get("rich_429_streak") or 0) == 4,
          _rate_fourth_err26 or _detail26(_rate_fourth_state26))

    _reset26()
    _enable_rich26()
    _rate_reset_tg26 = RichRecTG([
        _ok26("26401"), _fail26("rate_limit", 429), _ok26("26401")])
    _prompt26(_rate_reset_tg26, "rate reset turn", session_id="rate-reset-26")
    _clock26.advance()
    _step26(_rate_reset_tg26, "pre", "Read", {"file_path": "/tmp/a-26.txt"},
            tuid="RRESET-A26", session_id="rate-reset-26")
    _clock26.advance()
    _, _rate_reset_err26 = _step26(
        _rate_reset_tg26, "pre", "Write", {"file_path": "/tmp/b-26.txt"},
        tuid="RRESET-B26", session_id="rate-reset-26")
    _rate_reset_state26 = load()
    check("26f a successful rich edit resets the consecutive 429 streak",
          not _rate_reset_err26
          and int(_rate_reset_state26.get("rich_429_streak") or 0) == 0
          and _state_is_rich26(_rate_reset_state26)
          and not _rate_reset_tg26.ops("send"),
          _rate_reset_err26 or _detail26(_rate_reset_state26))

    _reset26()
    _enable_rich26()
    _transient_tg26 = RichRecTG([
        _ok26("26501"), _fail26("timeout"),
        _fail26("connection_reset"), _fail26("network")])
    _prompt26(_transient_tg26, "transient turn", session_id="transient-26")
    _clock26.advance()
    _step26(_transient_tg26, "pre", "Read", {"file_path": "/tmp/t1-26.txt"},
            tuid="TRANSIENT-1-26", session_id="transient-26")
    _clock26.advance()
    _, _transient_second_err26 = _step26(
        _transient_tg26, "pre", "Write", {"file_path": "/tmp/t2-26.txt"},
        tuid="TRANSIENT-2-26", session_id="transient-26")
    _transient_second_state26 = load()
    check("26f first two transient rich failures skip without demotion",
          not _transient_second_err26 and not _transient_tg26.ops("send")
          and _state_is_rich26(_transient_second_state26)
          and int(_transient_second_state26.get("rich_transient_streak") or 0) == 2,
          _transient_second_err26 or _detail26(_transient_second_state26))
    _clock26.advance()
    _, _transient_third_err26 = _step26(
        _transient_tg26, "pre", "Bash", {"command": "true"},
        tuid="TRANSIENT-3-26", session_id="transient-26")
    _transient_third_state26 = load()
    check("26f third consecutive transient failure keeps the box rich and slows",
          not _transient_third_err26 and not _transient_tg26.ops("send")
          and _state_is_rich26(_transient_third_state26)
          and _transient_third_state26.get("rich_slow") is True
          and _transient_third_state26.get("rich_failed") is not True
          and int(_transient_third_state26.get("rich_transient_streak") or 0) == 3,
          _transient_third_err26 or _detail26(_transient_third_state26))

    _reset26()
    _enable_rich26()
    _stop_tg26 = RichRecTG([
        _ok26("26601"), _ok26("26601"), _fail26("api_400", 400),
        _fail26("api_400", 400)])
    _prompt26(_stop_tg26, "answer position turn", session_id="stop-session-26")
    _clock26.advance()
    _step26(_stop_tg26, "pre", "Read", {"file_path": "/tmp/stop-26.txt"},
            tuid="STOP26", session_id="stop-session-26")
    _clock26.advance()
    try:
        _stop_deferred26 = mod.record_reply(SDIR, _stop_tg26, final=True)
        mod._run_deferred(SDIR, _stop_tg26, _stop_deferred26)
        _stop_err26 = ""
    except Exception as exc:
        _stop_err26 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    check("26f terminal Stop-fold failure never cold-starts below the answer",
          not _stop_err26 and not _stop_tg26.ops("send"),
          _stop_err26 or _detail26(_stop_tg26.calls))

finally:
    mod.time.time = _real_time_lifecycle26

_reset26()
_enable_rich26()
_session_prompt_a26 = {
    "session_id": "session-A-26", "prompt_id": "prompt-A1-26",
    "prompt": "build the rich streamer",
}
_session_create26 = {
    "session_id": "session-A-26", "prompt_id": "prompt-A1-26",
    "tool_name": "TaskCreate", "tool_use_id": "TC26",
    "tool_input": {
        "subject": "Persist session plan", "description": "Carry it to the next prompt",
        "activeForm": "Persisting session plan", "metadata": {"owner": "parent"},
    },
    "tool_response": {
        "task": {"id": "session-task-26", "subject": "Persist session plan"},
    },
}
_session_update26 = {
    "session_id": "session-A-26", "prompt_id": "prompt-A1-26",
    "tool_name": "TaskUpdate", "tool_use_id": "TU26",
    "tool_input": {"taskId": "session-task-26", "status": "completed"},
    "tool_response": {
        "success": True, "taskId": "session-task-26", "updatedFields": ["status"],
        "statusChange": {"from": "pending", "to": "completed"},
    },
}
_proc_prompt_a26 = drive_main("prompt", _session_prompt_a26)
_proc_create26 = drive_main("post", _session_create26)
_session_after_create26 = load()
check("26g main forwards the exact TaskCreate response into durable plan state",
      _proc_prompt_a26.returncode == 0 and _proc_create26.returncode == 0
      and _row_subject26(_row26(_state_plan26(_session_after_create26),
                                "session-task-26")) == "Persist session plan",
      _detail26({
          "state": _session_after_create26,
          "stderr": _proc_create26.stderr.decode(errors="replace")[-180:],
      }))
_proc_update26 = drive_main("post", _session_update26)
_session_after_update26 = load()
check("26g main forwards successful TaskUpdate statusChange to the exact row",
      _proc_update26.returncode == 0
      and _row_status26(_row26(_state_plan26(_session_after_update26),
                               "session-task-26")) == "completed",
      _detail26({
          "state": _session_after_update26,
          "stderr": _proc_update26.stderr.decode(errors="replace")[-180:],
      }))
_proc_same_session26 = drive_main("prompt", {
    "session_id": "session-A-26", "prompt_id": "prompt-A2-26",
    "prompt": "continue the same session",
})
_session_same26 = load()
check("26g a fully-finished plan clears at a same-session boundary (v30.4)",
      _proc_same_session26.returncode == 0
      and _state_plan26(_session_same26) == [],
      _detail26(_session_same26))
_proc_other_session26 = drive_main("prompt", {
    "session_id": "session-B-26", "prompt_id": "prompt-B1-26",
    "prompt": "start a different session",
})
_session_other26 = load()
check("26g a different session starts with an empty parent plan",
      _proc_other_session26.returncode == 0
      and _state_plan26(_session_other26) == [],
      _detail26(_session_other26))

check("26g every in-process rich fixture remained offline",
      _NETWORK26 == [], _detail26(_NETWORK26))
mod.request.urlopen = _REAL_URLOPEN26
_reset26()

print("\n===== #27 (v30) Opus review regressions =====")

_collapse_state27 = {
    "msgid": "rich-collapse-27",
    "steps": [{"fam": "files", "cap": "interrupted rich work", "tuid": "RC27",
               "done": True, "k": "", "aid": "", "secs": 4}],
    "proc_start": 27000.0,
    "agents": {}, "panel": [], "ghosts": [],
    "folded": False, "box_is_last": False, "replied_this_turn": False,
    "rich_turn": True, "rich_box": True, "rich_failed": False,
    "rich_slow": False, "rich_plan": [], "rich_tools": [],
}
_collapse_value27, _collapse_err27 = _call26(
    "_collapse_prior_ex", _collapse_state27,
    "Interrupted rich mission", sdir=SDIR)
if isinstance(_collapse_value27, tuple) and len(_collapse_value27) == 3:
    _collapse_ops27, _collapse_mid27, _collapse_text27 = _collapse_value27
else:
    _collapse_ops27, _collapse_mid27, _collapse_text27 = [], None, None
check("27a interrupted rich collapse emits one rich-fold operation",
      not _collapse_err27 and bool(_collapse_ops27)
      and _collapse_ops27[0][0] == "rfold"
      and str(_collapse_ops27[0][1]) == "rich-collapse-27",
      _collapse_err27 or _detail26(_collapse_ops27))
check("27a rich-fold collapse returns the exact recap survivor and text",
      not _collapse_err27 and str(_collapse_mid27) == "rich-collapse-27"
      and isinstance(_collapse_text27, str)
      and _collapse_text27 == (_collapse_ops27[0][2] if _collapse_ops27 else None)
      and "Interrupted rich mission" in _collapse_text27,
      _collapse_err27 or _detail26((_collapse_mid27, _collapse_text27)))

_next_steps27 = [{"fam": "write", "cap": "next no-reply work", "tuid": "NEXT27",
                  "done": True, "k": "", "aid": "", "secs": 3}]
_next_state27 = {
    "msgid": "next-box-27", "steps": _next_steps27, "proc_start": 27010.0,
    "agents": {}, "panel": [], "rich_plan": [],
    "prev_recap_msgid": _collapse_mid27, "prev_recap_text": _collapse_text27,
    "prev_recap_rich": True, "rich_failed": False,
}
_merge_value27, _merge_err27 = _call26(
    "_noreply_merge_ex", _next_state27, _next_steps27, "Next no-reply mission")
if isinstance(_merge_value27, tuple) and len(_merge_value27) == 3:
    _merge_mid27, _merge_text27, _merge_rich27 = _merge_value27
else:
    _merge_mid27, _merge_text27, _merge_rich27 = None, None, False
check("27a next no-reply turn can merge into the returned rich survivor",
      not _merge_err27 and str(_merge_mid27) == "rich-collapse-27"
      and _merge_rich27 is True and isinstance(_merge_text27, str)
      and "Next no-reply mission" in _merge_text27
      and "next no-reply work" in _merge_text27,
      _merge_err27 or _detail26((_merge_mid27, _merge_text27, _merge_rich27)))

import io as _io27

class _HTTPReply27:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()

class _URLQueue27:
    def __init__(self, events):
        self.events = list(events)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((getattr(req, "full_url", str(req)), timeout))
        if not self.events:
            raise AssertionError("unexpected HTTP attempt")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return _HTTPReply27(event)

def _http_429_27(retry_after):
    body = _io27.BytesIO(json.dumps({
        "ok": False, "error_code": 429,
        "parameters": {"retry_after": retry_after},
    }).encode())
    return mod.HTTPError("https://api.telegram.invalid", 429,
                         "Too Many Requests", {}, body)

_real_urlopen27 = mod.request.urlopen
_real_sleep27 = mod.time.sleep
_plain_queue27 = _URLQueue27([
    _http_429_27(9), {"ok": True, "result": {"message_id": 2701}},
])
_plain_sleeps27 = []
try:
    mod.request.urlopen = _plain_queue27
    mod.time.sleep = lambda seconds: _plain_sleeps27.append(seconds)
    _plain_wire_tg27 = mod.Telegram("TOKEN27", "999", False)
    _plain_mid27 = _plain_wire_tg27.send("plain retry compatibility")
    _plain_retry_err27 = ""
except Exception as exc:
    _plain_mid27 = None
    _plain_retry_err27 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
finally:
    mod.request.urlopen = _real_urlopen27
    mod.time.sleep = _real_sleep27
check("27b plain transport keeps the v29 two-second retry_after cap",
      not _plain_retry_err27 and str(_plain_mid27) == "2701"
      and _plain_sleeps27 == [2]
      and len(_plain_queue27.calls) == 2
      and all(call[1] == mod.HTTP_TIMEOUT for call in _plain_queue27.calls),
      _plain_retry_err27 or _detail26((_plain_sleeps27, _plain_queue27.calls)))

def _run_rich_retry27():
    if not callable(getattr(mod.Telegram, "edit_rich", None))\
            or not callable(getattr(mod, "_rich_deferred_edit", None)):
        return {}, [], [], "missing rich retry APIs"
    _reset26()
    state = {
        "msgid": "rich-rate-27", "steps": [
            {"fam": "files", "cap": "rate limited work", "tuid": "RATE27",
             "done": False, "k": "", "aid": "", "t0": 27000.0}],

        "proc_start": mod.time.time(), "last_edit": 27000.0, "frame": 1,
        "last_live": "prior rich frame", "agents": {}, "panel": [],
        "box_is_last": True, "folded": False, "ghosts": [],
        "rich_turn": True, "rich_box": True, "rich_failed": False,
        "rich_slow": False, "rich_429_streak": 0, "rich_transient_streak": 0,
        "rich_health_logged": False, "rich_plan": [], "rich_tools": [],
        "rich_tool_count": 0, "rich_orphans": [],
    }
    mod.save_state(SDIR, state)
    queue = _URLQueue27([
        _http_429_27(7), _http_429_27(7),
        {"ok": True, "result": {"message_id": 2799}},
    ])
    sleeps = []
    real_urlopen = mod.request.urlopen
    real_sleep = mod.time.sleep
    try:
        mod.request.urlopen = queue
        mod.time.sleep = lambda seconds: sleeps.append(seconds)
        tg = mod.Telegram("TOKEN27", "999", False)
        mod._rich_deferred_edit(
            SDIR, tg, "rich-rate-27", "changed rich frame", live_fallback=True)
        after = load()
        err = ""
    except Exception as exc:
        after = load()
        err = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    finally:
        mod.request.urlopen = real_urlopen
        mod.time.sleep = real_sleep
    return after, sleeps, queue.calls, err

_rich_retry_state27, _rich_sleeps27, _rich_http_calls27, _rich_retry_err27 =\
    _run_rich_retry27()
_rich_methods27 = [url.rsplit("/", 1)[-1] for url, _ in _rich_http_calls27]
check("27b rich retry caps retry_after at two seconds under the state lock",
      not _rich_retry_err27 and _rich_sleeps27 == [2]
      and _rich_methods27[:2] == ["editMessageText", "editMessageText"],
      _rich_retry_err27 or _detail26((_rich_sleeps27, _rich_methods27)))
check("27b one rich call counts its initial and retried 429 as one event",
      not _rich_retry_err27
      and int(_rich_retry_state27.get("rich_429_streak") or 0) == 1
      and _rich_retry_state27.get("rich_slow") is not True
      and _rich_retry_state27.get("rich_failed") is not True
      and _state_is_rich26(_rich_retry_state27)
      and str(_rich_retry_state27.get("msgid")) == "rich-rate-27"
      and _rich_methods27 == ["editMessageText", "editMessageText"],
      _rich_retry_err27 or _detail26((_rich_retry_state27, _rich_methods27)))
_rich_min27, _rich_min_err27 = _call26(
    "_effective_min_edit", SDIR, _rich_retry_state27)
_rich_anim27, _rich_anim_err27 = _call26(
    "_effective_anim_interval", SDIR, _rich_retry_state27)

check("27b one rate-limit event retains the normal rich cadence",
      not _rich_min_err27 and not _rich_anim_err27
      and float(_rich_min27) == 1.5 and float(_rich_anim27) == 1.5,
      _rich_min_err27 or _rich_anim_err27)

def _run_animator_budget27():
    if not callable(getattr(mod, "run_animator", None))\
            or not callable(getattr(mod.Telegram, "reset_event_budget", None)):
        return {}, [], [], [], 0.0, "missing animator event-budget APIs"
    _reset26()
    _enable_rich26()
    now = mod.time.time()
    state = {
        "msgid": "animator-budget-27",
        "steps": [{"fam": "files", "cap": "animator budget work",
                   "tuid": "ANIM-BUDGET-27", "done": False, "k": "",
                   "aid": "", "t0": now - 5}],
        "proc_start": now - 10, "last_activity": now,
        "last_edit": 0, "last_live": "stale animator rich frame", "frame": 0,
        "agents": {}, "panel": [], "ghosts": [], "typing_done": True,
        "folded": False, "box_is_last": True, "replied_this_turn": False,
        "rich_turn": True, "rich_box": True, "rich_failed": False,
        "rich_slow": False, "rich_429_streak": 0,
        "rich_transient_streak": 0, "rich_hard_streak": 0,
        "rich_pending_open": False, "rich_health_logged": False,
        "rich_health_classes": [],
        "rich_plan": [], "rich_tools": [], "rich_tool_count": 0,
        "rich_orphans": [],
    }
    mod.save_state(SDIR, state)
    queue = _URLQueue27([
        _http_429_27(1), {"ok": True, "result": {"message_id": 2788}},
    ])
    sleeps = []
    reset_marks = []
    mono = [100.0]
    real_urlopen = mod.request.urlopen
    real_sleep = mod.time.sleep
    real_monotonic = mod.time.monotonic
    try:
        mod.time.monotonic = lambda: mono[0]
        tg = mod.Telegram("TOKEN27", "999", False)
        initial_started = float(tg._event_started)
        mono[0] = initial_started + float(mod.RICH_EVENT_BUDGET) + 1.0
        stale_age = mono[0] - initial_started
        real_reset = tg.reset_event_budget

        def _record_reset27():
            reset_marks.append(mono[0])
            return real_reset()

        def _animator_sleep27(seconds):
            sleeps.append(seconds)
            if float(seconds) != 1.0:
                current = mod.load_state(SDIR)
                current["folded"] = True
                mod.save_state(SDIR, current)

        tg.reset_event_budget = _record_reset27
        mod.request.urlopen = queue
        mod.time.sleep = _animator_sleep27
        mod.run_animator(SDIR, tg)
        after = mod.load_state(SDIR)
        err = ""
    except Exception as exc:
        after = mod.load_state(SDIR)
        stale_age = 0.0
        err = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    finally:
        mod.request.urlopen = real_urlopen
        mod.time.sleep = real_sleep
        mod.time.monotonic = real_monotonic
    return after, sleeps, queue.calls, reset_marks, stale_age, err

(_anim_budget_state27, _anim_budget_sleeps27, _anim_budget_calls27,
 _anim_budget_resets27, _anim_budget_stale27, _anim_budget_err27) =\
    _run_animator_budget27()
_anim_budget_methods27 = [url.rsplit("/", 1)[-1]
                          for url, _ in _anim_budget_calls27]
check("27b animator resets a transport budget that was stale before its tick",
      not _anim_budget_err27
      and _anim_budget_stale27 > float(getattr(mod, "RICH_EVENT_BUDGET", 18.0))
      and bool(_anim_budget_resets27),
      _anim_budget_err27 or _detail26((
          _anim_budget_stale27, _anim_budget_resets27)))
check("27b animator budget reset permits the exact one-second rich retry",
      not _anim_budget_err27
      and _anim_budget_sleeps27[:1] == [1]
      and _anim_budget_methods27 == ["editMessageText", "editMessageText"]
      and isinstance(_anim_budget_state27.get("last_live"), str)
      and "animator budget work" in _anim_budget_state27.get("last_live", ""),
      _anim_budget_err27 or _detail26((
          _anim_budget_sleeps27, _anim_budget_methods27, _anim_budget_state27)))

_oversize_plan27 = [
    {"id": "OV-%02d" % i,
     "subject": "PLAN-%02d-%s::END::" % (i, "p" * 96),
     "status": "pending", "activeForm": "Working plan %02d" % i}
    for i in range(55)
]
_oversize_tools27 = [
    {"cap": "TOOL-%02d-%s::END::" % (i, "t" * 120), "elapsed": i + 1}
    for i in range(20)
]
_oversize_steps27 = [
    {"fam": "write", "cap": "CURRENT-SENTINEL-27", "tuid": "CURRENT27",
     "done": False, "k": "", "aid": "", "t0": 27000.0},
]
_real_time_fallback27 = mod.time.time
try:
    mod.time.time = lambda: 27100.0
    _oversize_value27, _oversize_err27 = _render_live26(
        SDIR, _oversize_steps27, 27000.0, {},
        mission="MISSION-SENTINEL-27 " + ("m" * 250), panel=[],
        plan=_oversize_plan27,
        tools={"items": _oversize_tools27, "count": len(_oversize_tools27)},
        cadence=1.5, fallback=True)
    _oversize_text27 = _text26(_oversize_value27)
finally:
    mod.time.time = _real_time_fallback27
_oversize_lines27 = _oversize_text27.splitlines()
_omitted_lines27 = [line for line in _oversize_lines27
                    if line.startswith("- +") and "earlier" in line]
_kept_plan_lines27 = [line for line in _oversize_lines27
                      if line.startswith("- [ ] PLAN-")]
check("27c oversized plain fallback stays below 3500 characters",
      not _oversize_err27 and bool(_oversize_text27)
      and len(_oversize_text27) < 3500,
      _oversize_err27 or "len=%d" % len(_oversize_text27))
check("27c oversized fallback retains mission, current, plan summary and footer",
      not _oversize_err27
      and "MISSION-SENTINEL-27" in _oversize_text27
      and "CURRENT-SENTINEL-27" in _oversize_text27
      and "📋 Plan · 0/55 done" in _oversize_text27
      and _oversize_text27.endswith("plain fallback"),
      _oversize_err27 or _detail26(_oversize_lines27[-6:]))
check("27c fallback trims all tools before omitting the oldest plan rows",
      not _oversize_err27 and len(_omitted_lines27) == 1
      and "TOOL-" not in _oversize_text27
      and "PLAN-00-" not in _oversize_text27
      and "PLAN-54-" in _oversize_text27,
      _oversize_err27 or _detail26((_omitted_lines27, _oversize_lines27)))
check("27c fallback never cuts a retained plan line or its final footer",
      not _oversize_err27 and bool(_kept_plan_lines27)
      and all(line.endswith("::END::") for line in _kept_plan_lines27)
      and bool(_oversize_lines27)
      and _oversize_lines27[-1].endswith("plain fallback"),
      _oversize_err27 or _detail26(_oversize_lines27[-8:]))

_confirmed_create27, _confirmed_create_err27 = _plan_call26(
    [], "TaskCreate",
    {"subject": "unconfirmed input subject", "description": "create description",
     "activeForm": "Creating task"},
    {"task": {"id": "confirmed-create-27", "subject": "confirmed response subject"}})
check("27d TaskCreate prefers the confirmed response task subject",
      not _confirmed_create_err27
      and _row_subject26(_row26(_confirmed_create27, "confirmed-create-27"))
      == "confirmed response subject",
      _confirmed_create_err27 or _detail26(_confirmed_create27))

_base_update27 = [{
    "id": "update-a-27", "subject": "old subject", "description": "old description",
    "activeForm": "Old active form", "status": "pending",
}]
_mismatch_update27, _mismatch_update_err27 = _plan_call26(
    _base_update27, "TaskUpdate",
    {"taskId": "update-a-27", "status": "completed"},
    {"success": True, "taskId": "different-task-27", "updatedFields": ["status"],
     "statusChange": {"from": "pending", "to": "completed"}})
check("27d TaskUpdate rejects a response whose task id mismatches the input",
      not _mismatch_update_err27 and _mismatch_update27 == _base_update27,
      _mismatch_update_err27 or _detail26(_mismatch_update27))

_confirmed_update27, _confirmed_update_err27 = _plan_call26(
    _base_update27, "TaskUpdate",
    {"taskId": "update-a-27", "subject": "unconfirmed subject",
     "description": "confirmed description", "activeForm": "Unconfirmed active form",
     "status": "completed"},
    {"success": True, "taskId": "update-a-27",
     "updatedFields": ["description", "status"],
     "statusChange": {"from": "pending", "to": "in_progress"}})
_confirmed_update_row27 = _row26(_confirmed_update27, "update-a-27")
check("27d TaskUpdate applies only fields named by confirmed updatedFields",
      not _confirmed_update_err27
      and _row_subject26(_confirmed_update_row27) == "old subject"
      and _confirmed_update_row27.get("description") == "confirmed description"
      and (_confirmed_update_row27.get("activeForm")
           or _confirmed_update_row27.get("active_form")) == "Old active form",
      _confirmed_update_err27 or _detail26(_confirmed_update_row27))
check("27d TaskUpdate statusChange overrides an unconfirmed input status",
      not _confirmed_update_err27
      and _row_status26(_confirmed_update_row27) == "in_progress",
      _confirmed_update_err27 or _detail26(_confirmed_update_row27))

_reset26()
_enable_rich26()
_tool_clock27 = _Clock26(30000.0)
_real_time_tool27 = mod.time.time
try:
    mod.time.time = _tool_clock27.time
    _tool_tg27 = RichRecTG([_ok26("tool-offset-27"), _ok26("tool-offset-27"),
                            _ok26("tool-offset-27")])
    _, _tool_prompt_err27 = _prompt26(
        _tool_tg27, "tool offset turn", session_id="tool-offset-session-27")
    _tool_clock27.advance(7)
    _, _tool_pre_err27 = _step26(
        _tool_tg27, "pre", "Read", {"file_path": "/tmp/tool-offset-27.txt"},
        tuid="TOOL-OFFSET-27", session_id="tool-offset-session-27")
    _tool_clock27.advance(5)
    _, _tool_post_err27 = _step26(
        _tool_tg27, "post", "Read", {"file_path": "/tmp/tool-offset-27.txt"},
        tuid="TOOL-OFFSET-27", tresponse={"ok": True},
        session_id="tool-offset-session-27")
    _tool_state27 = load()
    _tool_items27 = _tool_state27.get("rich_tools")\
        if isinstance(_tool_state27.get("rich_tools"), list) else []
    _tool_frame_value27, _tool_frame_err27 = _render_live26(
        SDIR, _tool_state27.get("steps") or [], _tool_state27.get("proc_start"),
        _tool_state27.get("agents") or {}, mission="Tool offset mission",
        panel=_tool_state27.get("panel"), plan=_state_plan26(_tool_state27),
        tools={"items": _tool_items27,
               "count": _tool_state27.get("rich_tool_count") or len(_tool_items27)},
        cadence=1.5)
    _tool_frame27 = _text26(_tool_frame_value27)
finally:
    mod.time.time = _real_time_tool27
_tool_segment27 = ""
if "🔧 Tools" in _tool_frame27:
    _tool_segment27 = _tool_frame27.split("🔧 Tools", 1)[1].split("</details>", 1)[0]
check("27e completed runtime tool stores its offset from turn start",
      not _tool_prompt_err27 and not _tool_pre_err27 and not _tool_post_err27
      and len(_tool_items27) == 1
      and float(_tool_items27[0].get("elapsed")) == 12.0,
      _tool_prompt_err27 or _tool_pre_err27 or _tool_post_err27
      or _detail26(_tool_items27))
check("27e tools panel marks the zero-padded completion offset as a timestamp",
      not _tool_frame_err27 and "at 00:12 ·" in _tool_segment27
      and "00:05" not in _tool_segment27,
      _tool_frame_err27 or _detail26(_tool_segment27))
_reset26()

print("\n===== #28 (v30) failure-handling regressions =====")

def _rich_state28(mid="rich-old-28", now=None, **updates):
    now = mod.time.time() if now is None else float(now)
    state = {
        "msgid": str(mid) if mid is not None else None,
        "steps": [{"fam": "files", "cap": "prior rich work", "tuid": "OLD28",
                   "done": True, "k": "", "aid": "", "t0": now - 20,
                   "secs": 4}],
        "proc_start": now - 60, "last_edit": now - 10,
        "last_activity": now - 10, "last_reply": 0,
        "last_react": now - 10, "last_spawn": now - 10, "frame": 4,
        "folded": False, "last_live": "prior rich live frame", "agents": {},
        "next_agent_n": 1, "ghosts": [], "typing_done": False,
        "reanchor": False, "box_is_last": False,
        "folded_above_msgid": None, "replied_this_turn": False,
        "mission": "prior mission", "panel": [],
        "prev_recap_msgid": None, "prev_recap_text": None,
        "prev_recap_rich": False, "noreply_grace_until": None,
        "rich_turn": True, "rich_box": True, "rich_failed": False,
        "rich_slow": False, "rich_429_streak": 0,
        "rich_transient_streak": 0, "rich_health_logged": False,
        "rich_plan": [], "rich_tools": [], "rich_tool_count": 0,
        "rich_session_id": "session-28", "rich_orphans": [],
        "folded_above_rich": False,
    }
    state.update(updates)
    return state

_reset26()
_enable_rich26()
mod.save_state(SDIR, _rich_state28(mid="old-rfold-on-28"))
_cross_on_tg28 = RichRecTG([
    _ok26("fresh-rich-on-28"), _fail26("api_400", 400)])
_cross_on_deferred28 = mod.on_prompt(
    SDIR, _cross_on_tg28, prompt_text="new rich turn",
    session_id="session-28")
_cross_on_fresh28 = load()
mod._run_deferred(SDIR, _cross_on_tg28, _cross_on_deferred28)
_cross_on_after28 = load()
check("28a failed prior rfold cannot downgrade the new rich turn",
      str(_cross_on_after28.get("msgid")) == "fresh-rich-on-28"
      and _cross_on_after28.get("rich_box") is True
      and _cross_on_after28.get("rich_failed") is not True
      and not _cross_on_tg28.ops("send"),
      _detail26({"before": _cross_on_fresh28, "after": _cross_on_after28,
                 "calls": _cross_on_tg28.calls}))
check("28a failed prior rfold is ghost-tracked without orphaning the fresh box",
      "old-rfold-on-28" in [str(x) for x in (_cross_on_after28.get("ghosts") or [])]
      and "fresh-rich-on-28" not in [
          str(x) for x in (_cross_on_after28.get("rich_orphans") or [])],
      _detail26(_cross_on_after28))

_reset26()
_enable_rich26()
_prior_recap28 = (
    "✅ **Cedar 🌵 · done in 00:31**\n\n"
    "<details><summary>Run record · 1/1 · 0 lanes · tap</summary>\n"
    "**Mission** · earlier mission\n- [x] earlier work\n</details>\n\n"
    "<footer>Cedar · turn complete · rich v30 · 1.5s</footer>")
mod.save_state(SDIR, _rich_state28(
    mid="old-noreply-box-28", box_is_last=True,
    prev_recap_msgid="old-rrecap-target-28", prev_recap_text=_prior_recap28,
    prev_recap_rich=True))
_cross_recap_tg28 = RichRecTG([
    _ok26("fresh-rich-recap-28"), _fail26("api_400", 400)])
_cross_recap_deferred28 = mod.on_prompt(
    SDIR, _cross_recap_tg28, prompt_text="new turn after recap",
    session_id="session-28")
mod._run_deferred(SDIR, _cross_recap_tg28, _cross_recap_deferred28)
_cross_recap_after28 = load()
check("28a failed prior rrecap cannot contaminate the new rich turn",
      str(_cross_recap_after28.get("msgid")) == "fresh-rich-recap-28"
      and _cross_recap_after28.get("rich_box") is True
      and _cross_recap_after28.get("rich_failed") is not True
      and "old-rrecap-target-28" not in [
          str(x) for x in (_cross_recap_after28.get("ghosts") or [])],
      _detail26({"state": _cross_recap_after28,
                 "calls": _cross_recap_tg28.calls}))

_reset26()
mod.save_state(SDIR, _rich_state28(mid="old-rfold-off-28"))
_cross_off_tg28 = RichRecTG([_fail26("api_400", 400)])
_cross_off_deferred28 = mod.on_prompt(
    SDIR, _cross_off_tg28, prompt_text="new flag-off turn",
    session_id="session-28")
_cross_off_fresh28 = load()
_cross_off_mid28 = str(_cross_off_fresh28.get("msgid"))
mod._run_deferred(SDIR, _cross_off_tg28, _cross_off_deferred28)
_cross_off_after28 = load()
check("28a failed prior rich fold cannot cold-start v30 fallback in flag-off mode",
      bool(_cross_off_mid28)
      and str(_cross_off_after28.get("msgid")) == _cross_off_mid28
      and len(_cross_off_tg28.ops("send")) == 1
      and _cross_off_after28.get("rich_failed") is not True
      and "⚡" not in _cross_off_tg28.ops("send")[0][2],
      _detail26({"before": _cross_off_fresh28, "after": _cross_off_after28,
                 "calls": _cross_off_tg28.calls}))
check("28a flag-off prior-fold failure tracks only the old rich target",
      "old-rfold-off-28" in [str(x) for x in (_cross_off_after28.get("ghosts") or [])]
      and _cross_off_mid28 not in [
          str(x) for x in (_cross_off_after28.get("rich_orphans") or [])],
      _detail26(_cross_off_after28))

_reset26()
_enable_rich26()
mod.save_state(SDIR, _rich_state28(mid="old-fold-after-send-fail-28"))
_old_fold_tg28 = RichRecTG([
    _fail26("api_400", 400), _ok26("old-fold-after-send-fail-28")])
_old_fold_deferred28 = mod.on_prompt(
    SDIR, _old_fold_tg28, prompt_text="new send fails",
    session_id="session-28")
_old_fold_new28 = load()
mod._run_deferred(SDIR, _old_fold_tg28, _old_fold_deferred28)
_old_fold_after28 = load()
_old_fold_edits28 = [c for c in _old_fold_tg28.ops("edit_rich")
                     if c[1] == "old-fold-after-send-fail-28"]
check("28b new-turn rich_failed does not skip the old box fold",
      _old_fold_new28.get("rich_failed") is True
      and len(_old_fold_edits28) == 1,
      _detail26({"state": _old_fold_after28, "calls": _old_fold_tg28.calls}))
check("28b old fold leaves the new plain fallback pointer unchanged",
      str(_old_fold_after28.get("msgid")) == str(_old_fold_new28.get("msgid"))
      and _old_fold_after28.get("rich_box") is False
      and len(_old_fold_tg28.ops("send")) == 1,
      _detail26({"before": _old_fold_new28, "after": _old_fold_after28}))

_fallback_now28 = 28000.0
_fallback_state28 = _rich_state28(
    mid="fallback-frame-28", now=_fallback_now28,
    steps=[
        {"fam": "files", "cap": "reading fallback input", "tuid": "F1-28",
         "done": True, "k": "", "aid": "", "t0": _fallback_now28 - 20,
         "secs": 5},
        {"fam": "write", "cap": "writing fallback output", "tuid": "F2-28",
         "done": False, "k": "", "aid": "", "t0": _fallback_now28 - 4},
    ],
    proc_start=_fallback_now28 - 40, last_edit=0,
    last_activity=_fallback_now28, last_react=_fallback_now28,
    typing_done=True, rich_box=False, rich_failed=True,
    rich_health_logged=True,
    rich_tools=[{"cap": "reading fallback input", "elapsed": 20}],
    rich_tool_count=1)
_real_time28 = mod.time.time
_real_sleep28 = mod.time.sleep
try:
    mod.time.time = lambda: _fallback_now28
    _reset26()
    mod.save_state(SDIR, dict(_fallback_state28))
    _expected_fallback28 = mod._render_live_state(
        SDIR, dict(_fallback_state28), _fallback_state28["steps"], frame=5)
    _anim_fallback_tg28 = RecTG()

    def _stop_animator28(seconds):
        del seconds
        stopped = load()
        stopped["folded"] = True
        mod.save_state(SDIR, stopped)

    mod.time.sleep = _stop_animator28
    mod.run_animator(SDIR, _anim_fallback_tg28)
    _anim_edits28 = _anim_fallback_tg28.ops("edit")

    _reset26()
    mod.save_state(SDIR, dict(_fallback_state28))
    _hook_fallback_tg28 = RecTG()
    _hook_ops28 = mod.handle_step(
        SDIR, _hook_fallback_tg28, "post", "Read",
        {"file_path": "/tmp/unmatched-fallback-28"}, "UNKNOWN-28",
        None, None, tresponse={"ok": True}, session_id="session-28")
    mod._run_deferred(SDIR, _hook_fallback_tg28, _hook_ops28)
    _hook_edits28 = _hook_fallback_tg28.ops("edit")
finally:
    mod.time.time = _real_time28
    mod.time.sleep = _real_sleep28
check("28c animator repaint uses _render_live_state after plain fallback",
      len(_anim_edits28) == 1
      and _anim_edits28[0][2] == _expected_fallback28
      and _anim_edits28[0][2].startswith("🎯 "),
      _detail26(_anim_edits28))
check("28c animator and hook emit the identical v30 fallback frame",
      len(_anim_edits28) == 1 and len(_hook_edits28) == 1
      and _anim_edits28[0][2] == _hook_edits28[0][2],
      _detail26({"anim": _anim_edits28, "hook": _hook_edits28}))

_anim_skip_now28 = 28100.0
_anim_skip_state28 = _rich_state28(
    mid="anim-skip-28", now=_anim_skip_now28,
    proc_start=_anim_skip_now28 - 10, last_edit=_anim_skip_now28,
    last_activity=_anim_skip_now28, last_react=_anim_skip_now28,
    reanchor=True, box_is_last=False, typing_done=True)
_real_time_anim_skip28 = mod.time.time
_real_sleep_anim_skip28 = mod.time.sleep
try:
    mod.time.time = lambda: _anim_skip_now28
    _reset26()
    mod.save_state(SDIR, _anim_skip_state28)
    _anim_skip_tg28 = RichRecTG([_fail26("timeout")])

    def _stop_anim_skip28(seconds):
        del seconds
        stopped = load()
        stopped["folded"] = True
        mod.save_state(SDIR, stopped)

    mod.time.sleep = _stop_anim_skip28
    mod.run_animator(SDIR, _anim_skip_tg28)
    _anim_skip_after28 = load()
finally:
    mod.time.time = _real_time_anim_skip28
    mod.time.sleep = _real_sleep_anim_skip28
check("28c skipped animator re-anchor persists its transient streak",
      int(_anim_skip_after28.get("rich_transient_streak") or 0) == 1
      and str(_anim_skip_after28.get("msgid")) == "anim-skip-28",
      _detail26({"state": _anim_skip_after28,
                 "calls": _anim_skip_tg28.calls}))

_reset26()
_codex_orphan_state28 = _rich_state28(
    mid="codex-live-28", rich_box=False, rich_failed=True,
    rich_health_logged=True, rich_orphans=["orphan-a-28", "orphan-b-28"],
    steps=[{"fam": "run", "cap": "Codex: open lane", "tuid": "CODEX-28",
            "done": False, "k": "", "aid": "", "codex": True,
            "codex_bg": True, "t0": mod.time.time() - 30}])
mod.save_state(SDIR, _codex_orphan_state28)
_codex_orphan_tg28 = RecTG()
_codex_orphan_ops28 = mod.record_reply(SDIR, _codex_orphan_tg28, final=True)
_codex_orphan_after28 = load()
check("28d open Codex Stop preserves rich orphans when no deletes are returned",
      _codex_orphan_ops28 == [("typing", None, None, False)]
      and [str(x) for x in (_codex_orphan_after28.get("rich_orphans") or [])]
      == ["orphan-a-28", "orphan-b-28"],
      _detail26({"ops": _codex_orphan_ops28, "state": _codex_orphan_after28}))

class _NoDelete28(RecTG):
    def delete(self, msgid):
        self.calls.append(("delete_FAIL", str(msgid)))
        return False

_reset26()
mod.save_state(SDIR, _rich_state28(
    mid=None, steps=[], rich_box=False, rich_failed=True,
    rich_health_logged=True, rich_orphans=["orphan-delete-28"]))
_orphan_delete_tg28 = _NoDelete28()
_orphan_delete_ops28 = mod.record_reply(SDIR, _orphan_delete_tg28, final=True)
check("28e orphan cleanup returns a tracked delete operation",
      any(op[0] == "delete" and str(op[1]) == "orphan-delete-28"
          and op[3] is True for op in _orphan_delete_ops28),
      _detail26(_orphan_delete_ops28))
mod._run_deferred(SDIR, _orphan_delete_tg28, _orphan_delete_ops28)
_orphan_delete_after28 = load()
check("28e twice-refused orphan delete is ghost-stashed",
      len(_orphan_delete_tg28.ops("delete_FAIL")) == 2
      and "orphan-delete-28" in [
          str(x) for x in (_orphan_delete_after28.get("ghosts") or [])],
      _detail26({"calls": _orphan_delete_tg28.calls,
                 "state": _orphan_delete_after28}))

for _pointer_case28, _rich_box28, _above_rich28 in (
        ("live pointer", True, False), ("above pointer", False, True)):
    _reset26()
    mod.save_state(SDIR, _rich_state28(
        mid="same-pointer-28", folded_above_msgid="same-pointer-28",
        rich_box=_rich_box28, folded_above_rich=_above_rich28))
    _pointer_tg28 = RichRecTG([_ok26("same-pointer-28")])
    mod._fold_to_done(SDIR, _pointer_tg28)
    check("28f %s classifies the shared fold target as rich" % _pointer_case28,
          len(_pointer_tg28.ops("edit_rich")) == 1
          and not _pointer_tg28.ops("edit"),
          _detail26(_pointer_tg28.calls))

_reset26()
_enable_rich26()
mod.save_state(SDIR, _rich_state28(
    mid=None, steps=[], proc_start=mod.time.time(), last_edit=0,
    rich_box=False, rich_failed=False))
_react_tg28 = RichRecTG([_ok26("react-rich-28")])
_react_ops28 = mod.on_react(SDIR, _react_tg28)
mod._run_deferred(SDIR, _react_tg28, _react_ops28)
_react_after28 = load()
check("28g rich react with no msgid opens exactly one fresh rich box",
      len(_react_tg28.ops("send_rich")) == 1
      and not _react_tg28.ops("send")
      and str(_react_after28.get("msgid")) == "react-rich-28"
      and _react_after28.get("rich_box") is True,
      _detail26({"calls": _react_tg28.calls, "state": _react_after28}))

_reset26()
_enable_rich26()
_health_state28 = {"rich_turn": True, "rich_health_logged": False}
for _health_i28 in range(225):
    _health_state28["rich_health_logged"] = False
    mod._append_rich_health(SDIR, _health_state28, "event_%03d" % _health_i28)
_health_path28 = os.path.join(SDIR, "rich-health.log")
try:
    with open(_health_path28, "r") as _health_file28:
        _health_lines28 = _health_file28.read().splitlines()
except OSError:
    _health_lines28 = []
check("28h rich health log retains exactly the newest 200 content-free records",
      len(_health_lines28) == 200
      and any(line.endswith(" event_224") for line in _health_lines28)
      and not any(line.endswith(" event_000") for line in _health_lines28),
      _detail26({"count": len(_health_lines28),
                 "first": _health_lines28[:1], "last": _health_lines28[-1:]}))

_collapse_wrapper_state28 = _rich_state28(mid="collapse-wrapper-28")
try:
    _collapse_wrapper_ops28 = mod._collapse_prior(
        _collapse_wrapper_state28, "wrapper rich mission", sdir=SDIR)
    _collapse_wrapper_err28 = ""
except Exception as exc:
    _collapse_wrapper_ops28 = []
    _collapse_wrapper_err28 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
_collapse_wrapper_text28 = (_collapse_wrapper_ops28[0][2]
                            if _collapse_wrapper_ops28 else "")
check("28h collapse wrapper forwards sdir and builds a real rich recap",
      not _collapse_wrapper_err28
      and bool(_collapse_wrapper_ops28)
      and _collapse_wrapper_ops28[0][0] == "rfold"
      and mod._is_rich_recap_text(_collapse_wrapper_text28),
      _collapse_wrapper_err28 or _detail26(_collapse_wrapper_ops28))

_reset26()
_enable_rich26()
_real_render_rich_live28 = mod.render_rich_live
try:
    def _raise_render28(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("render fixture failure")

    mod.render_rich_live = _raise_render28
    try:
        mod._open_fresh_box(SDIR, RichRecTG(), session_id="render-fail-28")
        _render_open_err28 = ""
    except Exception as exc:
        _render_open_err28 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
    _render_open_state28 = load()
finally:
    mod.render_rich_live = _real_render_rich_live28
check("28h fresh rich render failure is contained and persists retryable state",
      not _render_open_err28 and _render_open_state28.get("rich_turn") is True
      and _render_open_state28.get("msgid") is None
      and int(_render_open_state28.get("rich_transient_streak") or 0) == 1
      and _render_open_state28.get("rich_failed") is not True,
      _render_open_err28 or _detail26(_render_open_state28))

_reset26()
_failed_codex_state28 = _rich_state28(
    mid="failed-rich-codex-28", rich_failed=True,
    steps=[{"fam": "run", "cap": "Codex: failed transport lane",
            "tuid": "FAILED-CODEX-28", "done": False, "k": "", "aid": "",
            "codex": True, "codex_bg": True, "t0": mod.time.time() - 20}])
mod.save_state(SDIR, _failed_codex_state28)
_failed_codex_ops28 = mod._close_codex_notification(
    SDIR, load(), _terminal_notification25(
        "FAILED-CODEX-28", "completed", task_id="failed-codex-28"))
_failed_codex_after28 = load()
_failed_codex_lane28 = next(
    (s for s in (_failed_codex_after28.get("steps") or [])
     if s.get("tuid") == "FAILED-CODEX-28"), {})
check("28h Codex closure never repaints an already failed rich target",
      _failed_codex_lane28.get("done") is True
      and not any(op[0] in ("edit", "redit")
                  and str(op[1]) == "failed-rich-codex-28"
                  for op in (_failed_codex_ops28 or [])),
      _detail26({"ops": _failed_codex_ops28, "state": _failed_codex_after28}))

print("\n===== #29 (v30) adversarial closure races =====")

class _FoldRaceTG29:
    def __init__(self):
        self.calls = []
        self.physical = []
        self.live_started = threading.Event()
        self.release_live = threading.Event()
        self.fold_sent = threading.Event()

    def edit_rich(self, msgid, frame):
        if frame == "LIVE-RACE-29":
            self.calls.append(("live_enter", str(msgid)))
            self.live_started.set()
            self.release_live.wait(2.0)
            self.physical.append(("live", str(msgid), frame))
            self.calls.append(("live_land", str(msgid)))
        else:
            self.physical.append(("final", str(msgid), frame))
            self.calls.append(("final_land", str(msgid)))
            self.fold_sent.set()
        return {"ok": True, "message_id": str(msgid)}

    def delete(self, msgid):
        self.calls.append(("delete", str(msgid)))
        return True

_reset26()
mod.save_state(SDIR, _rich_state28(mid="fold-race-29"))
_fold_race_tg29 = _FoldRaceTG29()
_fold_race_errors29 = []

def _run_live_race29():
    try:
        mod._rich_deferred_edit(
            SDIR, _fold_race_tg29, "fold-race-29", "LIVE-RACE-29",
            live_fallback=True)
    except Exception as exc:
        _fold_race_errors29.append("live:%s" % type(exc).__name__)

def _run_fold_race29():
    try:
        mod._fold_to_done(SDIR, _fold_race_tg29)
    except Exception as exc:
        _fold_race_errors29.append("fold:%s" % type(exc).__name__)

_live_thread29 = threading.Thread(target=_run_live_race29, daemon=True)
_live_thread29.start()
_live_entered29 = _fold_race_tg29.live_started.wait(1.5)
_fold_thread29 = threading.Thread(target=_run_fold_race29, daemon=True)
_fold_thread29.start()
_fold_landed_while_live_blocked29 = _fold_race_tg29.fold_sent.wait(0.5)
_fold_race_tg29.release_live.set()
_live_thread29.join(2.0)
_fold_thread29.join(2.0)
_fold_race_after29 = load()
check("29a terminal fold transport is not blocked by an in-flight rich edit",
      _live_entered29 and _fold_landed_while_live_blocked29
      and not _live_thread29.is_alive() and not _fold_thread29.is_alive()
      and not _fold_race_errors29,
      _detail26({"errors": _fold_race_errors29,
                 "calls": _fold_race_tg29.calls}))
check("29a late live result cannot overwrite the completed recap state",
      _fold_race_after29.get("folded") is True
      and mod._is_rich_recap_text(_fold_race_after29.get("last_live") or ""),
      _detail26({"physical": _fold_race_tg29.physical,
                 "state": _fold_race_after29}))

for _boundary_flag29 in (True, False):
    _reset26()
    if _boundary_flag29:
        _enable_rich26()
    mod.save_state(SDIR, _rich_state28(
        mid=None, steps=[], rich_box=False, rich_failed=True,
        rich_health_logged=True, rich_orphans=["boundary-orphan-29"]))
    _boundary_tg29 = RichRecTG([_ok26("boundary-fresh-rich-29")])\
        if _boundary_flag29 else RecTG()
    _boundary_ops29 = mod.on_prompt(
        SDIR, _boundary_tg29, prompt_text="boundary orphan cleanup",
        session_id="boundary-29")
    _boundary_label29 = "flag-on" if _boundary_flag29 else "flag-off"
    check("29b %s prompt returns a tracked rich-orphan delete" % _boundary_label29,
          sum(1 for op in _boundary_ops29
              if op[0] == "delete" and str(op[1]) == "boundary-orphan-29"
              and op[3] is True) == 1,
          _detail26(_boundary_ops29))
    if _boundary_flag29:
        _boundary_fail_tg29 = _NoDelete28()
        mod._run_deferred(SDIR, _boundary_fail_tg29, _boundary_ops29)
        _boundary_after29 = load()
        check("29b twice-refused prompt-boundary orphan becomes a fresh-turn ghost",
              len(_boundary_fail_tg29.ops("delete_FAIL")) == 2
              and "boundary-orphan-29" in [
                  str(x) for x in (_boundary_after29.get("ghosts") or [])],
              _detail26({"calls": _boundary_fail_tg29.calls,
                         "state": _boundary_after29}))

_reset26()
mod.save_state(SDIR, _rich_state28(
    mid="normal-stop-alias-29", folded_above_msgid="normal-stop-alias-29",
    rich_box=False, folded_above_rich=True, box_is_last=False))
_normal_alias_tg29 = RichRecTG([_ok26("normal-stop-alias-29")])
_normal_alias_ops29 = mod.record_reply(SDIR, _normal_alias_tg29, final=True)
mod._run_deferred(SDIR, _normal_alias_tg29, _normal_alias_ops29)
_normal_alias_after29 = load()
check("29c normal Stop ORs rich transport across aliased pointers",
      len(_normal_alias_tg29.ops("edit_rich")) == 1
      and not [c for c in _normal_alias_tg29.ops("edit")
               if str(c[1]) == "normal-stop-alias-29"],
      _detail26(_normal_alias_tg29.calls))
check("29c aliased rich fold persists the physical transport on its owner",
      _normal_alias_after29.get("folded") is True
      and _normal_alias_after29.get("rich_box") is True
      and _normal_alias_after29.get("folded_above_msgid") is None
      and mod._pointer_is_rich(
          _normal_alias_after29, "normal-stop-alias-29") is True,
      _detail26(_normal_alias_after29))
_normal_alias_boundary_tg29 = RecTG()
_normal_alias_boundary_ops29 = mod.on_prompt(
    SDIR, _normal_alias_boundary_tg29,
    prompt_text="turn after aliased rich fold", session_id="alias-boundary-29")
_normal_alias_boundary_after29 = load()
check("29c next prompt carries the aliased recap with rich transport intact",
      str(_normal_alias_boundary_after29.get("prev_recap_msgid"))
      == "normal-stop-alias-29"
      and _normal_alias_boundary_after29.get("prev_recap_rich") is True
      and mod._is_rich_recap_text(
          _normal_alias_boundary_after29.get("prev_recap_text")),
      _detail26({"ops": _normal_alias_boundary_ops29,
                 "state": _normal_alias_boundary_after29,
                 "calls": _normal_alias_boundary_tg29.calls}))

_anim_once_now29 = 29000.0
_anim_once_state29 = _rich_state28(
    mid="anim-once-29", now=_anim_once_now29,
    proc_start=_anim_once_now29 - 20, last_edit=0,
    last_activity=_anim_once_now29, last_react=_anim_once_now29,
    reanchor=True, box_is_last=False, typing_done=True)
_real_time_anim_once29 = mod.time.time
_real_sleep_anim_once29 = mod.time.sleep
try:
    mod.time.time = lambda: _anim_once_now29
    _reset26()
    mod.save_state(SDIR, _anim_once_state29)
    _anim_once_tg29 = RichRecTG([_fail26("timeout"), _fail26("timeout")])

    def _stop_anim_once29(seconds):
        del seconds
        stopped = load()
        stopped["folded"] = True
        mod.save_state(SDIR, stopped)

    mod.time.sleep = _stop_anim_once29
    mod.run_animator(SDIR, _anim_once_tg29)
    _anim_once_after29 = load()
finally:
    mod.time.time = _real_time_anim_once29
    mod.time.sleep = _real_sleep_anim_once29
_anim_once_rich_calls29 = (
    _anim_once_tg29.ops("send_rich") + _anim_once_tg29.ops("edit_rich"))
check("29d skipped rich re-anchor spends only one transport event that tick",
      len(_anim_once_rich_calls29) == 1
      and int(_anim_once_after29.get("rich_transient_streak") or 0) == 1,
      _detail26({"calls": _anim_once_tg29.calls,
                 "state": _anim_once_after29}))

def _failed_codex_state29(two_lanes=False):
    rows = [{"fam": "run", "cap": "Codex: failed owner", "tuid": "CODEX-A-29",
             "done": False, "k": "", "aid": "", "codex": True,
             "codex_bg": True, "t0": mod.time.time() - 20}]
    if two_lanes:
        rows.append({"fam": "run", "cap": "Codex: sibling", "tuid": "CODEX-B-29",
                     "done": False, "k": "", "aid": "", "codex": True,
                     "codex_bg": True, "t0": mod.time.time() - 10})
    return _rich_state28(
        mid="failed-rich-owner-29", rich_failed=True,
        rich_health_logged=True, steps=rows)

_reset26()
_enable_rich26()
mod.save_state(SDIR, _failed_codex_state29())
_failed_close_tg29 = RichRecTG()
try:
    _failed_close_ops29 = mod._close_codex_notification(
        SDIR, load(), _terminal_notification25(
            "CODEX-A-29", "completed", task_id="codex-a-29"),
        tg=_failed_close_tg29)
    _failed_close_err29 = ""
except Exception as exc:
    _failed_close_ops29 = []
    _failed_close_err29 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
_failed_close_after29 = load()
if not _failed_close_err29:
    mod._run_deferred(SDIR, _failed_close_tg29, _failed_close_ops29)
check("29e failed-rich Codex closure cold-starts one tracked plain fallback",
      not _failed_close_err29
      and len(_failed_close_tg29.ops("send")) == 1
      and not _failed_close_tg29.ops("send_rich")
      and _failed_close_after29.get("rich_box") is False
      and str(_failed_close_after29.get("msgid")) != "failed-rich-owner-29"
      and "failed-rich-owner-29" in [
          str(x) for x in (_failed_close_after29.get("rich_orphans") or [])]
      and not any(c[0] in ("edit", "edit_rich")
                  and str(c[1]) == "failed-rich-owner-29"
                  for c in _failed_close_tg29.calls),
      _failed_close_err29 or _detail26({
          "calls": _failed_close_tg29.calls, "state": _failed_close_after29}))
if not _failed_close_err29:
    _failed_close_stop29 = mod.record_reply(SDIR, _failed_close_tg29, final=True)
    mod._run_deferred(SDIR, _failed_close_tg29, _failed_close_stop29)
_failed_close_final29 = load()
check("29e Stop folds only the plain fallback and retires the failed rich owner",
      not _failed_close_err29 and _failed_close_final29.get("folded") is True
      and _failed_close_final29.get("rich_box") is False
      and any(c[0] == "delete" and str(c[1]) == "failed-rich-owner-29"
              for c in _failed_close_tg29.calls)
      and not any(c[0] in ("edit", "edit_rich")
                  and str(c[1]) == "failed-rich-owner-29"
                  for c in _failed_close_tg29.calls),
      _failed_close_err29 or _detail26({
          "calls": _failed_close_tg29.calls, "state": _failed_close_final29}))

_reset26()
_enable_rich26()
mod.save_state(SDIR, _failed_codex_state29())
_failed_send_tg29 = RichRecTG(fail_send=True)
try:
    _failed_send_ops29 = mod._close_codex_notification(
        SDIR, load(), _terminal_notification25(
            "CODEX-A-29", "completed", task_id="codex-send-fail-29"),
        tg=_failed_send_tg29)
    _failed_send_err29 = ""
except Exception as exc:
    _failed_send_ops29 = []
    _failed_send_err29 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
_failed_send_after29 = load()
check("29e failed plain cold-start keeps the abandoned rich id tracked",
      not _failed_send_err29 and _failed_send_after29.get("msgid") is None
      and _failed_send_after29.get("rich_box") is False
      and "failed-rich-owner-29" in [
          str(x) for x in (_failed_send_after29.get("rich_orphans") or [])]
      and not any(c[0] in ("edit", "edit_rich")
                  and str(c[1]) == "failed-rich-owner-29"
                  for c in _failed_send_tg29.calls),
      _failed_send_err29 or _detail26({
          "calls": _failed_send_tg29.calls, "state": _failed_send_after29}))

_reset26()
_enable_rich26()
mod.save_state(SDIR, _failed_codex_state29(two_lanes=True))
_sibling_close_tg29 = RichRecTG()
try:
    _sibling_close_ops29 = mod._close_codex_notification(
        SDIR, load(), _terminal_notification25(
            "CODEX-A-29", "completed", task_id="codex-sibling-29"),
        tg=_sibling_close_tg29)
    mod._run_deferred(SDIR, _sibling_close_tg29, _sibling_close_ops29)
    _sibling_stop_ops29 = mod.record_reply(SDIR, _sibling_close_tg29, final=True)
    _sibling_after29 = load()
    _sibling_close_err29 = ""
except Exception as exc:
    _sibling_stop_ops29 = []
    _sibling_after29 = load()
    _sibling_close_err29 = "%s: %s" % (type(exc).__name__, str(exc)[:180])
check("29e open sibling lane preserves failed-rich orphan until terminal cleanup",
      not _sibling_close_err29
      and _sibling_stop_ops29 == [("typing", None, None, False)]
      and "failed-rich-owner-29" in [
          str(x) for x in (_sibling_after29.get("rich_orphans") or [])],
      _sibling_close_err29 or _detail26({
          "ops": _sibling_stop_ops29, "state": _sibling_after29}))

_reset26()
_plain_open_fail_tg29 = RichRecTG(fail_send=True)
mod.on_prompt(SDIR, _plain_open_fail_tg29,
              prompt_text="plain open fails before flag", session_id="gate-29")
_plain_open_fail_state29 = load()
_enable_rich26()
_plain_open_fail_tg29.fail_send = False
_plain_flip_ops29 = mod.handle_step(
    SDIR, _plain_open_fail_tg29, "pre", "Read",
    {"file_path": "/tmp/gate-29.txt"}, "GATE-29", None, None,
    session_id="gate-29")
mod._run_deferred(SDIR, _plain_open_fail_tg29, _plain_flip_ops29)
_plain_flip_after29 = load()
check("29f flag appearing after a failed plain open cannot convert that turn to rich",
      _plain_open_fail_state29.get("msgid") is None
      and not _state_is_rich26(_plain_open_fail_state29)
      and not _plain_open_fail_tg29.ops("send_rich")
      and len(_plain_open_fail_tg29.ops("send")) == 1
      and not _state_is_rich26(_plain_flip_after29),
      _detail26({"calls": _plain_open_fail_tg29.calls,
                 "state": _plain_flip_after29}))

_reset26()
_enable_rich26()
_historical_recap_mid29 = "historical-rich-recap-29"
mod.save_state(SDIR, _rich_state28(
    mid=_historical_recap_mid29, folded=True, box_is_last=False,
    last_live=_prior_recap28, rich_box=True))
_historical_merge_tg29 = RichRecTG([_fail26("api_400", 400)])
_historical_merge_ops29 = mod.on_prompt(
    SDIR, _historical_merge_tg29,
    prompt_text="failed rich turn with historical recap",
    session_id="historical-merge-29")
mod._run_deferred(SDIR, _historical_merge_tg29, _historical_merge_ops29)
_historical_plain_mid29 = str(load().get("msgid"))
_step26(
    _historical_merge_tg29, "pre", "Read",
    {"file_path": "/tmp/historical-merge-work-29.txt"},
    tuid="HISTORICAL-MERGE-29", session_id="historical-merge-29")
_historical_stop_ops29 = mod.record_reply(
    SDIR, _historical_merge_tg29, final=True)
mod._run_deferred(SDIR, _historical_merge_tg29, _historical_stop_ops29)
_historical_due29 = load()
_historical_due29["noreply_grace_until"] = mod.time.time() - 1
mod.save_state(SDIR, _historical_due29)
_historical_fired29 = mod._noreply_grace_sweep(
    SDIR, _historical_merge_tg29)
_historical_after29 = load()
_historical_edits29 = [
    call for call in _historical_merge_tg29.ops("edit_rich")
    if str(call[1]) == _historical_recap_mid29]
check("29g failed current rich owner still merges into distinct historical rich recap",
      _historical_fired29 is True and len(_historical_edits29) == 1,
      _detail26({"calls": _historical_merge_tg29.calls,
                 "state": _historical_after29}))
check("29g historical merge retains current work and retires only the plain box",
      bool(_historical_edits29)
      and "historical-merge-work-29.txt" in _historical_edits29[-1][2]
      and any(str(call[1]) == _historical_plain_mid29
              for call in _historical_merge_tg29.ops("delete"))
      and not (_historical_after29.get("steps") or [])
      and str(_historical_after29.get("prev_recap_msgid"))
      == _historical_recap_mid29
      and _historical_after29.get("prev_recap_rich") is True,
      _detail26({"plain": _historical_plain_mid29,
                 "edits": _historical_edits29,
                 "calls": _historical_merge_tg29.calls,
                 "state": _historical_after29}))

_reset26()
_enable_rich26()
_historical_retry_mid29 = "historical-rich-retry-29"
mod.save_state(SDIR, _rich_state28(
    mid=_historical_retry_mid29, folded=True, box_is_last=False,
    last_live=_prior_recap28, rich_box=True))
_historical_retry_tg29 = RichRecTG([
    _fail26("api_400", 400),
    _fail26("api_400", 400),
    _ok26(_historical_retry_mid29),
])
_historical_retry_prompt_ops29 = mod.on_prompt(
    SDIR, _historical_retry_tg29,
    prompt_text="retry refused historical recap",
    session_id="historical-retry-29")
mod._run_deferred(
    SDIR, _historical_retry_tg29, _historical_retry_prompt_ops29)
_historical_retry_plain29 = str(load().get("msgid"))
_step26(
    _historical_retry_tg29, "pre", "Read",
    {"file_path": "/tmp/historical-retry-work-29.txt"},
    tuid="HISTORICAL-RETRY-29", session_id="historical-retry-29")
_historical_retry_stop_ops29 = mod.record_reply(
    SDIR, _historical_retry_tg29, final=True)
mod._run_deferred(
    SDIR, _historical_retry_tg29, _historical_retry_stop_ops29)
_historical_retry_due29 = load()
_historical_retry_due29["noreply_grace_until"] = mod.time.time() - 1
mod.save_state(SDIR, _historical_retry_due29)
_historical_retry_first29 = mod._noreply_grace_sweep(
    SDIR, _historical_retry_tg29)
_historical_retry_after_first29 = load()
check("29g refused historical merge preserves the plain box and retry state",
      _historical_retry_first29 is False
      and str(_historical_retry_after_first29.get("msgid"))
      == _historical_retry_plain29
      and _historical_retry_after_first29.get("folded") is True
      and _historical_retry_after_first29.get("noreply_grace_until") is not None
      and bool(_historical_retry_after_first29.get("steps"))
      and not [call for call in _historical_retry_tg29.ops("delete")
               if str(call[1]) == _historical_retry_plain29]
      and _historical_retry_after_first29.get("rich_failed") is True,
      _detail26({"calls": _historical_retry_tg29.calls,
                 "state": _historical_retry_after_first29}))
_historical_retry_second29 = mod._noreply_grace_sweep(
    SDIR, _historical_retry_tg29)
_historical_retry_after_second29 = load()
_historical_retry_edits29 = [
    call for call in _historical_retry_tg29.ops("edit_rich")
    if str(call[1]) == _historical_retry_mid29]
check("29g later historical retry merges work before retiring the plain box",
      _historical_retry_second29 is True
      and len(_historical_retry_edits29) == 2
      and "historical-retry-work-29.txt" in _historical_retry_edits29[-1][2]
      and sum(1 for call in _historical_retry_tg29.ops("delete")
              if str(call[1]) == _historical_retry_plain29) == 1
      and not (_historical_retry_after_second29.get("steps") or [])
      and _historical_retry_after_second29.get("rich_failed") is True,
      _detail26({"edits": _historical_retry_edits29,
                 "calls": _historical_retry_tg29.calls,
                 "state": _historical_retry_after_second29}))

def _failed_above_state29(label):
    return _rich_state28(
        mid="plain-trailing-%s-29" % label,
        steps=[{"fam": "files", "cap": "trailing work %s" % label,
                "tuid": "ABOVE-%s-29" % label.upper(), "done": True,
                "k": "", "aid": "", "t0": mod.time.time() - 8,
                "secs": 3}],
        rich_box=False, rich_failed=True, rich_health_logged=True,
        folded_above_msgid="rich-above-%s-29" % label,
        folded_above_rich=True, box_is_last=True,
        replied_this_turn=True, folded=False)

_reset26()
_above_stop_state29 = _failed_above_state29("stop")
mod.save_state(SDIR, _above_stop_state29)
_above_stop_tg29 = RichRecTG([_ok26("rich-above-stop-29")])
_above_stop_ops29 = mod.record_reply(SDIR, _above_stop_tg29, final=True)
mod._run_deferred(SDIR, _above_stop_tg29, _above_stop_ops29)
_above_stop_after29 = load()
_above_stop_edits29 = [
    call for call in _above_stop_tg29.ops("edit_rich")
    if str(call[1]) == "rich-above-stop-29"]
check("29h Stop updates a distinct rich fold-above despite current rich failure",
      len(_above_stop_edits29) == 1
      and "trailing work stop" in _above_stop_edits29[-1][2]
      and any(str(call[1]) == "plain-trailing-stop-29"
              for call in _above_stop_tg29.ops("delete"))
      and str(_above_stop_after29.get("msgid")) == "rich-above-stop-29"
      and _above_stop_after29.get("rich_box") is True,
      _detail26({"ops": _above_stop_ops29,
                 "calls": _above_stop_tg29.calls,
                 "state": _above_stop_after29}))

_reset26()
mod.save_state(SDIR, _failed_above_state29("prompt"))
_above_prompt_tg29 = RichRecTG([_ok26("rich-above-prompt-29")])
_above_prompt_ops29 = mod.on_prompt(
    SDIR, _above_prompt_tg29,
    prompt_text="turn after failed rich trailing work",
    session_id="above-prompt-29")
_above_prompt_fresh29 = load()
mod._run_deferred(SDIR, _above_prompt_tg29, _above_prompt_ops29)
_above_prompt_edits29 = [
    call for call in _above_prompt_tg29.ops("edit_rich")
    if str(call[1]) == "rich-above-prompt-29"]
check("29h prompt boundary carries and updates the distinct rich fold-above",
      any(op[0] == "rrecap" and str(op[1]) == "rich-above-prompt-29"
          for op in _above_prompt_ops29)
      and len(_above_prompt_edits29) == 1
      and "trailing work prompt" in _above_prompt_edits29[-1][2]
      and str(_above_prompt_fresh29.get("prev_recap_msgid"))
      == "rich-above-prompt-29"
      and _above_prompt_fresh29.get("prev_recap_rich") is True
      and not any(op[0] == "delete"
                  and str(op[1]) == "rich-above-prompt-29"
                  for op in _above_prompt_ops29),
      _detail26({"ops": _above_prompt_ops29,
                 "calls": _above_prompt_tg29.calls,
                 "state": _above_prompt_fresh29}))

_reset26()
mod.save_state(SDIR, _failed_above_state29("backstop"))
_above_backstop_tg29 = RichRecTG([_ok26("rich-above-backstop-29")])
mod._fold_to_done(SDIR, _above_backstop_tg29)
_above_backstop_after29 = load()
_above_backstop_edits29 = [
    call for call in _above_backstop_tg29.ops("edit_rich")
    if str(call[1]) == "rich-above-backstop-29"]
check("29h idle backstop updates the distinct rich fold-above before cleanup",
      len(_above_backstop_edits29) == 1
      and "trailing work backstop" in _above_backstop_edits29[-1][2]
      and any(str(call[1]) == "plain-trailing-backstop-29"
              for call in _above_backstop_tg29.ops("delete"))
      and str(_above_backstop_after29.get("msgid"))
      == "rich-above-backstop-29"
      and _above_backstop_after29.get("rich_box") is True,
      _detail26({"calls": _above_backstop_tg29.calls,
                 "state": _above_backstop_after29}))

class _NoDeleteRich29(RichRecTG):
    def delete(self, msgid):
        self.calls.append(("delete_FAIL", str(msgid)))
        return False

_reset26()
_sweep_delete_mid29 = "plain-sweep-delete-29"
mod.save_state(SDIR, _rich_state28(
    mid=_sweep_delete_mid29,
    steps=[{"fam": "files", "cap": "sweep delete work",
            "tuid": "SWEEP-DELETE-29", "done": True,
            "k": "", "aid": "", "t0": mod.time.time() - 8,
            "secs": 3}],
    folded=True, box_is_last=True, replied_this_turn=False,
    noreply_grace_until=mod.time.time() - 1,
    rich_box=False, rich_failed=True, rich_health_logged=True,
    prev_recap_msgid="rich-sweep-recap-29",
    prev_recap_text=_prior_recap28, prev_recap_rich=True))
_sweep_delete_tg29 = _NoDeleteRich29()
_sweep_delete_fired29 = mod._noreply_grace_sweep(
    SDIR, _sweep_delete_tg29)
_sweep_delete_after29 = load()
check("29i refused grace-sweep delete ghost-tracks the retired plain box",
      _sweep_delete_fired29 is True
      and len(_sweep_delete_tg29.ops("delete_FAIL")) >= 1
      and _sweep_delete_mid29 in [
          str(x) for x in (_sweep_delete_after29.get("ghosts") or [])],
      _detail26({"calls": _sweep_delete_tg29.calls,
                 "state": _sweep_delete_after29}))

_reset26()
_backstop_delete_mid29 = "plain-backstop-delete-29"
mod.save_state(SDIR, _rich_state28(
    mid=_backstop_delete_mid29,
    steps=[{"fam": "files", "cap": "backstop delete work",
            "tuid": "BACKSTOP-DELETE-29", "done": True,
            "k": "", "aid": "", "t0": mod.time.time() - 8,
            "secs": 3}],
    folded=False, box_is_last=True, replied_this_turn=False,
    rich_box=False, rich_failed=True, rich_health_logged=True,
    prev_recap_msgid="rich-backstop-recap-29",
    prev_recap_text=_prior_recap28, prev_recap_rich=True))
_backstop_delete_tg29 = _NoDeleteRich29()
mod._fold_to_done(SDIR, _backstop_delete_tg29)
_backstop_delete_after29 = load()
check("29i refused idle-backstop delete ghost-tracks the retired plain box",
      len(_backstop_delete_tg29.ops("delete_FAIL")) >= 1
      and _backstop_delete_mid29 in [
          str(x) for x in (_backstop_delete_after29.get("ghosts") or [])],
      _detail26({"calls": _backstop_delete_tg29.calls,
                 "state": _backstop_delete_after29}))

_reset26()
_enable_rich26()
_reanchor_fallback_tg29 = RichRecTG([
    _ok26("rich-first-reanchor-29"),
    _fail26("api_400", 400),
])
_reanchor_prompt_ops29 = mod.on_prompt(
    SDIR, _reanchor_fallback_tg29,
    prompt_text="reanchor fallback keeps prior work",
    session_id="reanchor-fallback-29")
mod._run_deferred(SDIR, _reanchor_fallback_tg29, _reanchor_prompt_ops29)
_step26(
    _reanchor_fallback_tg29, "pre", "Read",
    {"file_path": "/tmp/before-reanchor-29.txt"},
    tuid="BEFORE-REANCHOR-29", session_id="reanchor-fallback-29")
_reanchor_reply_ops29 = mod.record_reply(
    SDIR, _reanchor_fallback_tg29, final=False)
mod._run_deferred(
    SDIR, _reanchor_fallback_tg29, _reanchor_reply_ops29)
_step26(
    _reanchor_fallback_tg29, "pre", "Read",
    {"file_path": "/tmp/after-reanchor-29.txt"},
    tuid="AFTER-REANCHOR-29", session_id="reanchor-fallback-29")
_reanchor_fallback_live29 = load()
_reanchor_plain_mid29 = str(_reanchor_fallback_live29.get("msgid"))
check("29j hook re-anchor fallback preserves the old rich box as fold-above",
      _reanchor_plain_mid29 not in ("", "None", "rich-first-reanchor-29")
      and str(_reanchor_fallback_live29.get("folded_above_msgid"))
      == "rich-first-reanchor-29"
      and _reanchor_fallback_live29.get("folded_above_rich") is True
      and "rich-first-reanchor-29" not in [
          str(x) for x in (_reanchor_fallback_live29.get("rich_orphans") or [])]
      and any(str(call[1]) == "rich-first-reanchor-29"
              for call in _reanchor_fallback_tg29.ops("edit_rich")),
      _detail26({"calls": _reanchor_fallback_tg29.calls,
                 "state": _reanchor_fallback_live29}))
_reanchor_final_ops29 = mod.record_reply(
    SDIR, _reanchor_fallback_tg29, final=True)
mod._run_deferred(SDIR, _reanchor_fallback_tg29, _reanchor_final_ops29)
_reanchor_fallback_final29 = load()
_reanchor_final_edits29 = [
    call for call in _reanchor_fallback_tg29.ops("edit_rich")
    if str(call[1]) == "rich-first-reanchor-29"
    and "before-reanchor-29.txt" in call[2]
    and "after-reanchor-29.txt" in call[2]]
check("29j hook Stop leaves one complete recap above the reply after fallback",
      bool(_reanchor_final_edits29)
      and str(_reanchor_fallback_final29.get("msgid"))
      == "rich-first-reanchor-29"
      and _reanchor_fallback_final29.get("rich_box") is True
      and any(str(call[1]) == _reanchor_plain_mid29
              for call in _reanchor_fallback_tg29.ops("delete"))
      and not any(str(call[1]) == "rich-first-reanchor-29"
                  for call in _reanchor_fallback_tg29.ops("delete")),
      _detail26({"ops": _reanchor_final_ops29,
                 "calls": _reanchor_fallback_tg29.calls,
                 "state": _reanchor_fallback_final29}))

class _AnimatorReanchorFallbackTG29(RichRecTG):
    def send(self, text, markdown=False):
        mid = super().send(text, markdown=markdown)
        with open(os.path.join(STATE_DIR, "stream.off"), "w"):
            pass
        return mid

_reset26()
_enable_rich26()
_anim_reanchor_now29 = mod.time.time()
mod.save_state(SDIR, _rich_state28(
    mid="rich-anim-reanchor-29", now=_anim_reanchor_now29,
    steps=[{"fam": "files", "cap": "animator reanchor work",
            "tuid": "ANIM-REANCHOR-29", "done": True,
            "k": "", "aid": "", "t0": _anim_reanchor_now29 - 8,
            "secs": 3}],
    reanchor=True, box_is_last=False, replied_this_turn=True,
    folded=False, rich_box=True, rich_failed=False))
_anim_reanchor_tg29 = _AnimatorReanchorFallbackTG29([
    _fail26("api_400", 400)])
_real_sleep_reanchor29 = mod.time.sleep
try:
    mod.time.sleep = lambda seconds: None
    mod.run_animator(SDIR, _anim_reanchor_tg29)
finally:
    mod.time.sleep = _real_sleep_reanchor29
    try:
        os.remove(os.path.join(STATE_DIR, "stream.off"))
    except OSError:
        pass
_anim_reanchor_live29 = load()
_anim_reanchor_plain29 = str(_anim_reanchor_live29.get("msgid"))
check("29j animator re-anchor fallback preserves the old rich fold-above",
      _anim_reanchor_plain29 not in ("", "None", "rich-anim-reanchor-29")
      and str(_anim_reanchor_live29.get("folded_above_msgid"))
      == "rich-anim-reanchor-29"
      and _anim_reanchor_live29.get("folded_above_rich") is True
      and "rich-anim-reanchor-29" not in [
          str(x) for x in (_anim_reanchor_live29.get("rich_orphans") or [])]
      and any(str(call[1]) == "rich-anim-reanchor-29"
              for call in _anim_reanchor_tg29.ops("edit_rich")),
      _detail26({"calls": _anim_reanchor_tg29.calls,
                 "state": _anim_reanchor_live29}))
_anim_reanchor_stop_ops29 = mod.record_reply(
    SDIR, _anim_reanchor_tg29, final=True)
mod._run_deferred(SDIR, _anim_reanchor_tg29, _anim_reanchor_stop_ops29)
_anim_reanchor_final29 = load()
check("29j animator fallback Stop keeps one rich recap and retires the plain box",
      str(_anim_reanchor_final29.get("msgid")) == "rich-anim-reanchor-29"
      and _anim_reanchor_final29.get("rich_box") is True
      and any(str(call[1]) == _anim_reanchor_plain29
              for call in _anim_reanchor_tg29.ops("delete"))
      and not any(str(call[1]) == "rich-anim-reanchor-29"
                  for call in _anim_reanchor_tg29.ops("delete")),
      _detail26({"ops": _anim_reanchor_stop_ops29,
                 "calls": _anim_reanchor_tg29.calls,
                 "state": _anim_reanchor_final29}))

_reset26()
_terminal_stop_mid29 = "terminal-stop-owner-29"
mod.save_state(SDIR, _rich_state28(
    mid=_terminal_stop_mid29, box_is_last=False,
    replied_this_turn=False, rich_box=True, rich_failed=False))
_terminal_stop_tg29 = _NoDeleteRich29([_fail26("api_400", 400)])
_terminal_stop_ops29 = mod.record_reply(
    SDIR, _terminal_stop_tg29, final=True)
mod._run_deferred(SDIR, _terminal_stop_tg29, _terminal_stop_ops29)
_terminal_stop_after29 = load()
check("29k terminal Stop-fold refusal retires and tracks the failed rich owner",
      not _terminal_stop_tg29.ops("send")
      and any(op[0] == "delete" and str(op[1]) == _terminal_stop_mid29
              and op[3] is True for op in _terminal_stop_ops29)
      and _terminal_stop_after29.get("msgid") is None
      and _terminal_stop_after29.get("folded") is True
      and _terminal_stop_after29.get("rich_box") is False
      and _terminal_stop_after29.get("rich_failed") is True
      and _terminal_stop_mid29 in [
          str(x) for x in (_terminal_stop_after29.get("ghosts") or [])],
      _detail26({"ops": _terminal_stop_ops29,
                 "calls": _terminal_stop_tg29.calls,
                 "state": _terminal_stop_after29}))

_reset26()
_terminal_backstop_mid29 = "terminal-backstop-owner-29"
mod.save_state(SDIR, _rich_state28(
    mid=_terminal_backstop_mid29, box_is_last=False,
    replied_this_turn=False, rich_box=True, rich_failed=False))
_terminal_backstop_tg29 = _NoDeleteRich29([_fail26("api_400", 400)])
mod._fold_to_done(SDIR, _terminal_backstop_tg29)
_terminal_backstop_after29 = load()
check("29k terminal idle-fold refusal removes the live owner or ghost-tracks it",
      not _terminal_backstop_tg29.ops("send")
      and _terminal_backstop_after29.get("msgid") is None
      and _terminal_backstop_after29.get("folded") is True
      and _terminal_backstop_after29.get("rich_box") is False
      and _terminal_backstop_after29.get("rich_failed") is True
      and len(_terminal_backstop_tg29.ops("delete_FAIL")) >= 1
      and _terminal_backstop_mid29 in [
          str(x) for x in (_terminal_backstop_after29.get("ghosts") or [])],
      _detail26({"calls": _terminal_backstop_tg29.calls,
                 "state": _terminal_backstop_after29}))

_reset26()
_enable_rich26()
_double_hook_tg29 = RichRecTG([
    _ok26("rich-double-hook-29"), _fail26("api_400", 400)],
    fail_send=True)
_double_hook_prompt_ops29 = mod.on_prompt(
    SDIR, _double_hook_tg29, prompt_text="double failed hook reanchor",
    session_id="double-hook-29")
mod._run_deferred(SDIR, _double_hook_tg29, _double_hook_prompt_ops29)
_step26(
    _double_hook_tg29, "pre", "Read",
    {"file_path": "/tmp/double-hook-before-29.txt"},
    tuid="DOUBLE-HOOK-BEFORE-29", session_id="double-hook-29")
_double_hook_reply_ops29 = mod.record_reply(
    SDIR, _double_hook_tg29, final=False)
mod._run_deferred(SDIR, _double_hook_tg29, _double_hook_reply_ops29)
_step26(
    _double_hook_tg29, "pre", "Read",
    {"file_path": "/tmp/double-hook-after-29.txt"},
    tuid="DOUBLE-HOOK-AFTER-29", session_id="double-hook-29")
_double_hook_failed29 = load()
check("29l double-failed hook reanchor keeps X rich and globally selects fallback",
      str(_double_hook_failed29.get("msgid")) == "rich-double-hook-29"
      and _double_hook_failed29.get("rich_box") is True
      and _double_hook_failed29.get("rich_failed") is True
      and not mod._rich_failed_owner(
          _double_hook_failed29, "rich-double-hook-29")
      and _double_hook_failed29.get("reanchor") is True
      and "rich-double-hook-29" not in [
          str(x) for x in (_double_hook_failed29.get("rich_orphans") or [])],
      _detail26({"calls": _double_hook_tg29.calls,
                 "state": _double_hook_failed29}))
_double_hook_tg29.fail_send = False
_double_hook_rich_sends29 = len(_double_hook_tg29.ops("send_rich"))
_step26(
    _double_hook_tg29, "pre", "Read",
    {"file_path": "/tmp/double-hook-retry-29.txt"},
    tuid="DOUBLE-HOOK-RETRY-29", session_id="double-hook-29")
_double_hook_retry29 = load()
check("29l next hook retry opens plain and preserves X as the rich fold-above",
      len(_double_hook_tg29.ops("send_rich")) == _double_hook_rich_sends29
      and str(_double_hook_retry29.get("msgid")) != "rich-double-hook-29"
      and _double_hook_retry29.get("rich_box") is False
      and str(_double_hook_retry29.get("folded_above_msgid"))
      == "rich-double-hook-29"
      and _double_hook_retry29.get("folded_above_rich") is True
      and "rich-double-hook-29" not in [
          str(x) for x in (_double_hook_retry29.get("rich_orphans") or [])],
      _detail26({"calls": _double_hook_tg29.calls,
                 "state": _double_hook_retry29}))

class _AnimatorDoubleFailTG29(RichRecTG):
    def send(self, text, markdown=False):
        self.calls.append(("send_FAIL", text, markdown))
        with open(os.path.join(STATE_DIR, "stream.off"), "w"):
            pass
        return None

_reset26()
_enable_rich26()
_double_anim_now29 = mod.time.time()
mod.save_state(SDIR, _rich_state28(
    mid="rich-double-anim-29", now=_double_anim_now29,
    reanchor=True, box_is_last=False, replied_this_turn=True,
    rich_box=True, rich_failed=False))
_double_anim_tg29 = _AnimatorDoubleFailTG29([_fail26("api_400", 400)])
_real_double_anim_sleep29 = mod.time.sleep
try:
    mod.time.sleep = lambda seconds: None
    mod.run_animator(SDIR, _double_anim_tg29)
finally:
    mod.time.sleep = _real_double_anim_sleep29
    try:
        os.remove(os.path.join(STATE_DIR, "stream.off"))
    except OSError:
        pass
_double_anim_after29 = load()
check("29l double-failed animator reanchor preserves X transport and ownership",
      str(_double_anim_after29.get("msgid")) == "rich-double-anim-29"
      and _double_anim_after29.get("rich_box") is True
      and _double_anim_after29.get("rich_failed") is True
      and not mod._rich_failed_owner(
          _double_anim_after29, "rich-double-anim-29")
      and _double_anim_after29.get("reanchor") is True
      and "rich-double-anim-29" not in [
          str(x) for x in (_double_anim_after29.get("rich_orphans") or [])],
      _detail26({"calls": _double_anim_tg29.calls,
                 "state": _double_anim_after29}))

_reset26()
_alias_refusal_mid29 = "alias-refusal-owner-29"
mod.save_state(SDIR, _rich_state28(
    mid=_alias_refusal_mid29, folded_above_msgid=_alias_refusal_mid29,
    rich_box=False, folded_above_rich=True, box_is_last=False))
_alias_refusal_tg29 = _NoDeleteRich29([_fail26("api_400", 400)])
_alias_refusal_ops29 = mod.record_reply(
    SDIR, _alias_refusal_tg29, final=True)
mod._run_deferred(SDIR, _alias_refusal_tg29, _alias_refusal_ops29)
_alias_refusal_after29 = load()
check("29m aliased terminal refusal cannot re-promote the retired rich owner",
      _alias_refusal_after29.get("msgid") is None
      and _alias_refusal_after29.get("folded_above_msgid") is None
      and _alias_refusal_after29.get("rich_box") is False
      and _alias_refusal_after29.get("folded_above_rich") is False
      and _alias_refusal_mid29 in [
          str(x) for x in (_alias_refusal_after29.get("ghosts") or [])],
      _detail26({"ops": _alias_refusal_ops29,
                 "calls": _alias_refusal_tg29.calls,
                 "state": _alias_refusal_after29}))

_reset26()
_auto_retry_mid29 = "plain-auto-retry-29"
mod.save_state(SDIR, _rich_state28(
    mid=_auto_retry_mid29,
    steps=[{"fam": "files", "cap": "automatic retry work",
            "tuid": "AUTO-RETRY-29", "done": True, "k": "", "aid": "",
            "t0": mod.time.time() - 8, "secs": 3}],
    folded=True, box_is_last=True, replied_this_turn=False,
    noreply_grace_until=mod.time.time() - 1,
    rich_box=False, rich_failed=True, rich_health_logged=True,
    prev_recap_msgid="rich-auto-retry-recap-29",
    prev_recap_text=_prior_recap28, prev_recap_rich=True))
_auto_retry_tg29 = RichRecTG([
    _fail26("api_400", 400), _ok26("rich-auto-retry-recap-29")])
_real_auto_retry_sleep29 = mod.time.sleep
try:
    mod.time.sleep = lambda seconds: None
    mod.run_animator(SDIR, _auto_retry_tg29)
finally:
    mod.time.sleep = _real_auto_retry_sleep29
_auto_retry_after29 = load()
_auto_retry_edits29 = [
    call for call in _auto_retry_tg29.ops("edit_rich")
    if str(call[1]) == "rich-auto-retry-recap-29"]
check("29n animator retries a refused rich grace merge without another hook edge",
      len(_auto_retry_edits29) == 2
      and any(str(call[1]) == _auto_retry_mid29
              for call in _auto_retry_tg29.ops("delete"))
      and _auto_retry_after29.get("msgid") is None
      and not (_auto_retry_after29.get("steps") or []),
      _detail26({"calls": _auto_retry_tg29.calls,
                 "state": _auto_retry_after29}))

_reset26()
_completion_probe_now29 = mod.time.time()
mod.save_state(SDIR, _rich_state28(
    mid="completion-probe-29", now=_completion_probe_now29,
    last_activity=_completion_probe_now29 - mod.ANIM_MAX_IDLE - 5,
    last_react=0, rich_box=True, rich_failed=False))
_completion_probe_calls29 = []
_real_fold_probe29 = mod._fold_to_done
_real_sleep_probe29 = mod.time.sleep

def _fold_probe29(sdir, tg):
    del tg
    _completion_probe_calls29.append(len(_completion_probe_calls29) + 1)
    if len(_completion_probe_calls29) >= 3:
        state = mod.load_state(sdir)
        state["folded"] = True
        mod.save_state(sdir, state)
        return True
    return False

try:
    mod._fold_to_done = _fold_probe29
    mod.time.sleep = lambda seconds: None
    mod.run_animator(SDIR, RecTG())
finally:
    mod._fold_to_done = _real_fold_probe29
    mod.time.sleep = _real_sleep_probe29
check("29n animator breaks only after the idle-fold helper reports completion",
      _completion_probe_calls29 == [1, 2, 3],
      _detail26(_completion_probe_calls29))

_reset26()
_enable_rich26()
_stale_fold_now29 = mod.time.time()
mod.save_state(SDIR, _rich_state28(
    mid="generation-fold-X-29", now=_stale_fold_now29,
    reanchor=True, box_is_last=False, replied_this_turn=True,
    rich_box=True, rich_failed=False))
_stale_fold_tg29 = RichRecTG([
    _ok26("generation-fold-Y-29"), _ok26("generation-fold-X-29")])
_stale_fold_ops29 = mod.handle_step(
    SDIR, _stale_fold_tg29, "pre", "Read",
    {"file_path": "/tmp/generation-fold-new-29.txt"},
    "GENERATION-FOLD-NEW-29", None, None, session_id="generation-fold-29")
_stale_fold_stop_ops29 = mod.record_reply(
    SDIR, _stale_fold_tg29, final=True)
mod._run_deferred(SDIR, _stale_fold_tg29, _stale_fold_stop_ops29)
mod._run_deferred(SDIR, _stale_fold_tg29, _stale_fold_ops29)
_stale_fold_x_edits29 = [
    call for call in _stale_fold_tg29.ops("edit_rich")
    if str(call[1]) == "generation-fold-X-29"]
check("29o delayed reanchor rfold cannot overwrite the completed Stop recap",
      len(_stale_fold_x_edits29) == 1
      and "generation-fold-new-29.txt" in _stale_fold_x_edits29[-1][2]
      and mod._is_rich_recap_text(_stale_fold_x_edits29[-1][2]),
      _detail26({"ops": _stale_fold_ops29,
                 "calls": _stale_fold_tg29.calls}))

for _compound_label29, _compound_above_rich29 in (
        ("rrefold", True), ("mixed", False)):
    _reset26()
    _enable_rich26()
    _compound_x29 = "generation-%s-X-29" % _compound_label29
    _compound_a29 = "generation-%s-A-29" % _compound_label29
    _compound_y29 = "generation-%s-Y-29" % _compound_label29
    mod.save_state(SDIR, _rich_state28(
        mid=_compound_x29, folded_above_msgid=_compound_a29,
        folded_above_rich=_compound_above_rich29,
        reanchor=True, box_is_last=False, replied_this_turn=True,
        rich_box=True, rich_failed=False))
    _compound_tg29 = RichRecTG([
        _ok26(_compound_y29), _ok26(_compound_x29)])
    _compound_ops29 = mod.handle_step(
        SDIR, _compound_tg29, "pre", "Read",
        {"file_path": "/tmp/generation-%s-new-29.txt" % _compound_label29},
        "GENERATION-%s-NEW-29" % _compound_label29.upper(),
        None, None, session_id="generation-%s-29" % _compound_label29)
    _compound_stop_ops29 = mod.record_reply(
        SDIR, _compound_tg29, final=True)
    mod._run_deferred(SDIR, _compound_tg29, _compound_stop_ops29)
    mod._run_deferred(SDIR, _compound_tg29, _compound_ops29)
    _compound_x_edits29 = [
        call for call in _compound_tg29.ops("edit_rich")
        if str(call[1]) == _compound_x29]
    check("29o stale %s cannot delete or rewrite finalized owners"
          % _compound_label29,
          len(_compound_x_edits29) == 1
          and not any(str(call[1]) == _compound_a29
                      for call in _compound_tg29.ops("delete"))
          and "generation-%s-new-29.txt" % _compound_label29
          in _compound_x_edits29[-1][2],
          _detail26({"ops": _compound_ops29,
                     "calls": _compound_tg29.calls}))

_reset26()
_generation_recap_mid29 = "generation-recap-X-29"
mod.save_state(SDIR, _rich_state28(
    mid="generation-recap-P1-29", rich_box=False, rich_failed=True,
    rich_health_logged=True, box_is_last=True, replied_this_turn=False,
    steps=[{"fam": "files", "cap": "generation recap A",
            "tuid": "GEN-RECAP-A-29", "done": True, "k": "", "aid": "",
            "t0": mod.time.time() - 8, "secs": 3}],
    prev_recap_msgid=_generation_recap_mid29,
    prev_recap_text=_prior_recap28, prev_recap_rich=True))
_generation_recap_tg29 = RichRecTG()
_generation_ops_b29 = mod.on_prompt(
    SDIR, _generation_recap_tg29, prompt_text="generation boundary B",
    session_id="generation-recap-29")
_generation_state_b29 = load()
_generation_state_b29.update({
    "steps": [{"fam": "files", "cap": "generation recap B",
               "tuid": "GEN-RECAP-B-29", "done": True, "k": "", "aid": "",
               "t0": mod.time.time() - 4, "secs": 2}],
    "box_is_last": True, "replied_this_turn": False, "folded": False,
})
mod.save_state(SDIR, _generation_state_b29)
_generation_ops_c29 = mod.on_prompt(
    SDIR, _generation_recap_tg29, prompt_text="generation boundary C",
    session_id="generation-recap-29")
mod._run_deferred(SDIR, _generation_recap_tg29, _generation_ops_c29)
mod._run_deferred(SDIR, _generation_recap_tg29, _generation_ops_b29)
_generation_recap_edits29 = [
    call for call in _generation_recap_tg29.ops("edit_rich")
    if str(call[1]) == _generation_recap_mid29]
check("29o reverse prompt rrecaps leave only the newest merged generation",
      len(_generation_recap_edits29) == 1
      and "generation recap A" in _generation_recap_edits29[-1][2]
      and "generation recap B" in _generation_recap_edits29[-1][2],
      _detail26({"ops_b": _generation_ops_b29,
                 "ops_c": _generation_ops_c29,
                 "calls": _generation_recap_tg29.calls}))

_reset26()
_enable_rich26()
_owner_survivor_x29 = "owner-survivor-X-29"
mod.save_state(SDIR, _rich_state28(
    mid="owner-fallback-P-29", rich_box=False, rich_failed=True,
    rich_failed_msgid="failed-owner-Y-29", rich_health_logged=True,
    folded_above_msgid=_owner_survivor_x29, folded_above_rich=True,
    box_is_last=True, replied_this_turn=True))
_owner_survivor_tg29 = RichRecTG([_ok26(_owner_survivor_x29)])
_owner_survivor_stop_ops29 = mod.record_reply(
    SDIR, _owner_survivor_tg29, final=True)
mod._run_deferred(SDIR, _owner_survivor_tg29, _owner_survivor_stop_ops29)
_owner_survivor_promoted29 = load()
_owner_survivor_step_ops29 = mod.handle_step(
    SDIR, _owner_survivor_tg29, "pre", "Read",
    {"file_path": "/tmp/owner-continuation-29.txt"},
    "OWNER-CONTINUE-29", None, None, session_id="owner-continuation-29")
mod._run_deferred(SDIR, _owner_survivor_tg29, _owner_survivor_step_ops29)
_owner_survivor_after29 = load()
check("29p promoted healthy recap keeps failed-owner identity during continuation",
      str(_owner_survivor_promoted29.get("msgid")) == _owner_survivor_x29
      and not mod._rich_failed_owner(
          _owner_survivor_promoted29, _owner_survivor_x29)
      and str(_owner_survivor_after29.get("folded_above_msgid"))
      == _owner_survivor_x29
      and _owner_survivor_after29.get("folded_above_rich") is True
      and _owner_survivor_after29.get("rich_box") is False
      and _owner_survivor_x29 not in [
          str(x) for x in (_owner_survivor_after29.get("rich_orphans") or [])]
      and not [call for call in _owner_survivor_tg29.ops("send_rich")
               if str(call[1]) != _owner_survivor_x29],
      _detail26({"stop_ops": _owner_survivor_stop_ops29,
                 "step_ops": _owner_survivor_step_ops29,
                 "calls": _owner_survivor_tg29.calls,
                 "state": _owner_survivor_after29}))

_reset26()
_backstop_survivor_x29 = "backstop-survivor-X-29"
_backstop_failed_y29 = "backstop-failed-Y-29"
mod.save_state(SDIR, _rich_state28(
    mid=_backstop_failed_y29, rich_box=True, rich_failed=False,
    folded_above_msgid=_backstop_survivor_x29, folded_above_rich=True,
    box_is_last=False, replied_this_turn=False))
_backstop_survivor_tg29 = RichRecTG([_fail26("api_400", 400)])
mod._fold_to_done(SDIR, _backstop_survivor_tg29)
_backstop_survivor_after29 = load()
check("29p idle terminal refusal promotes the distinct healthy fold-above survivor",
      str(_backstop_survivor_after29.get("msgid"))
      == _backstop_survivor_x29
      and _backstop_survivor_after29.get("rich_box") is True
      and _backstop_survivor_after29.get("folded") is True
      and _backstop_survivor_after29.get("folded_above_msgid") is None
      and any(str(call[1]) == _backstop_failed_y29
              for call in _backstop_survivor_tg29.ops("delete"))
      and not any(str(call[1]) == _backstop_survivor_x29
                  for call in _backstop_survivor_tg29.ops("delete")),
      _detail26({"calls": _backstop_survivor_tg29.calls,
                 "state": _backstop_survivor_after29}))

print("\n===== #30 (v30 fix cycle 2) pre-fleet hardening =====")

def _lock_available30():
    handle = None
    acquired = False
    try:
        handle = open(os.path.join(SDIR, "lock"), "w")
        mod.fcntl.flock(handle, mod.fcntl.LOCK_EX | mod.fcntl.LOCK_NB)
        acquired = True
        return True
    except Exception:
        return False
    finally:
        if handle is not None:
            if acquired:
                try:
                    mod.fcntl.flock(handle, mod.fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                handle.close()
            except Exception:
                pass

class _LockProbeTG30(RichRecTG):
    def __init__(self, rich_results=None, delete_results=None):
        super().__init__(rich_results=rich_results)
        self.lock_checks = []
        self.delete_results = dict(delete_results or {})

    def _probe(self, operation, msgid=None):
        available = _lock_available30()
        self.lock_checks.append((operation, str(msgid) if msgid is not None else None,
                                 available))

    def send(self, text, markdown=False):
        self._probe("send")
        return super().send(text, markdown=markdown)

    def edit(self, msgid, text, markdown=False):
        self._probe("edit", msgid)
        return super().edit(msgid, text, markdown=markdown)

    def edit_rich(self, msgid, markdown):
        self._probe("edit_rich", msgid)
        return super().edit_rich(msgid, markdown)

    def delete(self, msgid):
        self._probe("delete", msgid)
        result = self.delete_results.get(str(msgid), True)
        self.calls.append(("delete" if result else "delete_FAIL", str(msgid)))
        return result

_reset26()
_enable_rich26()
_deferred_owner30 = "deferred-lock-owner-30"
mod.save_state(SDIR, _rich_state28(
    mid=_deferred_owner30, rich_box=True, rich_failed=False,
    rich_session_id="deferred-lock-30"))
_deferred_lock_tg30 = _LockProbeTG30([_fail26("api_400", 400)])
mod._rich_deferred_edit(
    SDIR, _deferred_lock_tg30, _deferred_owner30, "LIVE-LOCK-30",
    live_fallback=True, intent="live")
_deferred_lock_after30 = load()
_deferred_transport_checks30 = [
    row for row in _deferred_lock_tg30.lock_checks
    if row[0] in ("edit_rich", "send")]
check("30a rich deferred edit and fallback send both run outside sdir lock",
      [row[0] for row in _deferred_transport_checks30]
      == ["edit_rich", "send"]
      and all(row[2] for row in _deferred_transport_checks30),
      _detail26({"locks": _deferred_lock_tg30.lock_checks,
                 "calls": _deferred_lock_tg30.calls}))
check("30a unlocked fallback commit tracks the old rich owner and new plain box",
      str(_deferred_lock_after30.get("msgid")) != _deferred_owner30
      and _deferred_lock_after30.get("rich_box") is False
      and _deferred_owner30 in [
          str(x) for x in (_deferred_lock_after30.get("rich_orphans") or [])],
      _detail26(_deferred_lock_after30))

_reset26()
_compound_stale30 = "compound-stale-30"
_compound_new30 = "compound-new-30"
_compound_generation30 = 30
mod.save_state(SDIR, _rich_state28(
    mid=_compound_new30, folded_above_msgid=_compound_new30,
    folded_above_rich=True, rich_op_seq=_compound_generation30,
    rich_op_latest={_compound_stale30: _compound_generation30,
                    _compound_new30: _compound_generation30}))
_compound_lock_tg30 = _LockProbeTG30(
    [_ok26(_compound_stale30)],
    delete_results={_compound_stale30: False, _compound_new30: True})
_compound_result30 = mod._rich_compound_refold(
    SDIR, _compound_lock_tg30, "rrefold", _compound_stale30,
    (_compound_new30, "COMPOUND-FOLD-30"), _compound_generation30,
    (_compound_stale30, _compound_new30))
_compound_after30 = load()
check("30a compound rich refold runs every delete and edit outside sdir lock",
      _compound_result30 is True
      and len(_compound_lock_tg30.lock_checks) >= 4
      and all(row[2] for row in _compound_lock_tg30.lock_checks),
      _detail26({"locks": _compound_lock_tg30.lock_checks,
                 "calls": _compound_lock_tg30.calls}))
check("30a unlocked compound commit preserves the one-recap pointer decision",
      str(_compound_after30.get("folded_above_msgid")) == _compound_stale30,
      _detail26(_compound_after30))

_reset26()
_animator_unlock_now30 = mod.time.time()
mod.save_state(SDIR, _rich_state28(
    mid="animator-finally-owner-30", now=_animator_unlock_now30,
    last_edit=0, last_live="stale animator frame", frame=0,
    rich_box=True, rich_failed=False))
_animator_unlock_probe30 = []
_animator_save_raised30 = [False]
_real_animator_save30 = mod.save_state
_real_animator_deferred30 = mod._rich_deferred_edit
_real_animator_sleep30 = mod.time.sleep

def _raising_animator_save30(sdir, state):
    if not _animator_save_raised30[0]:
        _animator_save_raised30[0] = True
        raise RuntimeError("fixture save failure after do_edit")
    return _real_animator_save30(sdir, state)

def _probe_animator_deferred30(*args, **kwargs):
    del args, kwargs
    _animator_unlock_probe30.append(_lock_available30())
    with open(os.path.join(STATE_DIR, "stream.off"), "w"):
        pass
    return False

try:
    mod.save_state = _raising_animator_save30
    mod._rich_deferred_edit = _probe_animator_deferred30
    mod.time.sleep = lambda seconds: None
    mod.run_animator(SDIR, RichRecTG())
finally:
    mod.save_state = _real_animator_save30
    mod._rich_deferred_edit = _real_animator_deferred30
    mod.time.sleep = _real_animator_sleep30
    try:
        os.remove(os.path.join(STATE_DIR, "stream.off"))
    except OSError:
        pass
check("30b animator releases sdir lock in finally before deferred rich edit",
      _animator_save_raised30 == [True]
      and _animator_unlock_probe30 == [True],
      _detail26({"raised": _animator_save_raised30,
                 "lock_probe": _animator_unlock_probe30}))

_reset26()
mod.save_state(SDIR, _rich_state28(mid="recap-current-owner-30"))
_recap_refusal_tg30 = RichRecTG([_fail26("api_400", 400)])
mod._run_deferred(
    SDIR, _recap_refusal_tg30,
    [("rrecap", "valid-rich-recap-30", "RICH-RECAP-30", True)])
_recap_refusal_after30 = load()
check("30c failed rrecap leaves the existing valid recap out of ghost cleanup",
      "valid-rich-recap-30" not in [
          str(x) for x in (_recap_refusal_after30.get("ghosts") or [])],
      _detail26({"calls": _recap_refusal_tg30.calls,
                 "state": _recap_refusal_after30}))

_reset26()
mod.save_state(SDIR, _rich_state28(mid="fold-current-owner-30"))
_fold_refusal_tg30 = RichRecTG([_fail26("api_400", 400)])
mod._run_deferred(
    SDIR, _fold_refusal_tg30,
    [("rfold", "failed-rich-fold-30", "RICH-FOLD-30", True)])
_fold_refusal_after30 = load()
check("30c failed rfold remains ghost-tracked for later cleanup",
      "failed-rich-fold-30" in [
          str(x) for x in (_fold_refusal_after30.get("ghosts") or [])],
      _detail26({"calls": _fold_refusal_tg30.calls,
                 "state": _fold_refusal_after30}))

_reset26()
_enable_rich26()
_live_refusal_owner30 = "failed-live-redit-30"
mod.save_state(SDIR, _rich_state28(mid=_live_refusal_owner30))
_live_refusal_tg30 = RichRecTG([_fail26("api_400", 400)])
mod._run_deferred(
    SDIR, _live_refusal_tg30,
    [("redit", _live_refusal_owner30, "LIVE-REFUSAL-30", False)])
_live_refusal_after30 = load()
check("30c failed live redit keeps its abandoned owner tracked for cleanup",
      _live_refusal_owner30 in [
          str(x) for x in (_live_refusal_after30.get("rich_orphans") or [])],
      _detail26({"calls": _live_refusal_tg30.calls,
                 "state": _live_refusal_after30}))

with open(MOD_PATH, encoding="utf-8") as _deferred_source30:
    _deferred_tree30 = ast.parse(_deferred_source30.read())
_deferred_node30 = next(node for node in _deferred_tree30.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_run_deferred")
if (isinstance(_deferred_node30.body[0], ast.Expr)
        and isinstance(_deferred_node30.body[0].value, ast.Constant)
        and isinstance(_deferred_node30.body[0].value.value, str)):
    _deferred_node30.body = _deferred_node30.body[1:]
_deferred_hash30 = _behavior_hash(_deferred_node30)
check("30d all deferred-op handlers and tuple fields preserve their executable contract",
      _deferred_hash30 == "81d1f142afcbe5bfd1ecbde1bda96290e581fe0da8dfcd5b144daaddeab2beb1", _deferred_hash30)

_reset26()
_flag_off_health_state30 = _rich_state28(mid="flag-off-health-30")
_flag_off_health_tg30 = RecTG()
_flag_off_health_result30 = mod._cold_start_plain_state(
    SDIR, _flag_off_health_tg30, _flag_off_health_state30, "api_400")
_flag_off_health_path30 = os.path.join(SDIR, "rich-health.log")
check("30e guarded cold start cannot create rich-health log while flag is absent",
      _flag_off_health_result30 is None
      and not os.path.exists(_flag_off_health_path30)
      and _flag_off_health_state30.get("rich_health_logged") is not True
      and not _flag_off_health_tg30.calls,
      _detail26({"state": _flag_off_health_state30,
                 "calls": _flag_off_health_tg30.calls}))
_flag_off_health_state30["rich_health_logged"] = False
mod._append_rich_health(SDIR, _flag_off_health_state30, "api_401")
check("30e direct health logger also refuses every flag-off artifact",
      not os.path.exists(_flag_off_health_path30)
      and _flag_off_health_state30.get("rich_health_logged") is not True,
      _detail26(_flag_off_health_state30))

_long_subject_create30 = "C" * 5000
_subject_plan30, _subject_create_err30 = _plan_call26(
    [], "TaskCreate", {"subject": _long_subject_create30},
    {"task": {"id": "subject-cap-30", "subject": _long_subject_create30}})
_subject_created30 = _row26(_subject_plan30, "subject-cap-30")
check("30f TaskCreate stores at most 200 subject characters",
      not _subject_create_err30
      and _row_subject26(_subject_created30) == _long_subject_create30[:200],
      _subject_create_err30 or _detail26(_subject_created30))

_long_subject_response30 = "R" * 5000
_subject_plan30, _subject_response_err30 = _plan_call26(
    _subject_plan30, "TaskUpdate",
    {"taskId": "subject-cap-30", "subject": "ignored input"},
    {"success": True, "taskId": "subject-cap-30",
     "updatedFields": ["subject"],
     "task": {"id": "subject-cap-30", "subject": _long_subject_response30}})
check("30f TaskUpdate caps a response-snapshot subject at 200 characters",
      not _subject_response_err30
      and _row_subject26(_row26(_subject_plan30, "subject-cap-30"))
      == _long_subject_response30[:200],
      _subject_response_err30 or _detail26(_subject_plan30))

_long_subject_input30 = "I" * 5000
_subject_plan30, _subject_input_err30 = _plan_call26(
    _subject_plan30, "TaskUpdate",
    {"taskId": "subject-cap-30", "subject": _long_subject_input30},
    {"success": True, "taskId": "subject-cap-30",
     "updatedFields": ["subject"]})
check("30f TaskUpdate caps a confirmed input subject at 200 characters",
      not _subject_input_err30
      and _row_subject26(_row26(_subject_plan30, "subject-cap-30"))
      == _long_subject_input30[:200],
      _subject_input_err30 or _detail26(_subject_plan30))

_reset26()
_visibility_session30 = "plan-visibility-session-30"
_visibility_tg30 = RichRecTG()
_visibility_prompt_ops30, _visibility_prompt_err30 = _prompt26(
    _visibility_tg30, "flag-off task projection", _visibility_session30)
_visibility_plain_mid30 = str(load().get("msgid"))
_visibility_create_ops30, _visibility_create_err30 = _step26(
    _visibility_tg30, "post", "TaskCreate",
    {"subject": "Flag-off real task", "description": "Persist this real task"},
    tuid="PLAN-VIS-CREATE-30",
    tresponse={"task": {"id": "plan-visible-30",
                         "subject": "Flag-off real task"}},
    session_id=_visibility_session30)
_visibility_created30 = load()
check("30g flag-off TaskCreate persists a transport-neutral session projection",
      not _visibility_prompt_err30 and not _visibility_create_err30
      and _row_subject26(_row26(
          _state_plan26(_visibility_created30), "plan-visible-30"))
      == "Flag-off real task"
      and str(_visibility_created30.get("msgid")) == _visibility_plain_mid30
      and not _visibility_created30.get("rich_turn")
      and not mod._rich_state_present(_visibility_created30)
      and not _visibility_tg30.ops("send_rich")
      and not _visibility_tg30.ops("edit_rich"),
      _visibility_prompt_err30 or _visibility_create_err30
      or _detail26({"ops": _visibility_create_ops30,
                    "state": _visibility_created30,
                    "calls": _visibility_tg30.calls}))

_visibility_carry_ops30, _visibility_carry_err30 = _prompt26(
    _visibility_tg30, "another flag-off turn", _visibility_session30)
_visibility_carried30 = load()
check("30g a flag-off boundary carries the prior active plan (v30.4)",
      not _visibility_carry_err30
      and _row_status26(_row26(
          _state_plan26(_visibility_carried30), "plan-visible-30")) == "pending"
      and not _visibility_carried30.get("rich_turn")
      and not mod._rich_state_present(_visibility_carried30),
      _visibility_carry_err30
      or _detail26({"ops": _visibility_carry_ops30,
                    "state": _visibility_carried30}))

_visibility_update_ops30, _visibility_update_err30 = _step26(
    _visibility_tg30, "post", "TaskUpdate",
    {"taskId": "plan-visible-30", "status": "completed"},
    tuid="PLAN-VIS-UPDATE-30",
    tresponse={"success": True, "taskId": "plan-visible-30",
               "updatedFields": ["status"],
               "statusChange": {"from": "pending", "to": "completed"}},
    session_id=_visibility_session30)
_visibility_updated30 = load()
check("30g flag-off TaskUpdate completes a carried projected task",
      not _visibility_update_err30
      and _row_status26(_row26(
          _state_plan26(_visibility_updated30), "plan-visible-30")) == "completed"
      and not _visibility_tg30.ops("send_rich")
      and not _visibility_tg30.ops("edit_rich"),
      _visibility_update_err30
      or _detail26({"ops": _visibility_update_ops30,
                    "state": _visibility_updated30,
                    "calls": _visibility_tg30.calls}))

_enable_rich26()
_visibility_rich_ops30, _visibility_rich_err30 = _prompt26(
    _visibility_tg30, "pilot turns rich", _visibility_session30)
_visibility_rich30 = load()
_visibility_rich_frames30 = [call[2] for call in _visibility_tg30.ops("send_rich")]
check("30g enabling rich stays empty after finished tasks cleared (v30.4)",
      not _visibility_rich_err30
      and _visibility_rich30.get("rich_turn") is True
      and _state_plan26(_visibility_rich30) == []
      and _visibility_rich_frames30
      and "Flag-off real task" not in _visibility_rich_frames30[-1]

      and "📋 Plan · 0/0 done" in _visibility_rich_frames30[-1]
      and "nothing logged yet" in _visibility_rich_frames30[-1]
      and "🔧 Tools · 0 calls" in _visibility_rich_frames30[-1],
      _visibility_rich_err30
      or _detail26({"ops": _visibility_rich_ops30,
                    "state": _visibility_rich30,
                    "frames": _visibility_rich_frames30[-1:]}))

_visibility_new_ops30, _visibility_new_err30 = _prompt26(
    _visibility_tg30, "a different session", "plan-visibility-new-session-30")
_visibility_new30 = load()
check("30g a different session does not inherit another session task projection",
      not _visibility_new_err30
      and not _state_plan26(_visibility_new30),
      _visibility_new_err30
      or _detail26({"ops": _visibility_new_ops30,
                    "state": _visibility_new30}))

print("\n===== #31 agent completion notifications close panel rows =====")

def _agent_notification31(tuid, summary, task_id="agent-task-31"):
    return (
        "<task-notification>\n"
        "<task-id>%s</task-id>\n"
        "<tool-use-id>%s</tool-use-id>\n"
        "<summary>%s</summary>\n"
        "</task-notification>"
    ) % (task_id, tuid, summary)

def _panel_row31(state, tuid):
    return next((row for row in (state.get("panel") or [])
                 if row.get("tuid") == tuid), {})

_reset26()
_enable_rich26()
_clock31 = _Clock26(31000.0)
_real_time31 = mod.time.time
try:
    mod.time.time = _clock31.time
    _tg31 = RichRecTG()
    _, _prompt_err31 = _prompt26(
        _tg31, "dispatch two background agents", "agent-notification-session-31")
    _agent_input31 = {
        "description": "Audit box for leftover styles",
        "subagent_type": "explorer",
        "model": "gpt-5.6-sol",
        "run_in_background": True,
    }
    _clock31.advance(4)
    _, _pre_ok_err31 = _step26(
        _tg31, "pre", "Agent", _agent_input31, tuid="AGENT-OK-31",
        session_id="agent-notification-session-31")
    _clock31.advance(1)
    _, _post_ok_err31 = _step26(
        _tg31, "post", "Agent", _agent_input31, tuid="AGENT-OK-31",
        tresponse={"result": "Async agent launched successfully. agentId: ok31"},
        session_id="agent-notification-session-31")
    _failed_input31 = dict(_agent_input31, description="Probe the failure path")
    _clock31.advance(2)
    _, _pre_fail_err31 = _step26(
        _tg31, "pre", "Agent", _failed_input31, tuid="AGENT-FAIL-31",
        session_id="agent-notification-session-31")
    _clock31.advance(1)
    _, _post_fail_err31 = _step26(
        _tg31, "post", "Agent", _failed_input31, tuid="AGENT-FAIL-31",
        tresponse={"result": "Async agent launched successfully. agentId: fail31"},
        session_id="agent-notification-session-31")

    _before_notify31 = load()
    _before_ok_row31 = _panel_row31(_before_notify31, "AGENT-OK-31")
    _before_mid31 = str(_before_notify31.get("msgid"))
    _before_start31 = _before_notify31.get("proc_start")
    _before_sends31 = len(_rich_ops26(_tg31, "send_rich"))
    _before_edits31 = len(_rich_ops26(_tg31, "edit_rich"))
    _clock31.advance(149)
    _finished_prompt31 = _agent_notification31(
        "AGENT-OK-31", 'Agent "Audit box for leftover styles" finished')
    _finished_ops31, _finished_err31 = _prompt26(
        _tg31, _finished_prompt31, "agent-notification-session-31")
    _after_finished31 = load()
    _ok_row31 = _panel_row31(_after_finished31, "AGENT-OK-31")
    _fail_open_row31 = _panel_row31(_after_finished31, "AGENT-FAIL-31")

    check("31a finished agent notification closes only its exact row with real duration",
          not any((_prompt_err31, _pre_ok_err31, _post_ok_err31,
                   _pre_fail_err31, _post_fail_err31, _finished_err31))
          and _before_ok_row31.get("done") is False
          and _ok_row31.get("done") is True
          and _ok_row31.get("outcome") == "completed"
          and abs(float(_ok_row31.get("secs", -1)) - 153.0) < 0.001
          and _fail_open_row31.get("done") is False,
          _finished_err31 or _detail26({"before": _before_ok_row31,
                                        "after": _ok_row31,
                                        "sibling": _fail_open_row31}))
    check("31b matched agent notification repaints the same box without a turn boundary",
          not _finished_err31
          and str(_after_finished31.get("msgid")) == _before_mid31
          and _after_finished31.get("proc_start") == _before_start31
          and len(_rich_ops26(_tg31, "send_rich")) == _before_sends31
          and len(_rich_ops26(_tg31, "edit_rich")) == _before_edits31 + 1
          and any(op[0] == "redit" and str(op[1]) == _before_mid31
                  for op in _finished_ops31),
          _finished_err31 or _detail26({"ops": _finished_ops31,
                                        "calls": _tg31.calls[-4:],
                                        "state": _after_finished31}))

    _before_dup31 = load()
    _before_dup_calls31 = list(_tg31.calls)
    _dup_ops31, _dup_err31 = _prompt26(
        _tg31, _finished_prompt31, "agent-notification-session-31")
    _after_dup31 = load()
    check("31c duplicate agent notification is a no-op and leaves its sibling open",
          not _dup_err31 and _dup_ops31 == []
          and _after_dup31 == _before_dup31
          and _tg31.calls == _before_dup_calls31
          and _panel_row31(_after_dup31, "AGENT-FAIL-31").get("done") is False,
          _dup_err31 or _detail26({"ops": _dup_ops31,
                                   "before": _before_dup31,
                                   "after": _after_dup31}))

    _unmatched_state31 = json.loads(json.dumps(_after_dup31))
    _unmatched_before31 = json.loads(json.dumps(_unmatched_state31))
    _unmatched_result31 = mod._close_codex_notification(
        SDIR, _unmatched_state31,
        _agent_notification31(
            "AGENT-MISSING-31", 'Agent "Unknown dispatch" finished',
            task_id="agent-missing-31"),
        tg=_tg31)
    check("31d unmatched agent notification leaves the existing close path untouched",
          _unmatched_result31 is None
          and _unmatched_state31 == _unmatched_before31,
          _detail26({"result": _unmatched_result31,
                     "before": _unmatched_before31,
                     "after": _unmatched_state31}))

    _open_fold31, _open_fold_err31 = _render_final26(
        SDIR, _after_dup31.get("steps") or [], _after_dup31.get("proc_start"),
        mission="Agent notification regression",
        panel=_after_dup31.get("panel"), plan=_state_plan26(_after_dup31),
        outcome="done")
    _open_fold_text31 = _text26(_open_fold31)

    check("31e a known background row stays visibly live when a boundary folds",
          not _open_fold_err31
          and "◉ Probe the failure p" in _open_fold_text31
          and "(GPT-5.6 Sol)" in _open_fold_text31
          and "| \u2014 |" not in _open_fold_text31
          and "■ Probe the failure p" not in _open_fold_text31,
          _open_fold_err31 or _detail26(_open_fold_text31))

    _clock31.advance(43)
    _failed_ops31, _failed_err31 = _prompt26(
        _tg31,
        _agent_notification31(
            "AGENT-FAIL-31", 'Agent "Probe the failure path" failed',
            task_id="agent-failed-31"),
        "agent-notification-session-31")
    _after_failed31 = load()
    _failed_row31 = _panel_row31(_after_failed31, "AGENT-FAIL-31")
    check("31f failed agent summary closes its exact row with the honest failure glyph",
          not _failed_err31
          and _failed_row31.get("done") is True
          and _failed_row31.get("outcome") == "failed"
          and abs(float(_failed_row31.get("secs", -1)) - 193.0) < 0.001
          and mod._rich_lane_state(_failed_row31) == "! fail"
          and any(op[0] == "redit" for op in _failed_ops31),
          _failed_err31 or _detail26({"ops": _failed_ops31,
                                      "row": _failed_row31}))
finally:
    mod.time.time = _real_time31

_reset26()

print("\n===== #32 active task plans survive prompt boundaries =====")

def _create_task32(tg, task_id, subject, description, active_form, session_id):
    return _step26(
        tg, "post", "TaskCreate",
        {"subject": subject, "description": description,
         "activeForm": active_form},
        tuid="CREATE-" + task_id,
        tresponse={"task": {"id": task_id, "subject": subject}},
        session_id=session_id)

def _set_status32(tg, task_id, old_status, new_status, session_id):
    return _step26(
        tg, "post", "TaskUpdate",
        {"taskId": task_id, "status": new_status},
        tuid="UPDATE-%s-%s" % (task_id, new_status),
        tresponse={
            "success": True, "taskId": task_id, "updatedFields": ["status"],
            "statusChange": {"from": old_status, "to": new_status},
        }, session_id=session_id)

_reset26()
_enable_rich26()
_boundary_session32 = "boundary-session-32"
_boundary_tg32 = RichRecTG()
_, _boundary_open_err32 = _prompt26(
    _boundary_tg32, "build a multi-turn checklist", _boundary_session32)
_boundary_errors32 = [_boundary_open_err32]
for _task32 in (
        ("pending-32", "First pending", "pending description", "Keeping first pending"),
        ("running-32", "Second running", "running description", "Running second"),
        ("finished-32", "Third finished", "finished description", "Finishing third"),
        ("cancelled-32", "Fourth cancelled", "cancelled description", "Cancelling fourth")):
    _, _create_err32 = _create_task32(
        _boundary_tg32, *_task32, _boundary_session32)
    _boundary_errors32.append(_create_err32)
for _task_id32, _old32, _new32 in (
        ("running-32", "pending", "in_progress"),
        ("finished-32", "pending", "completed"),
        ("cancelled-32", "pending", "cancelled")):
    _, _status_err32 = _set_status32(
        _boundary_tg32, _task_id32, _old32, _new32, _boundary_session32)
    _boundary_errors32.append(_status_err32)
for _mode32 in ("pre", "post"):
    _, _prior_work_err32 = _step26(
        _boundary_tg32, _mode32, "Read",
        {"file_path": "/tmp/boundary-plan-32.txt"}, tuid="PRIOR-WORK-32",
        session_id=_boundary_session32)
    _boundary_errors32.append(_prior_work_err32)
try:
    mod._run_deferred(
        SDIR, _boundary_tg32,
        mod.record_reply(SDIR, _boundary_tg32, final=False))
    _boundary_errors32.append("")
except Exception as _prior_reply_exc32:
    _boundary_errors32.append(
        "%s: %s" % (type(_prior_reply_exc32).__name__, str(_prior_reply_exc32)[:180]))

_boundary_before32 = load()
_boundary_prior_mid32 = str(_boundary_before32.get("msgid"))
_boundary_active_before32 = [
    dict(_row26(_state_plan26(_boundary_before32), task_id))
    for task_id in ("pending-32", "running-32")
]
_, _boundary_prompt_err32 = _prompt26(
    _boundary_tg32, "continue the active checklist", _boundary_session32)
_boundary_after32 = load()
_boundary_plan32 = _state_plan26(_boundary_after32)
_boundary_live_frames32 = [call[2] for call in _boundary_tg32.ops("send_rich")]
_boundary_live32 = _boundary_live_frames32[-1] if _boundary_live_frames32 else ""
_boundary_prior_folds32 = [
    call[2] for call in _boundary_tg32.ops("edit_rich")
    if str(call[1]) == _boundary_prior_mid32
]
_boundary_prior_fold32 = _boundary_prior_folds32[-1]\
    if _boundary_prior_folds32 else ""

check("32a pending and in-progress rows carry exactly, in order, at 0 of N",
      not any(_boundary_errors32) and not _boundary_prompt_err32
      and _boundary_plan32 == _boundary_active_before32
      and [_row_id26(row) for row in _boundary_plan32]
      == ["pending-32", "running-32"]
      and "📋 Plan · 0/2 done" in _boundary_live32
      and _boundary_live32.find("First pending")
      < _boundary_live32.find("Second running"),
      next((err for err in _boundary_errors32 if err), "")
      or _boundary_prompt_err32
      or _detail26({"before": _boundary_active_before32,
                    "after": _boundary_plan32,
                    "frame": _boundary_live32}))
check("32b completed and cancelled rows drop only after the prior full recap renders",
      all(_row26(_boundary_plan32, task_id) == {}
          for task_id in ("finished-32", "cancelled-32"))
      and all(subject in _boundary_prior_fold32 for subject in (
          "First pending", "Second running", "Third finished", "Fourth cancelled")),
      _detail26({"plan": _boundary_plan32,
                 "prior_fold": _boundary_prior_fold32}))
check("32g a carried in-progress row keeps rich bold and pointer treatment",
      "- [ ] " + _BOLD26 + "Second running" + _BOLD26 + " ◀"
      in _boundary_live32,
      _detail26(_boundary_live32))

_real_time32 = mod.time.time
mod.time.time = lambda: _real_time32() + 3.0
try:
    _, _carried_tick_err32 = _set_status32(
        _boundary_tg32, "pending-32", "pending", "completed", _boundary_session32)
finally:
    mod.time.time = _real_time32
_carried_tick_state32 = load()
_carried_tick_frames32 = [call[2] for call in _boundary_tg32.ops("edit_rich")]
_carried_tick_frame32 = _carried_tick_frames32[-1] if _carried_tick_frames32 else ""
check("32c this-turn TaskUpdate ticks a carried id live - Foxtrot regression",
      not _carried_tick_err32
      and _row_status26(_row26(
          _state_plan26(_carried_tick_state32), "pending-32")) == "completed"
      and "📋 Plan · 1/2 done" in _carried_tick_frame32
      and "- [x] First pending" in _carried_tick_frame32,
      _carried_tick_err32
      or _detail26({"state": _carried_tick_state32,
                    "frame": _carried_tick_frame32}))

_unknown_before32 = [dict(row) for row in _state_plan26(_carried_tick_state32)]
_, _unknown_err32 = _set_status32(
    _boundary_tg32, "unknown-32", "pending", "completed", _boundary_session32)
_unknown_after32 = _state_plan26(load())
check("32f an unknown-id TaskUpdate remains a literal plan no-op",
      not _unknown_err32 and _unknown_after32 == _unknown_before32,
      _unknown_err32
      or _detail26({"before": _unknown_before32, "after": _unknown_after32}))

_reset26()
_enable_rich26()
_finished_tg32 = RichRecTG()
_finished_session32 = "finished-session-32"
_, _finished_open_err32 = _prompt26(
    _finished_tg32, "start a short checklist", _finished_session32)
_, _finished_create_err32 = _create_task32(
    _finished_tg32, "only-finished-32", "Only finished",
    "one finished row", "Finishing the only row", _finished_session32)
_, _finished_update_err32 = _set_status32(
    _finished_tg32, "only-finished-32", "pending", "completed",
    _finished_session32)
_, _finished_boundary_err32 = _prompt26(
    _finished_tg32, "next turn after completion", _finished_session32)
_finished_boundary32 = load()
_finished_frames32 = [call[2] for call in _finished_tg32.ops("send_rich")]
_finished_frame32 = _finished_frames32[-1] if _finished_frames32 else ""
check("32d a fully-completed prior plan yields a genuinely empty boundary plan",
      not any((_finished_open_err32, _finished_create_err32,
               _finished_update_err32, _finished_boundary_err32))
      and _state_plan26(_finished_boundary32) == []
      and "📋 Plan · 0/0 done" in _finished_frame32
      and "nothing logged yet" in _finished_frame32,
      next((err for err in (_finished_open_err32, _finished_create_err32,
                            _finished_update_err32, _finished_boundary_err32)
            if err), "")
      or _detail26({"state": _finished_boundary32,
                    "frame": _finished_frame32}))

_reset26()
_enable_rich26()
_cross_tg32 = RichRecTG()
_, _cross_open_err32 = _prompt26(
    _cross_tg32, "start session A", "boundary-session-A-32")
_, _cross_create_err32 = _create_task32(
    _cross_tg32, "cross-active-32", "Session A active",
    "must not cross sessions", "Working in session A", "boundary-session-A-32")
_, _cross_boundary_err32 = _prompt26(
    _cross_tg32, "start session B", "boundary-session-B-32")
_cross_state32 = load()
_cross_frames32 = [call[2] for call in _cross_tg32.ops("send_rich")]
_cross_frame32 = _cross_frames32[-1] if _cross_frames32 else ""
check("32e a brand-new rich session starts empty despite prior active tasks",
      not any((_cross_open_err32, _cross_create_err32, _cross_boundary_err32))
      and _state_plan26(_cross_state32) == []
      and "📋 Plan · 0/0 done" in _cross_frame32
      and "Session A active" not in _cross_frame32,
      next((err for err in (
          _cross_open_err32, _cross_create_err32, _cross_boundary_err32) if err), "")
      or _detail26({"state": _cross_state32, "frame": _cross_frame32}))

_reset26()

print("\n===== #33 per-lane Codex liveness and terminal glyph semantics =====")

def _cmd33(path):
    return {
        "command": (
            "codex exec -m gpt-5.6-sol -c model_reasoning_effort=max "
            "--sandbox read-only --skip-git-repo-check --color never "
            "-o %s 'bounded lane task'" % path
        ),
        "description": "Launch keyed Codex lane",
        "run_in_background": True,
    }

def _lane33(state, tuid):
    return next((row for row in (state.get("steps") or [])
                 if row.get("tuid") == tuid), {})

def _ere_arg33(value):
    specials = set("\\.^$*+?{}[]()|")
    literal = "".join(("\\" + char) if char in specials else char for char in value)
    return "(^|[[:space:]])-o[[:space:]]+" + literal + "([[:space:]]|$)"

def _grep_ere33(pattern, command):
    result = _REAL_SUBPROCESS_RUN(
        ["/usr/bin/grep", "-Eq", pattern], input=(command + "\n").encode(),
        stdout=_sp.PIPE, stderr=_sp.PIPE)
    return result.returncode == 0

_reset26()
_enable_rich26()
_dead_key33 = "/private/tmp/tgstream33/dead-lane33.md"
_live_key33 = "/private/tmp/tgstream33/live-lane33.md"
_next_key33 = "/private/tmp/tgstream33/next-lane33.md"
_PGREP_TEST_STATE.update({"global": True, "paths": {}, "calls": []})
_tg33 = RichRecTG()
_, _open_err33 = _prompt26(_tg33, "reproduce the cross-lane resurrection", "lane-33")
for _mode33 in ("pre", "post"):
    _, _dead_err33 = _step26(
        _tg33, _mode33, "Bash", _cmd33(_dead_key33), tuid="DEAD-33",
        session_id="lane-33")
    _, _live_err33 = _step26(
        _tg33, _mode33, "Bash", _cmd33(_live_key33), tuid="LIVE-33",
        session_id="lane-33")

_opened33 = load()
check("33a each Codex lane stores its exact bounded output-path key",
      not any((_open_err33, _dead_err33, _live_err33))
      and _lane33(_opened33, "DEAD-33").get("codex_key") == _dead_key33
      and _lane33(_opened33, "LIVE-33").get("codex_key") == _live_key33,
      _detail26(_opened33.get("steps")))

_long_path33 = "/private/tmp/" + ("x" * 1500) + ".md"
try:
    _long_key33 = mod._codex_process_key(_cmd33(_long_path33))
    _long_key_err33 = ""
except Exception as _long_key_exc33:
    _long_key33 = None
    _long_key_err33 = "%s: %s" % (
        type(_long_key_exc33).__name__, str(_long_key_exc33)[:120])
check("33a an over-limit output path falls back instead of storing a partial key",
      not _long_key_err33 and _long_key33 == "",
      _long_key_err33 or repr(_long_key33))

_timed33 = load()
_real_now33 = time.time()
_lane33(_timed33, "DEAD-33")["t0"] = _real_now33 - 14.0
_lane33(_timed33, "LIVE-33")["t0"] = _real_now33 - 2.0
_timed33["last_edit"] = 0
mod.save_state(SDIR, _timed33)
_PGREP_TEST_STATE["paths"] = {
    _ere_arg33(_dead_key33): False,
    _ere_arg33(_live_key33): True,
}
_PGREP_TEST_STATE["calls"] = []
animator_tick23(_tg33)
_closed33 = load()
_dead33 = _lane33(_closed33, "DEAD-33")
_live33 = _lane33(_closed33, "LIVE-33")

check("33b a dead keyed lane closes in state while a different keyed lane stays live",
      _dead33.get("done") is True
      and _dead33.get("codex_terminal_status") == "stopped"
      and _dead33.get("codex_inferred") is True
      and _live33.get("done") is False,
      _detail26({"dead": _dead33, "live": _live33}))
check("33b inferred closure freezes a real tick-time duration",
      isinstance(_dead33.get("secs"), (int, float))
      and not isinstance(_dead33.get("secs"), bool)
      and 12.0 <= float(_dead33.get("secs")) <= 16.0,
      repr(_dead33.get("secs")))
_patterns33 = [call[-1] for call in _PGREP_TEST_STATE["calls"]]
check("33b liveness probes use each complete path and never the global pattern",
      set(_patterns33) == {_ere_arg33(_dead_key33), _ere_arg33(_live_key33)}
      and "codex exec" not in _patterns33,
      repr(_patterns33))
try:
    _exact_pattern33 = mod._pgrep_literal(_dead_key33)
    _exact_match33 = _grep_ere33(
        _exact_pattern33, "codex exec -o %s bounded" % _dead_key33)
    _prefix_match33 = _grep_ere33(
        _exact_pattern33, "codex exec -o %s.backup bounded" % _dead_key33)
    _t_suffix_match33 = _grep_ere33(
        _exact_pattern33, "codex exec -o %st bounded" % _dead_key33)
    _prompt_only_match33 = _grep_ere33(
        _exact_pattern33,
        "codex exec -o /private/tmp/other.md inspect %s now" % _dead_key33)
    _exact_pattern_err33 = ""
except Exception as _exact_pattern_exc33:
    _exact_pattern33 = ""
    _exact_match33 = False
    _prefix_match33 = _t_suffix_match33 = _prompt_only_match33 = True
    _exact_pattern_err33 = "%s: %s" % (
        type(_exact_pattern_exc33).__name__, str(_exact_pattern_exc33)[:120])
check("33b liveness binds the exact path to -o with portable POSIX boundaries",
      not _exact_pattern_err33 and _exact_pattern33 == _ere_arg33(_dead_key33)
      and _exact_match33 and not _prefix_match33 and not _t_suffix_match33
      and not _prompt_only_match33,
      _exact_pattern_err33 or repr(_exact_pattern33))

_dead_frozen33 = dict(_dead33)
_frame_a33, _frame_a_err33 = _render_live26(
    SDIR, _closed33.get("steps") or [], _closed33.get("proc_start"), {},
    mission="Persist closure", panel=[], plan=[], tools={"items": [], "count": 0})
_frame_b33, _frame_b_err33 = _render_live26(
    SDIR, _closed33.get("steps") or [], _closed33.get("proc_start"), {},
    mission="Persist closure", panel=[], plan=[], tools={"items": [], "count": 0})
_PGREP_TEST_STATE["paths"][_ere_arg33(_next_key33)] = True
for _mode33 in ("pre", "post"):
    _, _next_err33 = _step26(
        _tg33, _mode33, "Bash", _cmd33(_next_key33), tuid="NEXT-33",
        session_id="lane-33")
_after_next33 = load()
check("33c renders and later Codex launches cannot resurrect a persisted closure",
      not any((_frame_a_err33, _frame_b_err33, _next_err33))
      and _lane33(_after_next33, "DEAD-33") == _dead_frozen33
      and mod._rich_lane_state(_lane33(_after_next33, "DEAD-33")) == "■ stop"
      and mod._rich_lane_state(_lane33(_after_next33, "NEXT-33")) == "◉ live",
      _detail26(_after_next33.get("steps")))

_PGREP_TEST_STATE.update({"global": True, "paths": {}, "calls": []})
_keyless_alive33 = {"codex_bg": True, "done": False, "t0": 10.0}
try:
    _keyless_alive_count33 = mod._close_gone_codex_bg([_keyless_alive33], 15.0)
    _keyless_alive_err33 = ""
except Exception as _keyless_alive_exc33:
    _keyless_alive_count33 = None
    _keyless_alive_err33 = "%s: %s" % (
        type(_keyless_alive_exc33).__name__, str(_keyless_alive_exc33)[:120])
check("33d a keyless legacy lane falls back to the global alive check",
      not _keyless_alive_err33 and _keyless_alive_count33 == 0
      and _keyless_alive33.get("done") is False
      and [call[-1] for call in _PGREP_TEST_STATE["calls"]] == ["codex exec"],
      _keyless_alive_err33 or _detail26(_keyless_alive33))

_PGREP_TEST_STATE.update({"global": False, "paths": {}, "calls": []})
_keyless_gone33 = {"codex_bg": True, "done": False, "t0": 10.0}
try:
    _keyless_gone_count33 = mod._close_gone_codex_bg([_keyless_gone33], 15.0)
    _keyless_gone_err33 = ""
except Exception as _keyless_gone_exc33:
    _keyless_gone_count33 = None
    _keyless_gone_err33 = "%s: %s" % (
        type(_keyless_gone_exc33).__name__, str(_keyless_gone_exc33)[:120])
check("33d a keyless legacy lane closes only when the global fallback is gone",
      not _keyless_gone_err33 and _keyless_gone_count33 == 1
      and _keyless_gone33.get("done") is True
      and _keyless_gone33.get("codex_terminal_status") == "stopped"
      and abs(float(_keyless_gone33.get("secs", -1)) - 5.0) < 0.001,
      _keyless_gone_err33 or _detail26(_keyless_gone33))

_PGREP_TEST_STATE.update({"global": None, "paths": {}, "calls": []})
_keyless_unknown33 = {"codex_bg": True, "done": False, "t0": 10.0}
try:
    _keyless_unknown_count33 = mod._close_gone_codex_bg([_keyless_unknown33], 15.0)
    _keyless_unknown_err33 = ""
except Exception as _keyless_unknown_exc33:
    _keyless_unknown_count33 = None
    _keyless_unknown_err33 = "%s: %s" % (
        type(_keyless_unknown_exc33).__name__, str(_keyless_unknown_exc33)[:120])
check("33d a failed pgrep probe is unknown and cannot close a lane",
      not _keyless_unknown_err33 and _keyless_unknown_count33 == 0
      and _keyless_unknown33.get("done") is False,
      _keyless_unknown_err33 or _detail26(_keyless_unknown33))

_PGREP_TEST_STATE.update({
    "global": True, "paths": {_ere_arg33(_dead_key33): False}, "calls": []})
_startup_grace33 = getattr(mod, "CODEX_PROCESS_START_GRACE", 3.0)
_startup_lane33 = {
    "codex_bg": True, "done": False, "t0": 100.0, "codex_key": _dead_key33}
try:
    _startup_count33 = mod._close_gone_codex_bg([_startup_lane33], 100.5)
    _startup_err33 = ""
except Exception as _startup_exc33:
    _startup_count33 = None
    _startup_err33 = "%s: %s" % (
        type(_startup_exc33).__name__, str(_startup_exc33)[:120])
check("33d one missing probe inside the launch grace cannot close a keyed lane",
      not _startup_err33 and hasattr(mod, "CODEX_PROCESS_START_GRACE")
      and _startup_count33 == 0 and _startup_lane33.get("done") is False,
      _startup_err33 or _detail26({"grace": _startup_grace33,
                                  "lane": _startup_lane33}))
_never_started33 = {
    "codex_bg": True, "done": False, "t0": 100.0, "codex_key": _dead_key33}
try:
    _never_started_count33 = mod._close_gone_codex_bg(
        [_never_started33], 100.0 + _startup_grace33 + 0.25)
    _never_started_err33 = ""
except Exception as _never_started_exc33:
    _never_started_count33 = None
    _never_started_err33 = "%s: %s" % (
        type(_never_started_exc33).__name__, str(_never_started_exc33)[:120])
check("33d a keyed lane still gone after launch grace closes with elapsed time",
      not _never_started_err33 and _never_started_count33 == 1
      and _never_started33.get("done") is True
      and abs(float(_never_started33.get("secs", -1))
              - (_startup_grace33 + 0.25)) < 0.001,
      _never_started_err33 or _detail26(_never_started33))

_glyph_now33 = time.time()
_glyph_steps33 = [
    {"codex_bg": True, "cap": "Killed lane", "done": True, "secs": 4,
     "codex_terminal_status": "killed"},
    {"codex_bg": True, "cap": "Cancelled lane", "done": True, "secs": 5,
     "codex_terminal_status": "cancelled"},
    {"codex_bg": True, "cap": "Failed lane", "done": True, "secs": 6,
     "codex_terminal_status": "failed"},
    {"codex_bg": True, "cap": "Stopped lane", "done": True, "secs": 7,
     "codex_terminal_status": "stopped"},
    {"codex_bg": True, "cap": "Completed lane", "done": True, "secs": 8,
     "codex_terminal_status": "completed"},
    {"codex_bg": True, "cap": "Still running", "done": False,
     "t0": _glyph_now33 - 9, "codex_key": _live_key33},
]
_glyph_panel33 = [{"lab": "Unknown agent", "done": False, "t0": _glyph_now33 - 11}]
_PGREP_TEST_STATE.update({
    "global": True,
    "paths": {_ere_arg33(_live_key33): True},
    "calls": []})
_glyph_live33, _glyph_live_err33 = _render_live26(
    SDIR, _glyph_steps33, _glyph_now33 - 20, {}, mission="Glyph contract",
    panel=_glyph_panel33, plan=[], tools={"items": [], "count": 0})
_glyph_fold33, _glyph_fold_err33 = _render_final26(
    SDIR, _glyph_steps33, _glyph_now33 - 20, mission="Glyph contract",
    panel=_glyph_panel33, plan=[], outcome="done")
_glyph_live_text33 = _text26(_glyph_live33)
_glyph_fold_text33 = _text26(_glyph_fold33)
_glyph_both33 = _glyph_live_text33 + "\n" + _glyph_fold_text33
check("33e lane-state mapping reserves the cross for killed and cancelled",
      [mod._rich_lane_state(row) for row in _glyph_steps33[:5]]
      == ["✕ kill", "✕ kill", "! fail", "■ stop", "✓ done"],
      repr([mod._rich_lane_state(row) for row in _glyph_steps33[:5]]))
_cross_lines33 = [line for line in _glyph_both33.splitlines() if "✕" in line]

check("33e every rendered cross belongs to a killed or cancelled lane",
      not any((_glyph_live_err33, _glyph_fold_err33))
      and len(_cross_lines33) == 4
      and all("Killed lane" in line or "Cancelled lane" in line
              for line in _cross_lines33)
      and "×" not in _glyph_both33,
      _detail26(_cross_lines33))
try:
    _fold_rows33 = mod._rich_lane_rows(
        _glyph_steps33, _glyph_panel33, _glyph_now33, fold=True)
    _fold_rows_err33 = ""
except Exception as _fold_rows_exc33:
    _fold_rows33 = []
    _fold_rows_err33 = "%s: %s" % (
        type(_fold_rows_exc33).__name__, str(_fold_rows_exc33)[:120])
check("33e rendered frames and fold rows never contain the forbidden cross-ended state",
      not _fold_rows_err33 and "✕ ended" not in _glyph_both33
      and all(state != "✕ ended" for _, state, _ in _fold_rows33),
      _fold_rows_err33 or _detail26(_fold_rows33))
check("33f a still-live Codex lane keeps its live glyph and elapsed clock at fold",
      "| ◉ Still running (Codex) | 00:" in _glyph_fold_text33
      and "- [x] Still running" not in _glyph_fold_text33
      and "- [ ] Still running" in _glyph_fold_text33,
      _detail26(_glyph_fold_text33))
check("33g an unknown agent row uses the neutral square at fold, never the cross",
      "| ■ Unknown agent (Agent) | \u2014 |" in _glyph_fold_text33
      and "| ✕ Unknown agent (Agent) |" not in _glyph_fold_text33,
      _detail26(_glyph_fold_text33))

_reset26()
_enable_rich26()
_late_tg33 = RichRecTG()
_, _late_open_err33 = _prompt26(
    _late_tg33, "receive late terminal truth", "late-notification-33")
_late_state33 = load()
_late_state33["steps"] = [{
    "fam": "run", "cap": "Codex Sol · late33", "tuid": "LATE-33",
    "done": True, "codex": True, "codex_bg": True,
    "codex_key": "/private/tmp/tgstream33/late33.md", "t0": 10.0,
    "secs": 12.0, "codex_terminal_status": "completed", "codex_inferred": True,
}]
mod.save_state(SDIR, _late_state33)
_late_ops33 = mod._close_codex_notification(
    SDIR, _late_state33, _terminal_notification25("LATE-33", "failed"),
    tg=_late_tg33)
_late_after33 = _lane33(load(), "LATE-33")
check("33h a late terminal notification outranks inferred completion",
      not _late_open_err33 and _late_ops33 is not None
      and _late_after33.get("done") is True
      and _late_after33.get("codex_terminal_status") == "failed"
      and "codex_inferred" not in _late_after33
      and abs(float(_late_after33.get("secs", -1)) - 12.0) < 0.001
      and (_late_after33.get("cap") or "").endswith(" · failed")
      and (_late_after33.get("cap") or "").count(" · failed") == 1,
      _detail26({"ops": _late_ops33, "lane": _late_after33}))

_reset26()
_enable_rich26()
_mixed_tg33 = RichRecTG()
_, _mixed_open_err33 = _prompt26(
    _mixed_tg33, "persist one dead lane beside one live lane", "mixed-sweep-33")
_mixed_now33 = mod.time.time()
_mixed_state33 = load()
_mixed_state33["steps"] = [
    {"fam": "run", "cap": "Codex Sol · dead-sweep33", "tuid": "DEAD-SWEEP-33",
     "done": False, "codex": True, "codex_bg": True,
     "codex_key": _dead_key33, "t0": _mixed_now33 - 14.0},
    {"fam": "run", "cap": "Codex Sol · live-sweep33", "tuid": "LIVE-SWEEP-33",
     "done": False, "codex": True, "codex_bg": True,
     "codex_key": _live_key33, "t0": _mixed_now33 - 4.0},
]
_mixed_state33["noreply_grace_until"] = _mixed_now33 - 1.0
mod.save_state(SDIR, _mixed_state33)
_PGREP_TEST_STATE.update({
    "global": True,
    "paths": {_ere_arg33(_dead_key33): False, _ere_arg33(_live_key33): True},
    "calls": []})
_mixed_fired33 = mod._noreply_grace_sweep(SDIR, _mixed_tg33)
_mixed_after33 = load()
check("33i grace sweep persists a dead lane even while its sibling stays live",
      not _mixed_open_err33 and _mixed_fired33 is False
      and _lane33(_mixed_after33, "DEAD-SWEEP-33").get("done") is True
      and _lane33(_mixed_after33, "LIVE-SWEEP-33").get("done") is False,
      _detail26(_mixed_after33))

_PGREP_TEST_STATE.update({"global": True, "paths": {}, "calls": []})
_reset26()

print("\n===== #34 structured background Agent liveness and carry =====")

def _agent_input34(description="Visual page audit of booklet", background=True):
    return {
        "description": description,
        "subagent_type": "opus-executor",
        "model": "opus",
        "run_in_background": background,
    }

_reset26()
_enable_rich26()
_clock34 = _Clock26(34000.0)
_real_time34 = mod.time.time
try:
    mod.time.time = _clock34.time
    _tg34 = RichRecTG()
    _session34 = "background-agent-session-34"
    _, _prompt_err34 = _prompt26(_tg34, "run the background audit", _session34)
    _clock34.advance(4)
    _, _pre_err34 = _step26(
        _tg34, "pre", "Agent", _agent_input34(), tuid="AGENT-34",
        session_id=_session34)
    _clock34.advance(1.4)
    _, _post_err34 = _step26(
        _tg34, "post", "Agent", _agent_input34(), tuid="AGENT-34",
        tresponse={
            "isAsync": True,
            "status": "async_launched",
            "agentId": "agent-internal-34",
            "description": "Visual page audit of booklet",
            "resolvedModel": "claude-opus-5[1m]",
        },
        session_id=_session34)
    _launched34 = load()
    _row34 = _panel_row31(_launched34, "AGENT-34")
    _terminal_probe34 = [{
        "tuid": "TERMINAL-PROBE-34", "done": False,
        "background_requested": True,
    }]
    _terminal_promoted34 = mod._panel_accept_background_launch(
        _terminal_probe34, "TERMINAL-PROBE-34",
        {"status": "killed",
         "agentId": "agent-sync-34", "description": "Background research"})
    check("34a structured async metadata is recognized without prose scraping",
          mod._is_async_agent_launch({
              "isAsync": True, "status": "async_launched",
              "agentId": "agent-internal-34"}) is True
          and mod._is_async_agent_launch({
              "isAsync": False, "status": "completed",
              "agentId": "agent-sync-34",
              "description": "Background research"}) is False
          and _terminal_promoted34 is False
          and not _terminal_probe34[0].get("background")
          and "background_requested" not in _terminal_probe34[0]
          and mod._rich_lane_state(_terminal_probe34[0]) == "✕ kill",
          _detail26({"row": _row34, "terminal_probe": _terminal_probe34}))
    check("34b the real background request promotes one durable open panel row",
          not any((_prompt_err34, _pre_err34, _post_err34))
          and _row34.get("background") is True
          and "background_requested" not in _row34
          and _row34.get("done") is False,
          _detail26(_row34))

    _clock34.advance(119.6)
    _later_live34, _later_live_err34 = _render_live26(
        SDIR, _launched34.get("steps") or [], _launched34.get("proc_start"), {},
        mission="Auditing the booklet", panel=_launched34.get("panel"), plan=[],
        tools={"items": [], "count": 0})
    _later_text34 = _text26(_later_live34)

    check("34c the live row names the short description and advances elapsed time",
          not _later_live_err34
          and "| ◉ Visual page audit of boo… (Opus 5) | 02:01 |" in _later_text34
          and "Waiting on the agent" in _later_text34,
          _later_live_err34 or _detail26(_later_text34))

    _stop_ops34 = mod.record_reply(SDIR, _tg34, final=True)
    mod._run_deferred(SDIR, _tg34, _stop_ops34)
    _after_stop34 = load()
    check("34d Stop keeps the same box, animator state and typing live",
          _stop_ops34 == [("typing", None, None, False)]
          and _after_stop34.get("folded") is False
          and _after_stop34.get("typing_done") is False
          and _panel_row31(_after_stop34, "AGENT-34").get("done") is False,
          _detail26({"ops": _stop_ops34, "state": _after_stop34}))

    _old_t0_34 = _panel_row31(_after_stop34, "AGENT-34").get("t0")
    _clock34.advance(10)
    _, _boundary_err34 = _prompt26(
        _tg34, "continue while the audit runs", _session34)
    _carried34 = load()
    _carried_row34 = _panel_row31(_carried34, "AGENT-34")
    _carried_mid34 = str(_carried34.get("msgid"))
    _carried_sends34 = len(_rich_ops26(_tg34, "send_rich"))
    _react_ops34 = mod.on_react(SDIR, _tg34)
    mod._run_deferred(SDIR, _tg34, _react_ops34)
    _after_react34 = load()

    check("34e a same-session prompt carries the row, clock and launch sentinel",
          not _boundary_err34 and _carried_row34.get("background") is True
          and _carried_row34.get("done") is False
          and _carried_row34.get("t0") == _old_t0_34
          and any(step.get("tuid") == "AGENT-34"
                  for step in (_carried34.get("steps") or []))
          and "| ◉ Visual page audit of boo… (Opus 5) | 02:11 |"
          in (_carried34.get("last_live") or "")
          and "| ✓ Visual page audit of boo… (Opus 5) | 02:11 |"
          not in (_carried34.get("last_live") or "")
          and _react_ops34 == []
          and str(_after_react34.get("msgid")) == _carried_mid34
          and len(_rich_ops26(_tg34, "send_rich")) == _carried_sends34,
          _boundary_err34 or _detail26({
              "row": _carried_row34, "live": _carried34.get("last_live"),
              "react_ops": _react_ops34, "after_react": _after_react34}))

    _clock34.advance(17)
    _notification34 = _agent_notification31(
        "AGENT-34", 'Agent "Visual page audit of booklet" finished',
        task_id="agent-task-34")
    _, _notify_err34 = _prompt26(_tg34, _notification34, _session34)
    _closed34 = _panel_row31(load(), "AGENT-34")
    check("34f the exact terminal notification freezes the full launch-to-finish time",
          not _notify_err34 and _closed34.get("done") is True
          and _closed34.get("outcome") == "completed"
          and abs(float(_closed34.get("secs", -1)) - 148.0) < 0.001,
          _notify_err34 or _detail26(_closed34))

    _terminal_stop_ops34 = mod.record_reply(SDIR, _tg34, final=True)
    mod._run_deferred(SDIR, _tg34, _terminal_stop_ops34)
    _terminal34 = load()

    check("34g the carried launch sentinel lets terminal Stop retain the Agent recap",
          _terminal34.get("folded") is True
          and _panel_row31(_terminal34, "AGENT-34").get("done") is True
          and "✓ Visual page audit" in (_terminal34.get("last_live") or "")
          and "(Opus 5)" in (_terminal34.get("last_live") or ""),
          _detail26({"ops": _terminal_stop_ops34, "state": _terminal34}))

    _sync_tg34 = RichRecTG()
    _reset26()
    _enable_rich26()
    _, _sync_prompt_err34 = _prompt26(
        _sync_tg34, "run one synchronous agent", "sync-agent-session-34")
    _clock34.advance(1)
    _, _sync_pre_err34 = _step26(
        _sync_tg34, "pre", "Agent", _agent_input34("Synchronous audit", False),
        tuid="SYNC-34", session_id="sync-agent-session-34")
    _clock34.advance(3)
    _, _sync_post_err34 = _step26(
        _sync_tg34, "post", "Agent", _agent_input34("Synchronous audit", False),
        tuid="SYNC-34",
        tresponse={"result": "audit complete", "success": True,
                   "isAsync": False, "status": "completed",
                   "agentId": "agent-sync-34",
                   "description": "Background research"},
        session_id="sync-agent-session-34")
    _sync_row34 = _panel_row31(load(), "SYNC-34")
    check("34h synchronous Agent behavior stays launch-to-return and terminal",
          not any((_sync_prompt_err34, _sync_pre_err34, _sync_post_err34))
          and _sync_row34.get("done") is True
          and not _sync_row34.get("background")
          and abs(float(_sync_row34.get("secs", -1)) - 3.0) < 0.001,
          _detail26(_sync_row34))

    _clock34.advance(1)
    _, _failed_pre_err34 = _step26(
        _sync_tg34, "pre", "Agent", _agent_input34("Unavailable worker", True),
        tuid="FAILED-LAUNCH-34", session_id="sync-agent-session-34")
    _clock34.advance(1)
    _, _failed_post_err34 = _step26(
        _sync_tg34, "post", "Agent", _agent_input34("Unavailable worker", True),
        tuid="FAILED-LAUNCH-34",
        tresponse={"success": False, "error": "worker unavailable",
                   "isAsync": False, "status": "failed"},
        session_id="sync-agent-session-34")
    _failed_launch_row34 = _panel_row31(load(), "FAILED-LAUNCH-34")
    check("34i an explicitly failed launch cannot become a two-hour background lane",
          not any((_failed_pre_err34, _failed_post_err34))
          and _failed_launch_row34.get("done") is True
          and not _failed_launch_row34.get("background")
          and "background_requested" not in _failed_launch_row34
          and mod._rich_lane_state(_failed_launch_row34) == "! fail",
          _detail26(_failed_launch_row34))

    _stale_row34 = {
        "tuid": "STALE-34", "lab": "Abandoned background audit",
        "background": True, "done": False,
        "t0": _clock34.time() - mod.CODEX_BG_MAX_AGE - 1,
    }
    try:
        _expired34 = mod._expire_background_agents(
            [_stale_row34], _clock34.time())
        _stale_open34 = mod._has_open_background_agents(
            [_stale_row34], _clock34.time())
        _stale_err34 = ""
    except Exception as exc:
        _expired34 = -1
        _stale_open34 = True
        _stale_err34 = repr(exc)
    check("34j a missing notification cannot hold the box beyond the existing cap",
          _expired34 == 1 and _stale_row34.get("done") is True
          and _stale_row34.get("outcome") == "stale"
          and not _stale_open34,
          _stale_err34 or _detail26(_stale_row34))
finally:
    mod.time.time = _real_time34

_reset26()

print("\n===== #35 rich transport budget, retry and recovery =====")

_budget_now35 = 35000.0
_budget_plan35 = [
    {"id": "pending-first-35", "subject": "KEEP-PENDING-35", "status": "pending"},
]
_budget_plan35.extend({
    "id": "completed-%02d-35" % i,
    "subject": "COMPLETE-%02d-35 %s" % (i, "c" * 150),
    "status": "completed",
} for i in range(40))
_budget_plan35.append({
    "id": "live-last-35", "subject": "KEEP-LIVE-35", "status": "in_progress",
})
_budget_tools35 = {
    "count": 30,
    "items": [
        {"cap": "TOOL-%02d-35 %s" % (i, "t" * 150), "elapsed": i + 1}
        for i in range(30)
    ],
}
_budget_panel35 = [
    {"tuid": "AGENT-A-35", "lab": "KEEP-AGENT-ALPHA-35",
     "background": True, "done": False, "t0": _budget_now35 - 17},
    {"tuid": "AGENT-B-35", "lab": "KEEP-AGENT-BETA-35",
     "background": True, "done": False, "t0": _budget_now35 - 11},
    {"tuid": "AGENT-C-35", "lab": "KEEP-AGENT-GAMMA-35",
     "background": True, "done": False, "t0": _budget_now35 - 5},
]
_real_time35 = mod.time.time
try:
    mod.time.time = lambda: _budget_now35
    _budget_frame_a35 = mod.render_rich_live(
        SDIR, [], _budget_now35 - 60, {}, "KEEP-MISSION-35",
        panel=_budget_panel35, plan=_budget_plan35, tools=_budget_tools35, frame=7)
    _budget_frame_b35 = mod.render_rich_live(
        SDIR, [], _budget_now35 - 60, {}, "KEEP-MISSION-35",
        panel=_budget_panel35, plan=_budget_plan35, tools=_budget_tools35, frame=7)
finally:
    mod.time.time = _real_time35

check("35a over-budget live boxes trim deterministically by priority",
      _budget_frame_a35 == _budget_frame_b35
      and "TOOL-" not in _budget_frame_a35
      and "COMPLETE-21-35" not in _budget_frame_a35
      and "COMPLETE-22-35" not in _budget_frame_a35
      and "COMPLETE-23-35" in _budget_frame_a35
      and "- +23 lower-priority details hidden" in _budget_frame_a35
      and "KEEP-PENDING-35" in _budget_frame_a35
      and "KEEP-LIVE-35" in _budget_frame_a35,
      _detail26(_budget_frame_a35))
_budget_fit35, _budget_fit_err35 = _call26("_rich_frame_fits", _budget_frame_a35)
check("35a budget preserves mission, clock/activity and the whole agent table",
      not _budget_fit_err35 and _budget_fit35 is True
      and "KEEP-MISSION-35" in _budget_frame_a35
      and "👾 Working" in _budget_frame_a35
      and "1:00" in _budget_frame_a35
      and "⏳ Waiting on the agents" in _budget_frame_a35
      and "KEEP-AGENT-ALPHA-35" in _budget_frame_a35
      and "KEEP-AGENT-BETA-35" in _budget_frame_a35
      and "KEEP-AGENT-GAMMA-35" in _budget_frame_a35
      and _budget_frame_a35.index("KEEP-AGENT-ALPHA-35")
      < _budget_frame_a35.index("KEEP-AGENT-BETA-35")
      < _budget_frame_a35.index("KEEP-AGENT-GAMMA-35")
      and "🤖 Agents" not in _budget_frame_a35
      and "| ◉ KEEP-AGENT-ALPHA-35 (Agent) | 00:17 |" in _budget_frame_a35
      and _budget_frame_a35.count("<details>") == 2
      and _budget_frame_a35.count("</details>") == 2
      and _budget_frame_a35.count("<summary>") == 2
      and _budget_frame_a35.count("</summary>") == 2,
      _budget_fit_err35 or _detail26(_budget_frame_a35))
_overblock_frame35 = "\n".join(
    ["<details><summary>x</summary>"] + ["row"] * 497 + ["</details>"])
check("35a malformed rich-tag nesting is rejected before transport",
      mod._rich_frame_fits("<details><summary>x</details></summary>") is False
      and mod._rich_frame_fits("</details><details>") is False
      and mod._rich_frame_fits(
          "<details><summary>x</summary><details><summary>y</summary>"
          "</details></details>") is False
      and mod._rich_frame_fits("<details>missing summary</details>") is False
      and mod._rich_frame_fits("<foo>bar</foo>") is False
      and mod._rich_frame_fits(
          "<details><summary>x</summary><script>bad</script></details>") is False
      and mod._rich_frame_fits(
          "<DETAILS><SUMMARY>x</SUMMARY></DETAILS>") is False
      and mod._rich_frame_fits("<bold>x<bold>") is False
      and mod._rich_frame_fits("**broken") is False
      and mod._rich_frame_fits("**paired**") is True
      and mod._rich_frame_fits("<footer>legacy") is False
      and mod._rich_frame_fits("legacy</footer>") is False
      and mod._rich_frame_fits("body\n<footer") is False
      and mod._rich_frame_fits("body\n</footer") is False
      and mod._rich_frame_fits(
          "<footer><footer>x</footer></footer>") is False
      and mod._rich_frame_fits("<footer>legacy</footer>") is True
      and mod._rich_wire_fits(None) is False
      and mod._rich_wire_fits(_overblock_frame35) is False,
      "malformed rich nesting was accepted")

_priority_complete_old35 = (
    "- [x] PRIORITY-COMPLETE-OLD-35 " + "o" * 80)
_priority_complete_new35 = (
    "- [x] PRIORITY-COMPLETE-NEW-35 " + "n" * 80)
_priority_lines35 = [
    "🎯 PRIORITY-MISSION-35", "👾 Working · rail · 1:00",
    "⏳ Waiting on the agents", "| 🤖 Agent | Time |", "|:---|---:|",
    "| ◉ PRIORITY-AGENT-35 | 00:17 |",
    "<details><summary>📋 Plan · 2/4 done · tap</summary>",
    _priority_complete_old35, _priority_complete_new35,
    "- [ ] PRIORITY-PENDING-35", "- [ ] PRIORITY-ACTIVE-35 ◀", "</details>",
    "<details><summary>🔧 Tools · 2 calls · tap</summary>",
    "- at 00:01 · PRIORITY-TOOL-OLD-35",
    "- at 00:02 · PRIORITY-TOOL-NEW-35", "</details>",
]
_priority_raw35 = "\n".join(_priority_lines35)
_priority_after_one_tool35 = "\n".join(
    line for line in _priority_lines35
    if line != "- at 00:01 · PRIORITY-TOOL-OLD-35")
_priority_after_one_tool_lines35 = _priority_after_one_tool35.splitlines()
_priority_after_one_tool_lines35.insert(
    _priority_after_one_tool_lines35.index(
        "<details><summary>🔧 Tools · 2 calls · tap</summary>") + 1,
    "… 1 earlier not shown")
_priority_after_one_tool35 = "\n".join(_priority_after_one_tool_lines35)
_priority_tool_budget35 = mod._budget_rich_frame(
    _priority_raw35, frame_limit=len(_priority_after_one_tool35) + 1)
check("35a oldest tool detail yields before any completed plan detail",
      "PRIORITY-TOOL-OLD-35" not in _priority_tool_budget35
      and "PRIORITY-TOOL-NEW-35" in _priority_tool_budget35
      and "PRIORITY-COMPLETE-OLD-35" in _priority_tool_budget35
      and "PRIORITY-COMPLETE-NEW-35" in _priority_tool_budget35,
      _detail26(_priority_tool_budget35))

_plan_priority_lines35 = _priority_lines35[:12]
_plan_priority_raw35 = "\n".join(_plan_priority_lines35)
_plan_priority_expected35 = list(_plan_priority_lines35)
_plan_priority_expected35.remove(_priority_complete_old35)
_plan_priority_expected35.insert(
    _plan_priority_expected35.index(
        "<details><summary>📋 Plan · 2/4 done · tap</summary>") + 1,
    "- +1 lower-priority detail hidden")
_priority_plan_budget35 = mod._budget_rich_frame(
    _plan_priority_raw35,
    frame_limit=len("\n".join(_plan_priority_expected35)) + 1)
check("35a completed plan detail yields oldest-first before pending or active rows",
      "PRIORITY-COMPLETE-OLD-35" not in _priority_plan_budget35
      and "PRIORITY-COMPLETE-NEW-35" in _priority_plan_budget35
      and "PRIORITY-PENDING-35" in _priority_plan_budget35
      and "PRIORITY-ACTIVE-35" in _priority_plan_budget35
      and "- +1 lower-priority detail hidden" in _priority_plan_budget35,
      _detail26(_priority_plan_budget35))

try:
    mod.time.time = lambda: _budget_now35
    _raw_plan_rows35 = [mod._rich_plan_line(item) for item in _budget_plan35]
    _raw_tools35, _raw_tool_count35 = mod._rich_tool_rows(_budget_tools35)
    _raw_lanes35 = mod._rich_lane_rows([], _budget_panel35, _budget_now35)
    _raw_frame35 = mod._assemble_rich_live(
        "", "", "KEEP-MISSION-35", "1:00", 40, 42,
        "⏳ Waiting on the agents", _raw_plan_rows35, 0, _raw_lanes35,
        _raw_tools35, _raw_tool_count35, mod.RICH_MIN_EDIT_INTERVAL,
        False, 7)
finally:
    mod.time.time = _real_time35

_budget_send_tg35 = WireTG([
    {"ok": True, "result": {"message_id": 3501}},
])
_budget_send_result35 = _budget_send_tg35.send_rich(_raw_frame35)
_budget_send_content35 = _rich_content26(
    _budget_send_tg35.wire[0][1] if _budget_send_tg35.wire else {})
_transport_frame35 = _budget_send_content35.get("markdown", "")
check("35a transport budgets a raw oversized box before its first rich send",
      len(_raw_frame35) >= mod.RICH_FRAME_LIMIT
      and _budget_send_result35.get("ok") is True
      and len(_budget_send_tg35.wire) == 1
      and _budget_send_tg35.wire[0][0] == "sendRichMessage"
      and mod._rich_frame_fits(_transport_frame35)
      and all(label in _transport_frame35 for label in (
          "KEEP-AGENT-ALPHA-35", "KEEP-AGENT-BETA-35",
          "KEEP-AGENT-GAMMA-35"))
      and "TOOL-" not in _transport_frame35,
      _detail26({"result": _budget_send_result35,
                 "wire": _budget_send_tg35.wire}))

_retry_once_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_retry_once_result35 = _retry_once_tg35.edit_rich(
    "rich-3501", _transport_frame35)
_retry_once_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _retry_once_tg35.wire if call[0] == "editMessageText"
]
check("35b one API 400 gets exactly one rich retry and no plain transport",
      _retry_once_result35.get("ok") is True
      and _retry_once_result35.get("api_400_retried") is True
      and len(_retry_once_tg35.wire) == 2
      and all(call[0] == "editMessageText" for call in _retry_once_tg35.wire),
      _detail26({"result": _retry_once_result35,
                 "wire": _retry_once_tg35.wire}))
check("35b the single retry is harder trimmed but keeps the agent table",
      len(_retry_once_frames35) == 2
      and len(_retry_once_frames35[1].encode("utf-8"))
      < len(_retry_once_frames35[0].encode("utf-8"))
      and all(label in _retry_once_frames35[1] for label in (
          "KEEP-AGENT-ALPHA-35", "KEEP-AGENT-BETA-35",
          "KEEP-AGENT-GAMMA-35"))
      and "KEEP-MISSION-35" in _retry_once_frames35[1]
      and "👾 Working" in _retry_once_frames35[1]
      and "1:00" in _retry_once_frames35[1]
      and "⏳ Waiting on the agents" in _retry_once_frames35[1],
      _detail26(_retry_once_frames35))

_minimal_frame35 = (
    "🎯 MINIMAL-MISSION-35\n👾 Working · rail · 0:01\n💭 Thinking\n"
    "<details><summary>📋 Plan · 0/0 done · tap</summary>\n"
    "- nothing logged yet\n</details>")
_minimal_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_minimal_result35 = _minimal_tg35.edit_rich("minimal-3501", _minimal_frame35)
_minimal_frames35 = [
    _rich_content26(call[1]).get("markdown", "") for call in _minimal_tg35.wire]
check("35b even a minimal valid frame gets one different bounded retry",
      _minimal_result35.get("ok") is True
      and _minimal_result35.get("api_400_retried") is True
      and len(_minimal_frames35) == 2
      and len(_minimal_frames35[1]) < len(_minimal_frames35[0])
      and "lower-priority detail" not in _minimal_frames35[1]
      and "nothing logged" not in _minimal_frames35[1],
      _detail26({"result": _minimal_result35,
                 "frames": _minimal_frames35}))

_irreducible_frame35 = "<details><summary>x</summary></details>"
_irreducible_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_irreducible_result35 = _irreducible_tg35.edit_rich(
    "irreducible-3501", _irreducible_frame35)
_irreducible_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _irreducible_tg35.wire]
check("35b an irreducible valid rich wrapper still gets one harder retry",
      _irreducible_result35.get("ok") is True
      and _irreducible_result35.get("api_400_retried") is True
      and _irreducible_frames35 == [_irreducible_frame35, "x"],
      _detail26({"result": _irreducible_result35,
                 "frames": _irreducible_frames35}))

_multiline_irreducible35 = (
    "<details><summary>x</summary>\ny\n</details>")
_multiline_irreducible_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_multiline_irreducible_result35 = _multiline_irreducible_tg35.edit_rich(
    "multiline-irreducible-3501", _multiline_irreducible35)
_multiline_irreducible_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _multiline_irreducible_tg35.wire]
_empty_summary_tg35 = WireTG([])
_empty_summary_result35 = _empty_summary_tg35.edit_rich(
    "empty-summary-3501", "<details><summary></summary></details>")
_one_char_tg35 = WireTG([])
_one_char_result35 = _one_char_tg35.edit_rich("one-char-3501", "x")
check("35b every accepted odd-shaped frame pre-proves a smaller valid retry",
      _multiline_irreducible_result35.get("ok") is True
      and _multiline_irreducible_frames35
      == [_multiline_irreducible35, "x\ny"]
      and _empty_summary_result35.get("error_class") == "content_budget"
      and _empty_summary_tg35.wire == []
      and _one_char_result35.get("error_class") == "content_budget"
      and _one_char_tg35.wire == [],
      _detail26({"multiline": _multiline_irreducible_frames35,
                 "empty": _empty_summary_result35,
                 "one_char": _one_char_result35}))

def _http_400_35(description):
    body = _io27.BytesIO(json.dumps({
        "ok": False, "error_code": 400, "description": description,
    }).encode())
    return mod.HTTPError(
        "https://api.telegram.invalid", 400, "Bad Request", {}, body)

_http_400_queue35 = _URLQueue27([
    _http_400_35("Bad Request: RICH_MESSAGE_INVALID"),
    _http_400_35("Bad Request: RICH_MESSAGE_TEXT_TOO_LONG"),
])
_real_urlopen35 = mod.request.urlopen
try:
    mod.request.urlopen = _http_400_queue35
    _http_400_tg35 = mod.Telegram("TOKEN35", "999", False)
    _http_400_result35 = _http_400_tg35.edit_rich(
        "rich-http-3501", _budget_frame_a35)
finally:
    mod.request.urlopen = _real_urlopen35
check("35b real HTTPError bodies survive the single retry as bounded reasons",
      len(_http_400_queue35.calls) == 2
      and _http_400_result35.get("ok") is False
      and _http_400_result35.get("api_400_retried") is True
      and _http_400_result35.get("first_error_description")
      == "Bad Request: RICH_MESSAGE_INVALID"
      and _http_400_result35.get("error_description")
      == "Bad Request: RICH_MESSAGE_TEXT_TOO_LONG",
      _detail26({"calls": _http_400_queue35.calls,
                 "result": _http_400_result35}))

_not_modified_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": (
         "Bad Request: message is not modified: specified new message content "
         "and reply markup are exactly the same")},
])
_not_modified_result35 = _not_modified_tg35.edit_rich(
    "rich-idempotent-3501", _transport_frame35)
_not_modified_frame35 = _rich_content26(
    _not_modified_tg35.wire[0][1]).get("markdown", "")
check("35b an identical-edit 400 is semantic success without destructive trim",
      _not_modified_result35.get("ok") is True
      and _not_modified_result35.get("idempotent") is True
      and _not_modified_result35.get("frame") == _not_modified_frame35
      and len(_not_modified_tg35.wire) == 1,
      _detail26({"wire": _not_modified_tg35.wire,
                 "result": _not_modified_result35}))

_retry_not_modified_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": False, "error_code": 400,
     "description": "Bad Request: message is not modified"},
])
_retry_not_modified_result35 = _retry_not_modified_tg35.edit_rich(
    "rich-retry-idempotent-3501", _transport_frame35)
_retry_not_modified_frame35 = _rich_content26(
    _retry_not_modified_tg35.wire[-1][1]).get("markdown", "")
check("35b a retry not-modified 400 is semantic success, never a hard demotion",
      _retry_not_modified_result35.get("ok") is True
      and _retry_not_modified_result35.get("idempotent") is True
      and _retry_not_modified_result35.get("api_400_retried") is True
      and _retry_not_modified_result35.get("frame")
      == _retry_not_modified_frame35
      and len(_retry_not_modified_tg35.wire) == 2,
      _detail26({"wire": _retry_not_modified_tg35.wire,
                 "result": _retry_not_modified_result35}))

_mixed_400_queue35 = _URLQueue27([
    _http_400_35("Bad Request: RICH_MESSAGE_INVALID"),
    _http_429_27(9),
])
_mixed_400_sleeps35 = []
_real_urlopen_mixed35 = mod.request.urlopen
_real_sleep_mixed35 = mod.time.sleep
try:
    mod.request.urlopen = _mixed_400_queue35
    mod.time.sleep = lambda seconds: _mixed_400_sleeps35.append(seconds)
    _mixed_400_tg35 = mod.Telegram("TOKEN35", "999", False)
    _mixed_400_result35 = _mixed_400_tg35.edit_rich(
        "rich-mixed-3501", _transport_frame35)
finally:
    mod.request.urlopen = _real_urlopen_mixed35
    mod.time.sleep = _real_sleep_mixed35
check("35b the one 400 retry cannot expand into an inner rate-limit retry",
      len(_mixed_400_queue35.calls) == 2
      and _mixed_400_sleeps35 == []
      and _mixed_400_result35.get("ok") is False
      and _mixed_400_result35.get("error_class") == "rate_limit"
      and _mixed_400_result35.get("api_400_retried") is True,
      _detail26({"calls": _mixed_400_queue35.calls,
                 "sleeps": _mixed_400_sleeps35,
                 "result": _mixed_400_result35}))

_rate_then_400_queue35 = _URLQueue27([
    _http_429_27(9),
    _http_400_35("Bad Request: RICH_MESSAGE_INVALID"),
    _http_429_27(9),
])
_rate_then_400_sleeps35 = []
_real_urlopen_rate_then35 = mod.request.urlopen
_real_sleep_rate_then35 = mod.time.sleep
try:
    mod.request.urlopen = _rate_then_400_queue35
    mod.time.sleep = lambda seconds: _rate_then_400_sleeps35.append(seconds)
    _rate_then_400_tg35 = mod.Telegram("TOKEN35", "999", False)
    _rate_then_400_result35 = _rate_then_400_tg35.edit_rich(
        "rich-rate-then-400-3501", _transport_frame35)
finally:
    mod.request.urlopen = _real_urlopen_rate_then35
    mod.time.sleep = _real_sleep_rate_then35
check("35b a 429 then 400 owns only one final non-sleeping rich retry",
      len(_rate_then_400_queue35.calls) == 3
      and _rate_then_400_sleeps35 == [2]
      and _rate_then_400_result35.get("ok") is False
      and _rate_then_400_result35.get("error_class") == "rate_limit"
      and _rate_then_400_result35.get("api_400_retried") is True,
      _detail26({"calls": _rate_then_400_queue35.calls,
                 "sleeps": _rate_then_400_sleeps35,
                 "result": _rate_then_400_result35}))

class _RetryFlagTG35(mod.Telegram):
    def __init__(self):
        super().__init__("TOKEN35", "999", False)
        self.retry_flags = []

    def _call(self, method, params, _retry=True, _meta=None):
        del params, _meta
        self.retry_flags.append((method, _retry))
        return {"ok": True, "result": {"message_id": 3504}}

_retry_flag_tg35 = _RetryFlagTG35()
_retry_flag_tg35.typing_once()
_retry_flag_tg35.send_once("plain fallback")
_event_worst_case35 = mod.HTTP_TIMEOUT * 5 + 2
check("35b inline rich open caps typing and fallback to one attempt each",
      _retry_flag_tg35.retry_flags
      == [("sendChatAction", False), ("sendMessage", False)]
      and _event_worst_case35 == 17
      and _event_worst_case35 < mod.RICH_EVENT_BUDGET,
      _detail26({"flags": _retry_flag_tg35.retry_flags,
                 "worst_case": _event_worst_case35,
                 "budget": mod.RICH_EVENT_BUDGET}))

class _DeferredBudgetTG35:
    def __init__(self):
        self.calls = []

    def typing_once(self):
        self.calls.append("typing_once")

    def typing(self):
        self.calls.append("typing_retry")

    def edit_rich(self, msgid, frame):
        self.calls.append(("edit_rich", str(msgid), frame))
        return {"ok": False, "error_class": "api_400",
                "api_400_retried": True}

    def send_once(self, frame, markdown=False):
        self.calls.append(("send_once", frame, markdown))
        return "deferred-plain-3501"

    def send(self, frame, markdown=False):
        self.calls.append(("send_retry", frame, markdown))
        return "unexpected-retrying-send-3501"

_reset26()
_enable_rich26()
mod.save_state(SDIR, _rich_state28(
    mid="deferred-budget-owner-35", rich_box=True, rich_failed=False,
    rich_session_id="deferred-budget-session-35"))
_deferred_budget_tg35 = _DeferredBudgetTG35()
mod._run_deferred(SDIR, _deferred_budget_tg35, [
    ("typing", None, None, False),
    ("redit", "deferred-budget-owner-35", "DEFERRED-BUDGET-FRAME-35", False),
])
_deferred_budget_after35 = load()
check("35b deferred rich failure selects one-shot typing and one-shot plain fallback",
      _deferred_budget_tg35.calls[0] == "typing_once"
      and any(call[0] == "edit_rich" for call in _deferred_budget_tg35.calls
              if isinstance(call, tuple))
      and any(call[0] == "send_once" for call in _deferred_budget_tg35.calls
              if isinstance(call, tuple))
      and not any(call[0] == "send_retry" for call in _deferred_budget_tg35.calls
                  if isinstance(call, tuple))
      and _deferred_budget_after35.get("msgid") == "deferred-plain-3501"
      and _event_worst_case35 == (
          mod.HTTP_TIMEOUT + (mod.HTTP_TIMEOUT * 3 + 2) + mod.HTTP_TIMEOUT),
      _detail26({"calls": _deferred_budget_tg35.calls,
                 "state": _deferred_budget_after35,
                 "worst_case": _event_worst_case35}))

_reset26()
_enable_rich26()
_secret_state35 = _rich_state28(mid="secret-reason-35")
_secret_description35 = (
    "Bad Request: Bearer eyJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature "
    "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== "
    "api_key=" + "sk-" + "proj-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
    "api key WHITESPACE_VALUE_SHOULD_NOT_LEAK "
    "access_token:ACCESS_VALUE_SHOULD_NOT_LEAK "
    "refresh token REFRESH_VALUE_SHOULD_NOT_LEAK "
    "client_secret CLIENT_SECRET_VALUE_SHOULD_NOT_LEAK "
    "oauth_token OAUTH_VALUE_SHOULD_NOT_LEAK "
    "x-api-key X_API_VALUE_SHOULD_NOT_LEAK "
    "GITHUB_TOKEN GITHUB_VALUE_SHOULD_NOT_LEAK "
    "AWS_SECRET_ACCESS_KEY AWS_VALUE_SHOULD_NOT_LEAK "
    "TELEGRAM_BOT_TOKEN TELEGRAM_VALUE_SHOULD_NOT_LEAK "
    "DB_PASSWORD DB_VALUE_SHOULD_NOT_LEAK "
    '{"password":"VIOLET RIVER 7382"} '
    'client_secret: "TWO WORD SECRET VALUE" '
    r'{"password":"ESCAPED \"RIVER\" 8841","code":400}')
_secret_action35, _ = mod._apply_rich_result(
    _secret_state35,
    {"ok": False, "error_class": "api_400",
     "api_400_retried": True,
     "error_description": _secret_description35})
mod._append_rich_health(SDIR, _secret_state35, "api_400")
_, _secret_health_text35 = _health26()
_secret_state_text35 = json.dumps(_secret_state35, sort_keys=True)
check("35b API reasons are bounded and credential-redacted before state or log",
      _secret_action35 == "fallback"
      and "sk-proj-" not in _secret_state_text35
      and "eyJhbGci" not in _secret_state_text35
      and "sk-proj-" not in _secret_health_text35
      and "eyJhbGci" not in _secret_health_text35
      and "QWxhZGRpb" not in _secret_state_text35
      and "WHITESPACE_VALUE" not in _secret_state_text35
      and "ACCESS_VALUE" not in _secret_state_text35
      and "REFRESH_VALUE" not in _secret_state_text35
      and "CLIENT_SECRET_VALUE" not in _secret_state_text35
      and "OAUTH_VALUE" not in _secret_state_text35
      and "X_API_VALUE" not in _secret_state_text35
      and "GITHUB_VALUE" not in _secret_state_text35
      and "AWS_VALUE" not in _secret_state_text35
      and "TELEGRAM_VALUE" not in _secret_state_text35
      and "DB_VALUE" not in _secret_state_text35
      and "VIOLET" not in _secret_state_text35
      and "RIVER" not in _secret_state_text35
      and "7382" not in _secret_state_text35
      and "TWO WORD SECRET VALUE" not in _secret_state_text35
      and "ESCAPED" not in _secret_state_text35
      and "8841" not in _secret_state_text35
      and "QWxhZGRpb" not in _secret_health_text35
      and "WHITESPACE_VALUE" not in _secret_health_text35
      and "ACCESS_VALUE" not in _secret_health_text35
      and "REFRESH_VALUE" not in _secret_health_text35
      and "CLIENT_SECRET_VALUE" not in _secret_health_text35
      and "OAUTH_VALUE" not in _secret_health_text35
      and "X_API_VALUE" not in _secret_health_text35
      and "GITHUB_VALUE" not in _secret_health_text35
      and "AWS_VALUE" not in _secret_health_text35
      and "TELEGRAM_VALUE" not in _secret_health_text35
      and "DB_VALUE" not in _secret_health_text35
      and "VIOLET" not in _secret_health_text35
      and "RIVER" not in _secret_health_text35
      and "7382" not in _secret_health_text35
      and "TWO WORD SECRET VALUE" not in _secret_health_text35
      and "ESCAPED" not in _secret_health_text35
      and "8841" not in _secret_health_text35
      and "[redacted]" in _secret_state_text35
      and all(len(line) <= 512 for line in _secret_health_text35.splitlines()),
      _detail26({"state": _secret_state35,
                 "health": _secret_health_text35}))

_isolated_secret_probes35 = {
    'githubToken GITHUB CAMEL VALUE': ("GITHUB", "CAMEL", "VALUE"),
    'openaiApiKey OPENAI CAMEL VALUE': ("OPENAI", "CAMEL", "VALUE"),
    'stripeSecretKey STRIPE CAMEL VALUE': ("STRIPE", "CAMEL", "VALUE"),
    'databasePassword DATABASE CAMEL VALUE': ("DATABASE", "CAMEL", "VALUE"),
    'telegramBotToken TELEGRAM CAMEL VALUE': ("TELEGRAM", "CAMEL", "VALUE"),
    r'{"password":"VIOLET \"RIVER\" 7382","code":400}':
        ("VIOLET", "RIVER", "7382"),
    '{"githubToken":"JSON CAMEL VALUE","code":400}':
        ("JSON", "CAMEL", "VALUE"),
}
_isolated_secret_results35 = {
    raw: mod._bounded_api_description(raw)
    for raw in _isolated_secret_probes35
}
check("35b isolated credential shapes cannot hide behind an earlier probe",
      all(
          "[redacted]" in _isolated_secret_results35[raw]
          and all(value not in _isolated_secret_results35[raw]
                  for value in forbidden)
          for raw, forbidden in _isolated_secret_probes35.items()),
      _detail26(_isolated_secret_results35))

_stale_reason_state35 = _rich_state28(mid="stale-reason-35")
mod._apply_rich_result(
    _stale_reason_state35,
    {"ok": False, "error_class": "rate_limit",
     "error_description": "retry after 7 seconds"})
mod._apply_rich_result(
    _stale_reason_state35, {"ok": False, "error_class": "api_401"})
check("35b a description-less terminal failure cannot inherit a stale reason",
      "rich_error_description" not in _stale_reason_state35,
      _detail26(_stale_reason_state35))

_reset26()
_enable_rich26()
_fallback_state35 = _rich_state28(
    mid=None, rich_box=False, rich_failed=False, panel=_budget_panel35,
    rich_plan=_budget_plan35, rich_tools=_budget_tools35["items"],
    rich_tool_count=_budget_tools35["count"])
_fallback_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_TEXT_TOO_LONG"},
    {"ok": True, "result": {"message_id": 3502}},
])
_fallback_mid35, _fallback_mode35 = mod._try_rich_send_state(
    SDIR, _fallback_tg35, _fallback_state35, _raw_frame35)
_fallback_methods35 = [call[0] for call in _fallback_tg35.wire]
check("35c two API 400s perform one retry then exactly one plain fallback",
      _fallback_methods35 == ["sendRichMessage", "sendRichMessage", "sendMessage"]
      and _fallback_mode35 == "plain" and str(_fallback_mid35) == "3502"
      and _fallback_state35.get("rich_failed") is True
      and _fallback_state35.get("rich_box") is False,
      _detail26({"methods": _fallback_methods35, "state": _fallback_state35}))
_fallback_health_paths35, _fallback_health_text35 = _health26()
check("35c the health record carries the bounded final Telegram reason",
      bool(_fallback_health_paths35)
      and "api_400" in _fallback_health_text35
      and "Bad Request: RICH_MESSAGE_TEXT_TOO_LONG" in _fallback_health_text35
      and all(len(line) <= 512 for line in _fallback_health_text35.splitlines()),
      _detail26({"paths": _fallback_health_paths35,
                 "text": _fallback_health_text35}))

_reset26()
_enable_rich26()
_live_retry_state35 = _rich_state28(
    mid="live-retry-owner-35", panel=_budget_panel35,
    rich_plan=_budget_plan35, rich_tools=_budget_tools35["items"],
    rich_tool_count=_budget_tools35["count"])
mod.save_state(SDIR, _live_retry_state35)
_live_retry_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_live_retry_ok35 = mod._rich_deferred_edit(
    SDIR, _live_retry_tg35, "live-retry-owner-35", _raw_frame35)
_live_retry_after35 = load()
_live_retry_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _live_retry_tg35.wire if call[0] == "editMessageText"]
check("35c a successful live retry persists the exact installed wire frame",
      _live_retry_ok35 is True and len(_live_retry_frames35) == 2
      and _live_retry_after35.get("last_live") == _live_retry_frames35[1],
      _detail26({"state": _live_retry_after35,
                 "frames": _live_retry_frames35}))

_reset26()
_enable_rich26()
_recap_retry_state35 = _rich_state28(
    mid="current-owner-35", prev_recap_msgid="carried-recap-35",
    prev_recap_text=_raw_frame35, prev_recap_rich=True)
mod.save_state(SDIR, _recap_retry_state35)
_recap_retry_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_recap_retry_ok35 = mod._rich_deferred_edit(
    SDIR, _recap_retry_tg35, "carried-recap-35", _raw_frame35,
    intent="recap")
_recap_retry_after35 = load()
_recap_retry_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _recap_retry_tg35.wire if call[0] == "editMessageText"]
check("35c a successful carried-recap retry persists its exact whole-message body",
      _recap_retry_ok35 is True and len(_recap_retry_frames35) == 2
      and _recap_retry_after35.get("prev_recap_text")
      == _recap_retry_frames35[1]
      and _recap_retry_after35.get("prev_recap_rich") is True,
      _detail26({"state": _recap_retry_after35,
                 "frames": _recap_retry_frames35}))

_reset26()
_enable_rich26()
_fold_retry_state35 = _rich_state28(
    mid="fold-owner-35", last_live=_raw_frame35, folded=True)
mod.save_state(SDIR, _fold_retry_state35)
_fold_retry_tg35 = WireTG([
    {"ok": False, "error_code": 400,
     "description": "Bad Request: RICH_MESSAGE_INVALID"},
    {"ok": True, "result": True},
])
_fold_retry_ok35 = mod._rich_deferred_edit(
    SDIR, _fold_retry_tg35, "fold-owner-35", _raw_frame35, intent="fold")
_fold_retry_after35 = load()
_fold_retry_frames35 = [
    _rich_content26(call[1]).get("markdown", "")
    for call in _fold_retry_tg35.wire if call[0] == "editMessageText"]
check("35c a successful folded-owner retry refreshes last_live exactly",
      _fold_retry_ok35 is True and len(_fold_retry_frames35) == 2
      and _fold_retry_after35.get("last_live") == _fold_retry_frames35[1],
      _detail26({"state": _fold_retry_after35,
                 "frames": _fold_retry_frames35}))

_legacy_marker_frame35 = (
    "🎯 LEGACY-35\n👾 Working · rail · 1:00\n💭 Thinking\n"
    "<details><summary>📋 Plan · 12/12 done · tap</summary>\n"
    "- [x] +10 earlier\n- [x] LEGACY-NEW-A-35\n"
    "- [x] LEGACY-NEW-B-35\n</details>")
_legacy_trim35 = mod._budget_rich_frame(
    _legacy_marker_frame35, frame_limit=176)
check("35c retries retain legacy omission counts when adding newly hidden rows",
      _legacy_trim35 is not None
      and ("+11 lower-priority details hidden" in _legacy_trim35
           or "+12 lower-priority details hidden" in _legacy_trim35)
      and "+1 lower-priority detail hidden" not in _legacy_trim35,
      _detail26(_legacy_trim35))

_merge_now35 = mod.time.time()
_merge_base35, _ = mod.render_rich_final(
    SDIR,
    [{"fam": "files", "cap": "MERGE-BASE-35", "done": True}],
    _merge_now35 - 5, mission="MERGE-BASE-MISSION-35")
_merge_priority35 = mod.merge_rich_recap(
    _merge_base35, SDIR,
    [{"fam": "files", "cap": "MERGE-TRIGGER-35", "done": True}],
    _merge_now35 - 5, mission="MERGE-CONTINUATION-35",
    plan=_budget_plan35)
check("35c carried recap budgeting also protects pending and newest active detail",
      isinstance(_merge_priority35, str)
      and "KEEP-PENDING-35" in _merge_priority35
      and "KEEP-LIVE-35" in _merge_priority35
      and "COMPLETE-00-35" not in _merge_priority35
      and "plan details hidden" in _merge_priority35,
      _detail26(_merge_priority35))

_reset26()
_enable_rich26()
mod.save_state(SDIR, _fallback_state35)
_recovered_tg35 = RichRecTG([_ok26("3503")])
_recovered_ops35, _recovered_err35 = _prompt26(
    _recovered_tg35, "RECOVERED-RICH-35", "recovered-session-35")
_recovered_state35 = load()
check("35d the next box clears sticky fallback and returns to rich automatically",
      not _recovered_err35
      and _recovered_state35.get("rich_turn") is True
      and _recovered_state35.get("rich_failed") is False
      and _recovered_state35.get("rich_health_logged") is False
      and _recovered_state35.get("rich_box") is True
      and str(_recovered_state35.get("msgid")) == "3503"
      and len(_recovered_tg35.ops("send_rich")) == 1
      and not _recovered_tg35.ops("send"),
      _recovered_err35 or _detail26({"ops": _recovered_ops35,
                                     "state": _recovered_state35,
                                     "calls": _recovered_tg35.calls}))

_reset26()

print("\n===== #36 true elapsed, interrupted folds and full-picture summaries =====")

_plan36 = [
    {"id": "done-36", "subject": "DONE-PLAN-36", "status": "completed"},
    {"id": "pending-36", "subject": "PENDING-PLAN-36", "status": "pending"},
    {"id": "active-36", "subject": "ACTIVE-PLAN-36", "status": "in_progress"},
]
_steps36 = [
    {"fam": "files", "cap": "DONE-STEP-36", "tuid": "DONE-STEP-36",
     "done": True, "k": "", "aid": "", "t0": 35910.0, "secs": 5.0},
    {"fam": "shell", "cap": "OPEN-STEP-36", "tuid": "OPEN-STEP-36",
     "done": False, "k": "", "aid": "", "t0": 35920.0},
    {"fam": "files", "cap": "DONE-STEP-TWO-36", "tuid": "DONE-STEP-TWO-36",
     "done": True, "k": "", "aid": "", "t0": 35930.0, "secs": 7.0},
]
_panel36 = [{
    "tuid": "OPEN-AGENT-36", "typ": "opus-executor", "mdl": "opus",
    "lab": "STILL-RUNNING-AGENT-36", "background": True,
    "done": False, "t0": 35920.0,
}]
_small_tools36 = {
    "count": 3,
    "items": [
        {"cap": "TOOL-%02d-36" % i, "elapsed": 11 + i}
        for i in range(3)
    ],
}

_reset26()
_enable_rich26()
_clock36 = _Clock26(36000.0)
_real_time36 = mod.time.time
try:
    mod.time.time = _clock36.time
    _timer_tg36 = RichRecTG()
    _timer_session36 = "timer-session-36"
    _, _timer_prompt_err36 = _prompt26(
        _timer_tg36, "launch the durable background audit", _timer_session36)
    _clock36.advance(10)
    _, _timer_pre_err36 = _step26(
        _timer_tg36, "pre", "Agent", _agent_input34("TRUE-TIMER-AGENT-36"),
        tuid="TRUE-TIMER-36", session_id=_timer_session36)
    _timer_t0_36 = _panel_row31(load(), "TRUE-TIMER-36").get("t0")
    _clock36.advance(1)
    _, _timer_post_err36 = _step26(
        _timer_tg36, "post", "Agent", _agent_input34("TRUE-TIMER-AGENT-36"),
        tuid="TRUE-TIMER-36",
        tresponse={"isAsync": True, "status": "async_launched",
                   "agentId": "timer-agent-36"},
        session_id=_timer_session36)
    _clock36.advance(49)
    _, _timer_repop_one_err36 = _prompt26(
        _timer_tg36, "first message while it runs", _timer_session36)
    _timer_repop_one36 = load()
    _timer_row_one36 = _panel_row31(_timer_repop_one36, "TRUE-TIMER-36")
    _timer_frame_one36 = _timer_repop_one36.get("last_live") or ""
    check("36a first re-pop renders launch-anchored Agent elapsed beside a reset box clock",
          not any((_timer_prompt_err36, _timer_pre_err36, _timer_post_err36,
                   _timer_repop_one_err36))
          and _timer_t0_36 == 36010.0
          and _timer_row_one36.get("t0") == _timer_t0_36
          and "👾 Working" in _timer_frame_one36
          and "0:00" in _timer_frame_one36
          and "| ◉ TRUE-TIMER-AGENT-36 (Opus) | 00:50 |"
          in _timer_frame_one36,
          _detail26({"errors": [_timer_prompt_err36, _timer_pre_err36,
                                _timer_post_err36, _timer_repop_one_err36],
                     "row": _timer_row_one36, "frame": _timer_frame_one36}))

    _timer_mid_one36 = str(_timer_repop_one36.get("msgid"))
    _timer_react_ops36 = mod.on_react(SDIR, _timer_tg36)
    mod._run_deferred(SDIR, _timer_tg36, _timer_react_ops36)
    _timer_after_react36 = load()
    _timer_reconcile_panel36 = _timer_after_react36.get("panel") or []
    _timer_reconcile_count36 = mod._expire_background_agents(
        _timer_reconcile_panel36, _clock36.time())
    _timer_after_react36["panel"] = _timer_reconcile_panel36
    mod.save_state(SDIR, _timer_after_react36)
    _timer_after_reconcile36 = load()
    check("36a carry, same-turn react and animator reconcile never rewrite Agent t0",
          _timer_react_ops36 == []
          and str(_timer_after_react36.get("msgid")) == _timer_mid_one36
          and _timer_reconcile_count36 == 0
          and _panel_row31(_timer_after_react36, "TRUE-TIMER-36").get("t0")
          == _timer_t0_36
          and _panel_row31(_timer_after_reconcile36, "TRUE-TIMER-36").get("t0")
          == _timer_t0_36,
          _detail26({"react_ops": _timer_react_ops36,
                     "state": _timer_after_reconcile36}))

    _clock36.advance(60)
    _, _timer_repop_two_err36 = _prompt26(
        _timer_tg36, "second message while it runs", _timer_session36)
    _timer_repop_two36 = load()
    _timer_row_two36 = _panel_row31(_timer_repop_two36, "TRUE-TIMER-36")
    _timer_frame_two36 = _timer_repop_two36.get("last_live") or ""
    check("36a second re-pop still renders elapsed from the original launch",
          not _timer_repop_two_err36
          and _timer_row_two36.get("t0") == _timer_t0_36
          and "0:00" in _timer_frame_two36
          and "| ◉ TRUE-TIMER-AGENT-36 (Opus) | 01:50 |"
          in _timer_frame_two36,
          _timer_repop_two_err36 or _detail26({
              "row": _timer_row_two36, "frame": _timer_frame_two36}))

    _clock36.advance(30)
    _timer_notification36 = _agent_notification31(
        "TRUE-TIMER-36", 'Agent "TRUE-TIMER-AGENT-36" finished',
        task_id="timer-task-36")
    _, _timer_notify_err36 = _prompt26(
        _timer_tg36, _timer_notification36, _timer_session36)
    _timer_closed36 = load()
    _timer_closed_row36 = _panel_row31(_timer_closed36, "TRUE-TIMER-36")
    _clock36.advance(30)
    _timer_late_render36, _timer_late_err36 = _render_live26(
        SDIR, _timer_closed36.get("steps") or [],
        _timer_closed36.get("proc_start"), {}, mission="timer verification",
        panel=_timer_closed36.get("panel"), plan=[],
        tools={"items": [], "count": 0}, state=_timer_closed36)
    _timer_late_text36 = _text26(_timer_late_render36)
    check("36a terminal notification after two re-pops freezes launch-to-finish duration",
          not _timer_notify_err36 and not _timer_late_err36
          and _timer_closed_row36.get("done") is True
          and _timer_closed_row36.get("t0") == _timer_t0_36
          and abs(float(_timer_closed_row36.get("secs", -1)) - 140.0) < 0.001
          and "| ✓ TRUE-TIMER-AGENT-36 (Opus) | 02:20 |"
          in _timer_late_text36,
          _timer_notify_err36 or _timer_late_err36 or _detail26({
              "row": _timer_closed_row36, "frame": _timer_late_text36}))
finally:
    mod.time.time = _real_time36

_clock_fold36 = _Clock26(36000.0)
_real_time_fold36 = mod.time.time
try:
    mod.time.time = _clock_fold36.time
    _cut_rich_state36 = _rich_state28(
        mid="cut-rich-36", now=36000.0, steps=_steps36, proc_start=35900.0,
        folded=False, typing_done=False, box_is_last=False, panel=_panel36,
        rich_plan=_plan36, rich_tools=_small_tools36["items"],
        rich_tool_count=_small_tools36["count"])
    _cut_rich_ops36, _, _cut_rich_text36 = mod._collapse_prior_ex(
        _cut_rich_state36, "CUT-MISSION-36", sdir=SDIR)
    _cut_rich_frame36 = _cut_rich_ops36[0][2] if _cut_rich_ops36 else ""
    _done_rich_state36 = dict(_cut_rich_state36, msgid="done-rich-36",
                              typing_done=True)
    _done_rich_ops36, _, _done_rich_text36 = mod._collapse_prior_ex(
        _done_rich_state36, "DONE-MISSION-36", sdir=SDIR)
    _done_rich_frame36 = _done_rich_ops36[0][2] if _done_rich_ops36 else ""
    check("36b a live untyped boundary is labeled interrupted with elapsed, step and Agent meta",
          _cut_rich_ops36 and _cut_rich_ops36[0][0] == "rfold"
          and _cut_rich_frame36.startswith(
              "<details><summary>⏸ Interrupted by user after 1:40 · 2/3 steps · 1 agent · tap</summary>")
          and "◉ STILL-RUNNING-AGENT-36" in _cut_rich_frame36
          and "✓ STILL-RUNNING-AGENT-36" not in _cut_rich_frame36
          and _cut_rich_text36 == _cut_rich_frame36,
          _detail26({"ops": _cut_rich_ops36, "frame": _cut_rich_frame36}))
    check("36b a Stop-completed fold keeps the normal header",
          _done_rich_ops36 and _done_rich_ops36[0][0] == "rfold"
          and _done_rich_frame36.startswith("<details><summary>✅ Done in 1:40")
          and "Interrupted by user" not in _done_rich_frame36
          and _done_rich_text36 == _done_rich_frame36,
          _detail26({"ops": _done_rich_ops36, "frame": _done_rich_frame36}))

    _cut_plain_state36 = dict(
        _cut_rich_state36, msgid="cut-plain-36", rich_turn=False,
        rich_box=False, folded_above_rich=False, prev_recap_rich=False)
    _cut_plain_ops36, _, _cut_plain_text36 = mod._collapse_prior_ex(
        _cut_plain_state36, "CUT-PLAIN-MISSION-36", sdir=SDIR)
    _cut_plain_frame36 = _cut_plain_ops36[0][2] if _cut_plain_ops36 else ""
    check("36b MarkdownV2 cutoff mirror is interrupted, escaped and keeps an open Agent honest",
          _cut_plain_ops36 and _cut_plain_ops36[0][0] == "edit"
          and _cut_plain_frame36.startswith("⏸ Interrupted by user")
          and "◉ running" in _cut_plain_frame36
          and "STILL\\-RUNNING\\-AGENT\\-36" in _cut_plain_frame36
          and _cut_plain_frame36.endswith("||")
          and mdv2_fully_escaped(_cut_plain_frame36),
          _detail26({"ops": _cut_plain_ops36, "frame": _cut_plain_frame36}))

    _rich_direct36, _rich_direct_err36 = _render_final26(
        SDIR, _steps36, 35900.0, mission="FULL-PICTURE-MISSION-36",
        panel=_panel36, plan=_plan36, outcome="interrupted by user",
        tools=_small_tools36)
    _rich_direct_text36 = _text26(_rich_direct36)
    _plain_direct36, _plain_direct_err36 = _call26(
        "render_final", _steps36, 35900.0, mission="FULL-PICTURE-MISSION-36",
        panel=_panel36, plan=_plan36, tools=_small_tools36,
        outcome="interrupted by user")
    _plain_direct_text36 = _text26(_plain_direct36)

    check("36c rich fold uses one balanced dropdown with blank-line Mission, Agents, Plan and Steps sections and NO Tools section",
          not _rich_direct_err36
          and _rich_direct_text36.count("<details>") == 1
          and _rich_direct_text36.count("</details>") == 1
          and mod._rich_markup_balanced(_rich_direct_text36)
          and "\n\n**Mission**" in _rich_direct_text36
          and "\n\n**Agents · 0 done today**\n\n" in _rich_direct_text36
          and "\n\n**📋 Plan · 1/3 done**\n\n" in _rich_direct_text36
          and "\n\n**Steps**\n\n" in _rich_direct_text36
          and "🔧 Tools" not in _rich_direct_text36
          and "TOOL-00-36" not in _rich_direct_text36
          and "- [x] DONE-PLAN-36" in _rich_direct_text36
          and "- [ ] PENDING-PLAN-36" in _rich_direct_text36,
          _rich_direct_err36 or _detail26(_rich_direct_text36))

    check("36c plain fold mirrors Plan and drops Tools within the UTF-16 and MarkdownV2 contract",
          not _plain_direct_err36 and bool(_plain_direct_text36)
          and _plain_direct_text36.startswith("⏸ Interrupted by user")
          and "📋 Plan · 1/3 done" in _plain_direct_text36
          and "🔧 Tools" not in _plain_direct_text36
          and "TOOL\\-00\\-36" not in _plain_direct_text36
          and "⏳" in _plain_direct_text36
          and mod.tg_len(_plain_direct_text36) <= mod.TG_HARD_LIMIT
          and _plain_direct_text36.endswith("||")
          and mdv2_fully_escaped(_plain_direct_text36),
          _plain_direct_err36 or _detail26(_plain_direct_text36))

    _rich_pair36 = mod._prev_pair("rich-pair-36", _rich_direct_text36, rich=True)
    _plain_pair36 = mod._prev_pair("plain-pair-36", _plain_direct_text36, rich=False)
    check("36d both recap detectors and carried-pair guards recognize interrupted headers",
          mod._is_rich_recap_text(_rich_direct_text36)
          and mod._is_recap_text(_plain_direct_text36)
          and _rich_pair36 == ("rich-pair-36", _rich_direct_text36)
          and _plain_pair36 == ("plain-pair-36", _plain_direct_text36),
          _detail26({"rich_pair": _rich_pair36, "plain_pair": _plain_pair36}))

    _cont_plan36 = [{"id": "cont-plan-36", "subject": "CONT-PLAN-36",
                     "status": "in_progress"}]
    _cont_tools36 = {"count": 1, "items": [
        {"cap": "CONT-TOOL-36", "elapsed": 9}]}
    _cont_steps36 = [{"fam": "files", "cap": "CONT-STEP-36",
                      "done": False, "k": "", "aid": ""}]
    _cont_panel36 = [{"tuid": "CONT-AGENT-36", "lab": "CONT-AGENT-36",
                      "background": True, "done": False, "t0": 35991.0}]
    _rich_merged36, _rich_merge_err36 = _call26(
        "merge_rich_recap", _rich_direct_text36, SDIR, _cont_steps36, 35990.0,
        mission="CONT-MISSION-36", panel=_cont_panel36, plan=_cont_plan36,
        tools=_cont_tools36, outcome="interrupted by user")
    _plain_merged36, _plain_merge_err36 = _call26(
        "merge_recap", _plain_direct_text36, _cont_steps36, 35990.0,
        mission="CONT-MISSION-36", panel=_cont_panel36, plan=_cont_plan36,
        tools=_cont_tools36, outcome="interrupted by user")
    _rich_merged_text36 = _text26(_rich_merged36)
    _plain_merged_text36 = _text26(_plain_merged36)
    check("36d rich interrupted recap merges a full-picture continuation without nesting details",
          not _rich_merge_err36 and bool(_rich_merged_text36)
          and _rich_merged_text36.count("<details>") == 1
          and _rich_merged_text36.count("</details>") == 1
          and "\n⏸ Interrupted by user after 0:10 · 0/1 steps · 1 agent\n" in
              _rich_merged_text36

          and all(token in _rich_merged_text36 for token in (
              "CONT-MISSION-36", "CONT-AGENT-36", "CONT-PLAN-36",
              "CONT-STEP-36"))
          and "CONT-TOOL-36" not in _rich_merged_text36,
          _rich_merge_err36 or _detail26(_rich_merged_text36))
    check("36d plain interrupted recap merges the same full-picture continuation",
          not _plain_merge_err36 and bool(_plain_merged_text36)
          and _plain_merged_text36.endswith("||")
          and mod.tg_len(_plain_merged_text36) <= mod.TG_HARD_LIMIT
          and mdv2_fully_escaped(_plain_merged_text36)
          and (">" + mod.escape_mdv2(
              "⏸ Interrupted by user · 0:10 · 0/1 steps · 1 agent")) in
              _plain_merged_text36

          and all(token.replace("-", "\\-") in _plain_merged_text36 for token in (
              "CONT-MISSION-36", "CONT-AGENT-36", "CONT-PLAN-36",
              "CONT-STEP-36"))
          and "CONT\\-TOOL\\-36" not in _plain_merged_text36,
          _plain_merge_err36 or _detail26(_plain_merged_text36))

    _dispatch_state36 = _rich_state28(
        mid="dispatch-live-36", now=36000.0, steps=_cont_steps36,
        proc_start=35990.0, panel=_cont_panel36, rich_plan=_cont_plan36,
        rich_tools=_cont_tools36["items"], rich_tool_count=1,
        prev_recap_msgid="dispatch-base-36",
        prev_recap_text=_rich_direct_text36, prev_recap_rich=True)
    _dispatch_merge36, _dispatch_merge_err36 = _call26(
        "_noreply_merge_ex", _dispatch_state36, _cont_steps36,
        "DISPATCH-MISSION-36", outcome="interrupted by user")

    check("36d no-reply dispatcher threads Plan and interruption into the carried recap, without Tools",
          not _dispatch_merge_err36 and isinstance(_dispatch_merge36, tuple)
          and len(_dispatch_merge36) == 3 and _dispatch_merge36[2] is True
          and "\n⏸ Interrupted by user after 0:10 · 0/1 steps · 1 agent\n" in
              (_dispatch_merge36[1] or "")
          and all(token in (_dispatch_merge36[1] or "") for token in (
              "DISPATCH-MISSION-36", "CONT-PLAN-36", "Interrupted by user"))
          and "CONT-TOOL-36" not in (_dispatch_merge36[1] or ""),
          _dispatch_merge_err36 or _detail26(_dispatch_merge36))
finally:
    mod.time.time = _real_time_fold36

_reset26()
_enable_rich26()
_double_state36 = _rich_state28(
    mid="double-stop-36", steps=[dict(_steps36[0])], panel=[],
    rich_plan=[dict(_plan36[0])], rich_tools=_small_tools36["items"],
    rich_tool_count=3, box_is_last=False, replied_this_turn=False)
mod.save_state(SDIR, _double_state36)
_double_tg36 = RichRecTG()
_double_ops_one36 = mod.record_reply(SDIR, _double_tg36, final=True)
mod._run_deferred(SDIR, _double_tg36, _double_ops_one36)
_double_edit_count36 = len(_double_tg36.ops("edit_rich"))
_double_ops_two36 = mod.record_reply(SDIR, _double_tg36, final=True)
mod._run_deferred(SDIR, _double_tg36, _double_ops_two36)
_double_after36 = load()
check("36e double Stop is idempotent and never reclassifies a completed fold as interrupted",
      _double_edit_count36 == 1
      and len(_double_tg36.ops("edit_rich")) == 1
      and _double_after36.get("folded") is True
      and _double_after36.get("replied_this_turn") is False
      and "✅ Done" in (_double_after36.get("last_live") or "")
      and "Interrupted by user" not in (_double_after36.get("last_live") or ""),
      _detail26({"first": _double_ops_one36, "second": _double_ops_two36,
                 "calls": _double_tg36.calls, "state": _double_after36}))

_reset26()
_enable_rich26()
_react_clock36 = _Clock26(36600.0)
_real_react_time36 = mod.time.time
try:
    mod.time.time = _react_clock36.time
    _react_state36 = _rich_state28(
        mid="react-cutoff-36", now=36600.0, proc_start=36500.0,
        steps=[dict(_steps36[0])], panel=[], folded=False, typing_done=False,
        rich_plan=_plan36, rich_tools=_small_tools36["items"], rich_tool_count=3)
    mod.save_state(SDIR, _react_state36)
    _react_tg36 = RichRecTG()
    _react_cutoff_ops36 = mod.on_react(SDIR, _react_tg36)
    _react_cutoff_frame36 = _react_cutoff_ops36[0][2] if _react_cutoff_ops36 else ""
    check("36e react preserves a fresh same-turn box but labels a genuine fallback collapse interrupted",
          _timer_react_ops36 == []
          and _react_cutoff_ops36 and _react_cutoff_ops36[0][0] == "rfold"
          and "⏸ Interrupted by user" in _react_cutoff_frame36,
          _detail26({"fresh_ops": _timer_react_ops36,
                     "fallback_ops": _react_cutoff_ops36}))
finally:
    mod.time.time = _real_react_time36

_reset26()
_enable_rich26()
_retention_clock36 = _Clock26(37000.0)
_real_retention_time36 = mod.time.time
try:
    mod.time.time = _retention_clock36.time
    _retained_seed36 = [
        {"cap": "RETAIN-%02d-36" % i, "elapsed": i}
        for i in range(60)
    ]
    _retention_state36 = _rich_state28(
        mid="retention-36", now=37000.0, proc_start=36900.0,
        steps=[{"fam": "files", "cap": "NEWEST-TOOL-36",
                "tuid": "NEWEST-TOOL-36", "done": False, "k": "",
                "aid": "", "t0": 36995.0}],
        rich_tools=_retained_seed36, rich_tool_count=60)
    mod.save_state(SDIR, _retention_state36)
    _, _retention_err36 = _step26(
        RichRecTG(), "post", "Read", {"file_path": "/tmp/retention-36"},
        tuid="NEWEST-TOOL-36")
    _retention_after36 = load()
    _retention_items36 = _retention_after36.get("rich_tools") or []
    check("36f runtime retains the newest 60 bounded tool records and an uncapped total",
          not _retention_err36 and mod.RICH_TOOL_LINES == 60
          and len(_retention_items36) == 60
          and _retention_after36.get("rich_tool_count") == 61
          and "… 1 earlier not shown" in
              (_retention_after36.get("last_live") or "")
          and not any(item.get("cap") == "RETAIN-00-36"
                      for item in _retention_items36)
          and any(item.get("cap") == "RETAIN-01-36"
                  for item in _retention_items36)
          and _retention_items36[-1].get("cap") == "NEWEST-TOOL-36"
          and all(len(str(item.get("cap") or "")) <= mod.RICH_TOOL_LINE_CAP
                  for item in _retention_items36),
          _retention_err36 or _detail26(_retention_after36))
finally:
    mod.time.time = _real_retention_time36

_huge_plan36 = [
    {"id": "huge-pending-36", "subject": "KEEP-PENDING-36", "status": "pending"},
]
_huge_plan36.extend({
    "id": "huge-complete-%02d-36" % i,
    "subject": "COMPLETE-%02d-36 %s" % (i, "c" * 170),
    "status": "completed",
} for i in range(40))
_huge_plan36.append({
    "id": "huge-active-36", "subject": "KEEP-ACTIVE-36", "status": "in_progress",
})
_huge_tools36 = {
    "count": 75,
    "items": [
        {"cap": "HUGE-TOOL-%02d-36 %s" % (i, "t" * 170), "elapsed": i + 1}
        for i in range(60)
    ],
}
_huge_panel36 = [
    {"tuid": "HUGE-AGENT-%d-36" % i, "lab": "KEEP-AGENT-%d-36" % i,
     "background": True, "done": False, "t0": 37900.0 + i}
    for i in range(3)
]
_huge_rich36, _huge_rich_err36 = _render_final26(
    SDIR, _steps36, 37900.0, mission="KEEP-HUGE-MISSION-36",
    panel=_huge_panel36, plan=_huge_plan36, outcome="interrupted by user",
    tools=_huge_tools36)
_huge_rich_text36 = _text26(_huge_rich36)
_huge_tool_rows36 = len(re.findall(r"^- at ", _huge_rich_text36, flags=re.M))
_huge_tool_omission_match36 = re.search(
    r"… (\d+) earlier not shown", _huge_rich_text36)
_huge_tool_omitted36 = int(_huge_tool_omission_match36.group(1))\
    if _huge_tool_omission_match36 else -1

check("36g rich oversize shrink yields Plan detail, carries no Tools section at all, and never clips markup or Agents",
      not _huge_rich_err36 and bool(_huge_rich_text36)
      and len(_huge_rich_text36) < mod.RICH_FRAME_LIMIT
      and mod._rich_markup_balanced(_huge_rich_text36)
      and _huge_rich_text36.count("<details>") == 1
      and _huge_rich_text36.count("</details>") == 1
      and "KEEP-HUGE-MISSION-36" in _huge_rich_text36
      and all("KEEP-AGENT-%d-36" % i in _huge_rich_text36 for i in range(3))
      and "**📋 Plan" in _huge_rich_text36
      and "🔧 Tools" not in _huge_rich_text36
      and "KEEP-PENDING-36" in _huge_rich_text36
      and "KEEP-ACTIVE-36" in _huge_rich_text36
      and "COMPLETE-00-36" not in _huge_rich_text36
      and _huge_tool_rows36 == 0
      and _huge_tool_omitted36 == -1,
      _huge_rich_err36 or _detail26({
          "shown": _huge_tool_rows36, "omitted": _huge_tool_omitted36,
          "frame": _huge_rich_text36}))

_huge_plain36, _huge_plain_err36 = _call26(
    "render_final", _steps36, 37900.0, mission="KEEP-HUGE-MISSION-36",
    panel=_huge_panel36, plan=_huge_plan36, tools=_huge_tools36,
    outcome="interrupted by user")
_huge_plain_text36 = _text26(_huge_plain36)
_huge_plain_tool_rows36 = len(re.findall(
    r"^>\\- at ", _huge_plain_text36, flags=re.M))
_huge_plain_tool_omission_match36 = re.search(
    r"^>… (\d+) earlier not shown(?:\|\|)?$", _huge_plain_text36, flags=re.M)
_huge_plain_tool_omitted36 = int(_huge_plain_tool_omission_match36.group(1))\
    if _huge_plain_tool_omission_match36 else -1
check("36g plain oversize shrink preserves the full-picture core and closes its dropdown",
      not _huge_plain_err36 and bool(_huge_plain_text36)
      and mod.tg_len(_huge_plain_text36) <= mod.TG_HARD_LIMIT
      and _huge_plain_text36.endswith("||")
      and mdv2_fully_escaped(_huge_plain_text36)
      and "KEEP\\-HUGE\\-MISSION\\-36" in _huge_plain_text36
      and all("KEEP\\-AGENT\\-%d\\-36" % i in _huge_plain_text36 for i in range(3))
      and "📋 Plan" in _huge_plain_text36

      and "🔧 Tools" not in _huge_plain_text36
      and "KEEP\\-PENDING\\-36" in _huge_plain_text36
      and "KEEP\\-ACTIVE\\-36" in _huge_plain_text36
      and _huge_plain_tool_rows36 == 0
      and _huge_plain_tool_omitted36 == -1,
      _huge_plain_err36 or _detail26({
          "shown": _huge_plain_tool_rows36,
          "omitted": _huge_plain_tool_omitted36,
          "frame": _huge_plain_text36}))

_max_caption36 = "😀" * mod.RICH_TOOL_LINE_CAP
_max_steps36 = [
    {"fam": "files", "cap": _max_caption36, "done": i % 2 == 0,
     "k": "", "aid": ""}
    for i in range(mod.MAX_STORED_STEPS)
]
_max_plan36 = [
    {"id": "max-plan-%d-36" % i, "subject": _max_caption36,
     "status": "completed" if i < mod.MAX_STORED_STEPS // 2 else "pending"}
    for i in range(mod.MAX_STORED_STEPS)
]
_max_tools36 = {
    "count": mod.RICH_TOOL_LINES,
    "items": [{"cap": _max_caption36, "elapsed": i}
              for i in range(mod.RICH_TOOL_LINES)],
}
_max_panel36 = [
    {"lab": "MAX-AGENT-%d-36" % i, "typ": "agent", "mdl": "model",
     "t0": 1.0, "done": False, "background": True}
    for i in range(mod.PANEL_ROWS)
]
_max_counts36 = {"body": 0, "length": 0}
_real_body_parts36 = mod._final_body_parts
_real_tg_len36 = mod.tg_len
def _counted_body_parts36(*args, **kwargs):
    _max_counts36["body"] += 1
    return _real_body_parts36(*args, **kwargs)
def _counted_tg_len36(value):
    _max_counts36["length"] += 1
    return _real_tg_len36(value)
try:
    mod._final_body_parts = _counted_body_parts36
    mod.tg_len = _counted_tg_len36
    _max_plain36, _ = mod.render_final(
        _max_steps36, 1.0, mission=_max_caption36, panel=_max_panel36,
        plan=_max_plan36, tools=_max_tools36, outcome="interrupted by user")
    _max_merge36 = mod.merge_recap(
        _plain_direct_text36, _max_steps36, 1.0,
        mission=_max_caption36, panel=_max_panel36,
        plan=_max_plan36, tools=_max_tools36, outcome="interrupted by user")
finally:
    mod._final_body_parts = _real_body_parts36
    mod.tg_len = _real_tg_len36
check("36g max-state plain fold and merge use bounded shrink rebuilds under the lock",
      bool(_max_plain36) and bool(_max_merge36)
      and _real_tg_len36(_max_plain36) <= mod.TG_HARD_LIMIT
      and _real_tg_len36(_max_merge36) <= mod.TG_HARD_LIMIT
      and _max_plain36.endswith("||") and _max_merge36.endswith("||")
      and _max_counts36["body"] == 2
      and _max_counts36["length"] <= 80,
      _detail26({"counts": _max_counts36,
                 "fold_len": _real_tg_len36(_max_plain36 or ""),
                 "merge_len": _real_tg_len36(_max_merge36 or "")}))

_plan_live36, _plan_live_err36 = _render_live26(
    SDIR, _steps36, mod.time.time() - 5, {}, mission="PLAN-LIVE-36",
    panel=[], plan=_plan36, tools=_small_tools36)
_plan_fallback36, _plan_fallback_err36 = _render_live26(
    SDIR, _steps36, mod.time.time() - 5, {}, mission="PLAN-FALLBACK-36",
    panel=[], plan=_plan36, tools=_small_tools36, fallback=True)
_plan_outputs36 = [
    _text26(_plan_live36), _text26(_plan_fallback36),
    _rich_direct_text36, _plain_direct_text36,
]
_old_plan_label36 = "The " + "plan"
check("36h rich live, plain fallback and both fold transports render the renamed Plan header",
      not _plan_live_err36 and not _plan_fallback_err36
      and all("📋 Plan" in text for text in _plan_outputs36)
      and "<details><summary>📋 Plan · 1/3 done · tap</summary>"
      in _plan_outputs36[0]
      and all(_old_plan_label36 not in text for text in _plan_outputs36),
      _detail26(_plan_outputs36))

_reset26()

print("\n===== #37 V-NEXT context, cumulative agents and never-ugly transport =====")

_ctx_now37 = _et_today(12, 0)
_ctx_path37 = os.path.join(STATE_DIR, "context-37.jsonl")
_ctx_valid37 = {
    "type": "assistant", "isSidechain": False,
    "message": {
        "model": "claude-fable-5",
        "usage": {
            "input_tokens": 1000,
            "cache_read_input_tokens": 300000,
            "cache_creation_input_tokens": 79000,
        },
    },
}
_ctx_sidechain37 = {
    "type": "assistant", "isSidechain": True,
    "message": {
        "model": "claude-fable-5",
        "usage": {"input_tokens": 990000},
    },
}
with open(_ctx_path37, "wb") as _ctx_file37:
    _ctx_file37.write((json.dumps(_ctx_valid37) + "\n").encode())
    _ctx_file37.write((json.dumps(_ctx_sidechain37) + "\n").encode())
    _ctx_file37.write(b'{"type":"assistant","message":')
_ctx_pct37, _ctx_model37 = mod._read_context_snapshot(
    _ctx_path37, "MODEL=claude-fable-5[1m]")
check("37a transcript reader skips sidechains and a partial final line",
      _ctx_pct37 == 38 and _ctx_model37 == "Fable 5",
      _detail26({"pct": _ctx_pct37, "model": _ctx_model37}))

_ctx_ambiguous_path37 = os.path.join(STATE_DIR, "context-ambiguous-37.jsonl")
with open(_ctx_ambiguous_path37, "w") as _ctx_file37:
    _ctx_file37.write(json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 50000,
                "cache_read_input_tokens": 25000,
                "cache_creation_input_tokens": 25000,
            },
        },
    }) + "\n")
_ctx_ambiguous37 = mod._read_context_snapshot(
    _ctx_ambiguous_path37, "MODEL=claude-opus-5")
_ctx_missing37 = mod._read_context_snapshot(
    os.path.join(STATE_DIR, "missing-context-37.jsonl"),
    "MODEL=claude-sonnet-5")
check("37a ambiguous caps use 200k and missing files preserve the model placeholder",
      _ctx_ambiguous37 == (50, "Opus 5")
      and _ctx_missing37 == (None, "Sonnet 5"),
      _detail26({"ambiguous": _ctx_ambiguous37, "missing": _ctx_missing37}))
_ctx_large_path37 = os.path.join(STATE_DIR, "context-large-37.jsonl")
with open(_ctx_large_path37, "wb") as _ctx_file37:
    _ctx_file37.write(b"x" * (mod.CONTEXT_TAIL_BYTES + 8192) + b"\n")
    _ctx_file37.write((json.dumps(_ctx_valid37) + "\n").encode())
_ctx_large37 = mod._read_context_snapshot(
    _ctx_large_path37, "MODEL=claude-fable-5")
_ctx_fifo_path37 = os.path.join(STATE_DIR, "context-fifo-37")
try:
    os.mkfifo(_ctx_fifo_path37)
    _ctx_fifo37 = mod._read_context_snapshot(
        _ctx_fifo_path37, "MODEL=claude-sonnet-5")
finally:
    if os.path.lexists(_ctx_fifo_path37):
        os.remove(_ctx_fifo_path37)
check("37a transcript scan is capped at 256 KB and refuses a FIFO before open",
      mod.CONTEXT_TAIL_BYTES == 256 * 1024
      and _ctx_large37 == (38, "Fable 5")
      and _ctx_fifo37 == (None, "Sonnet 5"),
      _detail26({"tail_bytes": mod.CONTEXT_TAIL_BYTES,
                 "large": _ctx_large37, "fifo": _ctx_fifo37}))
check("37a transcript fallback maps cwd and session id to the verified projects path",
      mod._context_transcript_path(
          None, "session-37", "/Users/example/claude-channels-6")
      == os.path.expanduser(
          "~/.claude/projects/-Users-example-claude-channels-6/session-37.jsonl"),
      mod._context_transcript_path(
          None, "session-37", "/Users/example/claude-channels-6"))

_ctx_state37 = {
    "ctx_pct": 38, "ctx_model": "claude-fable-5",
    "agent_history": mod._empty_agent_history(_ctx_now37),
    "rich_session_id": "context-session-37",
}
_real_ctx_time37 = mod.time.time
try:
    mod.time.time = lambda: _ctx_now37
    _ctx_rich37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Building the Foxtrot call script",
        panel=[], plan=[], tools={"items": [], "count": 0},
        frame=3, state=_ctx_state37)
    _ctx_plain37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Building the Foxtrot call script",
        panel=[], plan=[], tools={"items": [], "count": 0},
        fallback=True, frame=3, state=_ctx_state37)
    _ctx_unknown_rich37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Unknown context",
        panel=[], plan=[], tools={"items": [], "count": 0},
        frame=1, state={"ctx_pct": None, "ctx_model": "claude-sonnet-5"})
    _ctx_unknown_plain37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Unknown context",
        panel=[], plan=[], tools={"items": [], "count": 0},
        fallback=True, frame=1,
        state={"ctx_pct": None, "ctx_model": "claude-sonnet-5"})
finally:
    mod.time.time = _real_ctx_time37
_ctx_rich_lines37 = [line.rstrip() for line in _ctx_rich37.splitlines()]
_ctx_plain_lines37 = [line.rstrip() for line in _ctx_plain37.splitlines()]
check("37a live context values render identically in rich and plain at fixed line three",
      _ctx_rich_lines37[2] == "🧠 Fable 5 · ▓▓▓░░░░░ 38% context"
      and _ctx_plain_lines37[2] == _ctx_rich_lines37[2]
      and _ctx_rich_lines37[1].startswith("👾 Working")
      and _ctx_rich_lines37[3].startswith("💭"),
      _detail26({"rich": _ctx_rich_lines37[:4],
                 "plain": _ctx_plain_lines37[:4]}))
check("37a unknown context never removes or moves the model line",
      [line.rstrip() for line in _ctx_unknown_rich37.splitlines()][2]
      == "🧠 Sonnet 5 · ░░░░░░░░ ??% context"
      and [line.rstrip() for line in _ctx_unknown_plain37.splitlines()][2]
      == "🧠 Sonnet 5 · ░░░░░░░░ ??% context",
      _detail26({"rich": _ctx_unknown_rich37,
                 "plain": _ctx_unknown_plain37}))

_ctx_corrupt_lines37 = [
    mod._context_line({"ctx_pct": value, "ctx_model": model})
    for value, model in (
        (float("nan"), True), (float("inf"), 7),
        (float("-inf"), {"model": "opus"}), (None, ["sonnet"]))
]
check("37a corrupt and non-finite context state heals to fixed unknown furniture",
      all(line == "🧠 Unknown model · ░░░░░░░░ ??% context"
          for line in _ctx_corrupt_lines37)
      and mod._usage_token(float("inf")) == 0
      and mod._usage_token(float("nan")) == 0,
      _detail26(_ctx_corrupt_lines37))

_history_session37 = "history-session-37"
_history_state37 = {
    "rich_turn": True, "rich_session_id": _history_session37,
    "agent_history": mod._empty_agent_history(_ctx_now37),
}
_sync_panel37 = mod._panel_open(
    [], "SYNC-37",
    {"description": "sync-agent", "subagent_type": "opus-executor",
     "model": "claude-opus-5"},
    _ctx_now37 - 90, session_id=_history_session37)
_sync_panel37 = mod._panel_close(
    _sync_panel37, "SYNC-37", _ctx_now37,
    history_state=_history_state37, session_id=_history_session37)
_sync_rows37 = _history_state37["agent_history"]["rows"]

check("37b synchronous Agent close appends one frozen Opus record",
      _history_state37["agent_history"]["total"] == 1
      and _sync_rows37[-1]["family"] == "Opus 5"
      and _sync_rows37[-1]["secs"] == 90
      and _sync_rows37[-1]["outcome"] == "completed",
      _detail26(_history_state37["agent_history"]))

_failed_panel37 = mod._panel_open(
    [], "FAILED-37",
    {"description": "failed-agent", "subagent_type": "worker",
     "model": "claude-sonnet-5", "run_in_background": True},
    _ctx_now37 - 30, session_id=_history_session37)
_failed_launched37 = mod._panel_accept_background_launch(
    _failed_panel37, "FAILED-37",
    {"success": False, "status": "failed", "error": "launch refused"})
_failed_panel37 = mod._panel_close(
    _failed_panel37, "FAILED-37", _ctx_now37,
    history_state=_history_state37, session_id=_history_session37)

check("37b failed Agent launch closes honestly and never becomes a running lane",
      _failed_launched37 is False
      and _history_state37["agent_history"]["total"] == 1
      and len(_history_state37["agent_history"]["rows"]) == 2
      and _history_state37["agent_history"]["rows"][-1]["outcome"] == "failed"
      and _failed_panel37[0].get("background") is not True,
      _detail26({"panel": _failed_panel37,
                 "history": _history_state37["agent_history"]}))

_stale_panel37 = [{
    "tuid": "STALE-AGENT-37", "lab": "stale-agent",
    "typ": "worker", "mdl": "claude-fable-5",
    "background": True, "done": False,
    "t0": _ctx_now37 - mod.CODEX_BG_MAX_AGE - 1,
    "history_session_id": _history_session37,
}]
_stale_count37 = mod._expire_background_agents(
    _stale_panel37, _ctx_now37,
    history_state=_history_state37, session_id=_history_session37)
check("37b background Agent stale expiry appends the stale outcome once",
      _stale_count37 == 1
      and _history_state37["agent_history"]["total"] == 1
      and len(_history_state37["agent_history"]["rows"]) == 3
      and any(row.get("id", "").endswith(":agent:STALE-AGENT-37")
              and row.get("outcome") == "stale"
              for row in _history_state37["agent_history"]["rows"]),
      _detail26(_history_state37["agent_history"]))

_codex_steps37 = [{
    "tuid": "CODEX-INFERRED-37", "cap": "Codex Sol · streamer-fix",
    "codex_bg": True, "done": False, "t0": _ctx_now37 - 41,
    "history_session_id": _history_session37,
}]
_real_codex_alive37 = mod._codex_alive
try:
    mod._codex_alive = lambda step=None: False
    _codex_closed37 = mod._close_gone_codex_bg(
        _codex_steps37, _ctx_now37,
        history_state=_history_state37, session_id=_history_session37)
finally:
    mod._codex_alive = _real_codex_alive37

check("37b inferred Codex close stores Codex family and a de-duplicated label",
      _codex_closed37 == 1
      and _history_state37["agent_history"]["total"] == 1
      and len(_history_state37["agent_history"]["rows"]) == 4
      and any(row.get("id", "").endswith(":codex:CODEX-INFERRED-37")
              and row.get("family") == "Codex"
              and row.get("label") == "streamer-fix"
              and row.get("outcome") == "stopped"
              for row in _history_state37["agent_history"]["rows"]),
      _detail26(_history_state37["agent_history"]))

_stale_codex37 = [{
    "tuid": "CODEX-STALE-37", "cap": "Codex Terra · stale-fix",
    "codex_bg": True, "done": False,
    "t0": _ctx_now37 - mod.CODEX_BG_MAX_AGE - 1,
    "history_session_id": _history_session37,
}]
_codex_stale_count37 = mod._expire_codex_bg(
    _stale_codex37, _ctx_now37,
    history_state=_history_state37, session_id=_history_session37)
check("37b Codex stale expiry appends a frozen stale record",
      _codex_stale_count37 == 1
      and _history_state37["agent_history"]["total"] == 1
      and len(_history_state37["agent_history"]["rows"]) == 5
      and any(row.get("id", "").endswith(":codex:CODEX-STALE-37")
              and row.get("label") == "stale-fix"
              and row.get("outcome") == "stale"
              for row in _history_state37["agent_history"]["rows"]),
      _detail26(_history_state37["agent_history"]))

_stale_tree37 = mod._agent_tree_lines([{
    "label": "stale-fix", "family": "Codex", "running": False,
    "secs": mod.CODEX_BG_MAX_AGE + 1, "outcome": "stale",
}])
check("37b cumulative Codex rows render one terminal outcome outside the label",
      _stale_tree37
      == ["└ stale-fix (Codex) · 120:01 stale"]
      and "stale-fix · stale" not in _stale_tree37[0],
      _detail26(_stale_tree37))

_notify_panel37 = [{
    "tuid": "NOTIFY-AGENT-37", "lab": "notification-agent",
    "typ": "worker", "mdl": "claude-sonnet-5",
    "background": True, "done": False, "t0": _ctx_now37 - 61,
    "history_session_id": _history_session37,
}]
_notify_state37 = _rich_state28(
    mid=None, now=_ctx_now37, steps=[], panel=_notify_panel37,
    rich_box=False, rich_session_id=_history_session37,
    agent_history=_history_state37["agent_history"])
_notify_result37 = mod._close_codex_notification(
    SDIR, _notify_state37,
    _agent_notification31(
        "NOTIFY-AGENT-37", 'Agent "notification-agent" finished',
        task_id="notify-agent-task-37"))
_history_state37["agent_history"] = _notify_state37["agent_history"]
check("37b exact background notification appends once and duplicate is a no-op",
      isinstance(_notify_result37, list)
      and _history_state37["agent_history"]["total"] == 2
      and len(_history_state37["agent_history"]["rows"]) == 6
      and mod._close_codex_notification(
          SDIR, _notify_state37,
          _agent_notification31(
              "NOTIFY-AGENT-37", 'Agent "notification-agent" finished',
              task_id="notify-agent-task-37")) == []
      and _notify_state37["agent_history"]["total"] == 2,
      _detail26(_notify_state37["agent_history"]))

_late_codex_state37 = _rich_state28(
    mid=None, now=_ctx_now37, steps=_codex_steps37, panel=[],
    rich_box=False, rich_session_id=_history_session37,
    agent_history=_history_state37["agent_history"])
_late_codex_result37 = mod._close_codex_notification(
    SDIR, _late_codex_state37,
    _terminal_notification25(
        "CODEX-INFERRED-37", "failed", task_id="late-codex-task-37"))
_late_codex_rows37 = [
    row for row in _late_codex_state37["agent_history"]["rows"]
    if row.get("id", "").endswith(":codex:CODEX-INFERRED-37")
]

check("37b late Codex truth upserts inferred history without double-counting",
      isinstance(_late_codex_result37, list)
      and _late_codex_state37["agent_history"]["total"] == 2
      and len(_late_codex_rows37) == 1
      and _late_codex_rows37[0]["outcome"] == "failed",
      _detail26(_late_codex_state37["agent_history"]))

_before_midnight37 = mod.datetime(
    2026, 8, 5, 23, 59, tzinfo=mod.ET_ZONE).timestamp()
_boundary_row37 = [{
    "tuid": "BOUNDARY-37", "lab": "boundary-agent",
    "typ": "worker", "mdl": "claude-opus-5",
    "done": True, "t0": _before_midnight37 - 10, "secs": 5,
    "history_session_id": _history_session37,
}]
_boundary_history37 = mod._capture_closed_agents(
    None, _boundary_row37, [], _history_session37, _before_midnight37)
_after_midnight_history37 = mod._normalize_agent_history(
    _boundary_history37, now=_before_midnight37 + 120)
check("37b local midnight purges yesterday's rows, total and per-session counts",
      _boundary_history37["total"] == 1
      and _after_midnight_history37
      == mod._empty_agent_history(_before_midnight37 + 120),
      _detail26({"before": _boundary_history37,
                 "after": _after_midnight_history37}))

_cap_history37 = mod._empty_agent_history(_ctx_now37)
for _cap_index37 in range(61):
    _cap_row37 = [{
        "tuid": "CAP-%02d-37" % _cap_index37,
        "lab": "cap-agent-%02d" % _cap_index37,
        "typ": "worker", "mdl": "claude-opus-5",
        "done": True, "t0": _ctx_now37 - 100 + _cap_index37,
        "secs": 1, "history_session_id": _history_session37,
    }]
    _cap_history37 = mod._capture_closed_agents(
        _cap_history37, _cap_row37, [], _history_session37, _ctx_now37)
check("37b sixty-first closure drops only the oldest row while honest totals reach 61",
      len(_cap_history37["rows"]) == 60
      and _cap_history37["total"] == 61
      and _cap_history37["session_totals"][_history_session37] == 61
      and not any("CAP-00-37" in row.get("id", "")
                  for row in _cap_history37["rows"]),
      _detail26(_cap_history37))

_corrupt_history37 = mod._normalize_agent_history({
    "day": mod._et_day(_ctx_now37),
    "total": float("inf"),
    "session_totals": {_history_session37: float("inf")},
    "rows": [{
        "id": "CORRUPT-NUMERIC-37", "label": "corrupt numeric",
        "family": "Opus", "model": "claude-opus-5",
        "t0": float("inf"), "secs": float("nan"),
        "outcome": "</details>", "session_id": _history_session37,
        "day": mod._et_day(_ctx_now37), "closed_at": float("-inf"),
    }],
}, now=_ctx_now37)
_corrupt_tree37 = mod._agent_tree_lines(_corrupt_history37["rows"])

check("37b non-finite ledger fields and hostile outcomes normalize without markup breakage",
      _corrupt_history37["total"] == 0
      and _corrupt_history37["session_totals"][_history_session37] == 0
      and _corrupt_history37["rows"][0]["t0"] == 0
      and _corrupt_history37["rows"][0]["secs"] == 0
      and "</details>" not in _corrupt_tree37[0]
      and "‹/details›" in _corrupt_tree37[0],
      _detail26({"history": _corrupt_history37, "tree": _corrupt_tree37}))

_priority_history37 = {
    "day": mod._et_day(_ctx_now37), "total": 61,
    "session_totals": dict(
        [(_history_session37, 61)]
        + [("old-session-%02d-37" % i, 1)
           for i in range(mod.AGENT_HISTORY_SESSION_MAX)]),
    "rows": [{
        "id": "PRIORITY-37", "label": "priority agent", "family": "Codex",
        "model": "Codex", "t0": _ctx_now37 - 1, "secs": 1,
        "outcome": "completed", "session_id": _history_session37,
        "day": mod._et_day(_ctx_now37), "closed_at": _ctx_now37,
    }],
}
_priority_normalized37 = mod._normalize_agent_history(
    _priority_history37, now=_ctx_now37)
check("37b session-total cap reserves the retained chat count for honest earlier rows",
      len(_priority_normalized37["session_totals"])
      == mod.AGENT_HISTORY_SESSION_MAX
      and _priority_normalized37["session_totals"][_history_session37] == 61,
      _detail26(_priority_normalized37["session_totals"]))

_display_history37 = mod._capture_closed_agents(
    None,
    [{"tuid": "BOOKLET-37", "lab": "booklet-builder",
      "typ": "opus-executor", "mdl": "claude-opus-5",
      "done": True, "t0": _ctx_now37 - 919, "secs": 919,
      "history_session_id": _history_session37}],
    [], _history_session37, _ctx_now37)
_display_panel37 = [
    {"tuid": "STREAMER-37", "lab": "streamer-fix", "typ": "worker",
     "mdl": "gpt-5.6-sol", "done": False, "t0": _ctx_now37 - 581},
    {"tuid": "SKILL-37", "lab": "skill-author", "typ": "worker",
     "mdl": "claude-sonnet-5", "done": False, "t0": _ctx_now37 - 362},
]
_display_state37 = {
    "ctx_pct": 38, "ctx_model": "claude-fable-5",
    "rich_session_id": _history_session37,
    "agent_history": _display_history37,
}
_real_display_time37 = mod.time.time
try:
    mod.time.time = lambda: _ctx_now37
    _agent_frame_one37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Agent roster",
        panel=_display_panel37, plan=[],
        tools={"items": [{"cap": "one", "elapsed": 1}], "count": 1},
        state=_display_state37)
    mod.time.time = lambda: _ctx_now37 + 60
    _agent_frame_two37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 252, {}, "Agent roster",
        panel=_display_panel37, plan=[],
        tools={"items": [{"cap": "one", "elapsed": 1},
                         {"cap": "two", "elapsed": 2}], "count": 2},
        state=_display_state37)
finally:
    mod.time.time = _real_display_time37

_roster_one37, _roster_running37, _roster_done37, _roster_omitted37 =\
    mod._agent_display_records([], _display_panel37, _display_history37,
                               _history_session37, now=_ctx_now37)
_roster_two37 = mod._agent_display_records(
    [], _display_panel37, _display_history37, _history_session37,
    now=_ctx_now37 + 60)[0]
_roster_tree_one37 = mod._agent_tree_lines(_roster_one37, omitted=_roster_omitted37)
_roster_tree_two37 = mod._agent_tree_lines(_roster_two37)

check("37c the cumulative roster lists open rows first with family and launch clocks",
      (_roster_running37, _roster_done37) == (2, 1)
      and "├ streamer-fix (GPT-5.6 Sol) · running · 9:41" in _roster_tree_one37
      and "├ skill-author (Sonnet 5) · running · 6:02" in _roster_tree_one37
      and "└ booklet-builder (Opus 5) · 15:19 done" in _roster_tree_one37,
      _detail26({"tree": _roster_tree_one37, "running": _roster_running37,
                 "done": _roster_done37}))
check("37c running clocks advance while the completed runtime stays frozen",
      "├ streamer-fix (GPT-5.6 Sol) · running · 10:41" in _roster_tree_two37
      and "├ skill-author (Sonnet 5) · running · 7:02" in _roster_tree_two37
      and "└ booklet-builder (Opus 5) · 15:19 done" in _roster_tree_two37
      and "🔧 Tools · 1 call · tap" in _agent_frame_one37
      and "🔧 Tools · 2 calls · tap" in _agent_frame_two37,
      _detail26({"first": _roster_tree_two37, "second": _agent_frame_two37}))
check("37c the same open lanes still reach the live box through the mid-box table",
      "| ◉ streamer-fix (GPT-5.6 Sol) | 09:41 |" in _agent_frame_one37
      and "| ◉ skill-author (Sonnet 5) | 06:02 |" in _agent_frame_one37
      and "| ◉ streamer-fix (GPT-5.6 Sol) | 10:41 |" in _agent_frame_two37
      and "🤖 Agents" not in _agent_frame_one37
      and "🤖 Agents" not in _agent_frame_two37,
      _detail26({"first": _agent_frame_one37, "second": _agent_frame_two37}))

check("37c family resolver covers Fable, Opus, Sonnet, Codex and safe unknown values",
      [mod._agent_family(value) for value in (
          "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
          "gpt-5.6-sol", "custom-family")]
      == ["Fable 5", "Opus 5", "Sonnet 5", "GPT-5.6 Sol", "Custom Family"],
      _detail26([mod._agent_family(value) for value in (
          "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
          "gpt-5.6-sol", "custom-family")]))

_long_fold_rows37 = mod._rich_lane_rows(
    [{"cap": "Codex Sol · " + "C" * 70, "codex_bg": True,
      "done": True, "t0": _ctx_now37 - 5, "secs": 5}],
    [{"lab": "A" * 70, "typ": "opus-executor", "mdl": "claude-opus-5",
      "done": True, "t0": _ctx_now37 - 9, "secs": 9}],
    now=_ctx_now37, fold=True)

check("37c long fold labels trim before the required Opus and Codex family suffixes",
      len(_long_fold_rows37) == 2
      and _long_fold_rows37[0][0].endswith(" (Opus 5)")
      and _long_fold_rows37[1][0].endswith(" (Codex)")
      and all(len(row[0]) <= mod.RICH_TABLE_CELL_CAP
              for row in _long_fold_rows37),
      _detail26(_long_fold_rows37))

_terminal_tree37 = mod._agent_tree_lines([
    {"label": "failed-agent", "family": "Sonnet", "secs": 7,
     "outcome": "failed"},
    {"label": "stale-agent", "family": "Fable", "secs": 8,
     "outcome": "stale"},
    {"label": "killed-agent", "family": "Codex", "secs": 9,
     "outcome": "killed"},
])
check("37c failed, stale and killed agent rows retain their honest outcome words",
      any("0:07 failed" in row for row in _terminal_tree37)
      and any("0:08 stale" in row for row in _terminal_tree37)
      and any("0:09 killed" in row for row in _terminal_tree37),
      _detail26(_terminal_tree37))

_real_cap_time37 = mod.time.time
try:
    mod.time.time = lambda: _ctx_now37
    _cap_frame37 = mod.render_rich_live(
        SDIR, [], _ctx_now37 - 10, {}, "Capped cumulative roster",
        panel=[], plan=[], tools={"items": [], "count": 0},
        state={"ctx_pct": 5, "ctx_model": "claude-fable-5",
               "rich_session_id": _history_session37,
               "agent_history": _cap_history37})
finally:
    mod.time.time = _real_cap_time37
_cap_records37, _cap_running37, _cap_done37, _cap_omitted37 =\
    mod._agent_display_records([], [], _cap_history37, _history_session37,
                               now=_ctx_now37)
_cap_tree37 = mod._agent_tree_lines(_cap_records37, omitted=_cap_omitted37)
check("37c clipped cumulative roster keeps an honest done-today count and earlier marker",
      (_cap_running37, _cap_done37) == (0, 61)
      and any(re.match(r"└ … \+\d+ earlier", line) for line in _cap_tree37)
      and "🤖 Agents" not in _cap_frame37
      and _cap_frame37.count("<details>") == 2
      and _cap_frame37.count("</details>") == 2,
      _detail26({"tree": _cap_tree37, "frame": _cap_frame37}))

_reset26()
_enable_rich26()
_carry_state37 = _rich_state28(
    mid="carry-old-37", now=_ctx_now37, steps=[],
    rich_session_id=_history_session37,
    agent_history=_display_history37,
    ctx_pct=38, ctx_model="Fable 5")
mod.save_state(SDIR, _carry_state37)
_carry_tg37 = RichRecTG([
    _ok26("carry-one-37"), _ok26("carry-two-37")])
_carry_ops_one37 = mod.on_prompt(
    SDIR, _carry_tg37, prompt_text="first carry boundary",
    session_id=_history_session37)
_carry_one37 = load()
_carry_ops_two37 = mod.on_prompt(
    SDIR, _carry_tg37, prompt_text="second carry boundary",
    session_id=_history_session37)
_carry_two37 = load()
check("37c cumulative store survives two real prompt re-pops",
      _carry_one37.get("agent_history", {}).get("total") == 1
      and _carry_two37.get("agent_history", {}).get("total") == 1
      and len(_carry_two37.get("agent_history", {}).get("rows", [])) == 1,
      _detail26({"one": _carry_one37, "two": _carry_two37,
                 "ops": [_carry_ops_one37, _carry_ops_two37]}))
mod.handle_step(
    SDIR, _carry_tg37, "pre", "Read",
    {"file_path": "/tmp/history-rewrite-37.txt"},
    "HISTORY-REWRITE-37", None, None,
    session_id=_history_session37)
_carry_rewrite37 = load()
check("37c handle_step full-dict rewrite preserves the top-level cumulative store",
      _carry_rewrite37.get("agent_history", {}).get("total") == 1
      and len(_carry_rewrite37.get("agent_history", {}).get("rows", [])) == 1
      and "agent_history" not in (_carry_rewrite37.get("panel") or []),
      _detail26(_carry_rewrite37))

_fresh_guard_state37 = mod.fresh_state(
    _ctx_now37, ctx_pct=38, ctx_model="Fable 5",
    agent_history=_display_history37)
_fresh_guard_state37.update({
    "msgid": "fresh-guard-37", "rich_turn": True, "rich_box": True,
    "rich_session_id": _history_session37, "steps": [], "panel": [],
})
check("37c fresh-box guard accepts the new top-level context and history shapes",
      mod._is_fresh_open_box(_fresh_guard_state37, now=_ctx_now37 + 1) is True,
      _detail26(_fresh_guard_state37))

_agent_trim_records37 = [
    {"label": "AGENT-TRIM-%d-37-%s" % (i, "x" * 80),
     "family": "Opus", "running": False, "secs": 30 + i,
     "outcome": "completed"}
    for i in range(4)
]
_agent_trim_raw37 = mod._assemble_rich_live(
    "", "", "TRIM-MISSION-37", "1:00", 0, 0, "💭 thinking",
    [], 0, [], [], 0, mod.RICH_MIN_EDIT_INTERVAL, False, 1,
    "🧠 Fable 5 · ░░░░░░░░ 5% context",
    _agent_trim_records37, 0, 4, 0)
_agent_trim_once37 = mod._budget_rich_frame(
    _agent_trim_raw37, frame_limit=len(_agent_trim_raw37) - 60)

_agent_trim_synth37 = "\n".join([
    "🎯 TRIM-MISSION-37", "👾 Working · ▪︎▫︎▫︎▫︎▫︎ · 1:00",
    "🧠 Fable 5 · ░░░░░░░░ 5% context", "💭 thinking", "", "---", "",
    "<details><summary>📋 Plan · 0/0 done · tap</summary>",
    "- nothing logged yet", "</details>", "",
    "<details><summary>🔧 Tools · 0 calls · tap</summary>",
    "- nothing yet", "</details>", "",
    "<details><summary>🤖 Agents · 0 running · 4 done today · tap</summary>", "",
] + mod._agent_tree_lines(_agent_trim_records37) + ["", "</details>"])
_agent_trim_once37 = mod._budget_rich_frame(
    _agent_trim_synth37, frame_limit=len(_agent_trim_synth37) - 60)
check("37d transport shrink still drops an Agents tree tail last, after Tools and Plan",
      _agent_trim_once37 is not None
      and "🤖 Agents · 0 running · 4 done today · tap" in _agent_trim_once37
      and "AGENT-TRIM-0-37" in _agent_trim_once37
      and "AGENT-TRIM-3-37" not in _agent_trim_once37
      and "└ … +" in _agent_trim_once37
      and _agent_trim_once37.count("<details>") == 3
      and _agent_trim_once37.count("</details>") == 3,
      _detail26(_agent_trim_once37))
check("37d the live assembler itself emits no Agents disclosure to trim",
      "🤖 Agents" not in _agent_trim_raw37
      and _agent_trim_raw37.count("<details>") == 2
      and _agent_trim_raw37.count("</details>") == 2,
      _detail26(_agent_trim_raw37))

_transient_state37 = _rich_state28(mid="transient-owner-37")
_transient_actions37 = []
for _transient_class37 in (
        "timeout", "connection_reset", "network", "timeout", "network"):
    _transient_actions37.append(mod._apply_rich_result(
        _transient_state37,
        {"ok": False, "error_class": _transient_class37}))
for _rate_index37 in range(4):
    _transient_actions37.append(mod._apply_rich_result(
        _transient_state37,
        {"ok": False, "error_class": "rate_limit"}))
_transient_before_recovery37 = dict(_transient_state37)
_transient_recovery37 = mod._apply_rich_result(
    _transient_state37, {"ok": True})
check("37d transient and rate-limit streaks never demote rich and recovery restores cadence",
      all(action == "skip" for action, _ in _transient_actions37)
      and _transient_before_recovery37.get("rich_failed") is not True
      and _transient_before_recovery37.get("rich_slow") is True
      and _transient_recovery37 == ("ok", "")
      and _transient_state37.get("rich_slow") is False
      and _transient_state37.get("rich_transient_streak") == 0
      and _transient_state37.get("rich_429_streak") == 0,
      _detail26({"actions": _transient_actions37,
                 "before": _transient_before_recovery37,
                 "after": _transient_state37}))

_hard_budget_state37 = _rich_state28(mid="hard-budget-37")
_hard_budget_first37 = mod._apply_rich_result(
    _hard_budget_state37, {"ok": False, "error_class": "content_budget"})
_hard_budget_second37 = mod._apply_rich_result(
    _hard_budget_state37, {"ok": False, "error_class": "content_budget"})
_raw_400_state37 = _rich_state28(mid="raw-400-37")
_raw_400_action37 = mod._apply_rich_result(
    _raw_400_state37, {"ok": False, "error_class": "api_400"})
_retried_400_action37 = mod._apply_rich_result(
    _raw_400_state37,
    {"ok": False, "error_class": "api_400", "api_400_retried": True})
check("37d only persistent content-budget and post-retry API 400 are hard demotions",
      _hard_budget_first37[0] == "skip"
      and _hard_budget_second37[0] == "fallback"
      and _raw_400_action37[0] == "skip"
      and _retried_400_action37[0] == "fallback",
      _detail26({"budget": [_hard_budget_first37, _hard_budget_second37],
                 "api400": [_raw_400_action37, _retried_400_action37]}))

_corrupt_hard37 = _rich_state28(mid="corrupt-hard-37")
_corrupt_hard37["rich_hard_streak"] = "oops"
_corrupt_rate37 = _rich_state28(mid="corrupt-rate-37")
_corrupt_rate37["rich_429_streak"] = True
_corrupt_transient37 = _rich_state28(mid="corrupt-transient-37")
_corrupt_transient37["rich_transient_streak"] = []
_corrupt_actions37 = [
    mod._apply_rich_result(
        _corrupt_hard37, {"ok": False, "error_class": "content_budget"}),
    mod._apply_rich_result(
        _corrupt_rate37, {"ok": False, "error_class": "rate_limit"}),
    mod._apply_rich_result(
        _corrupt_transient37, {"ok": False, "error_class": "timeout"}),
]
check("37d corrupted streak counters heal without exceptions or early demotion",
      all(action[0] == "skip" for action in _corrupt_actions37)
      and _corrupt_hard37.get("rich_hard_streak") == 1
      and _corrupt_rate37.get("rich_429_streak") == 1
      and _corrupt_transient37.get("rich_transient_streak") == 1
      and all(state.get("rich_failed") is not True for state in (
          _corrupt_hard37, _corrupt_rate37, _corrupt_transient37)),
      _detail26({"actions": _corrupt_actions37,
                 "states": [_corrupt_hard37, _corrupt_rate37,
                            _corrupt_transient37]}))

_reset26()
_enable_rich26()
_pending_state37 = _rich_state28(
    mid=None, now=mod.time.time(), steps=[], panel=[],
    rich_box=False, rich_failed=False)
_pending_tg37 = RichRecTG([_fail26("timeout")])
_pending_mid37, _pending_mode37 = mod._try_rich_send_state(
    SDIR, _pending_tg37, _pending_state37, "pending rich frame 37")
mod.save_state(SDIR, _pending_state37)
_pending_react_tg37 = RichRecTG()
_pending_react_ops37 = mod.on_react(SDIR, _pending_react_tg37)
check("37d transient rich-open failure stays pending with no plain box or react duplicate",
      _pending_mid37 is None and _pending_mode37 == "skip"
      and _pending_state37.get("rich_pending_open") is True
      and not _pending_tg37.ops("send")
      and _pending_react_ops37 == []
      and not _pending_react_tg37.ops("send")
      and not _pending_react_tg37.ops("send_rich"),
      _detail26({"state": load(), "calls": _pending_tg37.calls,
                 "react": _pending_react_tg37.calls}))

_reset26()
_enable_rich26()
_health_state37 = _rich_state28(mid="health-owner-37")
for _ in range(5):
    mod._apply_rich_result(
        _health_state37, {"ok": False, "error_class": "timeout"}, sdir=SDIR)
mod._apply_rich_result(
    _health_state37, {"ok": False, "error_class": "network"}, sdir=SDIR)
_, _health_text37 = _health26()
_health_lines37 = [line for line in _health_text37.splitlines() if line.strip()]
check("37d skipped failures log once per class without a retry-log storm",
      len(_health_lines37) == 2
      and any("timeout" in line for line in _health_lines37)
      and any("network" in line for line in _health_lines37),
      _detail26({"lines": _health_lines37, "state": _health_state37}))

_reset26()
_enable_rich26()
_historical_health_state37 = _rich_state28(
    mid="current-owner-37", rich_box=True, rich_failed=False)
_historical_health_state37["rich_error_description"] = "STALE-DESCRIPTION-37"
_historical_action37 = mod._rich_result_for_target(
    _historical_health_state37, "historical-owner-37",
    {"ok": False, "error_class": "timeout"}, sdir=SDIR)
_, _historical_health_text37 = _health26()
check("37d historical-target skips log the current class without a stale description",
      _historical_action37 == ("skip", "timeout")
      and "timeout" in _historical_health_text37
      and "STALE-DESCRIPTION-37" not in _historical_health_text37,
      _detail26({"action": _historical_action37,
                 "health": _historical_health_text37,
                 "state": _historical_health_state37}))

_reset26()
_enable_rich26()
_noreply_health_state37 = _rich_state28(
    mid="noreply-trailing-37", rich_box=True, rich_failed=False)
_real_noreply_merge37 = mod._noreply_merge_ex
try:
    mod._noreply_merge_ex = lambda st, steps, mission: (
        "noreply-history-37", "NOREPLY-RICH-FRAME-37", True)
    _noreply_health_result37 = mod._noreply_cleanup_inline(
        SDIR, RichRecTG([_fail26("network")]),
        _noreply_health_state37, [], "noreply logging")
finally:
    mod._noreply_merge_ex = _real_noreply_merge37
_, _noreply_health_text37 = _health26()
check("37d no-reply historical cleanup logs a refused rich edit and preserves work",
      _noreply_health_result37 == (None, False)
      and "network" in _noreply_health_text37,
      _detail26({"result": _noreply_health_result37,
                 "health": _noreply_health_text37}))

_reset26()
_enable_rich26()
_compound_generation37 = 3701
_compound_stale37 = "compound-stale-37"
_compound_new37 = "compound-new-37"
mod.save_state(SDIR, _rich_state28(
    mid=_compound_new37, folded_above_msgid=_compound_new37,
    folded_above_rich=True, rich_box=True, rich_failed=False,
    rich_op_seq=_compound_generation37,
    rich_op_latest={_compound_stale37: _compound_generation37,
                    _compound_new37: _compound_generation37}))
_compound_health_result37 = mod._rich_compound_refold(
    SDIR, RichRecTG([_fail26("connection_reset")]), "rrefold",
    _compound_stale37, (_compound_new37, "COMPOUND-RICH-FRAME-37"),
    _compound_generation37, (_compound_stale37, _compound_new37))
_, _compound_health_text37 = _health26()
check("37d compound historical refold logs a refused rich edit without demotion",
      _compound_health_result37 is True
      and "connection_reset" in _compound_health_text37
      and load().get("rich_failed") is not True,
      _detail26({"result": _compound_health_result37,
                 "health": _compound_health_text37, "state": load()}))

_reset26()
_plain_guard_tg37 = RecTG()
_plain_guard_ops37 = mod.on_prompt(
    SDIR, _plain_guard_tg37, prompt_text="flag absent guard",
    session_id="plain-session-37")
_plain_guard_state37 = load()
_plain_guard_expected37 = mod.render_live(
    _plain_guard_state37.get("steps") or [],
    _plain_guard_state37.get("frame", 0),
    _plain_guard_state37.get("proc_start"),
    _plain_guard_state37.get("agents") or {},
    mod.current_mission(SDIR, _plain_guard_state37),
    _plain_guard_state37.get("panel"))
mod.handle_step(
    SDIR, _plain_guard_tg37, "pre", "Agent",
    {"description": "plain guard agent", "subagent_type": "worker",
     "model": "claude-opus-5"},
    "PLAIN-GUARD-37", None, None, session_id="plain-session-37")
_plain_guard_after37 = load()
check("37e flag-absent frame and state gain no context or cumulative-agent furniture",
      _plain_guard_state37.get("last_live") == _plain_guard_expected37
      and "🧠" not in (_plain_guard_state37.get("last_live") or "")
      and "ctx_pct" not in _plain_guard_state37
      and "ctx_model" not in _plain_guard_state37
      and "agent_history" not in _plain_guard_after37
      and "history_session_id" not in (
          (_plain_guard_after37.get("panel") or [{}])[0]),
      _detail26({"ops": _plain_guard_ops37,
                 "before": _plain_guard_state37,
                 "after": _plain_guard_after37}))

with open(MOD_PATH, "rb") as _plain_source_file37:
    _plain_source_bytes37 = _plain_source_file37.read()
_plain_source_tree37 = ast.parse(_plain_source_bytes37)
_render_live_node37 = next(
    node for node in _plain_source_tree37.body
    if isinstance(node, ast.FunctionDef) and node.name == "render_live")
_plain_source_lines37 = _plain_source_bytes37.splitlines(keepends=True)
_render_live_bytes37 = b"".join(
    _plain_source_lines37[
        _render_live_node37.lineno - 1:_render_live_node37.end_lineno])
_render_live_sha37 = _behavior_hash(_render_live_node37)
check("37e render_live behavior matches the v30.22 AST fixture after docstring scrub",
      _render_live_sha37
      == "06e02125d93790bfdd0676a5c8b62ce46940ecee72798f72d8345e937de08431",
      _render_live_sha37)

_reset26()

print("\n===== #38 v30.9 context resolution, agent counting and the restored table =====")

_now38 = _et_today(16, 21, 3)

def _usage_line38(input_tokens, cache_read=0, cache_creation=0,
                  model="claude-fable-5", sidechain=False):
    return json.dumps({
        "type": "assistant", "isSidechain": sidechain,
        "message": {
            "model": model,
            "usage": {"input_tokens": input_tokens,
                      "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": cache_creation},
        },
    }) + "\n"

def _write38(path, text, mtime):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.utime(path, (mtime, mtime))
    return path

_worktrees38 = [
    ("/Users/example/claude-channels", "-Users-example-claude-channels"),
    ("/Users/example/claude-channels-2", "-Users-example-claude-channels-2"),
    ("/Users/example/claude-channels-3", "-Users-example-claude-channels-3"),
    ("/Users/example/claude-channels-4", "-Users-example-claude-channels-4"),
    ("/Users/example/claude-channels-5", "-Users-example-claude-channels-5"),
    ("/Users/example/claude-channels-6", "-Users-example-claude-channels-6"),
]

_home38 = os.path.join(STATE_DIR, "home-38")
_projects38 = os.path.join(_home38, ".claude", "projects")
_model_state_path38 = os.path.join(STATE_DIR, "model-state")
_real_home38 = os.environ.get("HOME")
_delta_sid38 = "00000000-0000-4000-8000-000000000002"
try:
    for _cwd38, _key38 in _worktrees38:
        os.makedirs(os.path.join(_projects38, _key38), exist_ok=True)
    os.environ["HOME"] = _home38

    _write38(_model_state_path38, "MODEL=fable\nEFFORT=max\n", _now38)

    _delta_dir38 = os.path.join(_projects38, "-Users-example-claude-channels-4")

    _prev_path38 = _write38(
        os.path.join(_delta_dir38, "00000000-0000-4000-8000-000000000007.jsonl"),
        _usage_line38(2500, 80000, 10000)
        + _usage_line38(990000, model="claude-fable-5", sidechain=True),
        _now38 - 500)

    _write38(os.path.join(_delta_dir38, "notes-38.txt"),
             _usage_line38(999000), _now38 + 60)
    os.makedirs(os.path.join(_delta_dir38, _delta_sid38), exist_ok=True)
    _write38(os.path.join(_delta_dir38, _delta_sid38, "inner-38.jsonl"),
             _usage_line38(999000), _now38 + 70)
    os.utime(os.path.join(_delta_dir38, _delta_sid38), (_now38 + 70, _now38 + 70))
    _fifo_path38 = os.path.join(_delta_dir38, "fifo-38.jsonl")
    os.mkfifo(_fifo_path38)
    os.utime(_fifo_path38, (_now38 + 80, _now38 + 80))

    _write38(os.path.join(_projects38, "-Users-example-claude-channels-3",
                          "cedar-38.jsonl"),
             _usage_line38(990000), _now38 + 900)

    _beacon_dir38 = os.path.join(_projects38, "-Users-example-claude-channels-2")
    _write38(os.path.join(_beacon_dir38, "sess-current-38.jsonl"),
             _usage_line38(797, 200000, 10000), _now38 - 900)
    _write38(os.path.join(_beacon_dir38, "decoy-newer-38.jsonl"),
             _usage_line38(770000), _now38 + 900)

    _foxtrot_dir38 = os.path.join(_projects38, "-Users-example-claude-channels-6")
    _write38(os.path.join(_foxtrot_dir38, "fresh-38.jsonl"), "", _now38 - 900)
    _write38(os.path.join(_foxtrot_dir38, "no-usage-38.jsonl"),
             json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
             _now38 - 890)
    _write38(os.path.join(_foxtrot_dir38, "foxtrot-decoy-38.jsonl"),
             _usage_line38(770000), _now38 + 900)

    _huge_path38 = os.path.join(_foxtrot_dir38, "huge-38.jsonl")
    with open(_huge_path38, "wb") as _huge_file38:
        _huge_file38.write(b"x" * (mod.CONTEXT_TAIL_BYTES + 8192))
    os.utime(_huge_path38, (_now38 - 880, _now38 - 880))

    _mapped38 = [(cwd, mod._context_projects_dir(cwd)) for cwd, _ in _worktrees38]
    check("38a every bot worktree maps to its own projects dir, never a shared one",
          all(actual == os.path.join(_projects38, key)
              for (cwd, actual), (_, key) in zip(_mapped38, _worktrees38))
          and len({actual for _, actual in _mapped38}) == len(_worktrees38),
          _detail26(_mapped38))

    _delta_exact38 = mod._context_transcript_path(
        None, _delta_sid38, "/Users/example/claude-channels-4")
    _delta_exact_read38 = mod._read_context_snapshot(
        _delta_exact38, "MODEL=fable\nEFFORT=max\n")
    check("38a the missing-transcript fixture reproduces a pending fresh session",
          _delta_exact38 == os.path.join(_delta_dir38, _delta_sid38 + ".jsonl")
          and not os.path.exists(_delta_exact38)
          and _delta_exact_read38 == (None, "Fable"),
          _detail26({"path": _delta_exact38, "read": _delta_exact_read38}))

    _delta_turn38 = mod._turn_context(
        SDIR, session_id=_delta_sid38,
        project_dir="/Users/example/claude-channels-4")
    _delta_ex38 = mod._turn_context_ex(
        SDIR, session_id=_delta_sid38,
        project_dir="/Users/example/claude-channels-4")
    check("38a Delta-shaped resolution refuses the previous session and stays pending",
          _delta_turn38 == (None, "Fable")
          and _delta_ex38 == (None, "Fable", _delta_exact38)
          and mod._transcript_session_key(_prev_path38) != _delta_sid38,
          _detail26({"turn": _delta_turn38, "ex": _delta_ex38}))

    _newest38 = mod._context_newest_transcript(_delta_dir38)
    check("38a the fallback ignores subdirectories, non-.jsonl and non-regular files",
          _newest38 == _prev_path38,
          _detail26({"newest": _newest38, "expected": _prev_path38}))

    _noid38 = mod._turn_context(
        SDIR, project_dir="/Users/example/claude-channels-4")
    check("38a with no session identity at all the bounded mtime net still answers",
          _noid38 == (9, "Fable 5"),
          _detail26(_noid38))

    _cross_bot38 = mod._turn_context(
        SDIR, session_id="missing-38",
        project_dir="/Users/example/claude-channels-4")
    check("38a the fallback never reaches another bot's newer transcript",
          _cross_bot38 == (None, "Fable"),
          _detail26(_cross_bot38))

    _exact_wins38 = mod._turn_context(
        SDIR, session_id="sess-current-38",
        project_dir="/Users/example/claude-channels-2")
    check("38a an exact-session transcript still wins over a newer sibling",
          _exact_wins38 == (21, "Fable 5"),
          _detail26(_exact_wins38))

    _stdin_path38 = mod._turn_context(
        SDIR, transcript_path=_prev_path38,
        session_id="sess-current-38",
        project_dir="/Users/example/claude-channels-2")
    check("38a an event-supplied transcript_path outranks both later resolvers",
          _stdin_path38 == (9, "Fable 5"),
          _detail26(_stdin_path38))

    _listdir_calls38 = []
    _real_listdir38 = mod.os.listdir
    try:
        def _counting_listdir38(path):
            _listdir_calls38.append(path)
            return _real_listdir38(path)
        mod.os.listdir = _counting_listdir38
        _budget_turn38 = mod._turn_context(
            SDIR, session_id="missing-38",
            project_dir="/Users/example/claude-channels-4")
    finally:
        mod.os.listdir = _real_listdir38

    check("38a the fallback costs exactly one bounded directory listing",
          _budget_turn38 == (None, "Fable")
          and _listdir_calls38 == [_delta_dir38]
          and mod.CONTEXT_SCAN_LIMIT == 500,
          _detail26({"turn": _budget_turn38, "listdirs": _listdir_calls38}))

    _fresh_empty38 = mod._read_context_snapshot(
        os.path.join(_foxtrot_dir38, "fresh-38.jsonl"), "MODEL=fable")
    _fresh_norecord38 = mod._read_context_snapshot(
        os.path.join(_foxtrot_dir38, "no-usage-38.jsonl"), "MODEL=fable")
    check("38b a resolvable transcript with no usage record yet reads 0%, not ??",
          _fresh_empty38 == (0, "Fable") and _fresh_norecord38 == (0, "Fable"),
          _detail26({"empty": _fresh_empty38, "no_record": _fresh_norecord38}))

    _fresh_turn38 = mod._turn_context(
        SDIR, session_id="fresh-38",
        project_dir="/Users/example/claude-channels-6")
    check("38b a fresh chat returns 0% and never falls through to the mtime rule",
          _fresh_turn38 == (0, "Fable"),
          _detail26(_fresh_turn38))

    _huge_read38 = mod._read_context_snapshot(_huge_path38, "MODEL=fable")
    _fifo_read38 = mod._read_context_snapshot(_fifo_path38, "MODEL=fable")
    _empty_dir38 = os.path.join(_projects38, "-Users-example-claude-channels-5")
    _hard_turn38 = mod._turn_context(
        SDIR, session_id="missing-38",
        project_dir="/Users/example/claude-channels-5")
    check("38b hard failures still report ??: oversized tail, FIFO, nothing to read",
          _huge_read38 == (None, "Fable")
          and _fifo_read38 == (None, "Fable")
          and os.listdir(_empty_dir38) == []
          and _hard_turn38 == (None, "Fable"),
          _detail26({"huge": _huge_read38, "fifo": _fifo_read38,
                     "hard": _hard_turn38}))

    _zero_line38 = mod._context_line({"ctx_pct": 0, "ctx_model": "claude-fable-5"})
    _unknown_line38 = mod._context_line({"ctx_pct": None, "ctx_model": "fable"})
    check("38b the rendered line prints a real 0% and reserves ?? for a hard failure",
          _zero_line38 == "🧠 Fable 5 · %s 0%% context" % (mod.CONTEXT_BAR_EMPTY * 8)
          and _unknown_line38 == "🧠 Fable · %s ??%% context" % (
              mod.CONTEXT_BAR_EMPTY * 8),
          _detail26({"zero": _zero_line38, "unknown": _unknown_line38}))

    _pending_sid38 = "00000000-0000-4000-8000-000000000004"
    _pending_path38 = os.path.join(_delta_dir38, _pending_sid38 + ".jsonl")
    _pending_pct38, _pending_model38, _pending_arm38 = mod._turn_context_ex(
        SDIR, transcript_path=_pending_path38, session_id=_pending_sid38,
        project_dir="/Users/example/claude-channels-4")
    check("38z a brand new session opens unknown, never on the previous session's %",
          (_pending_pct38, _pending_model38) == (None, "Fable")
          and _pending_arm38 == _pending_path38
          and not os.path.exists(_pending_path38)
          and mod._read_context_snapshot(
              _prev_path38, "MODEL=fable\nEFFORT=max\n") == (9, "Fable 5"),
          _detail26({"pct": _pending_pct38, "model": _pending_model38,
                     "armed": _pending_arm38}))

    _fresh_box38 = mod.fresh_state(
        _now38, ctx_pct=_pending_pct38, ctx_model=_pending_model38,
        ctx_pending_path=_pending_arm38)
    _resolved_box38 = mod.fresh_state(_now38, ctx_pct=9, ctx_model="Fable 5")
    check("38z the pending marker is stored only while it is armed",
          _fresh_box38.get("ctx_pending_path") == _pending_path38
          and "ctx_pending_path" not in _resolved_box38
          and _fresh_box38.get("ctx_pct") is None,
          _detail26({"armed": sorted(_fresh_box38), "clean": sorted(_resolved_box38)}))

    _still_absent38 = mod._refresh_pending_context(SDIR, _fresh_box38)
    _armed_after38 = _fresh_box38.get("ctx_pending_path")
    _write38(_pending_path38, _usage_line38(3000, 120000, 7000), _now38 + 5)
    _now_resolves38 = mod._refresh_pending_context(SDIR, _fresh_box38)
    check("38z the moment the session's own transcript exists the box corrects itself",
          _still_absent38 is False
          and _armed_after38 == _pending_path38
          and _now_resolves38 is True
          and _fresh_box38.get("ctx_pct") == 13
          and _fresh_box38.get("ctx_model") == "Fable 5"
          and "ctx_pending_path" not in _fresh_box38
          and mod._refresh_pending_context(SDIR, _fresh_box38) is False,
          _detail26({"absent": _still_absent38, "resolved": _now_resolves38,
                     "state": _fresh_box38}))

    check("38z the corrected line renders this session's number, not 35%",
          mod._context_line(_fresh_box38)
          == "🧠 Fable 5 (MX) · %s%s 13%% context" % (
              mod.CONTEXT_BAR_FILL, mod.CONTEXT_BAR_EMPTY * 7)
          and _fresh_box38.get("ctx_effort") == "max",
          _detail26(mod._context_line(_fresh_box38)))
finally:
    if _real_home38 is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home38
    try:
        os.remove(_model_state_path38)
    except OSError:
        pass

_dispatch_t0_38 = _et_today(16, 27, 0)
_before_dispatch38 = _et_today(16, 26, 30)
_after_dispatch38 = _dispatch_t0_38 + 427
_proc_start38 = _et_today(16, 21, 3)
_agent_state38 = {
    "ctx_pct": 11, "ctx_model": "claude-fable-5",
    "agent_history": mod._empty_agent_history(_after_dispatch38),
    "rich_session_id": _delta_sid38,
}

_open_row38 = {
    "tuid": "toolu_fixture000000000000000005", "lab": "Sample reply",
    "typ": "opus-executor", "mdl": "opus", "t0": _dispatch_t0_38,
    "done": False, "background": True, "history_session_id": _delta_sid38,
}
_real_time38 = mod.time.time
try:
    mod.time.time = lambda: _before_dispatch38
    _frame_before38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "Routing Sample payment-link reply through Opus",
        panel=[], plan=[], tools={"items": [], "count": 0}, state=_agent_state38))
    mod.time.time = lambda: _after_dispatch38
    _frame_after38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "Routing Sample payment-link reply through Opus",
        panel=[dict(_open_row38)], plan=[], tools={"items": [], "count": 0},
        state=_agent_state38))
    _plain_after38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "Routing Sample payment-link reply through Opus",
        panel=[dict(_open_row38)], plan=[], tools={"items": [], "count": 0},
        fallback=True, state=_agent_state38))
finally:
    mod.time.time = _real_time38

check("38c the pre-dispatch frame shows no table and no agents zone at all",
      "🤖 Agents" not in _frame_before38
      and "| 🤖 Agent | Time |" not in _frame_before38
      and _frame_before38.count("\n---") == 1,
      _detail26(_frame_before38))

_after_records38, _after_running38, _after_done38, _ = mod._agent_display_records(
    [], [dict(_open_row38)], _agent_state38["agent_history"], _delta_sid38,
    now=_after_dispatch38)
check("38c one open background row is still counted as 1 running",
      (_after_running38, _after_done38) == (1, 0)
      and "└ Sample reply (Opus) · running · 7:07"
      in mod._agent_tree_lines(_after_records38),
      _detail26({"records": _after_records38, "running": _after_running38}))

check("38c the same open row is listed in the table with its live clock",
      "| ◉ Sample reply (Opus) | 07:07 |" in _frame_after38
      and "- ◉ Sample reply (Opus) · 07:07" in _plain_after38
      and "🤖 Agents" not in _frame_after38
      and "🤖 Agents" not in _plain_after38,
      _detail26({"rich": _frame_after38, "plain": _plain_after38}))

_table_now38 = _after_dispatch38
_done_row38 = {
    "tuid": "SOL-38", "lab": "Sol review", "typ": "codex", "mdl": "gpt-5.6-sol",
    "t0": _table_now38 - 300, "done": True, "secs": 42,
    "history_session_id": _delta_sid38,
}
_real_time38b = mod.time.time
try:
    mod.time.time = lambda: _table_now38
    _table_rich38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "Restored table",
        panel=[dict(_open_row38), dict(_done_row38)], plan=[],
        tools={"items": [], "count": 0}, state=_agent_state38))
    _table_plain38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "Restored table",
        panel=[dict(_open_row38), dict(_done_row38)], plan=[],
        tools={"items": [], "count": 0}, fallback=True, state=_agent_state38))
    _bare_rich38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "No agents this turn",
        panel=[], plan=[], tools={"items": [], "count": 0}, state=_agent_state38))
    _bare_plain38 = _text26(_render_live26(
        SDIR, [], _proc_start38, {}, "No agents this turn",
        panel=[], plan=[], tools={"items": [], "count": 0},
        fallback=True, state=_agent_state38))
finally:
    mod.time.time = _real_time38b

_table_lines38 = _table_rich38.splitlines()
check("38d the table is back between the live block and the dropdowns, with dividers",
      "| 🤖 Agent | Time |" in _table_lines38
      and "|:---|---:|" in _table_lines38
      and _table_lines38.index("| 🤖 Agent | Time |")
      > _table_lines38.index("---")
      and _table_lines38.index("| 🤖 Agent | Time |")
      < next(i for i, line in enumerate(_table_lines38)
             if line.startswith("<details><summary>📋 Plan"))
      and _table_rich38.count("\n---") == 2,
      _detail26(_table_rich38))

check("38d it carries this turn's open AND finished agents, the v30.7 population",
      "| ◉ Sample reply (Opus) | 07:07 |" in _table_rich38
      and "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |" in _table_rich38
      and "🤖 Agents" not in _table_rich38,
      _detail26(_table_rich38))

_plain_lines38 = _table_plain38.splitlines()

check("38d the plain fallback mirrors the table in the same position",
      "🤖 Agent · Time" in _plain_lines38
      and "- ◉ Sample reply (Opus) · 07:07" in _plain_lines38
      and "- ✓ Sol review (GPT-5.6 Sol) · 00:42" in _plain_lines38
      and _plain_lines38.index("🤖 Agent · Time")
      < next(i for i, line in enumerate(_plain_lines38)
             if line.startswith("📋 Plan · ")),
      _detail26(_table_plain38))

check("38d zero agent activity this turn renders no table at all, in either mode",
      "| 🤖 Agent | Time |" not in _bare_rich38
      and "|:---|---:|" not in _bare_rich38
      and _bare_rich38.count("\n---") == 1
      and "🤖 Agent · Time" not in _bare_plain38
      and "🤖 Agents" not in _bare_plain38,
      _detail26({"rich": _bare_rich38, "plain": _bare_plain38}))

_heavy_now38 = 38000.0
_heavy_plan38 = [{"id": "pending-38", "subject": "KEEP-PENDING-38", "status": "pending"}]
_heavy_plan38.extend({
    "id": "completed-%02d-38" % i,
    "subject": "COMPLETE-%02d-38 %s" % (i, "c" * 150),
    "status": "completed",
} for i in range(40))
_heavy_plan38.append({"id": "live-38", "subject": "KEEP-LIVE-38",
                      "status": "in_progress"})
_heavy_tools38 = {
    "count": 30,
    "items": [{"cap": "TOOL-%02d-38 %s" % (i, "t" * 150), "elapsed": i + 1}
              for i in range(30)],
}
_heavy_panel38 = [
    {"tuid": "HEAVY-A-38", "lab": "HEAVY-ALPHA-38", "typ": "opus-executor",
     "mdl": "opus", "background": True, "done": False, "t0": _heavy_now38 - 17},
    {"tuid": "HEAVY-B-38", "lab": "HEAVY-BETA-38", "typ": "opus-executor",
     "mdl": "opus", "background": True, "done": False, "t0": _heavy_now38 - 11},
    {"tuid": "HEAVY-C-38", "lab": "HEAVY-GAMMA-38", "typ": "opus-executor",
     "mdl": "opus", "background": True, "done": False, "t0": _heavy_now38 - 5},
]
_heavy_state38 = {
    "ctx_pct": 44, "ctx_model": "claude-fable-5",
    "agent_history": mod._normalize_agent_history({
        "day": mod._et_day(_heavy_now38), "total": 12,
        "session_totals": {_delta_sid38: 12},
        "rows": [{"id": "%s:agent:HIST-%02d-38" % (_delta_sid38, i),
                  "label": "HISTORY-%02d-38 %s" % (i, "h" * 40),
                  "family": "Opus", "model": "claude-opus-5",
                  "t0": _heavy_now38 - 900, "secs": 60 + i,
                  "outcome": "completed", "day": mod._et_day(_heavy_now38),
                  "session_id": _delta_sid38, "closed_at": _heavy_now38 - 100 + i}
                 for i in range(12)],
    }, now=_heavy_now38),
    "rich_session_id": _delta_sid38,
}
_real_time38c = mod.time.time
try:
    mod.time.time = lambda: _heavy_now38
    _heavy_rich38 = _text26(_render_live26(
        SDIR, [], _heavy_now38 - 60, {}, "KEEP-MISSION-38",
        panel=[dict(row) for row in _heavy_panel38], plan=_heavy_plan38,
        tools=_heavy_tools38, state=_heavy_state38))
    _heavy_plain38 = _text26(_render_live26(
        SDIR, [], _heavy_now38 - 60, {}, "KEEP-MISSION-38",
        panel=[dict(row) for row in _heavy_panel38], plan=_heavy_plan38,
        tools=_heavy_tools38, fallback=True, state=_heavy_state38))
finally:
    mod.time.time = _real_time38c

_heavy_fits38, _heavy_fits_err38 = _call26("_rich_frame_fits", _heavy_rich38)
check("38e an over-budget rich frame trims tools and plan rows, never the table",
      not _heavy_fits_err38 and _heavy_fits38 is True
      and "TOOL-" not in _heavy_rich38
      and "COMPLETE-00-38" not in _heavy_rich38
      and "… 30 earlier not shown" in _heavy_rich38

      and "- +23 lower-priority details hidden" in _heavy_rich38
      and "| ◉ HEAVY-ALPHA-38 (Opus) | 00:17 |" in _heavy_rich38
      and "| ◉ HEAVY-BETA-38 (Opus) | 00:11 |" in _heavy_rich38
      and "| ◉ HEAVY-GAMMA-38 (Opus) | 00:05 |" in _heavy_rich38
      and "KEEP-PENDING-38" in _heavy_rich38
      and "KEEP-LIVE-38" in _heavy_rich38,
      _heavy_fits_err38 or _detail26(_heavy_rich38))

check("38e the plain fallback protects its mirrored table the same way",
      len(_heavy_plain38) < mod.RICH_FRAME_LIMIT
      and "TOOL-" not in _heavy_plain38
      and "- ◉ HEAVY-ALPHA-38 (Opus) · 00:17" in _heavy_plain38
      and "- ◉ HEAVY-BETA-38 (Opus) · 00:11" in _heavy_plain38
      and "- ◉ HEAVY-GAMMA-38 (Opus) · 00:05" in _heavy_plain38
      and "🤖 Agents" not in _heavy_plain38,
      _detail26(_heavy_plain38))

_fold_now38 = _after_dispatch38
_fold_steps38 = [{"fam": "files", "cap": "reading the Sample thread", "tuid": "STEP-38",
                  "done": True, "secs": 3, "t0": _proc_start38 + 10}]
_fold_history38 = mod._normalize_agent_history({
    "day": mod._et_day(_fold_now38), "total": 1,
    "session_totals": {_delta_sid38: 1},
    "rows": [{"id": "%s:codex:SOL-38" % _delta_sid38, "label": "Sol review",
              "family": "Codex", "model": "gpt-5.6-sol", "t0": _fold_now38 - 300,
              "secs": 42, "outcome": "completed", "day": mod._et_day(_fold_now38),
              "session_id": _delta_sid38, "closed_at": _fold_now38 - 10}],
}, now=_fold_now38)
_real_time38d = mod.time.time
try:
    mod.time.time = lambda: _fold_now38
    _fold_result38, _fold_err38 = _call26(
        "render_rich_final", SDIR, _fold_steps38, _proc_start38, n_agents=1,
        mission="Restored table", panel=[dict(_done_row38)], plan=[],
        cadence=1.5, outcome="done", agent_history=_fold_history38)
    _fold_text38 = _text26(_fold_result38)
finally:
    mod.time.time = _real_time38d

with open(MOD_PATH, "rb") as _fold_source_file38:
    _fold_source_bytes38 = _fold_source_file38.read()
_fold_tree38 = ast.parse(_fold_source_bytes38)
_fold_source_lines38 = _fold_source_bytes38.splitlines(keepends=True)

def _fn_sha38(name):
    node = next((n for n in _fold_tree38.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        return "missing"
    return _behavior_hash(node)

_fold_shas38 = {name: _fn_sha38(name) for name in (
    "_rich_lane_table", "_rich_summary_sections", "_assemble_rich_final",
    "render_rich_final")}

check("38f fold functions preserve the v30.22 AST behavior after docstring scrub",
      _fold_shas38 == {
          "_rich_lane_table":
              "90ab07c37f268b47b28ace7f6542eeb91d6da8c20985de5219ba1ed401beb3f6",
          "_rich_summary_sections":
              "ccb1e3c894311ef92e26242ddb254e74b729d11636ef55d84508b83fb63b8cb0",
          "_assemble_rich_final":
              "90e0834667526dd37bda02edc453a7f6077246afff64caf191db2d595b2e822b",
          "render_rich_final":
              "95a497ab29675db7d81b932393103222dfc6dba7224f7daf0f4d10f636bffa04",
      },
      _detail26(_fold_shas38))

check("38f the fold still renders its v30.8 Agents section with family labels",
      not _fold_err38
      and "**Agents · 1 done today**" in _fold_text38
      and "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |" in _fold_text38
      and "🧠" not in _fold_text38,
      _fold_err38 or _detail26(_fold_text38))

_vanish_session38 = "vanish-session-38"
_vanish_now38 = _et_today(16, 52)
_vanish_state38 = {
    "rich_turn": True, "rich_session_id": _vanish_session38,
    "agent_history": mod._empty_agent_history(_vanish_now38),
}

_vanish_state38["agent_history"] = mod._capture_closed_agents(
    _vanish_state38["agent_history"],
    [{"tuid": "REAL-DONE-38", "lab": "evidence sweep", "typ": "opus-executor",
      "mdl": "claude-opus-5", "done": True, "t0": _vanish_now38 - 400,
      "secs": 400, "history_session_id": _vanish_session38}],
    [], _vanish_session38, _vanish_now38)
_killed_step38 = {
    "tuid": "SAMPLE-SOLAUDIT-38", "cap": "Codex Sol · sample-solaudit",
    "codex_bg": True, "done": False, "t0": _vanish_now38 - 301,
    "history_session_id": _vanish_session38,
}
_real_codex_alive38 = mod._codex_alive
try:
    mod._codex_alive = lambda step=None: False
    _vanish_closed38 = mod._close_gone_codex_bg(
        [_killed_step38], _vanish_now38, history_state=_vanish_state38,
        session_id=_vanish_session38)
finally:
    mod._codex_alive = _real_codex_alive38
_vanish_row38 = next(
    (row for row in _vanish_state38["agent_history"]["rows"]
     if row.get("id", "").endswith(":codex:SAMPLE-SOLAUDIT-38")), {})

check("38g a vanished Codex process closes as stopped, never as a completion",
      _vanish_closed38 == 1
      and _killed_step38.get("codex_terminal_status") == "stopped"
      and _killed_step38.get("codex_inferred") is True
      and _vanish_row38.get("outcome") == "stopped"
      and _vanish_row38.get("label") == "sample-solaudit",
      _detail26({"step": _killed_step38, "row": _vanish_row38}))

check("38g the honest row is kept but excluded from N done today",
      len(_vanish_state38["agent_history"]["rows"]) == 2
      and _vanish_state38["agent_history"]["total"] == 1
      and _vanish_state38["agent_history"]["session_totals"][
          _vanish_session38] == 1,
      _detail26(_vanish_state38["agent_history"]))

_real_time38e = mod.time.time
try:
    mod.time.time = lambda: _vanish_now38
    _vanish_rich38 = _text26(_render_live26(
        SDIR, [dict(_killed_step38)], _vanish_now38 - 600, {},
        "Killed the Sol audit", panel=[], plan=[],
        tools={"items": [], "count": 0}, state=_vanish_state38))
    _vanish_plain38 = _text26(_render_live26(
        SDIR, [dict(_killed_step38)], _vanish_now38 - 600, {},
        "Killed the Sol audit", panel=[], plan=[],
        tools={"items": [], "count": 0}, fallback=True, state=_vanish_state38))
    _vanish_fold38, _vanish_fold_err38 = _call26(
        "render_rich_final", SDIR, [dict(_killed_step38)], _vanish_now38 - 600,
        n_agents=1, mission="Killed the Sol audit", panel=[], plan=[],
        cadence=1.5, outcome=None,
        agent_history=_vanish_state38["agent_history"])
    _vanish_fold_text38 = _text26(_vanish_fold38)
finally:
    mod.time.time = _real_time38e

check("38g the mid-box table and its plain mirror show the stop glyph, not a check",
      "| ■ sample-solaudit (Codex) | 05:01 |" in _vanish_rich38
      and "| ✓ sample-solaudit (Codex) |" not in _vanish_rich38
      and "- ■ sample-solaudit (Codex) · 05:01" in _vanish_plain38,
      _detail26({"rich": _vanish_rich38, "plain": _vanish_plain38}))

_vanish_records38, _vanish_running38, _vanish_done38, _ = mod._agent_display_records(
    [dict(_killed_step38)], [], _vanish_state38["agent_history"],
    _vanish_session38, now=_vanish_now38)
_vanish_tree38 = mod._agent_tree_lines(_vanish_records38)

check("38g the cumulative view says stopped and still reads 1 done today",
      (_vanish_running38, _vanish_done38) == (0, 1)
      and "├ sample-solaudit (Codex) · 5:01 stopped" in _vanish_tree38
      and "└ evidence sweep (Opus 5) · 6:40 done" in _vanish_tree38
      and "🤖 Agents" not in _vanish_rich38
      and "🤖 Agents" not in _vanish_plain38,
      _detail26({"tree": _vanish_tree38, "rich": _vanish_rich38}))

check("38g the fold agrees, and an inferred stop never restates the whole turn",
      not _vanish_fold_err38
      and "**Agents · 1 done today**" in _vanish_fold_text38
      and "| ■ sample-solaudit (Codex) | 05:01 |" in _vanish_fold_text38
      and _vanish_fold_text38.startswith("<details><summary>✅ Done in ")
      and mod._rich_outcome([dict(_killed_step38)]) == "done"
      and mod._rich_outcome([{"codex_terminal_status": "stopped"}]) == "stopped",
      _vanish_fold_err38 or _detail26(_vanish_fold_text38))

_upgrade_state38 = _rich_state28(
    mid=None, now=_vanish_now38, steps=[dict(_killed_step38)], panel=[],
    rich_box=False, rich_session_id=_vanish_session38,
    agent_history=_vanish_state38["agent_history"])
_upgrade_result38 = mod._close_codex_notification(
    SDIR, _upgrade_state38,
    _terminal_notification25("SAMPLE-SOLAUDIT-38", "completed",
                             task_id="late-truth-38"))
_upgraded_step38 = next(
    (step for step in _upgrade_state38["steps"]
     if step.get("tuid") == "SAMPLE-SOLAUDIT-38"), {})
_upgraded_row38 = next(
    (row for row in _upgrade_state38["agent_history"]["rows"]
     if row.get("id", "").endswith(":codex:SAMPLE-SOLAUDIT-38")), {})

check("38h a genuine completion arriving later upgrades the inferred stop",
      isinstance(_upgrade_result38, list)
      and _upgraded_step38.get("codex_terminal_status") == "completed"
      and "codex_inferred" not in _upgraded_step38
      and _upgraded_row38.get("outcome") == "completed"
      and len(_upgrade_state38["agent_history"]["rows"]) == 2
      and _upgrade_state38["agent_history"]["total"] == 2,
      _detail26({"step": _upgraded_step38,
                 "history": _upgrade_state38["agent_history"]}))

_kill_state38 = _rich_state28(
    mid=None, now=_vanish_now38, steps=[dict(_killed_step38)], panel=[],
    rich_box=False, rich_session_id=_vanish_session38,
    agent_history=_vanish_state38["agent_history"])
_kill_result38 = mod._close_codex_notification(
    SDIR, _kill_state38,
    _terminal_notification25("SAMPLE-SOLAUDIT-38", "killed",
                             task_id="late-kill-38"))
_killed_row38 = next(
    (row for row in _kill_state38["agent_history"]["rows"]
     if row.get("id", "").endswith(":codex:SAMPLE-SOLAUDIT-38")), {})
_real_time38f = mod.time.time
try:
    mod.time.time = lambda: _vanish_now38
    _kill_rich38 = _text26(_render_live26(
        SDIR, _kill_state38["steps"], _vanish_now38 - 600, {},
        "Killed the Sol audit", panel=[], plan=[],
        tools={"items": [], "count": 0}, state=_kill_state38))
finally:
    mod.time.time = _real_time38f

check("38h a confirmed kill upgrades the word and still never counts as done",
      isinstance(_kill_result38, list)
      and _killed_row38.get("outcome") == "killed"
      and _kill_state38["agent_history"]["total"] == 1

      and "| ✕ sample-solaudit · killed (Codex) | 05:01 |" in _kill_rich38
      and "├ sample-solaudit (Codex) · 5:01 killed" in mod._agent_tree_lines(
          mod._agent_display_records(
              _kill_state38["steps"], [], _kill_state38["agent_history"],
              _vanish_session38, now=_vanish_now38)[0])
      and "🤖 Agents" not in _kill_rich38,
      _detail26({"row": _killed_row38,
                 "history": _kill_state38["agent_history"]}))

_repeat_history38 = mod._capture_closed_agents(
    _vanish_state38["agent_history"], [],
    [dict(_killed_step38)], _vanish_session38, _vanish_now38)
_repeat_history38 = mod._capture_closed_agents(
    _repeat_history38, [], [dict(_killed_step38)],
    _vanish_session38, _vanish_now38)
check("38h re-capturing the same closures is idempotent for rows and for the count",
      len(_repeat_history38["rows"]) == 2
      and _repeat_history38["total"] == 1,
      _detail26(_repeat_history38))

_stale_lane38 = {
    "tuid": "STALE-LANE-38", "cap": "Codex Terra · long-runner · stale",
    "codex_bg": True, "done": True, "secs": 7201, "codex_stale": True,
    "t0": _vanish_now38 - 7201, "history_session_id": _vanish_session38,
}
check("38h the two-hour stale cutoff also loses the completion check in the table",
      mod._rich_lane_state(_stale_lane38) == "■ stop"
      and mod._rich_lane_state({"codex_bg": True, "done": True}) == "✓ done",
      _detail26(mod._rich_lane_state(_stale_lane38)))

_stop_now38 = _et_today(16, 59)
_stop_key38 = "/private/tmp/tgstream38/force-stopped-38.md"
_stopped_lane38 = {
    "tuid": "FORCE-STOPPED-38", "cap": "Codex Sol · force-stopped-38",
    "codex_bg": True, "done": False, "codex_key": _stop_key38,
    "t0": _stop_now38 - 900, "history_session_id": _vanish_session38,
}
_unrelated_lane38 = {
    "tuid": "OTHER-BOT-38", "cap": "Codex Sol · other-bot-38",
    "codex_bg": True, "done": False,
    "codex_key": "/private/tmp/tgstream38/other-bot-38.md",
    "t0": _stop_now38 - 900, "history_session_id": _vanish_session38,
}

_PGREP_TEST_STATE.update({
    "global": True,
    "paths": {_ere_arg33(_stop_key38): False,
              _ere_arg33("/private/tmp/tgstream38/other-bot-38.md"): True},
    "calls": []})
_stop_closed38 = mod._close_gone_codex_bg(
    [_stopped_lane38, _unrelated_lane38], _stop_now38)
check("38i a keyed force-stopped lane closes as stopped while other codex runs",
      _stop_closed38 == 1
      and _stopped_lane38.get("done") is True
      and _stopped_lane38.get("codex_terminal_status") == "stopped"
      and _stopped_lane38.get("codex_inferred") is True
      and _unrelated_lane38.get("done") is False
      and "codex exec" not in [call[-1] for call in _PGREP_TEST_STATE["calls"]],
      _detail26({"stopped": _stopped_lane38, "other": _unrelated_lane38,
                 "probes": [call[-1] for call in _PGREP_TEST_STATE["calls"]]}))

_PGREP_TEST_STATE.update({"global": True, "paths": {}, "calls": []})
_young_keyless38 = {
    "tuid": "KEYLESS-YOUNG-38", "cap": "Codex Sol · keyless-young-38",
    "codex_bg": True, "done": False,
    "t0": _stop_now38 - (mod.CODEX_KEYLESS_HOLD_MAX / 2),
}
_old_keyless38 = {
    "tuid": "KEYLESS-OLD-38", "cap": "Codex Sol · keyless-old-38",
    "codex_bg": True, "done": False,
    "t0": _stop_now38 - (mod.CODEX_KEYLESS_HOLD_MAX + 1),
}
_keyless_closed38 = mod._close_gone_codex_bg(
    [_young_keyless38, _old_keyless38], _stop_now38)
check("38i an unrelated process holds a keyless legacy lane only within the bound",
      _keyless_closed38 == 1
      and _young_keyless38.get("done") is False
      and _old_keyless38.get("done") is True
      and _old_keyless38.get("codex_terminal_status") == "stopped"
      and 0 < mod.CODEX_KEYLESS_HOLD_MAX < mod.CODEX_BG_MAX_AGE,
      _detail26({"young": _young_keyless38, "old": _old_keyless38,
                 "bound": mod.CODEX_KEYLESS_HOLD_MAX}))

_reset26()
_enable_rich26()
_fold_lane38 = {
    "fam": "run", "cap": "Codex Sol · force-stopped-38", "tuid": "FOLD-LANE-38",
    "codex_bg": True, "done": False, "codex_key": _stop_key38, "k": "", "aid": "",
    "t0": mod.time.time() - 900,
}
_PGREP_TEST_STATE.update({
    "global": True, "paths": {_ere_arg33(_stop_key38): False}, "calls": []})
mod.save_state(SDIR, _rich_state28(
    mid="fold-after-stop-38", steps=[_fold_lane38], panel=[],
    replied_this_turn=True, box_is_last=False, typing_done=False,
    rich_session_id=_vanish_session38))
_fold_tg38 = RichRecTG([_ok26("fold-after-stop-38")])
_fold_done38 = mod._fold_to_done(SDIR, _fold_tg38)
_fold_state38 = load()
_fold_lane_after38 = next(
    (step for step in (_fold_state38.get("steps") or [])
     if step.get("tuid") == "FOLD-LANE-38"), {})
check("38i the box folds on the next pass once the last lane is reconciled shut",
      _fold_done38 is True
      and _fold_lane_after38.get("done") is True
      and _fold_lane_after38.get("codex_terminal_status") == "stopped"
      and _fold_state38.get("folded") is True
      and not mod._has_open_codex_bg(_fold_state38.get("steps") or []),
      _detail26({"folded": _fold_done38, "lane": _fold_lane_after38,
                 "state": _fold_state38}))

_PGREP_TEST_STATE.update({"global": True, "paths": {}, "calls": []})

_reset26()

print("\n----- 39 v30.10 live box: table always, dropdown never -----")
_reset26()
_enable_rich26()

_v3010_sid = "v3010-session"
_v3010_now = 39000.0
_v3010_open = {
    "tuid": "V3010-OPEN", "lab": "streamer fix", "typ": "opus-executor",
    "mdl": "claude-opus-5", "t0": _v3010_now - 90, "done": False,
    "background": True, "history_session_id": _v3010_sid,
}
_v3010_closed = {
    "tuid": "V3010-DONE", "lab": "Sol review", "typ": "codex",
    "mdl": "gpt-5.6-sol", "t0": _v3010_now - 300, "done": True, "secs": 42,
    "history_session_id": _v3010_sid,
}

def _v3010_ledger(count):
    if not count:
        return mod._empty_agent_history(_v3010_now)
    return mod._normalize_agent_history({
        "day": mod._et_day(_v3010_now), "total": count,
        "session_totals": {_v3010_sid: count},
        "rows": [{"id": "%s:agent:V3010-HIST-%02d" % (_v3010_sid, i),
                  "label": "earlier %02d" % i, "family": "Opus",
                  "model": "claude-opus-5", "t0": _v3010_now - 900,
                  "secs": 60 + i, "outcome": "completed",
                  "day": mod._et_day(_v3010_now), "session_id": _v3010_sid,
                  "closed_at": _v3010_now - 100 + i}
                 for i in range(count)],
    }, now=_v3010_now)

def _v3010_frame(panel, ledger_count, fallback=False, steps=None):
    return _text26(_render_live26(
        SDIR, steps or [], _v3010_now - 200, {}, "v30.10 live box",
        panel=panel, plan=[{"id": "p1", "subject": "do the thing",
                            "status": "in_progress"}],
        tools={"items": [{"cap": "Read file", "elapsed": 3}], "count": 1},
        fallback=fallback,
        state={"ctx_pct": 42, "ctx_model": "claude-opus-5",
               "rich_session_id": _v3010_sid,
               "agent_history": _v3010_ledger(ledger_count)}))

def _v3010_table_block(text):
    lines = text.splitlines()
    if "| 🤖 Agent | Time |" not in lines:
        return []
    start = lines.index("| 🤖 Agent | Time |")
    end = start + 1
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return lines[start:end]

def _v3015_cap():

    try:
        return int(getattr(mod, "RICH_LIVE_LANE_ROWS", 0) or 0)
    except Exception:
        return 0

def _v3010_current_rows(text, n):

    block = _v3010_table_block(text)
    return block[:2 + n] if block else []

def _v3010_foreign_ledger(count):

    rows = _v3010_ledger(count).get("rows") or []
    for row in rows:
        row["session_id"] = "v3010-not-this-session"
        row["id"] = row["id"].replace(_v3010_sid, "v3010-not-this-session")
    return mod._normalize_agent_history(
        {"day": mod._et_day(_v3010_now), "total": count,
         "session_totals": {"v3010-not-this-session": count}, "rows": rows},
        now=_v3010_now)

def _v3010_frame_hist(panel, history, fallback=False, steps=None):
    return _text26(_render_live26(
        SDIR, steps or [], _v3010_now - 200, {}, "v30.10 live box",
        panel=panel, plan=[{"id": "p1", "subject": "do the thing",
                            "status": "in_progress"}],
        tools={"items": [{"cap": "Read file", "elapsed": 3}], "count": 1},
        fallback=fallback,
        state={"ctx_pct": 42, "ctx_model": "claude-opus-5",
               "rich_session_id": _v3010_sid, "agent_history": history}))

_real_time3010 = mod.time.time
try:
    mod.time.time = lambda: _v3010_now
    _v3010_open_empty = _v3010_frame([dict(_v3010_open)], 0)
    _v3010_open_two = _v3010_frame([dict(_v3010_open)], 2)
    _v3010_open_twelve = _v3010_frame([dict(_v3010_open)], 12)
    _v3010_closed_empty = _v3010_frame([dict(_v3010_closed)], 0)
    _v3010_closed_two = _v3010_frame([dict(_v3010_closed)], 2)
    _v3010_both_two = _v3010_frame(
        [dict(_v3010_open), dict(_v3010_closed)], 2)
    _v3010_plain_two = _v3010_frame([dict(_v3010_open)], 2, fallback=True)
    _v3010_bare_rich = _v3010_frame([], 12)
    _v3010_bare_plain = _v3010_frame([], 12, fallback=True)

    _v3010_bare_rich_none = _v3010_frame([], 0)
    _v3010_bare_plain_none = _v3010_frame([], 0, fallback=True)
    _v3010_bare_rich_other = _v3010_frame_hist([], _v3010_foreign_ledger(12))
    _v3010_bare_plain_other = _v3010_frame_hist(
        [], _v3010_foreign_ledger(12), fallback=True)
finally:
    mod.time.time = _real_time3010

check("39a THE VANISH REPRO: a populated ledger never removes the mid-box table",
      _v3010_current_rows(_v3010_open_empty, 1)
      == ["| 🤖 Agent | Time |", "|:---|---:|",
          "| ◉ streamer fix (Opus 5) | 01:30 |"]
      and _v3010_current_rows(_v3010_open_two, 1)
      == _v3010_current_rows(_v3010_open_empty, 1)
      and _v3010_current_rows(_v3010_open_twelve, 1)
      == _v3010_current_rows(_v3010_open_empty, 1)

      and len(_v3010_table_block(_v3010_open_empty)) == 3
      and len(_v3010_table_block(_v3010_open_two)) == 5
      and len(_v3010_table_block(_v3010_open_twelve))
      == 2 + _v3015_cap(),
      _detail26({"empty": _v3010_table_block(_v3010_open_empty),
                 "two": _v3010_table_block(_v3010_open_two),
                 "twelve": _v3010_table_block(_v3010_open_twelve)}))

check("39a a lane CLOSED this turn keeps its row at every ledger state",
      _v3010_current_rows(_v3010_closed_empty, 1)
      == ["| 🤖 Agent | Time |", "|:---|---:|", "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |"]
      and _v3010_current_rows(_v3010_closed_two, 1)
      == _v3010_current_rows(_v3010_closed_empty, 1)
      and _v3010_current_rows(_v3010_both_two, 2) == [
          "| 🤖 Agent | Time |", "|:---|---:|",
          "| ◉ streamer fix (Opus 5) | 01:30 |",
          "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |"]

      and _v3010_table_block(_v3010_closed_two).count(
          "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |") == 1,
      _detail26({"closed": _v3010_table_block(_v3010_closed_two),
                 "both": _v3010_table_block(_v3010_both_two)}))

check("39a no lanes still means no table and no dividers, ledger notwithstanding",

      "| 🤖 Agent | Time |" not in _v3010_bare_rich_none
      and "|:---|---:|" not in _v3010_bare_rich_none
      and _v3010_bare_rich_none.count("\n---") == 1
      and "🤖 Agent · Time" not in _v3010_bare_plain_none
      and "| 🤖 Agent | Time |" not in _v3010_bare_rich_other
      and "|:---|---:|" not in _v3010_bare_rich_other
      and _v3010_bare_rich_other.count("\n---") == 1
      and "🤖 Agent · Time" not in _v3010_bare_plain_other,
      _detail26({"none": _v3010_bare_rich_none,
                 "other_session": _v3010_bare_rich_other,
                 "plain": _v3010_bare_plain_none}))

check("39a (v30.15) THIS session's finished agents DO keep the box's table alive",

      "| 🤖 Agent | Time |" in _v3010_bare_rich
      and _v3010_bare_rich.count("\n---") == 2
      and len(_v3010_table_block(_v3010_bare_rich))
      == 2 + _v3015_cap()
      and "🤖 Agent · Time" in _v3010_bare_plain
      and all(row.startswith("| ✓ earlier ")
              for row in _v3010_table_block(_v3010_bare_rich)[2:]),
      _detail26({"rich": _v3010_table_block(_v3010_bare_rich),
                 "plain": _v3010_bare_plain}))

check("39b the live rich frame carries NO Agents dropdown and exactly two disclosures",
      "🤖 Agents" not in _v3010_both_two
      and "<details><summary>🤖" not in _v3010_both_two
      and not any(line.startswith(("├ ", "└ "))
                  for line in _v3010_both_two.splitlines())
      and _v3010_both_two.count("<details>") == 2
      and _v3010_both_two.count("</details>") == 2
      and _v3010_both_two.index("📋 Plan") < _v3010_both_two.index("🔧 Tools")
      and _v3010_both_two.rstrip().endswith("</details>"),
      _detail26(_v3010_both_two))

check("39b the plain fallback mirror drops the same section and nothing else",
      "🤖 Agents" not in _v3010_plain_two
      and not any(line.startswith(("├ ", "└ "))
                  for line in _v3010_plain_two.splitlines())
      and "🤖 Agent · Time" in _v3010_plain_two.splitlines()
      and "- ◉ streamer fix (Opus 5) · 01:30" in _v3010_plain_two
      and _v3010_plain_two.splitlines().index("🤖 Agent · Time")
      < next(i for i, line in enumerate(_v3010_plain_two.splitlines())
             if line.startswith("📋 Plan · "))
      and _v3010_plain_two.rstrip().endswith("live · plain fallback"),
      _detail26(_v3010_plain_two))

check("39b blank-line structure survives the removal: balanced markup, no triple gap",
      mod._rich_markup_balanced(_v3010_both_two) is True
      and mod._rich_frame_fits(_v3010_both_two) is True
      and "\n\n\n" not in _v3010_both_two
      and not _v3010_both_two.endswith("\n")
      and _v3010_both_two.count("\n---") == 2,
      _detail26(_v3010_both_two))

_v3010_records = [{"label": "LEDGER-ONLY-%d" % i, "family": "Opus",
                   "running": False, "secs": 30 + i, "outcome": "completed"}
                  for i in range(6)]
_v3010_lanes = [("streamer fix (Opus)", "◉ live", "01:30")]
_v3010_args = ("", "", "v30.10 live box", "3:20", 0, 1, "💭 thinking",
               ["- [ ] **do the thing** ◀"], 0, _v3010_lanes,
               ["- at 00:03 · Read file"], 1, mod.RICH_MIN_EDIT_INTERVAL,
               False, 3, "🧠 Opus 5 · ▓▓▓░░░░░ 42% context")
_v3010_rich_none = mod._assemble_rich_live(*_v3010_args, None, 0, 0, 0)
_v3010_rich_empty = mod._assemble_rich_live(*_v3010_args, [], 0, 0, 0)
_v3010_rich_full = mod._assemble_rich_live(*_v3010_args, _v3010_records, 4, 61, 7)
_v3010_plain_args = ("", "", "v30.10 live box", "3:20", 0, 1, "💭 thinking",
                     [{"id": "p1", "subject": "do the thing",
                       "status": "in_progress"}], _v3010_lanes,
                     ["- at 00:03 · Read file"], 1, 3,
                     "🧠 Opus 5 · ▓▓▓░░░░░ 42% context")
_v3010_plain_none = mod._assemble_plain_fallback(*_v3010_plain_args, None, 0, 0, 0)
_v3010_plain_full = mod._assemble_plain_fallback(
    *_v3010_plain_args, _v3010_records, 4, 61, 7)

check("39c agent records and counts cannot move a byte of either live frame",
      _v3010_rich_none == _v3010_rich_empty == _v3010_rich_full
      and _v3010_plain_none == _v3010_plain_full
      and "🤖 Agents" not in _v3010_rich_full
      and "LEDGER-ONLY-0" not in _v3010_rich_full
      and "🤖 Agents" not in _v3010_plain_full
      and "LEDGER-ONLY-0" not in _v3010_plain_full,
      _detail26({"rich": _v3010_rich_full, "plain": _v3010_plain_full}))

_v3010_heavy_plan = [{"id": "keep-pending", "subject": "KEEP-PENDING-3010",
                      "status": "pending"}]
_v3010_heavy_plan.extend({"id": "done-%02d" % i,
                          "subject": "DROP-%02d-3010 %s" % (i, "d" * 150),
                          "status": "completed"} for i in range(40))
_v3010_heavy_plan.append({"id": "keep-live", "subject": "KEEP-LIVE-3010",
                          "status": "in_progress"})
_real_time3010b = mod.time.time
try:
    mod.time.time = lambda: _v3010_now
    _v3010_heavy = _text26(_render_live26(
        SDIR, [], _v3010_now - 200, {}, "KEEP-MISSION-3010",
        panel=[dict(_v3010_open), dict(_v3010_closed)], plan=_v3010_heavy_plan,
        tools={"count": 30,
               "items": [{"cap": "DROPTOOL-%02d-3010 %s" % (i, "t" * 150),
                          "elapsed": i + 1} for i in range(30)]},
        state={"ctx_pct": 42, "ctx_model": "claude-opus-5",
               "rich_session_id": _v3010_sid,
               "agent_history": _v3010_ledger(12)}))
finally:
    mod.time.time = _real_time3010b

check("39c an over-budget frame still trims tools then plan, never the table",
      mod._rich_frame_fits(_v3010_heavy) is True
      and "DROPTOOL-" not in _v3010_heavy
      and "DROP-00-3010" not in _v3010_heavy
      and "… 30 earlier not shown" in _v3010_heavy
      and "lower-priority details hidden" in _v3010_heavy
      and "KEEP-PENDING-3010" in _v3010_heavy
      and "KEEP-LIVE-3010" in _v3010_heavy

      and _v3010_current_rows(_v3010_heavy, 2) == [
          "| 🤖 Agent | Time |", "|:---|---:|",
          "| ◉ streamer fix (Opus 5) | 01:30 |",
          "| ✓ Sol review (GPT-5.6 Sol) | 00:42 |"]
      and all(row.startswith("| ✓ earlier ")
              for row in _v3010_table_block(_v3010_heavy)[4:])
      and len(_v3010_table_block(_v3010_heavy)) <= 2 + _v3015_cap()
      and "🤖 Agents" not in _v3010_heavy,
      _detail26(_v3010_heavy))

_reset26()
_enable_rich26()
_v3010_carry_state = _rich_state28(
    mid="v3010-carry-old", now=_v3010_now,
    steps=[{"fam": "agent", "cap": "streamer fix", "tuid": "V3010-OPEN",
            "done": False, "k": "", "aid": "", "t0": _v3010_now - 90}],
    panel=[dict(_v3010_open)], rich_session_id=_v3010_sid,
    agent_history=_v3010_ledger(2), ctx_pct=42, ctx_model="claude-opus-5")
mod.save_state(SDIR, _v3010_carry_state)
_v3010_carry_tg = RichRecTG([_ok26("v3010-carry-new")])
_real_time3010c = mod.time.time
try:
    mod.time.time = lambda: _v3010_now + 30
    _v3010_carry_ops = mod.on_prompt(
        SDIR, _v3010_carry_tg, prompt_text="another question",
        session_id=_v3010_sid)
finally:
    mod.time.time = _real_time3010c
_v3010_carried = load()
_v3010_carry_frame = _v3010_carried.get("last_live") or ""

check("39d a carried background lane keeps the table on the freshly re-popped box",
      (_v3010_carried.get("panel") or [{}])[0].get("tuid") == "V3010-OPEN"
      and "| 🤖 Agent | Time |" in _v3010_carry_frame
      and "| ◉ streamer fix (Opus 5) | 02:00 |" in _v3010_carry_frame
      and "🤖 Agents" not in _v3010_carry_frame

      and isinstance(_v3010_carried.get("agent_history"), dict),
      _detail26({"panel": _v3010_carried.get("panel"),
                 "frame": _v3010_carry_frame, "ops": _v3010_carry_ops}))

_v3010_fold_sid = "delta-session-39"
_v3010_fold_t0 = _et_today(16, 26, 30)
_v3010_fold_now = _v3010_fold_t0 + 427
_v3010_fold_proc = _et_today(16, 21, 3)
_v3010_fold_row = {"tuid": "SOL-39", "lab": "Sol review", "typ": "codex",
                   "mdl": "gpt-5.6-sol", "t0": _v3010_fold_now - 300,
                   "done": True, "secs": 42,
                   "history_session_id": _v3010_fold_sid}
_v3010_fold_history = mod._normalize_agent_history({
    "day": mod._et_day(_v3010_fold_now), "total": 1,
    "session_totals": {_v3010_fold_sid: 1},
    "rows": [{"id": "%s:codex:SOL-39" % _v3010_fold_sid, "label": "Sol review",
              "family": "Codex", "model": "gpt-5.6-sol",
              "t0": _v3010_fold_now - 300, "secs": 42, "outcome": "completed",
              "day": mod._et_day(_v3010_fold_now),
              "session_id": _v3010_fold_sid, "closed_at": _v3010_fold_now - 10}],
}, now=_v3010_fold_now)
_real_time3010d = mod.time.time
try:
    mod.time.time = lambda: _v3010_fold_now
    _v3010_fold_value, _v3010_fold_err = _call26(
        "render_rich_final", SDIR,
        [{"fam": "files", "cap": "reading the Sample thread", "tuid": "STEP-39",
          "done": True, "secs": 3, "t0": _v3010_fold_proc + 10}],
        _v3010_fold_proc, n_agents=1, mission="Restored table",
        panel=[dict(_v3010_fold_row)], plan=[], cadence=1.5, outcome="done",
        agent_history=_v3010_fold_history)
finally:
    mod.time.time = _real_time3010d
_v3010_fold_text = _text26(_v3010_fold_value)

_V3010_FOLD_EXPECTED = (
    "<details><summary>✅ Done in 12:34 · 1/1 steps · 1 agent · tap</summary>\n"
    "\n**Mission**  \nRestored table\n"
    "\n**Agents · 1 done today**\n"
    "\n| 🤖 Agent | Time |\n|:---|---:|\n| ✓ Sol review (GPT-5.6 Sol) | 00:42 |\n"
    "\n**📋 Plan · 0/0 done**\n\n- nothing logged yet\n"
    "\n**Steps**\n\n- [x] reading the Sample thread\n\n</details>")

check("39e the fold summary is byte-identical to the pre-v30.10 build except the retired Tools section and the v30.21 family label",
      not _v3010_fold_err
      and _v3010_fold_text == _V3010_FOLD_EXPECTED
      and hashlib.sha256(_v3010_fold_text.encode("utf-8")).hexdigest()
      == "7d5a626b9aae68d5ab5a462e0e08bed1746690802143b90e35c60155e7688969",
      _v3010_fold_err or _detail26(_v3010_fold_text))

_v3010_state_probe = {"ctx_pct": 42, "ctx_model": "claude-opus-5",
                      "rich_session_id": _v3010_sid,
                      "agent_history": _v3010_ledger(2)}
_v3010_keys_before = sorted(_v3010_state_probe)
_v3010_history_before = json.dumps(_v3010_state_probe["agent_history"],
                                   sort_keys=True)
_real_time3010e = mod.time.time
try:
    mod.time.time = lambda: _v3010_now
    _render_live26(SDIR, [], _v3010_now - 200, {}, "v30.10 live box",
                   panel=[dict(_v3010_open)], plan=[],
                   tools={"items": [], "count": 0}, state=_v3010_state_probe)
    _render_live26(SDIR, [], _v3010_now - 200, {}, "v30.10 live box",
                   panel=[dict(_v3010_open)], plan=[],
                   tools={"items": [], "count": 0}, fallback=True,
                   state=_v3010_state_probe)
finally:
    mod.time.time = _real_time3010e
check("39f rendering the live box adds no state key and rewrites no ledger",
      sorted(_v3010_state_probe) == _v3010_keys_before
      and json.dumps(_v3010_state_probe["agent_history"], sort_keys=True)
      == _v3010_history_before,
      _detail26(_v3010_state_probe))

_reset26()

print("\n===== #40 live notification texts close the row they name =====")

def _sample_report40(length):
    """Keep the original long-payload parser case while shipping only synthetic prose."""
    body = ("## Fixture review\n\nThe sample configuration was checked against its test contract.\n"
            "- The fixture preserves a long markdown result and its notification envelope.\n"
            "- No customer, project, financial, account, or operational data is included.\n\n")
    return (body * (length // len(body) + 1))[:length]


_LIVE_NOTIF_NO_TUID_40 = (
    ('<task-notification>\n<task-id>a0000000000000005</task-id>\n<output-file>/private/tmp/claude-501/-Users-example-claude-channels-3/00000000-0000-4000-8000-000000000003/tasks/a0000000000000005.output</output-file>\n<status>completed</status>\n<summary>Agent "Audit the Project Notes" finished</summary>\n<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>\n<result>'
    + _sample_report40(2481)
    + '</result>\n<usage><subagent_tokens>166439</subagent_tokens><tool_uses>24</tool_uses><duration_ms>2506357</duration_ms></usage>\n</task-notification>')
)

_LIVE_NOTIF_RESUMED_40 = (
    ('<task-notification>\n<task-id>a0000000000000005</task-id>\n<tool-use-id>toolu_fixture000000000000000006</tool-use-id>\n<output-file>/private/tmp/claude-501/-Users-example-claude-channels-3/00000000-0000-4000-8000-000000000003/tasks/a0000000000000005.output</output-file>\n<status>completed</status>\n<summary>Agent "Audit the Project Notes" finished</summary>\n<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>\n<result>'
    + _sample_report40(15570)
    + '</result>\n<usage><subagent_tokens>168946</subagent_tokens><tool_uses>0</tool_uses><duration_ms>79882</duration_ms></usage>\n</task-notification>')
)

_LIVE_LABEL_40 = "Audit the Project Notes"

def _agent_input40(description):
    return {"description": description, "subagent_type": "opus-executor",
            "model": "opus", "run_in_background": True}

def _launch40(tg, tuid, description, session, clock):
    _, pre_err = _step26(tg, "pre", "Agent", _agent_input40(description),
                         tuid=tuid, session_id=session)
    clock.advance(1)
    _, post_err = _step26(tg, "post", "Agent", _agent_input40(description),
                          tuid=tuid, session_id=session,
                          tresponse={"result": "Async agent launched successfully. "
                                               "agentId: %s" % tuid})
    return pre_err or post_err

_reset26()
_enable_rich26()
_clock40 = _Clock26(40000.0)
_real_time40 = mod.time.time
try:
    mod.time.time = _clock40.time
    _tg40 = RichRecTG()
    _session40 = "notification-regression-session-40"
    _, _open_err40 = _prompt26(_tg40, "audit the project notes", _session40)
    _clock40.advance(4)
    _launch_err40 = _launch40(_tg40, "AGENT-LAUNCH-40", _LIVE_LABEL_40,
                              _session40, _clock40)
    _row_at_launch40 = _panel_row31(load(), "AGENT-LAUNCH-40")
    _t0_40 = _row_at_launch40.get("t0")

    _clock40.advance(5100)
    _held_ops40 = mod.record_reply(SDIR, _tg40, final=True)
    mod._run_deferred(SDIR, _tg40, _held_ops40)
    _held40 = load()
    check("40a an open background row holds the box at the Stop edge",
          not (_open_err40 or _launch_err40)
          and _row_at_launch40.get("done") is False
          and _row_at_launch40.get("background") is True
          and mod._has_open_background_agents(_held40.get("panel")) is True
          and _held_ops40 == [("typing", None, None, False)]
          and _held40.get("folded") is False,
          _open_err40 or _launch_err40
          or _detail26({"ops": _held_ops40, "state": _held40}))

    _before_mid40 = str(_held40.get("msgid"))
    _before_start40 = _held40.get("proc_start")
    _before_sends40 = len(_rich_ops26(_tg40, "send_rich"))
    _clock40.advance(600)
    _notify_ops40, _notify_err40 = _prompt26(
        _tg40, _LIVE_NOTIF_RESUMED_40, _session40)
    _after_notify40 = load()
    _closed_row40 = _panel_row31(_after_notify40, "AGENT-LAUNCH-40")
    check("40b the live RESUMED-id notification closes the row it names, in place",
          not _notify_err40
          and _closed_row40.get("done") is True
          and _closed_row40.get("outcome") == "completed"
          and abs(float(_closed_row40.get("secs", -1))
                  - (_clock40.now - float(_t0_40))) < 0.001
          and abs(float(_closed_row40.get("secs", -1)) - 5701.0) < 0.001
          and str(_after_notify40.get("msgid")) == _before_mid40
          and _after_notify40.get("proc_start") == _before_start40
          and len(_rich_ops26(_tg40, "send_rich")) == _before_sends40
          and isinstance(_notify_ops40, list) and _notify_ops40,
          _notify_err40 or _detail26({"row": _closed_row40,
                                      "ops": _notify_ops40,
                                      "state": _after_notify40}))

    _fold_ops40 = mod.record_reply(SDIR, _tg40, final=True)
    mod._run_deferred(SDIR, _tg40, _fold_ops40)
    _folded40 = load()
    _hist_rows40 = ((_folded40.get("agent_history") or {}).get("rows") or [])
    check("40c the keepalive releases and the next Stop folds the box",
          mod._has_open_background_agents(_folded40.get("panel")) is False
          and _folded40.get("folded") is True
          and _folded40.get("typing_done") is True
          and len([r for r in _hist_rows40
                   if r.get("label") == _LIVE_LABEL_40]) == 1
          and all(r.get("outcome") == "completed" for r in _hist_rows40
                  if r.get("label") == _LIVE_LABEL_40),
          _detail26({"ops": _fold_ops40, "state": _folded40}))
finally:
    mod.time.time = _real_time40

_reset26()
_enable_rich26()
_clock40b = _Clock26(60000.0)
_real_time40b = mod.time.time
try:
    mod.time.time = _clock40b.time
    _tg40b = RichRecTG()
    _session40b = "notification-regression-session-40b"
    _, _open_err40b = _prompt26(_tg40b, "audit the project notes", _session40b)
    _clock40b.advance(4)
    _launch_err40b = _launch40(_tg40b, "AGENT-NAMED-40", _LIVE_LABEL_40,
                               _session40b, _clock40b)
    _clock40b.advance(2)
    _sibling_err40b = _launch40(_tg40b, "AGENT-SIBLING-40", "Review the notes",
                                _session40b, _clock40b)
    _clock40b.advance(300)
    _keys_before40 = sorted(load())
    _no_tuid_ops40, _no_tuid_err40 = _prompt26(
        _tg40b, _LIVE_NOTIF_NO_TUID_40, _session40b)
    _after_no_tuid40 = load()
    _named_row40 = _panel_row31(_after_no_tuid40, "AGENT-NAMED-40")
    _sibling_row40 = _panel_row31(_after_no_tuid40, "AGENT-SIBLING-40")
    check("40d a notification with NO tool-use-id closes the row its summary names",
          not (_open_err40b or _launch_err40b or _sibling_err40b or _no_tuid_err40)
          and _named_row40.get("done") is True
          and _named_row40.get("outcome") == "completed"
          and abs(float(_named_row40.get("secs", -1)) - 304.0) < 0.001
          and _sibling_row40.get("done") is False
          and sorted(_after_no_tuid40) == _keys_before40,
          _no_tuid_err40 or _detail26({"named": _named_row40,
                                       "sibling": _sibling_row40}))

    _before_repeat40 = load()
    _before_repeat_calls40 = list(_tg40b.calls)
    _repeat_ops40, _repeat_err40 = _prompt26(
        _tg40b, _LIVE_NOTIF_NO_TUID_40, _session40b)
    _after_repeat40 = load()
    check("40e repeating that notification is a no-op, not a new box",
          not _repeat_err40 and _repeat_ops40 == []
          and _after_repeat40 == _before_repeat40
          and _tg40b.calls == _before_repeat_calls40
          and _panel_row31(_after_repeat40, "AGENT-SIBLING-40").get("done") is False,
          _repeat_err40 or _detail26({"ops": _repeat_ops40,
                                      "after": _after_repeat40}))
finally:
    mod.time.time = _real_time40b

_twin_notification40 = _LIVE_NOTIF_RESUMED_40.replace(
    "toolu_fixture000000000000000006", "AGENT-TWIN-A-40")
_twin_state40 = {
    "steps": [], "msgid": "4040", "folded": False,
    "panel": [
        {"tuid": "AGENT-TWIN-A-40", "lab": _LIVE_LABEL_40, "typ": "opus-executor",
         "mdl": "opus", "t0": 70000.0, "done": False, "secs": 0, "background": True},
        {"tuid": "AGENT-TWIN-B-40", "lab": _LIVE_LABEL_40, "typ": "opus-executor",
         "mdl": "opus", "t0": 70010.0, "done": False, "secs": 0, "background": True},
    ],
}
_twin_result40 = mod._close_codex_notification(SDIR, _twin_state40,
                                               _twin_notification40)
check("40f a resolvable tool-use-id keeps the label from stealing a twin row",
      isinstance(_twin_result40, list)
      and _twin_state40["panel"][0].get("done") is True
      and _twin_state40["panel"][0].get("outcome") == "completed"
      and _twin_state40["panel"][1].get("done") is False,
      _detail26(_twin_state40["panel"]))

_unknown_state40 = {
    "steps": [], "msgid": "4041",
    "panel": [{"tuid": "AGENT-SYNC-40", "lab": _LIVE_LABEL_40, "typ": "opus-executor",
               "mdl": "opus", "t0": 70000.0, "done": False, "secs": 0}],
}
_unknown_before40 = json.loads(json.dumps(_unknown_state40))
_unknown_result40 = mod._close_codex_notification(SDIR, _unknown_state40,
                                                  _LIVE_NOTIF_NO_TUID_40)
_other_state40 = {
    "steps": [], "msgid": "4042",
    "panel": [{"tuid": "AGENT-OTHER-40", "lab": "Something else entirely",
               "typ": "opus-executor", "mdl": "opus", "t0": 70000.0,
               "done": False, "secs": 0, "background": True}],
}
_other_before40 = json.loads(json.dumps(_other_state40))
_other_result40 = mod._close_codex_notification(SDIR, _other_state40,
                                                _LIVE_NOTIF_NO_TUID_40)
check("40g an unmatched label changes nothing and never closes a synchronous row",
      _unknown_result40 is None and _unknown_state40 == _unknown_before40
      and _other_result40 is None and _other_state40 == _other_before40
      and mod._terminal_notification_agent_labels(
          "<task-notification>\n<status>completed</status>\n"
          "<summary>Background command \"Watch for the report\" completed "
          "(exit code 0)</summary>\n</task-notification>") == []
      and mod._terminal_notification_agent_labels(
          "what does <task-notification> mean?") == [],
      _detail26({"unknown": _unknown_state40, "other": _other_state40}))

_reset26()

print("\n===== #41 v30.13 latency: handoff, subagent suppression, "
      "bounded lock, agent-scaled cadence =====")

import fcntl as _fcntl41

_MIN_EDIT41 = mod.MIN_EDIT_INTERVAL
_ANIM41 = mod.ANIM_INTERVAL
_HANDOFF41 = mod.ANIM_HANDOFF_TICKS
_LOCKWAIT41 = mod.LOCK_WAIT_TOOL

class _Animator41:

    def __init__(self, sdir):
        self.path = os.path.join(sdir, "anim.lock")
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        _fcntl41.flock(self.fh, _fcntl41.LOCK_EX | _fcntl41.LOCK_NB)
        return self

    def __exit__(self, *exc):
        try:
            _fcntl41.flock(self.fh, _fcntl41.LOCK_UN)
            self.fh.close()
        except Exception:
            pass
        return False

def _open_box41(tg, mission="v30.13 latency"):
    reset_state()
    mod._run_deferred(SDIR, tg, mod.on_prompt(SDIR, tg, prompt_text=mission))
    tg.calls[:] = []
    return load()

def _age_last_edit41(seconds):

    st = load()
    st["last_edit"] = mod.time.time() - seconds
    mod.save_state(SDIR, st)
    return st["last_edit"]

_tg41a = RecTG()
_open_box41(_tg41a)
_aged41a = _age_last_edit41(_MIN_EDIT41 + 0.5)
with _Animator41(SDIR):
    run_step(_tg41a, "pre", "Read", {"file_path": "/tmp/handoff.txt"}, tuid="H41A")
    run_step(_tg41a, "post", "Read", {"file_path": "/tmp/handoff.txt"}, tuid="H41A")
_st41a = load()
check("41a a live, keeping-up animator takes the repaint: the tool event edits nothing",
      _tg41a.ops("edit") == [] and _tg41a.ops("send") == [],
      "calls=%r" % (_tg41a.calls,))
check("41a and spends no typing round trip either (v30.12's floor still holds)",
      _tg41a.ops("typing") == [], "typing=%d" % len(_tg41a.ops("typing")))
check("41a the step is still recorded in full - state work never moved",
      len(_st41a.get("steps") or []) == 1
      and (_st41a["steps"][0].get("cap") or "").endswith("handoff.txt")
      and _st41a["steps"][0].get("done") is True,
      "steps=%r" % (_st41a.get("steps"),))
check("41a last_edit is NOT stamped by a handed-off event - stamping it would "
      "starve the animator's own gate on a busy turn",
      abs(_st41a.get("last_edit", 0) - _aged41a) < 0.001,
      "last_edit=%r aged=%r" % (_st41a.get("last_edit"), _aged41a))
check("41a so the animator's gate is still open on the state the hook just wrote",
      (mod.time.time() - _st41a.get("last_edit", 0)) >= mod._effective_min_edit(SDIR, _st41a),
      "age=%.2f gate=%.2f" % (mod.time.time() - _st41a.get("last_edit", 0),
                              mod._effective_min_edit(SDIR, _st41a)))

_tg41b = RecTG()
_open_box41(_tg41b)
_age_last_edit41(_MIN_EDIT41 + 0.5)
run_step(_tg41b, "pre", "Read", {"file_path": "/tmp/noanim.txt"}, tuid="H41B")
_st41b = load()
check("41b with NO animator alive the hook paints inline exactly as v30.12 did",
      len(_tg41b.ops("edit")) == 1, "calls=%r" % (_tg41b.calls,))
check("41b and stamps last_edit, because it really did paint",
      (mod.time.time() - _st41b.get("last_edit", 0)) < 1.0,
      "last_edit age=%.2f" % (mod.time.time() - _st41b.get("last_edit", 0)))
check("41b and still refreshes native typing, since nobody else is",
      len(_tg41b.ops("typing")) == 1, "typing=%d" % len(_tg41b.ops("typing")))

_tg41c = RecTG()
_open_box41(_tg41c)
_age_last_edit41(_HANDOFF41 * _ANIM41 + 5.0)
with _Animator41(SDIR):
    run_step(_tg41c, "pre", "Read", {"file_path": "/tmp/wedged.txt"}, tuid="H41C")
check("41c an animator that holds the lock but has stopped painting does NOT get the "
      "handoff - the box can never freeze behind a wedged tick",
      len(_tg41c.ops("edit")) == 1, "calls=%r" % (_tg41c.calls,))

_tg41d = RecTG()
_open_box41(_tg41d)
_st41d0 = load()
_st41d0["folded"] = True
_st41d0["steps"] = [{"fam": "files", "cap": "reading before.txt", "tuid": "OLD41D",
                     "done": True, "k": "", "aid": ""}]
mod.save_state(SDIR, _st41d0)
with _Animator41(SDIR):
    run_step(_tg41d, "pre", "Read", {"file_path": "/tmp/revive.txt"}, tuid="H41D")
_st41d = load()
check("41d a REVIVED box still repaints instantly - last_edit 0 fails the handoff "
      "window by construction, so v26's un-fold keeps its immediate frame",
      len(_tg41d.ops("edit")) == 1 and _st41d.get("folded") is False,
      "calls=%r folded=%r" % (_tg41d.calls, _st41d.get("folded")))

_tg41e = RecTG()
_open_box41(_tg41e)
_age_last_edit41(_HANDOFF41 * _ANIM41 + 5.0)
with _Animator41(SDIR):
    run_step(_tg41e, "pre", "Read", {"file_path": "/tmp/sub.txt"}, tuid="H41E",
             agent_id="agent-41e")
_sub_edits41 = len(_tg41e.ops("edit"))
_age_last_edit41(_HANDOFF41 * _ANIM41 + 5.0)
with _Animator41(SDIR):
    run_step(_tg41e, "pre", "Read", {"file_path": "/tmp/parent.txt"}, tuid="H41E2")
_parent_edits41 = len(_tg41e.ops("edit")) - _sub_edits41
check("41e a subagent tool call never redraws the box, even past the handoff window",
      _sub_edits41 == 0, "subagent edits=%d" % _sub_edits41)
check("41e while the parent's own event in the same state still does",
      _parent_edits41 == 1, "parent edits=%d" % _parent_edits41)
_st41e = load()
check("41e the subagent's step is recorded in full - state only, never a lost row",
      any((s.get("aid") == "agent-41e") for s in (_st41e.get("steps") or [])),
      "steps=%r" % [(s.get("cap"), s.get("aid")) for s in (_st41e.get("steps") or [])])

def _all_subagent_turn41(with_animator):
    tg = RecTG()
    _open_box41(tg)
    holder = _Animator41(SDIR) if with_animator else None
    if holder:
        holder.__enter__()
    try:
        for i in range(3):
            _age_last_edit41(_MIN_EDIT41 + 0.5)
            run_step(tg, "pre", "Read", {"file_path": "/tmp/f%d.txt" % i},
                     tuid="F41F%d" % i, agent_id="agent-41f")
            run_step(tg, "post", "Read", {"file_path": "/tmp/f%d.txt" % i},
                     tuid="F41F%d" % i, agent_id="agent-41f")
    finally:
        if holder:
            holder.__exit__()
    work_edits = len(tg.ops("edit"))
    steps_before_stop = [dict(s) for s in (load().get("steps") or [])]
    run_reply(tg, final=True)
    fold = tg.ops("edit")[-1] if tg.ops("edit") else None
    return tg, work_edits, steps_before_stop, (fold[2] if fold else ""), load()

_tg41f, _work_edits41, _steps41f, _fold_text41, _end41f = _all_subagent_turn41(True)
_tg41f2, _work_edits41b, _steps41f2, _fold_text41b, _end41f2 = _all_subagent_turn41(False)
_norm41 = lambda t: re.sub(r"\d+:\d\d", "M:SS", t or "")
check("41f a turn whose work is ALL subagent calls spends zero edits while working",
      _work_edits41 == 0, "edits during work=%d" % _work_edits41)
check("41f while the same turn on the v30.12 path paid for every window it opened",
      _work_edits41b > 0, "inline edits=%d" % _work_edits41b)
check("41f every subagent step is in state, complete and flipped done - state work "
      "never moved, only who paints",
      len(_steps41f) == 3 and all(s.get("done") for s in _steps41f)
      and all(s.get("aid") == "agent-41f" for s in _steps41f),
      "steps=%r" % [(s.get("cap"), s.get("done"), s.get("aid")) for s in _steps41f])
check("41f and the Stop fold is BYTE-IDENTICAL to the fold the un-suppressed turn "
      "wrote - suppression changes what a turn pays, never what it shows",
      _fold_text41.startswith(mod.RECAP_MARK)
      and _norm41(_fold_text41) == _norm41(_fold_text41b),
      "suppressed=%r inline=%r" % (_norm41(_fold_text41)[:120],
                                   _norm41(_fold_text41b)[:120]))
check("41f the turn's final state is folded and typing is done - nothing left open",
      _end41f.get("folded") is True and _end41f.get("typing_done") is True,
      "folded=%r typing_done=%r" % (_end41f.get("folded"), _end41f.get("typing_done")))

_tg41g = RecTG()
reset_state()
with _Animator41(SDIR):
    run_step(_tg41g, "pre", "Read", {"file_path": "/tmp/cold.txt"}, tuid="H41G",
             agent_id="agent-41g")
check("41g a subagent event with NO box still OPENS one - suppression covers the "
      "repaint, never the cold start, or the turn would have no box at all",
      len(_tg41g.ops("send")) == 1 and load().get("msgid"),
      "calls=%r msgid=%r" % (_tg41g.calls, load().get("msgid")))

_lockpath41 = os.path.join(SDIR, "lock")
_held41 = open(_lockpath41, "w")
_fcntl41.flock(_held41, _fcntl41.LOCK_EX)
_t41 = time.time()
_fh41, _got41 = mod._acquire_state_lock(_lockpath41, 0.2)
_waited41 = time.time() - _t41
check("41h a bounded acquire gives up on real contention instead of blocking",
      _got41 is False and _fh41 is None, "got=%r fh=%r" % (_got41, _fh41))
check("41h and gives up INSIDE its budget (0.2s), not at the 20s harness timeout",
      0.15 <= _waited41 <= 2.0, "waited=%.3fs" % _waited41)
_fcntl41.flock(_held41, _fcntl41.LOCK_UN)
_held41.close()
_fh41b, _got41b = mod._acquire_state_lock(_lockpath41, 0.2)
check("41h an uncontended bounded acquire simply succeeds",
      _got41b is True and _fh41b is not None, "got=%r" % (_got41b,))
if _fh41b:
    _fcntl41.flock(_fh41b, _fcntl41.LOCK_UN)
    _fh41b.close()
_fh41c, _got41c = mod._acquire_state_lock(_lockpath41, None)
check("41h budget None is the v29 blocking acquire, unchanged",
      _got41c is True and _fh41c is not None, "got=%r" % (_got41c,))
if _fh41c:
    _fcntl41.flock(_fh41c, _fcntl41.LOCK_UN)
    _fh41c.close()
_fh41d, _got41d = mod._acquire_state_lock(
    os.path.join(SDIR, "no-such-dir-41", "lock"), 0.2)
check("41h a lock file that cannot even be opened FAILS OPEN, exactly as main() "
      "has always treated it - only real contention drops anything",
      _got41d is True and _fh41d is None, "got=%r fh=%r" % (_got41d, _fh41d))

_tg41i = RecTG()
_open_box41(_tg41i)
_age_last_edit41(_MIN_EDIT41 + 0.5)
_before41i = open(os.path.join(SDIR, "state.json"), "rb").read()
_blocker41 = open(_lockpath41, "w")
_fcntl41.flock(_blocker41, _fcntl41.LOCK_EX)
_t41i = time.time()
_p41i = drive_main("post", {"tool_name": "Read", "tool_use_id": "H41I",
                            "tool_input": {"file_path": "/tmp/blocked.txt"},
                            "tool_response": {"ok": True}},
                   extra_env={"STREAM_LOCK_WAIT": "0.2"})
_elapsed41i = time.time() - _t41i
_after41i = open(os.path.join(SDIR, "state.json"), "rb").read()
check("41i a tool event queued behind a held lock now gives up instead of blocking "
      "to the harness timeout",
      _elapsed41i < 4.0, "elapsed=%.2fs (post budget = 0.2 x 3)" % _elapsed41i)
check("41i and it exits 0 and writes NOTHING - a dropped frame, never a half state",
      _p41i.returncode == 0 and _after41i == _before41i,
      "rc=%d state_changed=%r" % (_p41i.returncode, _after41i != _before41i))
_fcntl41.flock(_blocker41, _fcntl41.LOCK_UN)
_blocker41.close()
_p41i2 = drive_main("pre", {"tool_name": "Read", "tool_use_id": "H41I2",
                            "tool_input": {"file_path": "/tmp/free.txt"}},
                    extra_env={"STREAM_LOCK_WAIT": "0.2"})
check("41i the very next event, with the lock free, records normally - the drop is "
      "one frame, never a poisoned state dir",
      _p41i2.returncode == 0 and any(
          "free.txt" in (s.get("cap") or "") for s in (load().get("steps") or [])),
      "steps=%r" % [s.get("cap") for s in (load().get("steps") or [])])
check("41i and the dropped event left no trace at all - no ghost row for H41I",
      not any("blocked.txt" in (s.get("cap") or "")
              for s in (load().get("steps") or [])),
      "steps=%r" % [s.get("cap") for s in (load().get("steps") or [])])
_p41i3 = drive_main("post", {"tool_name": "Read", "tool_use_id": "H41I2",
                             "tool_input": {"file_path": "/tmp/free.txt"},
                             "tool_response": {"ok": True}},
                    extra_env={"STREAM_LOCK_WAIT": "0.2"})
_tg41i_stop = RecTG()
run_reply(_tg41i_stop, final=True)
_fold41i = _tg41i_stop.ops("edit")[-1][2] if _tg41i_stop.ops("edit") else ""
check("41i AND the turn's final edit still lands after a dropped frame - the recap "
      "folds and carries the work that WAS recorded",
      load().get("folded") is True and _fold41i.startswith(mod.RECAP_MARK)
      and "free" in _fold41i,
      "folded=%r fold=%r" % (load().get("folded"), _fold41i[:110]))

_tg41j = RecTG()
_open_box41(_tg41j)
run_step(_tg41j, "pre", "Read", {"file_path": "/tmp/j.txt"}, tuid="H41J")
run_step(_tg41j, "post", "Read", {"file_path": "/tmp/j.txt"}, tuid="H41J")
_blocker41j = open(_lockpath41, "w")
_fcntl41.flock(_blocker41j, _fcntl41.LOCK_EX)

def _release41j():
    time.sleep(1.0)
    try:
        _fcntl41.flock(_blocker41j, _fcntl41.LOCK_UN)
        _blocker41j.close()
    except Exception:
        pass

_th41j = threading.Thread(target=_release41j)
_th41j.start()
_t41j = time.time()
_p41j = drive_main("stop", {}, extra_env={"STREAM_LOCK_WAIT": "0.05"})
_elapsed41j = time.time() - _t41j
_th41j.join()
_st41j = load()
check("41j a Stop hook still WAITS for the lock however long it takes - the bound is "
      "for tool events only, and a lost fold is not a dropped frame",
      _elapsed41j >= 0.9, "elapsed=%.2fs (blocked 1.0s, budget env 0.05s)" % _elapsed41j)
check("41j and the fold really landed after the wait",
      _st41j.get("folded") is True, "folded=%r" % (_st41j.get("folded"),))

def _panel41(n_active, n_done=0, age=0.0):
    return {"rich_box": True, "proc_start": mod.time.time() - age,
            "panel": ([{"done": False}] * n_active) + ([{"done": True}] * n_done)}

_table41 = [(n, mod._effective_min_edit(SDIR, _panel41(n))) for n in range(0, 7)]

_table41m = [(n, mod._effective_min_edit(
    SDIR, _panel41(n, age=mod.CADENCE_FAST_MAX))) for n in range(0, 7)]
check("41k one agent (or none) keeps the 1.5s rich window exactly as v30",
      _table41[0][1] == mod.RICH_MIN_EDIT_INTERVAL
      and _table41[1][1] == mod.RICH_MIN_EDIT_INTERVAL, "table=%r" % (_table41,))
check("41k every extra active agent widens the window by RICH_AGENT_EDIT_STEP",
      _table41[2][1] == mod.RICH_MIN_EDIT_INTERVAL + mod.RICH_AGENT_EDIT_STEP
      and _table41[3][1] == mod.RICH_MIN_EDIT_INTERVAL + 2 * mod.RICH_AGENT_EDIT_STEP,
      "table=%r" % (_table41,))

check("41k and it is capped at RICH_SLOW_INTERVAL, a cadence this file already ships",
      _table41m[4][1] == mod.RICH_SLOW_INTERVAL
      and _table41m[6][1] == mod.RICH_SLOW_INTERVAL
      and mod._effective_min_edit(SDIR, _panel41(9)) == mod.RICH_SLOW_INTERVAL,
      "fast=%r medium=%r" % (_table41, _table41m))
check("41k DONE agent rows never slow the box down - only live ones count",
      mod._effective_min_edit(SDIR, _panel41(1, n_done=5)) == mod.RICH_MIN_EDIT_INTERVAL,
      "with 5 finished rows -> %.2f" % mod._effective_min_edit(SDIR, _panel41(1, 5)))

_plain41 = {"proc_start": mod.time.time(), "panel": [{"done": False}] * 6}
check("41k a plain (flag-off) turn is untouched by the scaling",
      mod._effective_min_edit(SDIR, _plain41) ==
      mod.MIN_EDIT_INTERVAL, "plain=%r" % mod._effective_min_edit(SDIR, _plain41))
check("41k rich_slow still wins outright",
      mod._effective_min_edit(SDIR, {"rich_slow": True, "rich_box": True,
                                     "panel": [{"done": False}] * 6}) ==
      mod.RICH_SLOW_INTERVAL)
check("41k a corrupt panel can never be the thing that raises",
      mod._effective_min_edit(SDIR, {"rich_box": True, "panel": "not-a-list",
                                     "proc_start": mod.time.time()}) ==
      mod.RICH_MIN_EDIT_INTERVAL
      and mod._active_agent_count({"panel": None}) == 0
      and mod._active_agent_count(None) == 0)

_steps41l = [{"fam": "files", "cap": "reading a.txt", "tuid": "L1", "done": True,
              "k": "", "aid": ""},
             {"fam": "run", "cap": "running tests", "tuid": "L2", "done": False,
              "k": "", "aid": ""}]
_state41l = {"rich_box": True, "panel": [], "ctx_pct": 12, "ctx_model": "Fable 5"}
_frame_fast41 = mod.render_rich_live(SDIR, _steps41l, mod.time.time() - 30, {},
                                     "cadence proof", [], None, None, 1.5,
                                     frame=3, state=_state41l)
_frame_slow41 = mod.render_rich_live(SDIR, _steps41l, mod.time.time() - 30, {},
                                     "cadence proof", [], None, None, 3.0,
                                     frame=3, state=_state41l)
check("41l the rendered live frame is byte-identical at 1.5s and 3.0s cadence - "
      "priority 4 is latency only, the box does not move",
      _frame_fast41 == _frame_slow41,
      "len=%d/%d" % (len(_frame_fast41), len(_frame_slow41)))
_final_fast41, _ = mod.render_rich_final(SDIR, _steps41l, mod.time.time() - 30, 0,
                                         "cadence proof", [], None, 1.5)
_final_slow41, _ = mod.render_rich_final(SDIR, _steps41l, mod.time.time() - 30, 0,
                                         "cadence proof", [], None, 3.0)
check("41l and so is the folded recap",
      _final_fast41 == _final_slow41,
      "len=%d/%d" % (len(_final_fast41), len(_final_slow41)))

_tg41m = RecTG()
_open_box41(_tg41m)
_st41m = load()
_st41m["rich_box"] = True
_st41m["panel"] = [{"done": False, "tuid": "A%d" % i} for i in range(4)]
mod.save_state(SDIR, _st41m)

check("41m the animator's own repaint gate reads the scaled window off live state",
      mod._effective_min_edit(SDIR, load()) ==
      mod.RICH_MIN_EDIT_INTERVAL + 3 * mod.RICH_AGENT_EDIT_STEP,
      "min_edit=%.2f with 4 open dispatch rows" % mod._effective_min_edit(SDIR, load()))
check("41m while the animator's TICK stays fast, so the timer never stops climbing",
      mod._effective_anim_interval(SDIR, load()) == mod.RICH_ANIM_INTERVAL,
      "tick=%.2f" % mod._effective_anim_interval(SDIR, load()))

_steps41n = [
    {"fam": "files", "cap": "reading brief.md", "tuid": "N1", "done": True,
     "k": "", "aid": "", "secs": 2},
    {"fam": "run", "cap": "running the suite", "tuid": "N2", "done": True,
     "k": "", "aid": "", "secs": 9},
    {"fam": "write", "cap": "editing tg-stream.py", "tuid": "N3", "done": True,
     "k": "", "aid": "", "secs": 1},
]
_tools41n = {"items": [{"cap": "reading brief.md", "elapsed": 2},
                       {"cap": "running the suite", "elapsed": 11},
                       {"cap": "editing tg-stream.py", "elapsed": 12}], "count": 3}
_caps41n = ["reading brief.md", "running the suite", "editing tg-stream.py"]
_start41n = mod.time.time() - 77

_rich_fold41, _ = mod.render_rich_final(
    SDIR, _steps41n, _start41n, 0, "finish the ticket", [], None, 1.5,
    tools=_tools41n)
check("41n the RICH folded recap keeps the steps with their check marks",
      "**Steps**" in _rich_fold41
      and all(("- [x] " + c) in _rich_fold41 for c in _caps41n),
      _rich_fold41[:200])
check("41n and carries NO tools section - no heading, no rows, no omission marker",
      "🔧 Tools" not in _rich_fold41
      and "\n- at " not in _rich_fold41
      and "earlier not shown" not in _rich_fold41,
      _rich_fold41)
check("41n so every caption is spelled out EXACTLY ONCE, which is the whole ask",
      all(_rich_fold41.count(c) == 1 for c in _caps41n),
      repr([(c, _rich_fold41.count(c)) for c in _caps41n]))

_plain_fold41, _ = mod.render_final(
    _steps41n, _start41n, 0, "finish the ticket", [], plan=[], tools=_tools41n)
check("41n the PLAIN folded recap does the same: steps with ✓, no tools section",
      _plain_fold41.startswith(mod.RECAP_MARK)
      and all(("✓ " in line) for line in _plain_fold41.splitlines()
              if "reading brief" in line)
      and "🔧 Tools" not in _plain_fold41
      and ">\\- at " not in _plain_fold41,
      _plain_fold41)
check("41n and the same once-only rule holds on the escaped plain body",
      all(_plain_fold41.count(mod.escape_mdv2(c)) == 1 for c in _caps41n),
      repr([(c, _plain_fold41.count(mod.escape_mdv2(c))) for c in _caps41n]))

_live_state41o = {"rich_box": True, "panel": [], "ctx_pct": 12,
                  "ctx_model": "Fable 5"}
_live41o = mod.render_rich_live(
    SDIR, _steps41n, _start41n, {}, "finish the ticket", [], None, _tools41n,
    1.5, frame=3, state=_live_state41o)
_fallback41o = mod.render_rich_live(
    SDIR, _steps41n, _start41n, {}, "finish the ticket", [], None, _tools41n,
    1.5, fallback=True, frame=3, state=_live_state41o)
check("41o the LIVE rich box still carries its collapsed Tools dropdown and its rows",
      "<details><summary>🔧 Tools · 3 calls · tap</summary>" in _live41o
      and all(("· " + c) in _live41o for c in _caps41n),
      _live41o[:240])
check("41o the LIVE plain-fallback frame still carries its Tools line too",
      "🔧 Tools · 3 calls" in _fallback41o, _fallback41o[:240])
check("41o and the v29 plain live frame never had a tools section to lose",
      "🔧 Tools" not in mod.render_live(
          _steps41n, 3, _start41n, {}, "finish the ticket", []),
      "")

_merged41p = mod.merge_rich_recap(
    _rich_fold41, SDIR, _steps41n, _start41n, "finish the ticket", [], None,
    1.5, tools=_tools41n)
check("41p a rich merged continuation carries no tools section either",
      bool(_merged41p) and "🔧 Tools" not in _merged41p
      and "**Steps**" in _merged41p, (_merged41p or "")[:200])
_merged_plain41p = mod.merge_recap(
    _plain_fold41, _steps41n, _start41n, 0, "finish the ticket", [], plan=[],
    tools=_tools41n)
check("41p and neither does a plain merged continuation",
      bool(_merged_plain41p) and "🔧 Tools" not in _merged_plain41p,
      (_merged_plain41p or "")[:200])

_with_tools41q = len(_rich_fold41) + len("\n\n**🔧 Tools · 3 calls**\n\n") + sum(
    len("- at 00:%02d · %s\n" % (i, c)) for i, c in enumerate(_caps41n))
check("41q the folded recap is materially shorter than the same recap with tools",
      len(_rich_fold41) < _with_tools41q * 0.75,
      "now=%d chars, with the tools section it would be about %d" % (
          len(_rich_fold41), _with_tools41q))

mod.MIN_EDIT_INTERVAL = _MIN_EDIT41
mod.ANIM_INTERVAL = _ANIM41
mod.ANIM_HANDOFF_TICKS = _HANDOFF41
mod.LOCK_WAIT_TOOL = _LOCKWAIT41
reset_state()

print("\n===== #42 mid-turn background-agent notification sweep =====")

_PROJ42 = tempfile.mkdtemp(prefix="tgstream-proj42-")
_real_projects42 = getattr(mod, "_context_projects_dir", None)
_real_notify42 = getattr(mod, "_transcript_notifications", None)
_real_time42 = mod.time.time
_calls42 = []

_NOTE42 = ("A task-notification fires each time this agent stops with no live "
           "background children of its own. The user can send it another message and "
           "resume it, so the same task-id may notify more than once.")

def _iso42(epoch):
    return mod.datetime.fromtimestamp(
        float(epoch), mod.ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def _notify42(label, tuid="", task_id="a0000000000000004", status="completed",
              sid="session-42"):

    lines = ["<task-notification>", "<task-id>%s</task-id>" % task_id]
    if tuid:
        lines.append("<tool-use-id>%s</tool-use-id>" % tuid)
    lines += [
        "<output-file>/private/tmp/claude-501/-Users-example-claude-channels/"
        "%s/tasks/%s.output</output-file>" % (sid, task_id),
        "<status>%s</status>" % status,
        '<summary>Agent "%s" finished</summary>' % label,
        "<note>%s</note>" % _NOTE42,
        "<result>The deliverable is complete and verified.</result>",
        "</task-notification>",
    ]
    return "\n".join(lines)

def _qop42(text, epoch, operation="enqueue", sid="session-42"):
    return {"type": "queue-operation", "operation": operation,
            "timestamp": _iso42(epoch), "sessionId": sid, "content": text}

def _att42(text, epoch, sid="session-42"):
    return {"type": "attachment", "timestamp": _iso42(epoch), "sessionId": sid,
            "isSidechain": False,
            "attachment": {"type": "queued_command", "prompt": text}}

def _user42(text, epoch, sid="session-42"):
    return {"type": "user", "timestamp": _iso42(epoch), "sessionId": sid,
            "isSidechain": False,
            "message": {"role": "user", "content": text}}

def _noise42(epoch, sid="session-42", tag="ordinary assistant traffic"):
    return {"type": "assistant", "timestamp": _iso42(epoch), "sessionId": sid,
            "isSidechain": False,
            "message": {"model": "claude-opus-5", "role": "assistant",
                        "content": [{"type": "text", "text": tag}],
                        "usage": {"input_tokens": 12, "output_tokens": 3}}}

def _pad42(sid, target, tag):
    lines, written, n = [], 0, 0
    while written < target:
        row = json.dumps(_noise42(1.0, sid, "%s %d %s" % (tag, n, "x" * 900)))
        lines.append(row)
        written += len(row) + 1
        n += 1
    return lines

def _write42(sid, records, pad_before=0, pad_after=0, truncate_first=False):
    path = os.path.join(_PROJ42, sid + ".jsonl")
    lines = []
    if truncate_first:
        lines.append('{"type": "assistant", "message": {"content": [{"type"')
    if pad_before:
        lines += _pad42(sid, pad_before, "padb")
    lines += [json.dumps(r) for r in records]
    if pad_after:
        lines += _pad42(sid, pad_after, "pada")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path

def _agent_in42(description, background=True):
    return {"description": description, "subagent_type": "general-purpose",
            "model": "fable", "run_in_background": background}

def _launch42(tg, sid, tuid, description, clock, task_id="a0000000000000004"):
    _, err_pre = _step26(tg, "pre", "Agent", _agent_in42(description), tuid=tuid,
                         session_id=sid)
    clock.advance(0.4)
    _, err_post = _step26(
        tg, "post", "Agent", _agent_in42(description), tuid=tuid,
        tresponse={"isAsync": True, "status": "async_launched", "agentId": task_id,
                   "description": description, "resolvedModel": "claude-fable-5"},
        session_id=sid)
    return err_pre or err_post

def _live_text42(state, mission="de-instrumentation"):
    value, err = _render_live26(
        SDIR, state.get("steps") or [], state.get("proc_start"), {},
        mission=mission, panel=state.get("panel"), plan=[],
        tools={"items": [], "count": 0})
    return _text26(value), err

_missing42 = [_n for _n in (
    "_iso_epoch", "_notification_record_text", "_transcript_notifications",
    "_bg_notify_transcript_path", "_apply_notification_close",
    "_sweep_background_notifications", "BG_NOTIFY_TAIL_BYTES",
    "BG_NOTIFY_MAX_RECORDS", "BG_NOTIFY_SWEEP_MIN", "BG_NOTIFY_CLOCK_SLACK",
    "BG_NOTIFY_DEEP_TAIL_BYTES")
    if not hasattr(mod, _n)]

try:
    mod._context_projects_dir = lambda project_dir=None: _PROJ42

    check("42a the build carries the v30.14 sweep surface",
          not _missing42, "missing=%r" % _missing42)

    _reset26()
    _enable_rich26()
    _base42 = float(int(_real_time42()))
    _clock42 = _Clock26(_base42)
    _sid42 = "session-42b"
    try:
        mod.time.time = _clock42.time
        _tg42 = RichRecTG()
        _, _pe42 = _prompt26(_tg42, "voice note", _sid42)
        _clock42.advance(2)
        _t0_42 = _clock42.time()
        _launch_err42 = _launch42(_tg42, _sid42, "toolu_fixture000000000000000003",
                                  "Design de-instrumentation plan", _clock42)
        _open42 = _panel_row31(load(), "toolu_fixture000000000000000003")
        _clock42.advance(2013.0)
        _done_at42 = _clock42.time()

        _write42(_sid42, [
            _noise42(_done_at42 - 60, _sid42),
            _qop42(_notify42("Design de-instrumentation plan",
                             "toolu_fixture000000000000000003", sid=_sid42),
                   _done_at42, "enqueue", _sid42),
            _att42(_notify42("Design de-instrumentation plan",
                             "toolu_fixture000000000000000003", sid=_sid42),
                   _done_at42, _sid42),
            _qop42(_notify42("Design de-instrumentation plan",
                             "toolu_fixture000000000000000003", sid=_sid42),
                   _done_at42 + 0.9, "remove", _sid42),
        ])
        _still42 = _panel_row31(load(), "toolu_fixture000000000000000003")
        _waiting_text42, _waiting_err42 = _live_text42(load())
        _clock42.advance(4.0)
        _close_at42 = _clock42.time()
        _, _read_err42 = _step26(_tg42, "pre", "Read",
                                 {"file_path": "/tmp/next-thing.md"}, tuid="READ-42",
                                 session_id=_sid42)
        _closed42 = _panel_row31(load(), "toolu_fixture000000000000000003")
        _after_text42, _after_err42 = _live_text42(load())
        _history42 = (load().get("agent_history") or {}).get("rows") or []
    finally:
        mod.time.time = _real_time42

    check("42b the row is open and the box says 'waiting' before any edge consumes it",
          not any((_pe42, _launch_err42, _waiting_err42))
          and _open42.get("background") is True and _open42.get("done") is False
          and _still42.get("done") is False
          and "Waiting on the agent" in _waiting_text42,
          _detail26({"open": _open42, "still": _still42,
                     "line": _waiting_text42[:200]}))
    check("42b ONE ordinary tool event closes the row on the mid-turn notification",
          not _read_err42 and _closed42.get("done") is True
          and _closed42.get("outcome") == "completed"
          and abs(float(_closed42.get("secs", -1))
                  - (_close_at42 - _t0_42)) < 0.001,
          _detail26({"row": _closed42, "want_secs": _close_at42 - _t0_42}))

    check("42b and the headline leaves the waiting state on that same edge",
          not _after_err42 and "Waiting on the agent" not in _after_text42
          and "✓ Design de-instrumentati" in _after_text42
          and "(Fable 5)" in _after_text42,
          _after_err42 or _detail26(_after_text42[:280]))
    check("42b the closed agent lands in the cumulative ledger exactly once",
          len([r for r in _history42
               if str(r.get("id") or "").endswith("toolu_fixture000000000000000003")
               and r.get("outcome") == "completed"]) == 1,
          _detail26(_history42))

    _reset26()
    _enable_rich26()
    _sid42c = "session-42c"
    _tg42c = RichRecTG()
    _, _pe42c = _prompt26(_tg42c, "run the fable agent", _sid42c)
    _launch_err42c = _launch42(_tg42c, _sid42c, "toolu_fixture000000000000000040",
                               "Design de-instrumentation plan", _Clock26(0.0),
                               task_id="a0000000000000002")

    _now42c = _real_time42()
    _aged42c = load()
    _aged42c["proc_start"] = _now42c - 610.0
    _aged42c["last_edit"] = 0
    _aged42c["last_activity"] = _now42c - 600.0
    for _row in (_aged42c.get("panel") or []):
        _row["t0"] = _now42c - 600.0
    mod.save_state(SDIR, _aged42c)
    _write42(_sid42c, [
        _qop42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000040",
                         task_id="a0000000000000002", sid=_sid42c),
               _now42c - 3.0, "enqueue", _sid42c),
        _att42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000040",
                         task_id="a0000000000000002", sid=_sid42c),
               _now42c - 3.0, _sid42c),
    ])
    _before_tick42c = _panel_row31(load(), "toolu_fixture000000000000000040")
    _tick42c = animator_tick23(_tg42c)
    _tick_state42c = load()
    _after_tick42c = _panel_row31(_tick_state42c, "toolu_fixture000000000000000040")
    check("42c one real animator tick closes it with no hook event at all",
          not any((_pe42c, _launch_err42c))
          and _before_tick42c.get("done") is False
          and _before_tick42c.get("background") is True
          and _after_tick42c.get("done") is True
          and _after_tick42c.get("outcome") == "completed"
          and 595.0 < float(_after_tick42c.get("secs") or 0) < 615.0,
          _detail26({"before": _before_tick42c, "after": _after_tick42c}))
    check("42c and closing the row is what finally releases the box to fold",
          _tick_state42c.get("folded") is True
          and _tick_state42c.get("typing_done") is True
          and "Waiting on the agent" not in (_tick_state42c.get("last_live") or ""),
          _detail26({"folded": _tick_state42c.get("folded"),
                     "typing_done": _tick_state42c.get("typing_done"),
                     "live": (_tick_state42c.get("last_live") or "")[:200]}))

    _reset26()
    _enable_rich26()
    _sid42d = "session-42d"
    _clock42d = _Clock26(float(int(_real_time42())))
    try:
        mod.time.time = _clock42d.time
        _tg42d = RichRecTG()
        _, _pe42d = _prompt26(_tg42d, "start the long worker", _sid42d)
        _clock42d.advance(2)
        _launch_err42d = _launch42(_tg42d, _sid42d, "toolu_fixture000000000000000038",
                                   "Rewrite the pricing engine", _clock42d)

        _write42(_sid42d, [_noise42(_clock42d.time() - 30, _sid42d) for _ in range(6)]
                 + [_qop42(_notify42("An entirely different job", "toolu_fixture000000000000000030",
                                     task_id="deadbeefcafe0001", sid=_sid42d),
                           _clock42d.time() - 5, "enqueue", _sid42d)])
        _states42d = []
        for _i42d in range(25):
            _clock42d.advance(11.0)
            _step26(_tg42d, "pre", "Bash", {"command": "echo %d" % _i42d},
                    tuid="LOOP-42D-%d" % _i42d, session_id=_sid42d)
            _step26(_tg42d, "post", "Bash", {"command": "echo %d" % _i42d},
                    tuid="LOOP-42D-%d" % _i42d, session_id=_sid42d)
            _states42d.append(_panel_row31(load(), "toolu_fixture000000000000000038").get("done"))
        _text42d, _err42d = _live_text42(load())
    finally:
        mod.time.time = _real_time42
    for _t42d in range(3):
        animator_tick23(_tg42d)
        _states42d.append(_panel_row31(load(), "toolu_fixture000000000000000038").get("done"))
    check("42d a slow-but-alive agent stays open across 50 hook events and 3 ticks",
          not any((_pe42d, _launch_err42d))
          and _states42d == [False] * len(_states42d)
          and _panel_row31(load(), "toolu_fixture000000000000000038").get("outcome") is None,
          _detail26({"done_flags": _states42d,
                     "row": _panel_row31(load(), "toolu_fixture000000000000000038")}))
    check("42d and it is still honestly reported as being waited on",
          not _err42d and "Waiting on the agent" in _text42d,
          _err42d or _detail26(_text42d[:200]))

    _reset26()
    _enable_rich26()
    _sid42e = "session-42e"
    _clock42e = _Clock26(float(int(_real_time42())))
    try:
        mod.time.time = _clock42e.time
        _tg42e = RichRecTG()
        _, _pe42e = _prompt26(_tg42e, "dispatch two", _sid42e)
        _clock42e.advance(2)
        _launch42(_tg42e, _sid42e, "toolu_fixture000000000000000025", "Audit the invoice flow", _clock42e)
        _clock42e.advance(3)
        _launch42(_tg42e, _sid42e, "toolu_fixture000000000000000023", "Summarise the meeting notes",
                  _clock42e, task_id="b2c3d4e5f6a70008")
        _clock42e.advance(400.0)
        _write42(_sid42e, [
            _qop42(_notify42("Summarise the meeting notes", "toolu_fixture000000000000000023",
                             task_id="b2c3d4e5f6a70008", sid=_sid42e),
                   _clock42e.time(), "enqueue", _sid42e),
            _att42(_notify42("Summarise the meeting notes", "toolu_fixture000000000000000023",
                             task_id="b2c3d4e5f6a70008", sid=_sid42e),
                   _clock42e.time(), _sid42e),
        ])
        _clock42e.advance(3)
        _step26(_tg42e, "pre", "Read", {"file_path": "/tmp/x.md"}, tuid="R-42E",
                session_id=_sid42e)
        _keep42e = _panel_row31(load(), "toolu_fixture000000000000000025")
        _go42e = _panel_row31(load(), "toolu_fixture000000000000000023")
    finally:
        mod.time.time = _real_time42
    check("42e a different agent's notification closes ONLY that agent's row",
          not _pe42e and _go42e.get("done") is True
          and _go42e.get("outcome") == "completed"
          and _keep42e.get("done") is False
          and _keep42e.get("outcome") is None
          and _keep42e.get("background") is True,
          _detail26({"closed": _go42e, "kept": _keep42e}))

    _reset26()
    _enable_rich26()
    _sid42f = "session-42f"
    _clock42f = _Clock26(float(int(_real_time42())))
    _label42f = "Design de-instrumentation plan"
    try:
        mod.time.time = _clock42f.time
        _tg42f = RichRecTG()
        _, _pe42f = _prompt26(_tg42f, "first pass", _sid42f)
        _clock42f.advance(2)
        _launch42(_tg42f, _sid42f, "toolu_fixture000000000000000021", _label42f, _clock42f)
        _clock42f.advance(300.0)
        _old_notify_at42f = _clock42f.time()

        _, _first_close_err42f = _prompt26(
            _tg42f, _notify42(_label42f, "", task_id="1111111111111111",
                              sid=_sid42f), _sid42f)
        _first42f = _panel_row31(load(), "toolu_fixture000000000000000021")
        _clock42f.advance(30.0)

        _, _boundary_err42f = _prompt26(_tg42f, "now do it again", _sid42f)
        _clock42f.advance(5.0)
        _second_t0_42f = _clock42f.time()
        _launch42(_tg42f, _sid42f, "toolu_fixture000000000000000037", _label42f, _clock42f,
                  task_id="2222222222222222")
        _panel_after_boundary42f = [r.get("tuid") for r in (load().get("panel") or [])]

        _write42(_sid42f, [
            _qop42(_notify42(_label42f, "", task_id="1111111111111111", sid=_sid42f),
                   _old_notify_at42f, "enqueue", _sid42f),
            _att42(_notify42(_label42f, "", task_id="1111111111111111", sid=_sid42f),
                   _old_notify_at42f, _sid42f),
        ])
        _clock42f.advance(120.0)
        for _i42f in range(4):
            _step26(_tg42f, "pre", "Read", {"file_path": "/tmp/%d.md" % _i42f},
                    tuid="R42F-%d" % _i42f, session_id=_sid42f)
            _clock42f.advance(3)
        _second_stale42f = _panel_row31(load(), "toolu_fixture000000000000000037")

        _write42(_sid42f, [
            _qop42(_notify42(_label42f, "", task_id="1111111111111111", sid=_sid42f),
                   _old_notify_at42f, "enqueue", _sid42f),
            _qop42(_notify42(_label42f, "", task_id="3333333333333333", sid=_sid42f),
                   _clock42f.time(), "enqueue", _sid42f),
        ])
        _clock42f.advance(3)
        _step26(_tg42f, "pre", "Read", {"file_path": "/tmp/final.md"}, tuid="R42F-END",
                session_id=_sid42f)
        _second_fresh42f = _panel_row31(load(), "toolu_fixture000000000000000037")
    finally:
        mod.time.time = _real_time42
    check("42f the prompt edge still closes an id-less notification by label (v30.11)",
          not any((_pe42f, _first_close_err42f, _boundary_err42f))
          and _first42f.get("done") is True
          and _first42f.get("outcome") == "completed",
          _detail26(_first42f))
    check("42f a STALE same-label envelope cannot close a later row of that name",
          _panel_after_boundary42f == ["toolu_fixture000000000000000037"]
          and _second_stale42f.get("done") is False
          and _second_stale42f.get("outcome") is None,
          _detail26({"panel": _panel_after_boundary42f, "row": _second_stale42f}))
    check("42f but a FRESH same-label envelope does close it (the guard is live)",
          _second_fresh42f.get("done") is True
          and _second_fresh42f.get("outcome") == "completed",
          _detail26(_second_fresh42f))

    _reset26()
    _enable_rich26()
    _sid42g = "session-42g"
    _clock42g = _Clock26(float(int(_real_time42())))
    try:
        mod.time.time = _clock42g.time
        _tg42g = RichRecTG()
        _, _pe42g = _prompt26(_tg42g, "kick off the verifier", _sid42g)
        _clock42g.advance(2)
        _t0_42g = _clock42g.time()
        _launch42(_tg42g, _sid42g, "toolu_fixture000000000000000002",
                  "Verify skillOverrides semantics", _clock42g,
                  task_id="a0000000000000006")
        _mid_before42g = str(load().get("msgid"))
        _sends_before42g = len(_rich_ops26(_tg42g, "send_rich"))
        _clock42g.advance(122.6)
        _close_at42g = _clock42g.time()

        _write42(_sid42g, [_noise42(_close_at42g - 10, _sid42g)])
        _ops42g, _notify_err42g = _prompt26(
            _tg42g, _notify42("Verify skillOverrides semantics",
                              "toolu_fixture000000000000000002",
                              task_id="a0000000000000006", sid=_sid42g), _sid42g)
        _closed42g = _panel_row31(load(), "toolu_fixture000000000000000002")
        _mid_after42g = str(load().get("msgid"))
        _sends_after42g = len(_rich_ops26(_tg42g, "send_rich"))
    finally:
        mod.time.time = _real_time42
    check("42g the UserPromptSubmit notification path is byte-for-byte what it was",
          not any((_pe42g, _notify_err42g))
          and _closed42g.get("done") is True
          and _closed42g.get("outcome") == "completed"
          and abs(float(_closed42g.get("secs", -1)) - (_close_at42g - _t0_42g)) < 0.001
          and _mid_after42g == _mid_before42g
          and _sends_after42g == _sends_before42g,
          _detail26({"row": _closed42g, "ops": _ops42g,
                     "mid": (_mid_before42g, _mid_after42g)}))

    _reset26()
    _enable_rich26()
    _sid42o = "session-42o"
    _clock42o = _Clock26(float(int(_real_time42())))
    try:
        mod.time.time = _clock42o.time
        _tg42o = RichRecTG()
        _, _pe42o = _prompt26(_tg42o, "turn A: send the agent", _sid42o)
        _clock42o.advance(2)
        _t0_42o = _clock42o.time()
        _launch_err42o = _launch42(_tg42o, _sid42o, "toolu_fixture000000000000000018",
                                   "Design de-instrumentation plan", _clock42o)
        _clock42o.advance(60.0)
        _reply_ops42o = mod.record_reply(SDIR, _tg42o, final=False)
        mod._run_deferred(SDIR, _tg42o, _reply_ops42o)
        _stop_ops42o = mod.record_reply(SDIR, _tg42o, final=True)
        mod._run_deferred(SDIR, _tg42o, _stop_ops42o)
        _after_stop42o = load()
        _mid_a42o = str(_after_stop42o.get("msgid"))
        _clock42o.advance(4000.0)

        _, _turnb_err42o = _prompt26(_tg42o, "turn B: an unrelated question", _sid42o)
        _turn_b42o = load()
        _mid_b42o = str(_turn_b42o.get("msgid"))
        _carried42o = _panel_row31(_turn_b42o, "toolu_fixture000000000000000018")
        _clock42o.advance(30.0)
        _write42(_sid42o, [
            _qop42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000018",
                             sid=_sid42o), _clock42o.time(), "enqueue", _sid42o),
            _att42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000018",
                             sid=_sid42o), _clock42o.time(), _sid42o),
        ])
        _clock42o.advance(3.0)
        _close_at42o = _clock42o.time()
        _, _readb_err42o = _step26(_tg42o, "pre", "Read", {"file_path": "/tmp/b.md"},
                                   tuid="READ-42O", session_id=_sid42o)
        _closed42o = _panel_row31(load(), "toolu_fixture000000000000000018")
    finally:
        mod.time.time = _real_time42
    check("42o the boundary folds the old box and turn B carries the open row forward",
          not any((_pe42o, _launch_err42o, _turnb_err42o))
          and _stop_ops42o == [("typing", None, None, False)]
          and _mid_b42o != _mid_a42o
          and _carried42o.get("background") is True
          and _carried42o.get("done") is False
          and abs(float(_carried42o.get("t0") or 0) - _t0_42o) < 0.001,
          _detail26({"mid": (_mid_a42o, _mid_b42o), "carried": _carried42o}))
    check("42o a LATER turn closes the row it never dispatched, full duration intact",
          not _readb_err42o and _closed42o.get("done") is True
          and _closed42o.get("outcome") == "completed"
          and abs(float(_closed42o.get("secs", -1))
                  - (_close_at42o - _t0_42o)) < 0.001,
          _detail26({"row": _closed42o, "want_secs": _close_at42o - _t0_42o}))

    _reset26()
    _enable_rich26()
    _sid42p = "session-42p"
    _tg42p = RichRecTG()
    _, _pe42p = _prompt26(_tg42p, "answer, then wait on the agent", _sid42p)
    _launch_err42p = _launch42(_tg42p, _sid42p, "toolu_fixture000000000000000024",
                               "Design de-instrumentation plan", _Clock26(0.0))
    _reply_ops42p = mod.record_reply(SDIR, _tg42p, final=False)
    mod._run_deferred(SDIR, _tg42p, _reply_ops42p)
    _stop_ops42p = mod.record_reply(SDIR, _tg42p, final=True)
    mod._run_deferred(SDIR, _tg42p, _stop_ops42p)
    _held42p = load()
    _now42p = _real_time42()
    _idle42p = load()
    _idle42p["proc_start"] = _now42p - 5400.0
    _idle42p["last_edit"] = 0
    _idle42p["last_activity"] = _now42p - 5390.0
    _idle42p["last_reply"] = _now42p - 5380.0
    for _row in (_idle42p.get("panel") or []):
        _row["t0"] = _now42p - 5395.0
    mod.save_state(SDIR, _idle42p)
    _idle_text42p, _idle_err42p = _live_text42(load())
    _write42(_sid42p, [
        _qop42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000024",
                         sid=_sid42p), _now42p - 3.0, "enqueue", _sid42p)])
    animator_tick23(_tg42p)
    _released42p = load()
    check("42p an open row is what holds an already-answered, idle box alive",
          not any((_pe42p, _launch_err42p))
          and _stop_ops42p == [("typing", None, None, False)]
          and _held42p.get("folded") is False
          and _held42p.get("typing_done") is False
          and not _idle_err42p
          and "Waiting on the agent" in _idle_text42p,
          _detail26({"stop": _stop_ops42p, "held": _held42p.get("typing_done"),
                     "line": _idle_text42p[:200]}))
    check("42p closing it releases the box: typing stops and it folds to the recap",
          _panel_row31(_released42p, "toolu_fixture000000000000000024").get("done") is True
          and _released42p.get("folded") is True
          and _released42p.get("typing_done") is True
          and "Waiting on the agent" not in (_released42p.get("last_live") or "")
          and "tap" in (_released42p.get("last_live") or ""),
          _detail26({"row": _panel_row31(_released42p, "toolu_fixture000000000000000024"),
                     "folded": _released42p.get("folded"),
                     "live": (_released42p.get("last_live") or "")[:220]}))

    _sid42q = "session-42q"
    _now42q = float(int(_real_time42()))
    _deep42q = getattr(mod, "BG_NOTIFY_DEEP_TAIL_BYTES", 0)
    _write42(_sid42q, [
        _qop42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000019",
                         sid=_sid42q), _now42q - 3300.0, "enqueue", _sid42q)],
             pad_after=600 * 1024)
    _size42q = os.path.getsize(os.path.join(_PROJ42, _sid42q + ".jsonl"))

    def _state42q():
        return {"panel": [{"tuid": "toolu_fixture000000000000000019", "background": True, "done": False,
                           "lab": "Design de-instrumentation plan",
                           "t0": _now42q - 5000.0}],
                "steps": [], "rich_session_id": _sid42q}

    _shallow42q = _state42q()
    _deep_state42q = _state42q()
    _shallow_swept42q = mod._sweep_background_notifications(SDIR, _shallow42q, _now42q)
    _deep_swept42q = -1
    if _deep42q:
        _deep_swept42q = mod._sweep_background_notifications(
            SDIR, _deep_state42q, _now42q, tail_bytes=_deep42q)
    check("42q the steady-state window cannot reach an hour-old envelope, the deep one can",
          _size42q > mod.BG_NOTIFY_TAIL_BYTES
          and _shallow_swept42q == 0
          and _shallow42q["panel"][0].get("done") is False
          and _deep_swept42q == 1
          and _deep_state42q["panel"][0].get("done") is True
          and _deep_state42q["panel"][0].get("outcome") == "completed",
          _detail26({"size": _size42q, "window": mod.BG_NOTIFY_TAIL_BYTES,
                     "deep": _deep42q, "shallow": _shallow_swept42q,
                     "deep_swept": _deep_swept42q}))

    _reset26()
    _enable_rich26()
    _sid42q2 = "session-42q2"
    _tg42q = RichRecTG()
    _, _pe42q = _prompt26(_tg42q, "the turn that dispatched it", _sid42q2)
    _launch_err42q = _launch42(_tg42q, _sid42q2, "toolu_fixture000000000000000039",
                               "Design de-instrumentation plan", _Clock26(0.0))
    _now42q2 = _real_time42()
    _stuck42q = load()
    _stuck42q["proc_start"] = _now42q2 - 5000.0
    _stuck42q["last_edit"] = 0
    _stuck42q["last_activity"] = _now42q2 - 4990.0
    for _row in (_stuck42q.get("panel") or []):
        _row["t0"] = _now42q2 - 4995.0
    mod.save_state(SDIR, _stuck42q)
    _write42(_sid42q2, [
        _qop42(_notify42("Design de-instrumentation plan", "toolu_fixture000000000000000039",
                         sid=_sid42q2), _now42q2 - 3300.0, "enqueue", _sid42q2)],
             pad_after=600 * 1024)
    animator_tick23(_tg42q)
    _unstuck42q = _panel_row31(load(), "toolu_fixture000000000000000039")
    check("42q a fresh animator's first tick closes a row that was ALREADY stuck",
          not any((_pe42q, _launch_err42q))
          and _unstuck42q.get("done") is True
          and _unstuck42q.get("outcome") == "completed"
          and 4990.0 < float(_unstuck42q.get("secs") or 0) < 5010.0,
          _detail26(_unstuck42q))

    class _TwoTickClock42:

        def __init__(self, now, off_path):
            self.now = float(now)
            self.off_path = off_path
            self.sleeps = []

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if len(self.sleeps) >= 2:
                with open(self.off_path, "w") as handle:
                    handle.write("")

    _budgets42q = []
    _orig_tn42q = getattr(mod, "_transcript_notifications", None)

    def _record_budget42q(path, tail_bytes=None, limit=None):
        _budgets42q.append(tail_bytes)
        return _orig_tn42q(path, tail_bytes=tail_bytes, limit=limit)

    _reset26()
    _enable_rich26()
    _sid42q3 = "session-42q3"
    _tg42q3 = RichRecTG()
    _prompt26(_tg42q3, "a worker that never finishes", _sid42q3)
    _launch42(_tg42q3, _sid42q3, "toolu_fixture000000000000000011", "Still running", _Clock26(0.0))
    _write42(_sid42q3, [_noise42(_real_time42(), _sid42q3)])
    _off42q = os.path.join(STATE_DIR, "stream.off")
    _real_mod_time42q = mod.time
    _real_anim42q = (mod.ANIM_INTERVAL, mod.ANIM_MAX_RUN, mod.ANIM_MAX_IDLE,
                     mod.MIN_EDIT_INTERVAL)
    try:
        os.remove(_off42q)
    except OSError:
        pass
    try:
        if _orig_tn42q is not None:
            mod._transcript_notifications = _record_budget42q
        _clk42q = _TwoTickClock42(max(_real_time42(),
                                      float(load().get("proc_start") or 0) + 1), _off42q)
        mod.time = _clk42q
        mod.ANIM_INTERVAL, mod.ANIM_MAX_RUN = 0.05, 100000
        mod.ANIM_MAX_IDLE, mod.MIN_EDIT_INTERVAL = 100000, 100000
        mod.run_animator(SDIR, _tg42q3)
    finally:
        mod.time = _real_mod_time42q
        (mod.ANIM_INTERVAL, mod.ANIM_MAX_RUN, mod.ANIM_MAX_IDLE,
         mod.MIN_EDIT_INTERVAL) = _real_anim42q
        if _orig_tn42q is not None:
            mod._transcript_notifications = _orig_tn42q
        try:
            os.remove(_off42q)
        except OSError:
            pass
    check("42q the deep look is taken once per animator process, then never again",
          len(_budgets42q) >= 2 and _budgets42q[0] == _deep42q
          and all(b is None for b in _budgets42q[1:])
          and _panel_row31(load(), "toolu_fixture000000000000000011").get("done") is False,
          _detail26({"budgets": _budgets42q, "deep": _deep42q}))

    _cap_row42h = {"tuid": "toolu_fixture000000000000000015", "lab": "Abandoned worker",
                   "background": True, "done": False,
                   "t0": _real_time42() - mod.CODEX_BG_MAX_AGE - 1,
                   "history_session_id": "session-42h"}
    _cap_state42h = {"panel": [_cap_row42h], "steps": [],
                     "rich_session_id": "session-42h"}
    _write42("session-42h", [
        _qop42(_notify42("Abandoned worker", "toolu_fixture000000000000000015", sid="session-42h"),
               _real_time42(), "enqueue", "session-42h")])
    _cap_swept42h = -1
    if not _missing42:
        _cap_swept42h = mod._sweep_background_notifications(SDIR, _cap_state42h)
    _cap_expired42h = mod._expire_background_agents([_cap_row42h])
    check("42h a row past the two-hour cap is the cap's, not the sweep's, and still expires",
          _cap_swept42h == 0 and _cap_expired42h == 1
          and _cap_row42h.get("done") is True
          and _cap_row42h.get("outcome") == "stale",
          _detail26({"swept": _cap_swept42h, "row": _cap_row42h}))

    if not _missing42:

        mod._transcript_notifications = _real_notify42
        _orig_notify42 = mod._transcript_notifications

        def _counting_notify42(*args, **kwargs):
            _calls42.append(1)
            return _orig_notify42(*args, **kwargs)

        mod._transcript_notifications = _counting_notify42
        _reset26()
        _enable_rich26()
        _sid42i = "session-42i"
        _clock42i = _Clock26(float(int(_real_time42())))
        _write42(_sid42i, [_noise42(_clock42i.time(), _sid42i)])
        try:
            mod.time.time = _clock42i.time
            _tg42i = RichRecTG()
            _prompt26(_tg42i, "plain turn, no agents", _sid42i)
            del _calls42[:]
            for _i42i in range(30):
                _clock42i.advance(2.0)
                _step26(_tg42i, "pre", "Read", {"file_path": "/tmp/a%d" % _i42i},
                        tuid="COST-42I-%d" % _i42i, session_id=_sid42i)
                _step26(_tg42i, "post", "Read", {"file_path": "/tmp/a%d" % _i42i},
                        tuid="COST-42I-%d" % _i42i, session_id=_sid42i)
            _zero_row_calls42 = len(_calls42)
            _clock42i.advance(2.0)
            _launch42(_tg42i, _sid42i, "toolu_fixture000000000000000017", "A long background job",
                      _clock42i)
            del _calls42[:]
            for _i42i in range(10):
                _clock42i.advance(2.0)
                _step26(_tg42i, "pre", "Read", {"file_path": "/tmp/b%d" % _i42i},
                        tuid="COST-42I-B%d" % _i42i, session_id=_sid42i)
            _open_row_calls42 = len(_calls42)
        finally:
            mod.time.time = _real_time42
            mod._transcript_notifications = _real_notify42
        check("42i with NO open background row the sweep reads nothing at all",
              _zero_row_calls42 == 0, "reads=%d over 60 hook events" % _zero_row_calls42)
        check("42i with one open row it is exactly one bounded read per event",
              _open_row_calls42 == 10,
              "reads=%d over 10 hook events" % _open_row_calls42)

        _sid42j = "session-42j"
        _pad42j = mod.BG_NOTIFY_TAIL_BYTES + 64 * 1024
        _write42(_sid42j, [_noise42(_real_time42(), _sid42j)],
                 pad_before=_pad42j, truncate_first=True)
        _path42j = os.path.join(_PROJ42, _sid42j + ".jsonl")
        _size42j = os.path.getsize(_path42j)
        with open(_path42j, "r", encoding="utf-8") as _h42j:
            _head42j = _h42j.read()
        with open(_path42j, "w", encoding="utf-8") as _h42j:
            _h42j.write(json.dumps(
                _qop42(_notify42("Way out of reach", "toolu_fixture000000000000000020", sid=_sid42j),
                       _real_time42(), "enqueue", _sid42j)) + "\n" + _head42j)
        _far42j = mod._transcript_notifications(_path42j)
        _near42j = mod._transcript_notifications(
            _path42j, tail_bytes=os.path.getsize(_path42j))
        check("42j a notification beyond the tail budget is simply not seen",
              _size42j > mod.BG_NOTIFY_TAIL_BYTES and _far42j == []
              and len(_near42j) == 1
              and "toolu_fixture000000000000000020" in _near42j[0][1],
              _detail26({"size": _size42j, "budget": mod.BG_NOTIFY_TAIL_BYTES,
                         "far": len(_far42j), "near": len(_near42j)}))

        _now42k = float(int(_real_time42()))
        _round_trip42k = mod._iso_epoch(_iso42(_now42k))
        _checks42k = [
            mod._iso_epoch("2026-08-06T20:08:32.583Z") is not None,
            mod._iso_epoch("2026-08-06T20:08:32Z") is not None,
            mod._iso_epoch("2026-08-06T20:08:32.583+00:00") is not None,
            mod._iso_epoch("not a timestamp") is None,
            mod._iso_epoch(None) is None,
            mod._iso_epoch("") is None,
            abs((_round_trip42k or 0) - _now42k) < 0.002,
            mod._iso_epoch("2026-08-06T20:08:32.583Z")
            == mod._iso_epoch("2026-08-06T20:08:32.583+00:00"),
        ]
        check("42k the timestamp reader is exact, tolerant and never guesses",
              all(_checks42k), _detail26(_checks42k))

        _text42k = _notify42("Reader probe", "toolu_fixture000000000000000034")
        _shapes42k = [
            mod._notification_record_text(_qop42(_text42k, _now42k)) == _text42k,
            mod._notification_record_text(_att42(_text42k, _now42k)) == _text42k,
            mod._notification_record_text(_user42(_text42k, _now42k)) == _text42k,
            mod._notification_record_text(_noise42(_now42k)) == "",
            mod._notification_record_text(
                {"type": "attachment", "attachment": {"type": "hook_success",
                                                      "stdout": "{}"}}) == "",
            mod._notification_record_text(
                _user42("I was reading about <task-notification> blocks", _now42k)) == "",
            mod._notification_record_text({"type": "queue-operation"}) == "",
            mod._notification_record_text(None) == "",
            mod._notification_record_text({"type": "user", "message": {
                "content": [{"type": "text", "text": _text42k}]}}) == "",
        ]
        check("42k only a body that STARTS with the tag, in a known record, is ours",
              all(_shapes42k), _detail26(_shapes42k))

        _gate_row42k = {"tuid": "toolu_fixture000000000000000022", "lab": "Gate probe",
                        "background": True, "done": False, "t0": _now42k - 100}
        _gate_state42k = {"panel": [_gate_row42k], "steps": []}
        _m42k, _c42k, _, _ = mod._apply_notification_close(
            _gate_state42k, _notify42("Gate probe", ""), now=_now42k, label_gate=False)
        _id_row42k = {"tuid": "toolu_fixture000000000000000022", "lab": "Gate probe",
                      "background": True, "done": False, "t0": _now42k - 100}
        _id_state42k = {"panel": [_id_row42k], "steps": []}
        _m2_42k, _c2_42k, _, _ = mod._apply_notification_close(
            _id_state42k, _notify42("Gate probe", "toolu_fixture000000000000000022"), now=_now42k,
            label_gate=False)
        check("42k an unreadable notification time refuses the label, never the id",
              _c42k is False and _gate_row42k.get("done") is False
              and _c2_42k is True and _id_row42k.get("done") is True
              and _id_row42k.get("outcome") == "completed",
              _detail26({"label": (_m42k, _c42k, _gate_row42k),
                         "id": (_m2_42k, _c2_42k, _id_row42k)}))

        _sid42l = "session-42l"
        _now42l = float(int(_real_time42()))
        _write42(_sid42l, [
            _qop42(_notify42("Blast radius", "toolu_fixture000000000000000014", sid=_sid42l),
                   _now42l - 5, "enqueue", _sid42l)])
        _state42l = {"panel": [{"tuid": "toolu_fixture000000000000000014", "lab": "Blast radius",
                                "background": True, "done": False,
                                "t0": _now42l - 300}],
                     "steps": [], "rich_session_id": _sid42l,
                     "msgid": "42l", "last_activity": 111.0, "folded": False}
        _reset26()
        _state_file42l = os.path.join(SDIR, "state.json")
        _before_exists42l = os.path.exists(_state_file42l)
        _tg42l = RichRecTG()
        _swept42l = mod._sweep_background_notifications(SDIR, _state42l, _now42l)
        check("42l the sweep closes the row and touches nothing else",
              _swept42l == 1
              and _state42l["panel"][0].get("done") is True
              and _state42l.get("last_activity") == 111.0
              and _state42l.get("msgid") == "42l"
              and os.path.exists(_state_file42l) == _before_exists42l
              and _tg42l.calls == [],
              _detail26({"swept": _swept42l, "state": _state42l,
                         "tg": _tg42l.calls}))

        _sid42m = "session-42m"
        _now42m = float(int(_real_time42()))
        _write42(_sid42m, [
            _qop42(_notify42("Long resumable worker", "toolu_RESUME_CALL_42M",
                             task_id="cccccccccccccccc", sid=_sid42m),
                   _now42m - 4, "enqueue", _sid42m)])
        _row42m = {"tuid": "toolu_fixture000000000000000026", "lab": "Long resumable worker",
                   "background": True, "done": False, "t0": _now42m - 900}
        _state42m = {"panel": [_row42m], "steps": [], "rich_session_id": _sid42m}
        _swept42m = mod._sweep_background_notifications(SDIR, _state42m, _now42m)
        check("42m a resumed agent whose id names no row still closes by label",
              _swept42m == 1 and _row42m.get("done") is True
              and _row42m.get("outcome") == "completed"
              and abs(float(_row42m.get("secs", -1)) - 900.0) < 0.001,
              _detail26(_row42m))

        _no_sid42n = {"panel": [{"tuid": "toolu_fixture000000000000000029", "lab": "Orphan",
                                 "background": True, "done": False,
                                 "t0": _now42m - 60}], "steps": []}
        _swept42n = mod._sweep_background_notifications(SDIR, _no_sid42n, _now42m)
        _row_sid42n = {"panel": [{"tuid": "toolu_fixture000000000000000029", "lab": "Orphan",
                                  "background": True, "done": False,
                                  "t0": _now42m - 60,
                                  "history_session_id": _sid42m}], "steps": []}
        _swept_row42n = mod._sweep_background_notifications(SDIR, _row_sid42n, _now42m)
        check("42n with no resolvable session the sweep declines instead of guessing",
              _swept42n == 0 and _no_sid42n["panel"][0].get("done") is False
              and _swept_row42n == 0
              and mod._bg_notify_transcript_path(_row_sid42n) is not None,
              _detail26({"none": _swept42n, "row_fallback": _swept_row42n,
                         "path": mod._bg_notify_transcript_path(_row_sid42n)}))
except Exception as _exc42:
    check("42 SECTION ABORTED - this build does not carry the v30.14 sweep",
          False, "%s: %s" % (type(_exc42).__name__, str(_exc42)[:400]))
finally:
    mod.time.time = _real_time42
    if _real_projects42 is not None:
        mod._context_projects_dir = _real_projects42
    if _real_notify42 is not None:
        mod._transcript_notifications = _real_notify42
    try:
        shutil.rmtree(_PROJ42)
    except OSError:
        pass

_reset26()

print("\n===== #43 background agents stay visible on the live box =====")

_v3015_sid = "session-v3015"
_v3015_now = float(int(time.time()))
_v3015_open = {"tuid": "toolu_V3015_OPEN", "lab": "File NiceJob outcome records",
               "typ": "opus-executor", "mdl": "opus", "t0": _v3015_now - 700.0,
               "done": False, "secs": 0, "background": True,
               "history_session_id": _v3015_sid}

def _v3015_ledger(entries):
    rows = [{"id": "%s:agent:%s" % (_v3015_sid, tuid), "label": label,
             "family": "Opus", "model": "opus", "t0": _v3015_now - off,
             "secs": secs, "outcome": outcome, "session_id": _v3015_sid,
             "day": mod._et_day(_v3015_now), "closed_at": _v3015_now - off + secs}
            for label, off, secs, outcome, tuid in entries]
    return mod._normalize_agent_history(
        {"day": mod._et_day(_v3015_now), "total": len(rows),
         "session_totals": {_v3015_sid: len(rows)}, "rows": rows},
        now=_v3015_now)

_V3015_HIST = None

def _v3015_frame(panel, steps, history, fallback=False):
    return _text26(_render_live26(
        SDIR, steps, _v3015_now - 580.0, {}, "Filing the NiceJob outcomes",
        panel=panel, plan=[{"id": "p1", "subject": "Land the records",
                            "status": "completed"}],
        tools={"items": [{"cap": "reading outcomes.json", "elapsed": 500.0}],
               "count": 61},
        fallback=fallback,
        state={"ctx_pct": 36, "ctx_model": "claude-fable-5",
               "rich_session_id": _v3015_sid, "agent_history": history}))

def _v3015_frame_other(panel, steps, history):
    return _text26(_render_live26(
        SDIR, steps, _v3015_now - 580.0, {}, "Filing the NiceJob outcomes",
        panel=panel, plan=[], tools={"items": [], "count": 0},
        state={"ctx_pct": 36, "ctx_model": "claude-fable-5",
               "rich_session_id": "v3015-somebody-else",
               "agent_history": history}))

_real_time3015 = mod.time.time
try:
    mod.time.time = lambda: _v3015_now
    _V3015_HIST = _v3015_ledger([
        ("Fetch Sampleapp call transcript", 4800, 995.2, "completed", "H1"),
        ("Merge Sampleapp legal agreements", 3000, 665.9, "completed", "H2")])
    _v3015_busy_step = [{"cap": "reading outcomes.json", "k": "read",
                         "done": True, "tuid": "toolu_fixture000000000000000035", "secs": 3.0},
                        {"cap": "editing outcomes.json", "k": "edit",
                         "done": False, "tuid": "toolu_fixture000000000000000036",
                         "t0": _v3015_now - 4.0}]
    _v3015_idle_step = [_v3015_busy_step[0]]

    _v3015_working = _v3015_frame([dict(_v3015_open)], _v3015_busy_step,
                                  mod._empty_agent_history(_v3015_now))
    _v3015_waiting = _v3015_frame([dict(_v3015_open)], _v3015_idle_step,
                                  mod._empty_agent_history(_v3015_now))

    _v3015_cedar = _v3015_frame([], _v3015_busy_step, _V3015_HIST)
    _v3015_cedar_plain = _v3015_frame([], _v3015_busy_step, _V3015_HIST,
                                       fallback=True)

    _v3015_both = _v3015_frame([dict(_v3015_open)], _v3015_busy_step, _V3015_HIST)

    _v3015_killed = _v3015_frame([], _v3015_idle_step, _v3015_ledger(
        [("Force stopped worker", 900, 300.0, "killed", "K1")]))

    _v3015_flood = _v3015_frame(
        [dict(_v3015_open)], _v3015_idle_step,
        _v3015_ledger([("row %02d" % i, 3000 - i, 60.0, "completed",
                        "M%02d" % i) for i in range(30)]))

    _v3015_other = _v3015_frame_other([], _v3015_idle_step, _V3015_HIST)
finally:
    mod.time.time = _real_time3015

def _v3015_frame_capped():
    return _v3015_flood

_v3015_open_rows = [l for l in _v3010_table_block(_v3015_working)
                    if l.startswith("| ◉ ")]

check("43a the agent table is NOT gated on the activity state - working or waiting",
      _v3010_table_block(_v3015_working) == _v3010_table_block(_v3015_waiting)
      and _v3015_working.splitlines()[3].startswith("⏳ editing")
      and _v3015_waiting.splitlines()[3] == "⏳ Waiting on the agent"
      and len(_v3015_open_rows) == 1
      and "| 11:40 |" in _v3015_open_rows[0],
      _detail26({"working": _v3015_working.splitlines()[3],
                 "waiting": _v3015_waiting.splitlines()[3],
                 "table": _v3010_table_block(_v3015_working)}))

check("43a and that identity is a pure function of `lanes`, not of the headline",
      mod._rich_lane_rows(_v3015_busy_step, [dict(_v3015_open)], _v3015_now)
      == mod._rich_lane_rows(_v3015_idle_step, [dict(_v3015_open)], _v3015_now),
      _detail26(mod._rich_lane_rows(_v3015_busy_step, [dict(_v3015_open)],
                                    _v3015_now)))

check("43b THE COMPLETED-HISTORY SHAPE: a session whose agents finished still shows them",
      "| 🤖 Agent | Time |" in _v3015_cedar
      and "✓ Fetch Sampleapp call trans… (Opus)" in _v3015_cedar
      and "✓ Merge Sampleapp legal agre… (Opus)" in _v3015_cedar
      and "| 16:35 |" in _v3015_cedar and "| 11:05 |" in _v3015_cedar
      and _v3015_cedar.count("\n---") == 2,
      _detail26(_v3010_table_block(_v3015_cedar)))

check("43b the plain fallback mirrors it in the same position",
      "🤖 Agent · Time" in _v3015_cedar_plain
      and "- ✓ Fetch Sampleapp call trans… (Opus) · 16:35" in _v3015_cedar_plain
      and _v3015_cedar_plain.index("🤖 Agent · Time")
      < _v3015_cedar_plain.index("📋 Plan"),
      _detail26(_v3015_cedar_plain))

check("43c a running row always sorts above the carried ones and is never displaced",
      _v3010_table_block(_v3015_both)[2].startswith("| ◉ File NiceJob")
      and all(row.startswith("| ✓ ")
              for row in _v3010_table_block(_v3015_both)[3:])
      and len(_v3010_table_block(_v3015_both)) == 5,
      _detail26(_v3010_table_block(_v3015_both)))

check("43c the carried tail is capped, never unbounded",
      len(_v3010_table_block(_v3015_frame_capped())) == 2 + _v3015_cap()
      and _v3015_cap() >= 1,
      _detail26(_v3010_table_block(_v3015_frame_capped())))

check("43d STATE CARRY IS UNCHANGED - this is a render change and nothing else",
      mod._open_background_agent_rows([dict(_v3015_open)], _v3015_now)
      and mod._open_background_agent_rows(
          [dict(_v3015_open, done=True, secs=700.0)], _v3015_now) == []
      and "agent_history" in mod.fresh_state(
          _v3015_now, agent_history=_V3015_HIST),
      _detail26(mod._open_background_agent_rows([dict(_v3015_open)], _v3015_now)))

def _v3015_render_is_pure():
    st = {"panel": [dict(_v3015_open)], "steps": list(_v3015_busy_step),
          "agent_history": _V3015_HIST, "ctx_pct": 36,
          "ctx_model": "claude-fable-5", "rich_session_id": _v3015_sid}
    before = json.dumps(st, sort_keys=True, default=str)
    _text26(_render_live26(
        SDIR, st["steps"], _v3015_now - 580.0, {}, "Filing the NiceJob outcomes",
        panel=st["panel"], plan=[], tools={"items": [], "count": 0}, state=st))
    return before, json.dumps(st, sort_keys=True, default=str)

_v3015_pure_before, _v3015_pure_after = _v3015_render_is_pure()
check("43d the live renderer still adds no state key and rewrites no ledger",
      _v3015_pure_before == _v3015_pure_after,
      _detail26({"mutated": _v3015_pure_before != _v3015_pure_after}))

check("43e a killed or stale agent keeps its honest glyph in the carried rows",
      "✕ Force stopped worker (Opus)" in _v3015_killed
      and "✓ Force stopped worker" not in _v3015_killed,
      _detail26(_v3010_table_block(_v3015_killed)))

check("43e another session's agents are NEVER drawn on this session's box",
      "| 🤖 Agent | Time |" not in _v3015_other
      and "Fetch Sampleapp" not in _v3015_other
      and _v3015_other.count("\n---") == 1,
      _detail26(_v3015_other))

def _v3015_lanes_with_cap(cap):
    saved = mod.RICH_LIVE_LANE_ROWS
    try:
        mod.RICH_LIVE_LANE_ROWS = cap
        return mod._rich_live_lanes([], [dict(_v3015_open)], _V3015_HIST,
                                    _v3015_sid, _v3015_now)
    finally:
        mod.RICH_LIVE_LANE_ROWS = saved

try:
    check("43c `lane_floor` marks exactly the untrimmable current-turn rows",
          mod._rich_live_lanes([], [dict(_v3015_open)], _V3015_HIST,
                               _v3015_sid, _v3015_now)[1] == 1
          and mod._rich_live_lanes([], [], _V3015_HIST,
                                   _v3015_sid, _v3015_now)[1] == 0
          and mod._rich_live_lanes([], [], None, _v3015_sid, _v3015_now)
          == ([], 0),
          _detail26(mod._rich_live_lanes([], [dict(_v3015_open)], _V3015_HIST,
                                         _v3015_sid, _v3015_now)))

    check("43e a cross-DAY ledger row is refused at the source",
          mod._rich_history_lane_rows(
              [], [], {"day": "1999-01-01", "total": 1, "session_totals": {},
                       "rows": [{"id": "%s:agent:OLD" % _v3015_sid,
                                 "label": "Yesterday", "family": "Opus",
                                 "model": "opus", "t0": _v3015_now - 90000,
                                 "secs": 60.0, "outcome": "completed",
                                 "session_id": _v3015_sid, "day": "1999-01-01",
                                 "closed_at": _v3015_now - 89000}]},
              _v3015_sid, _v3015_now, 4) == []
          and mod._rich_history_lane_rows(
              [], [], _V3015_HIST, _v3015_sid, _v3015_now, 0) == [],
          "cross-day rows and a zero room budget both yield nothing")

    _v3015_off = _v3015_lanes_with_cap(0)
    check("43f RICH_LIVE_LANE_ROWS = 0 restores v30.14's lane list exactly",
          _v3015_off[0] == mod._rich_lane_rows([], [dict(_v3015_open)],
                                               _v3015_now)
          and _v3015_off[1] == 1
          and _v3015_cap() == 4,
          _detail26({"off": _v3015_off, "restored": _v3015_cap()}))
except Exception as _exc43:
    check("43 SECTION ABORTED - this build does not carry the v30.15 lane merge",
          False, "%s: %s" % (type(_exc43).__name__, str(_exc43)[:400]))

_reset26()

print("\n===== #44 (v30.16) typing survives real background work =====")

_reset26()
_v16_prev_run = mod.subprocess.run
try:

    _V16_ARGV_O = ("/private/tmp/claude-501/-Users-example-claude-channels/"
                   "00000000-0000-4000-8000-000000000005/scratchpad/"
                   "sol-reaudit-report.md")
    _V16_CMD = {
        "command": (
            'cd /Users/example/code/sample-project && '
            'S=/private/tmp/claude-501/-Users-example-claude-channels/'
            '00000000-0000-4000-8000-000000000005/scratchpad && '
            '/opt/homebrew/bin/codex exec resume -m gpt-5.6-sol '
            '-c model_reasoning_effort=max -c sandbox_mode=read-only '
            '--skip-git-repo-check -o "$S/sol-reaudit-report.md" '
            '"00000000-0000-4000-8000-000000000001" - '
            '< "$S/sol-reaudit-brief.txt" > "$S/sol-reaudit-run.log" 2>&1'),
        "description": "Resume Sol session for re-audit in background",
        "run_in_background": True,
    }
    _V16_PROC = (
        "/opt/homebrew/bin/codex exec resume -m gpt-5.6-sol "
        "-c model_reasoning_effort=max -c sandbox_mode=read-only "
        "--skip-git-repo-check -o %s 00000000-0000-4000-8000-000000000001 -"
        % _V16_ARGV_O)
    _V16_TUID = "toolu_fixture000000000000000001"

    def _v16_ere(pattern, subject):
        out = _REAL_SUBPROCESS_RUN(
            ["/usr/bin/grep", "-E", pattern], input=subject.encode(),
            stdout=mod.subprocess.DEVNULL, stderr=mod.subprocess.DEVNULL)
        return out.returncode == 0

    _V16_PROCS = [_V16_PROC]

    def _v16_run(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and list(argv[:2]) == ["/usr/bin/pgrep", "-f"]:
            pattern = str(argv[-1])
            return _PgrepTestResult(
                any(_v16_ere(pattern, line) for line in _V16_PROCS))
        return _REAL_SUBPROCESS_RUN(argv, *a, **kw)

    _v16_key = mod._codex_process_key(_V16_CMD)

    mod.subprocess.run = _v16_run
    _v16_now = 5000.0
    _v16_step = {"codex_bg": True, "done": False, "t0": _v16_now - 10.0,
                 "cap": "Codex Sol · sol-reaudit", "codex_key": _v16_key,
                 "tuid": _V16_TUID}
    _v16_steps = [_v16_step]
    _v16_closed = mod._close_gone_codex_bg(_v16_steps, now=_v16_now)
    check("44d a LIVE Codex lane is no longer closed as gone",
          _v16_closed == 0 and _v16_step.get("done") is not True
          and mod._has_open_codex_bg(_v16_steps, _v16_now) is True,
          _detail26({"closed": _v16_closed, "done": _v16_step.get("done")}))

    _V16_PROCS[:] = []
    _v16_step2 = {"codex_bg": True, "done": False, "t0": _v16_now - 10.0,
                  "cap": "Codex Sol · sol-reaudit", "codex_key": _v16_key,
                  "tuid": _V16_TUID}
    _v16_steps2 = [_v16_step2]
    _v16_closed2 = mod._close_gone_codex_bg(_v16_steps2, now=_v16_now)
    check("44e a genuinely vanished Codex process still closes its lane",
          _v16_closed2 == 1 and _v16_step2.get("done") is True
          and _v16_step2.get("codex_terminal_status") == "stopped"
          and mod._has_open_codex_bg(_v16_steps2, _v16_now) is False,
          _detail26({"closed": _v16_closed2,
                     "status": _v16_step2.get("codex_terminal_status")}))
    _V16_PROCS[:] = [_V16_PROC]

    _v16_pat = mod._codex_key_pattern(_v16_key)
    _v16_old = mod._pgrep_literal(_v16_key)
    check("44a the fleet's real `-o \"$S/...\"` dispatch is findable by pgrep again",
          _v16_key == "$S/sol-reaudit-report.md"
          and bool(_v16_pat)
          and _v16_ere(_v16_pat, _V16_PROC)
          and not _v16_ere(_v16_old, _V16_PROC),
          _detail26({"key": _v16_key, "new_matches": True,
                     "v30_9_pattern_matches": _v16_ere(_v16_old, _V16_PROC)}))

    _v16_lit = "/private/tmp/scratch/sample-solaudit-report.md"
    _v16_other = ("/opt/homebrew/bin/codex exec -m gpt-5.6-sol "
                  "-o /private/tmp/scratch/other-report.md prompt")
    check("44b a fully literal key keeps v30.9's pattern byte for byte",
          mod._codex_key_pattern(_v16_lit) == mod._pgrep_literal(_v16_lit)
          and _v16_ere(mod._codex_key_pattern(_v16_lit),
                       "codex exec -o %s x" % _v16_lit)
          and not _v16_ere(mod._codex_key_pattern(_v16_lit), _v16_other),
          _detail26(mod._codex_key_pattern(_v16_lit)))

    check("44b the salvaged tail matches only at a path boundary",
          _v16_ere(_v16_pat, "codex exec -o sol-reaudit-report.md x")
          and _v16_ere(_v16_pat, "codex exec -o /a/b/sol-reaudit-report.md x")

          and _v16_ere(_v16_pat,
                       "codex exec -o /a/my dir/sol-reaudit-report.md x")
          and not _v16_ere(_v16_pat,
                           "codex exec -o /a/b/other-sol-reaudit-report.md x")
          and not _v16_ere(_v16_pat, "codex exec -o /a/sol-reaudit-report.md.bak x")
          and not _v16_ere(_v16_pat, "codex exec -m sol /a/sol-reaudit-report.md x"),
          "boundary anchored")

    check("44c a key with no literal tail falls back to the keyless probe",
          mod._codex_key_pattern("$S/$NAME") == ""
          and mod._codex_key_pattern("~/$X") == ""
          and mod._codex_key_pattern("") == ""
          and mod._codex_key_pattern(None) == "",
          _detail26([mod._codex_key_pattern("$S/$NAME"),
                     mod._codex_key_pattern("~/$X")]))

    class _V16Clock(object):
        def __init__(self, now, off_path=None, stop_at=None, raise_at=None):
            self.now = float(now)
            self.off_path = off_path
            self.stop_at = stop_at
            self.raise_at = raise_at
            self.sleeps = []

        def __getattr__(self, name):

            return getattr(_v16_real_time, name)

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if self.raise_at is not None and self.now >= self.raise_at:
                raise RuntimeError("v30.16 fixture: animator dies mid-window")
            if self.stop_at is not None and self.now >= self.stop_at\
                    and self.off_path:
                with open(self.off_path, "w") as fh:
                    fh.write("")

    _v16_real_time = mod.time
    _v16_real_anim = mod.ANIM_INTERVAL
    _v16_real_run = mod.ANIM_MAX_RUN
    _v16_real_idle = mod.ANIM_MAX_IDLE
    _v16_real_edit = mod.MIN_EDIT_INTERVAL
    _v16_real_age = mod.CODEX_BG_MAX_AGE
    _v16_real_respawn = mod.ANIM_RESPAWN_MIN_LIFE
    _v16_off = os.path.join(STATE_DIR, "stream.off")

    def _v16_restore():
        mod.time = _v16_real_time
        mod.ANIM_INTERVAL = _v16_real_anim
        mod.ANIM_MAX_RUN = _v16_real_run
        mod.ANIM_MAX_IDLE = _v16_real_idle
        mod.MIN_EDIT_INTERVAL = _v16_real_edit
        mod.CODEX_BG_MAX_AGE = _v16_real_age
        mod.ANIM_RESPAWN_MIN_LIFE = _v16_real_respawn
        try:
            os.remove(_v16_off)
        except OSError:
            pass

    try:
        reset_state()
        _v16_clock = _V16Clock(9000.0, off_path=_v16_off, stop_at=9000.0 + 600.0)
        mod.time = _v16_clock
        mod.ANIM_INTERVAL = 3
        mod.ANIM_MAX_RUN = 100000
        mod.ANIM_MAX_IDLE = 100000
        mod.MIN_EDIT_INTERVAL = 0
        tg16 = RecTG()
        mod._run_deferred(SDIR, tg16, mod.on_prompt(SDIR, tg16, prompt_text="sol re-audit"))
        run_step(tg16, "pre", "Bash", _V16_CMD, tuid=_V16_TUID)
        run_step(tg16, "post", "Bash", _V16_CMD, tuid=_V16_TUID)
        _v16_stop = run_reply(tg16, final=True)
        _v16_after_stop = load()
        check("44f a live Codex lane holds the Stop edge open and keeps typing on",
              _v16_after_stop.get("typing_done") is False
              and _v16_after_stop.get("folded") is not True
              and [d[0] for d in _v16_stop] == ["typing"],
              _detail26({"typing_done": _v16_after_stop.get("typing_done"),
                         "folded": _v16_after_stop.get("folded"),
                         "deferred": [d[0] for d in _v16_stop]}))

        tg16.calls.clear()
        mod.run_animator(SDIR, tg16)
        _v16_typing = [i for i, c in enumerate(tg16.calls) if c[0] == "typing"]
        _v16_ticks = len(_v16_clock.sleeps)

        _v16_gap = mod.TYPING_MIN_INTERVAL

        def _v16_every(floor):
            n = 1
            while n * 3.0 < floor:
                n += 1
            return n

        _v16_expect = (100 // _v16_every(mod.TYPING_MIN_INTERVAL)
                       + 100 // _v16_every(mod.CADENCE_MEDIUM_TYPING))
        check("44f typing is sustained for the whole event-silence window",
              len(_v16_typing) >= _v16_expect and _v16_ticks >= 190
              and bool(_v16_typing)
              and (len(tg16.calls) - _v16_typing[-1]) <= 12
              and load().get("folded") is not True
              and load().get("typing_done") is False,
              _detail26({"typing": len(_v16_typing), "expect": _v16_expect,
                         "ticks": _v16_ticks, "calls": len(tg16.calls),
                         "last_typing_at": _v16_typing[-1:],
                         "folded": load().get("folded")}))

        reset_state()
        _v16_clock2 = _V16Clock(20000.0, off_path=_v16_off, stop_at=20000.0 + 30.0)
        mod.time = _v16_clock2
        mod.CODEX_BG_MAX_AGE = 40
        tg16b = RecTG()
        mod._run_deferred(SDIR, tg16b, mod.on_prompt(SDIR, tg16b, prompt_text="capped"))
        run_step(tg16b, "pre", "Bash", _V16_CMD, tuid=_V16_TUID)
        run_step(tg16b, "post", "Bash", _V16_CMD, tuid=_V16_TUID)
        _v16_clock2.now += 60.0
        _v16_capped = run_reply(tg16b, final=True)
        _v16_cap_state = load()
        check("44g the two-hour lane cap still stops typing - never an infinite bubble",
              mod._has_open_codex_bg(_v16_cap_state.get("steps"),
                                     _v16_clock2.now) is False
              and _v16_cap_state.get("typing_done") is True
              and [d[0] for d in _v16_capped] != ["typing"]
              and _V16_PROCS == [_V16_PROC],
              _detail26({"typing_done": _v16_cap_state.get("typing_done"),
                         "deferred": [d[0] for d in _v16_capped]}))
        mod.CODEX_BG_MAX_AGE = _v16_real_age

        check("44h _has_open_tool_step counts only bounded, in-flight foreground work",
              mod._has_open_tool_step([{"done": False, "t0": 100.0}], 200.0) is True
              and mod._has_open_tool_step([{"done": True, "t0": 100.0}], 200.0) is False
              and mod._has_open_tool_step(
                  [{"done": False, "t0": 100.0, "codex_bg": True}], 200.0) is False
              and mod._has_open_tool_step([{"done": False}], 200.0) is False
              and mod._has_open_tool_step(
                  [{"done": False, "t0": 100.0}], 100.0 + mod.CODEX_BG_MAX_AGE
                  + 1.0) is False
              and mod._has_open_tool_step([], 200.0) is False
              and mod._has_open_tool_step(None, 200.0) is False,
              "truth table")

        reset_state()

        _v16_fg = {"command": ("codex exec -m gpt-5.6-sol --sandbox read-only "
                               "-o /private/tmp/tgstream-fg/fg-report.md 'audit it'"),
                   "description": "Foreground Codex run"}
        _v16_clock3 = _V16Clock(30000.0, off_path=_v16_off, stop_at=30000.0 + 4.0)
        mod.time = _v16_clock3
        mod.ANIM_INTERVAL = 3
        mod.ANIM_MAX_RUN = 100000
        mod.ANIM_MAX_IDLE = 60
        mod.MIN_EDIT_INTERVAL = 0
        tg16c = RecTG()
        mod._run_deferred(SDIR, tg16c, mod.on_prompt(SDIR, tg16c, prompt_text="long run"))
        run_step(tg16c, "pre", "Bash", _v16_fg,
                 tuid="toolu_fixture000000000000000027")
        _v16_fg_steps = load().get("steps") or []
        _v16_clock3.now += 400.0
        _v16_clock3.stop_at = _v16_clock3.now + 4.0
        try:
            os.remove(_v16_off)
        except OSError:
            pass
        mod.run_animator(SDIR, tg16c)
        _v16_build_state = load()
        check("44i a long FOREGROUND tool call is not folded by the 30-minute backstop",
              _v16_build_state.get("folded") is not True
              and _v16_build_state.get("typing_done") is not True
              and len(tg16c.ops("typing")) >= 1
              and mod._has_open_tool_step(_v16_fg_steps, 30000.0 + 400.0) is True,
              _detail26({"folded": _v16_build_state.get("folded"),
                         "typing": len(tg16c.ops("typing")),
                         "steps": _v16_fg_steps}))

        mod.CODEX_BG_MAX_AGE = 30
        _v16_clock3.now += 100.0
        _v16_clock3.stop_at = _v16_clock3.now + 60.0
        try:
            os.remove(_v16_off)
        except OSError:
            pass
        tg16c.calls.clear()
        mod.run_animator(SDIR, tg16c)
        _v16_capped_build = load()
        check("44i the in-flight exemption is bounded - a capped step still folds",
              _v16_capped_build.get("folded") is True,
              _detail26({"folded": _v16_capped_build.get("folded")}))
        mod.CODEX_BG_MAX_AGE = _v16_real_age
        _reset26()

        _v16_spawns = []
        _v16_folds = []
        _v16_saves = []
        _v16_real_spawn = mod.spawn_animator
        _v16_real_fold = mod._fold_to_done
        _v16_real_save = mod.save_state

        def _v16_spawn(sdir=None):
            _v16_spawns.append(sdir)

        def _v16_fold(sdir, tg):
            _v16_folds.append(sdir)
            return _v16_real_fold(sdir, tg)

        def _v16_case(raise_after, respawn_min, with_lane):
            reset_state()
            del _v16_spawns[:]
            del _v16_folds[:]
            clock = _V16Clock(40000.0, off_path=_v16_off)
            mod.time = clock
            mod.ANIM_INTERVAL = 3
            mod.ANIM_MAX_RUN = 100000
            mod.ANIM_MAX_IDLE = 100000
            mod.MIN_EDIT_INTERVAL = 0
            mod.ANIM_RESPAWN_MIN_LIFE = respawn_min
            tgx = RecTG()
            mod._run_deferred(SDIR, tgx, mod.on_prompt(SDIR, tgx, prompt_text="crash"))
            if with_lane:
                run_step(tgx, "pre", "Bash", _V16_CMD, tuid=_V16_TUID)
                run_step(tgx, "post", "Bash", _V16_CMD, tuid=_V16_TUID)
            else:
                run_step(tgx, "pre", "Read", {"file_path": "/tmp/x"}, tuid="toolu_fixture000000000000000031")
                run_step(tgx, "post", "Read", {"file_path": "/tmp/x"}, tuid="toolu_fixture000000000000000031")
            mod.spawn_animator = _v16_spawn
            mod._fold_to_done = _v16_fold
            clock.raise_at = clock.now + raise_after
            try:
                mod.run_animator(SDIR, tgx)
                died = False
            except RuntimeError:
                died = True
            finally:
                mod.spawn_animator = _v16_real_spawn
                mod._fold_to_done = _v16_real_fold
            return died, list(_v16_spawns), list(_v16_folds), load()

        _diedA, _spawnA, _foldA, _stA = _v16_case(60.0, 30.0, True)
        check("44j a long-lived animator that DIES with work outstanding hands off",
              _diedA is True and len(_spawnA) == 1 and _foldA == []
              and _stA.get("folded") is not True
              and _stA.get("typing_done") is False,
              _detail26({"spawns": len(_spawnA), "folds": len(_foldA),
                         "folded": _stA.get("folded")}))

        _diedB, _spawnB, _foldB, _stB = _v16_case(6.0, 30.0, True)
        check("44j an animator that dies inside its first half-minute never respawns",
              _diedB is True and _spawnB == [] and len(_foldB) == 1,
              _detail26({"spawns": len(_spawnB), "folds": len(_foldB)}))

        _diedC, _spawnC, _foldC, _stC = _v16_case(60.0, 30.0, False)
        check("44j a death with NO outstanding work still folds, exactly as v30.15 did",
              _diedC is True and _spawnC == [] and len(_foldC) == 1,
              _detail26({"spawns": len(_spawnC), "folds": len(_foldC)}))

        def _v16_save(sdir, st):
            _v16_saves.append(sorted(st.keys()) if isinstance(st, dict) else None)
            return _v16_real_save(sdir, st)

        mod.save_state = _v16_save
        try:
            _v16_state_before = sorted(load().keys())
            mod.spawn_animator = _v16_spawn
            del _v16_saves[:]
            _diedD, _spawnD, _foldD, _stD = _v16_case(60.0, 30.0, True)
        finally:
            mod.save_state = _v16_real_save
            mod.spawn_animator = _v16_real_spawn
        _v16_pure = {"codex_key": _v16_key, "codex_bg": True, "done": False,
                     "t0": 1.0}
        _v16_pure_copy = dict(_v16_pure)
        mod._codex_key_pattern(_v16_key)
        mod._shell_literal_token(_v16_key)
        mod._has_open_tool_step([_v16_pure], 2.0)
        check("44k the v30.16 additions add no state key and mutate no row",
              _v16_pure == _v16_pure_copy
              and all("respawn" not in k and "tool_open" not in k
                      and "exited" not in k and "handoff" not in k
                      for k in _stD.keys()),
              _detail26(sorted(set(_stD.keys()) - set(_v16_state_before))))
    finally:
        _v16_restore()
except Exception as _exc44:
    check("44 SECTION ABORTED - this build does not carry the v30.16 typing fix",
          False, "%s: %s" % (type(_exc44).__name__, str(_exc44)[:400]))

mod.subprocess.run = _v16_prev_run
_reset26()

print("\n===== #45 background Bash lanes keep the box up (v30.17) =====")

_V17_TUID = "toolu_fixture000000000000000004"
_V17_TASK = "b196nve01"

_V17_INPUT = {
    "command": ('S=/private/tmp/claude-501/-Users-example-claude-channels-4/'
                '00000000-0000-4000-8000-000000000006/scratchpad; n=0; '
                'while [ $n -lt 120 ]; do if [ -s "$S/codex-browser-sync-report4.md" ]; '
                'then echo "SYNC_REPORT_READY"; exit 0; fi; sleep 10; n=$((n+1)); done; '
                'echo "WATCH_TIMEOUT_20MIN"'),
    "description": "Watch for sync worker report (20 min cap)",
    "run_in_background": True,
}

_V17_RESP_DICT = {
    "stdout": "", "stderr": "", "interrupted": False, "isImage": False,
    "noOutputExpected": False, "backgroundTaskId": _V17_TASK,
}
_V17_TASKDIR = tempfile.mkdtemp(prefix="tgstream-tasks-")
_V17_OUT = os.path.join(_V17_TASKDIR, "tasks", _V17_TASK + ".output")
os.makedirs(os.path.dirname(_V17_OUT), exist_ok=True)

_V17_RESP_TEXT = (
    "Command running in background with ID: %s. Output is being written to: %s. "
    "You will be notified when it completes. To check interim output, use Read on "
    "that file path." % (_V17_TASK, _V17_OUT))

_V17_NOTIFY = (
    "<task-notification>\n"
    "<task-id>%s</task-id>\n"
    "<tool-use-id>%s</tool-use-id>\n"
    "<output-file>%s</output-file>\n"
    "<status>completed</status>\n"
    "<summary>Background command \"Watch for sync worker report (20 min cap)\" "
    "completed (exit code 0)</summary>\n"
    "</task-notification>" % (_V17_TASK, _V17_TUID, _V17_OUT))
_V17_CODEX_INPUT = {
    "command": ('S=/private/tmp/tgstream-v17; /opt/homebrew/bin/codex exec '
                '-m gpt-5.6-sol --sandbox danger-full-access -o "$S/sol-v17.md" "go"'),
    "description": "Codex worker",
    "run_in_background": True,
}
_V17_AGENT_INPUT = {
    "description": "Streamer-doctor: snapshot+diagnose+stage",
    "subagent_type": "opus-executor", "model": "opus", "run_in_background": True,
}
_V17_AGENT_RESP = {"isAsync": True, "status": "async_launched",
                   "agentId": "a0000000000000007"}

def _v17_write(path, body):
    with open(path, "w") as fh:
        fh.write(body)

def _v17_step(state, key="bash_bg"):
    for s in (state.get("steps") or []):
        if isinstance(s, dict) and s.get(key):
            return s
    return {}

def _step17(tg, mode, tool, tinput=None, tuid=None, tresponse=None):

    deferred = mod.handle_step(SDIR, tg, mode, tool, tinput or {}, tuid,
                               None, None, tresponse=tresponse)
    mod._run_deferred(SDIR, tg, deferred)
    return deferred

def _v17_launch(tg, tinput=None, tuid=None, tresponse=None):
    _step17(tg, "pre", "Bash", tinput or _V17_INPUT, tuid=tuid or _V17_TUID)
    return _step17(tg, "post", "Bash", tinput or _V17_INPUT, tuid=tuid or _V17_TUID,
                   tresponse=_V17_RESP_DICT if tresponse is None else tresponse)

_v17_cases = [

    ("the real watcher dispatch", _V17_INPUT, True),
    ("a foreground command", dict(_V17_INPUT, run_in_background=False), False),
    ("no background key at all",
     {"command": "ls", "description": "list"}, False),
    ("a truthy-but-not-True flag", dict(_V17_INPUT, run_in_background=1), False),
    ("a string flag", dict(_V17_INPUT, run_in_background="true"), False),
    ("a background CODEX dispatch stays Codex's", _V17_CODEX_INPUT, False),
    ("an empty command", {"command": "   ", "run_in_background": True}, False),
    ("a non-string command", {"command": None, "run_in_background": True}, False),
]
check("45a the recognizer fires on exactly the real background-Bash shape",
      all(mod._is_background_bash(ti) is want for _lbl, ti, want in _v17_cases)
      and mod._is_background_bash(None) is False
      and mod._is_background_bash("not a dict") is False,
      _detail26([(lbl, mod._is_background_bash(ti), want)
                 for lbl, ti, want in _v17_cases]))

_v17_cap = mod.caption_of("Bash", _V17_INPUT, None)
_v17_cap_nodesc = mod.caption_of(
    "Bash", {"command": "sleep 600; echo done\n", "run_in_background": True}, None)
_v17_cap_heredoc = mod.caption_of(
    "Bash", {"command": "python3 <<'EOF'\nimport re\nEOF", "run_in_background": True},
    None)

check("45a the lane caption names WHAT is working, with no emoji of its own",
      _v17_cap == "background task · Watch for sync worker report (20 min cap)"
      and _v17_cap_nodesc == "background task · sleep 600; echo done"
      and _v17_cap_heredoc == "background task · python3"
      and "⏳" not in _v17_cap and len(_v17_cap) <= 80

      and mod.caption_of("Bash", _V17_CODEX_INPUT, None).startswith("GPT-5.6 Sol")

      and mod.caption_of("Bash", {"command": "ls", "description": "list"}, None)
      == "list",
      _detail26([_v17_cap, _v17_cap_nodesc, _v17_cap_heredoc]))

check("45a a very long description is bounded exactly like the Codex caption",
      len(mod.caption_of(
          "Bash", dict(_V17_INPUT, description="x" * 400), None)) <= 80,
      _detail26(len(mod.caption_of(
          "Bash", dict(_V17_INPUT, description="x" * 400), None))))

_v17_handles = [
    ("the real structured response", _V17_RESP_DICT, (_V17_TASK, "")),
    ("the real text response", _V17_RESP_TEXT, (_V17_TASK, _V17_OUT)),
    ("a nested / renamed id",
     {"meta": {"background_task_id": "bzz1"}}, ("bzz1", "")),
    ("path only, id inferred from it",
     "wrote /a/b/tasks/bqq9.output", ("bqq9", "/a/b/tasks/bqq9.output")),
    ("an ordinary synchronous Bash result",
     {"stdout": "hi\n", "stderr": "", "interrupted": False}, ("", "")),
    ("an error string", "Error: command not found", ("", "")),
    ("None", None, ("", "")),
    ("a list", [1, 2], ("", "")),
]
check("45b the launch handle is read from every real response shape",
      all(mod._bash_task_handle(resp) == want
          for _lbl, resp, want in _v17_handles),
      _detail26([(lbl, mod._bash_task_handle(resp), want)
                 for lbl, resp, want in _v17_handles]))

_v17_promote = {"bash_bg_requested": True, "t0": 100.0, "done": False}
_v17_ok = mod._bash_bg_accept_launch(_v17_promote, _V17_RESP_TEXT)
_v17_refuse = {"bash_bg_requested": True, "t0": 100.0, "done": False}
_v17_no = mod._bash_bg_accept_launch(
    _v17_refuse, {"stdout": "done\n", "stderr": "", "interrupted": False})
_v17_notreq = {"t0": 100.0, "done": False}
check("45b promotion needs a real task id, and refusing leaves today's behavior",
      _v17_ok is True and _v17_promote.get("bash_bg") is True
      and _v17_promote.get("bash_task_id") == _V17_TASK
      and _v17_promote.get("bash_task_out") == _V17_OUT
      and "bash_bg_requested" not in _v17_promote
      and _v17_no is False and "bash_bg" not in _v17_refuse
      and "bash_bg_requested" not in _v17_refuse

      and mod._bash_bg_accept_launch(_v17_notreq, _V17_RESP_TEXT) is False
      and "bash_bg" not in _v17_notreq
      and mod._bash_bg_accept_launch(None, _V17_RESP_TEXT) is False,
      _detail26([_v17_promote, _v17_refuse]))

_v17_probe = os.path.join(_V17_TASKDIR, "tasks", "probe.output")
_v17_probe_cases = [
    ("a finished command", "WATCH_TIMEOUT_20MIN\n\n[exited with code 0]\n",
     "completed"),
    ("a non-zero exit", "boom\n\n[exited with code 2]\n", "failed"),
    ("a killed command", "\n[killed]\n", "killed"),
    ("still running, output so far", "REPORT_READY\n", ""),
    ("created but empty", "", ""),
    ("the words mid-stream cannot fake a death",
     "saw [killed] in the log\nstill going\n", ""),
    ("a trailing blank line does not hide the marker",
     "x\n[exited with code 0]\n\n\n", "completed"),
]
_v17_probe_got = []
for _lbl, _body, _want in _v17_probe_cases:
    _v17_write(_v17_probe, _body)
    _v17_probe_got.append(
        (_lbl, mod._bash_task_status({"bash_task_out": _v17_probe}), _want))
check("45c the liveness probe reads only the last line, and reads it right",
      all(got == want for _lbl, got, want in _v17_probe_got),
      _detail26(_v17_probe_got))

check("45c an unreadable or absent path is None, never a guess",
      mod._bash_task_status({}) is None
      and mod._bash_task_status({"bash_task_out": ""}) is None
      and mod._bash_task_status(
          {"bash_task_out": os.path.join(_V17_TASKDIR, "nope.output")}) is None
      and mod._bash_task_status({"bash_task_out": "relative/path.output"}) is None
      and mod._bash_task_status({"bash_task_out": "/a\x00b"}) is None
      and mod._bash_task_status({"bash_task_out": "/" + "x" * 4000}) is None
      and mod._bash_task_status(None) is None,
      "truth table")

check("45c a huge output file still costs exactly one bounded tail read",
      (lambda p: (_v17_write(p, "y" * 400000 + "\n[exited with code 0]\n"),
                  mod._bash_task_status({"bash_task_out": p}))[1])(_v17_probe)
      == "completed"
      and os.path.getsize(_v17_probe) > mod.BASH_TASK_TAIL_BYTES,
      _detail26(os.path.getsize(_v17_probe)))

check("45c the lane predicates are the Codex ones, on the Bash key",
      mod._has_open_bash_bg([{"bash_bg": True, "done": False, "t0": 100.0}],
                            200.0) is True
      and mod._has_open_bash_bg([{"bash_bg": True, "done": True, "t0": 100.0}],
                                200.0) is False
      and mod._has_open_bash_bg([{"done": False, "t0": 100.0}], 200.0) is False
      and mod._has_open_bash_bg(
          [{"bash_bg": True, "done": False, "t0": 100.0}],
          100.0 + mod.CODEX_BG_MAX_AGE) is False
      and mod._has_open_bash_bg([], 200.0) is False
      and mod._has_open_bash_bg(None, 200.0) is False

      and mod._has_open_tool_step(
          [{"bash_bg": True, "done": False, "t0": 100.0}], 200.0) is False,
      "truth table")

class _V17Clock(object):

    def __init__(self, now, off_path=None, stop_at=None, raise_at=None):
        self.now = float(now)
        self.off_path = off_path
        self.stop_at = stop_at
        self.raise_at = raise_at
        self.sleeps = []

    def __getattr__(self, name):
        return getattr(_v17_real_time, name)

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.raise_at is not None and self.now >= self.raise_at:
            raise RuntimeError("v30.17 fixture: animator dies mid-window")
        if self.stop_at is not None and self.now >= self.stop_at and self.off_path:
            with open(self.off_path, "w") as fh:
                fh.write("")

_v17_real_time = mod.time
_v17_real_anim = mod.ANIM_INTERVAL
_v17_real_run = mod.ANIM_MAX_RUN
_v17_real_idle = mod.ANIM_MAX_IDLE
_v17_real_edit = mod.MIN_EDIT_INTERVAL
_v17_real_age = mod.CODEX_BG_MAX_AGE
_v17_real_blind = mod.CODEX_KEYLESS_HOLD_MAX
_v17_real_respawn = mod.ANIM_RESPAWN_MIN_LIFE
_v17_off = os.path.join(STATE_DIR, "stream.off")

def _v17_restore():
    mod.time = _v17_real_time
    mod.ANIM_INTERVAL = _v17_real_anim
    mod.ANIM_MAX_RUN = _v17_real_run
    mod.ANIM_MAX_IDLE = _v17_real_idle
    mod.MIN_EDIT_INTERVAL = _v17_real_edit
    mod.CODEX_BG_MAX_AGE = _v17_real_age
    mod.CODEX_KEYLESS_HOLD_MAX = _v17_real_blind
    mod.ANIM_RESPAWN_MIN_LIFE = _v17_real_respawn
    try:
        os.remove(_v17_off)
    except OSError:
        pass

try:

    _reset26()
    _v17_write(_V17_OUT, "")
    _v17_clock = _V17Clock(90000.0, off_path=_v17_off, stop_at=90000.0 + 600.0)
    mod.time = _v17_clock
    mod.ANIM_INTERVAL = 3
    mod.ANIM_MAX_RUN = 100000
    mod.ANIM_MAX_IDLE = 100000
    mod.MIN_EDIT_INTERVAL = 0
    tg17 = RecTG()
    mod._run_deferred(SDIR, tg17,
                      mod.on_prompt(SDIR, tg17, prompt_text="watch the worker"))
    _v17_launch(tg17, tresponse=_V17_RESP_TEXT)
    _v17_after_launch = load()
    check("45d the launch PostToolUse leaves the background lane OPEN",
          _v17_step(_v17_after_launch).get("done") is False
          and _v17_step(_v17_after_launch).get("bash_task_id") == _V17_TASK
          and _v17_step(_v17_after_launch).get("bash_task_out") == _V17_OUT
          and mod._has_open_bash_bg(_v17_after_launch.get("steps"),
                                    _v17_clock.now) is True,
          _detail26(_v17_step(_v17_after_launch)))

    _v17_stop = run_reply(tg17, final=True)
    _v17_stopped = load()
    check("45d a live background task holds the Stop edge open and keeps typing on",
          _v17_stopped.get("typing_done") is False
          and _v17_stopped.get("folded") is not True
          and [d[0] for d in _v17_stop] == ["typing"],
          _detail26({"typing_done": _v17_stopped.get("typing_done"),
                     "folded": _v17_stopped.get("folded"),
                     "deferred": [d[0] for d in _v17_stop]}))

    tg17.calls.clear()
    mod.run_animator(SDIR, tg17)
    _v17_typing = [i for i, c in enumerate(tg17.calls) if c[0] == "typing"]

    def _v17_every(floor):
        n = 1
        while n * 3.0 < floor:
            n += 1
        return n

    _v17_expect = (100 // _v17_every(mod.TYPING_MIN_INTERVAL)
                   + 100 // _v17_every(mod.CADENCE_MEDIUM_TYPING))
    check("45d typing is sustained for the whole event-silence window",
          len(_v17_typing) >= _v17_expect and len(_v17_clock.sleeps) >= 190
          and bool(_v17_typing)
          and (len(tg17.calls) - _v17_typing[-1]) <= 12
          and load().get("folded") is not True
          and load().get("typing_done") is False,
          _detail26({"typing": len(_v17_typing), "expect": _v17_expect,
                     "ticks": len(_v17_clock.sleeps), "calls": len(tg17.calls),
                     "last_typing_at": _v17_typing[-1:],
                     "folded": load().get("folded")}))

    _reset26()
    _v17_write(_V17_OUT, "")
    _v17_clock2 = _V17Clock(120000.0, off_path=_v17_off, stop_at=120000.0 + 30.0)
    mod.time = _v17_clock2
    mod.CODEX_BG_MAX_AGE = 40
    tg17b = RecTG()
    mod._run_deferred(SDIR, tg17b, mod.on_prompt(SDIR, tg17b, prompt_text="capped"))
    _v17_launch(tg17b, tresponse=_V17_RESP_TEXT)
    _v17_clock2.now += 60.0
    _v17_capped = run_reply(tg17b, final=True)
    _v17_cap_state = load()
    check("45e the two-hour lane cap still stops typing - never an infinite bubble",
          mod._has_open_bash_bg(_v17_cap_state.get("steps"),
                                _v17_clock2.now) is False
          and _v17_cap_state.get("typing_done") is True
          and [d[0] for d in _v17_capped] != ["typing"]
          and _v17_step(_v17_cap_state, "bash_stale").get("done") is True
          and _v17_step(_v17_cap_state, "bash_stale").get("cap", "").endswith(
              "· stale"),
          _detail26({"typing_done": _v17_cap_state.get("typing_done"),
                     "deferred": [d[0] for d in _v17_capped],
                     "step": _v17_step(_v17_cap_state, "bash_stale")}))
    mod.CODEX_BG_MAX_AGE = _v17_real_age

    for _v17_marker, _v17_word in (("\n[killed]\n", "killed"),
                                   ("done\n\n[exited with code 0]\n", "completed"),
                                   ("boom\n\n[exited with code 9]\n", "failed")):
        _reset26()
        _v17_write(_V17_OUT, "")
        _v17_clock3 = _V17Clock(150000.0)
        mod.time = _v17_clock3
        tg17c = RecTG()
        mod._run_deferred(SDIR, tg17c,
                          mod.on_prompt(SDIR, tg17c, prompt_text="watch"))
        _v17_launch(tg17c, tresponse=_V17_RESP_TEXT)
        _v17_held = run_reply(tg17c, final=True)
        _v17_write(_V17_OUT, _v17_marker)
        _v17_clock3.now += mod.CODEX_PROCESS_START_GRACE + 1.0
        _v17_released = run_reply(tg17c, final=True)
        _v17_dead = load()
        check("45f a %s task releases the hold on the next edge, in seconds"
              % _v17_word,
              [d[0] for d in _v17_held] == ["typing"]
              and mod._has_open_bash_bg(_v17_dead.get("steps"),
                                        _v17_clock3.now) is False
              and _v17_dead.get("typing_done") is True
              and [d[0] for d in _v17_released] != ["typing"]
              and _v17_step(_v17_dead, "bash_inferred").get("outcome") == _v17_word,
              _detail26({"deferred": [d[0] for d in _v17_released],
                         "step": _v17_step(_v17_dead, "bash_inferred")}))

    _reset26()
    _v17_clock4 = _V17Clock(180000.0)
    mod.time = _v17_clock4
    mod.CODEX_KEYLESS_HOLD_MAX = 300
    tg17d = RecTG()
    mod._run_deferred(SDIR, tg17d, mod.on_prompt(SDIR, tg17d, prompt_text="blind"))
    _v17_launch(tg17d, tresponse=_V17_RESP_DICT)
    _v17_blind_held = run_reply(tg17d, final=True)
    _v17_blind_state = load()
    _v17_clock4.now += 301.0
    _v17_blind_out = run_reply(tg17d, final=True)
    _v17_blind_after = load()
    check("45f a lane with no readable path is held, then bounded and closed honestly",
          [d[0] for d in _v17_blind_held] == ["typing"]
          and _v17_step(_v17_blind_state).get("bash_task_id") == _V17_TASK
          and "bash_task_out" not in _v17_step(_v17_blind_state)
          and [d[0] for d in _v17_blind_out] != ["typing"]
          and _v17_blind_after.get("typing_done") is True
          and _v17_step(_v17_blind_after, "bash_inferred").get("outcome")
          == "stopped",
          _detail26({"held": [d[0] for d in _v17_blind_held],
                     "after": _v17_step(_v17_blind_after, "bash_inferred")}))
    mod.CODEX_KEYLESS_HOLD_MAX = _v17_real_blind

    _reset26()
    _v17_clock5 = _V17Clock(200000.0)
    mod.time = _v17_clock5
    tg17e = RecTG()
    mod._run_deferred(SDIR, tg17e, mod.on_prompt(SDIR, tg17e, prompt_text="grace"))
    _v17_gone = os.path.join(_V17_TASKDIR, "tasks", "never-created.output")
    _v17_launch(tg17e, tresponse=(
        "Command running in background with ID: bgrace1. Output is being written "
        "to: %s. You will be notified when it completes." % _v17_gone))
    _v17_grace = run_reply(tg17e, final=True)
    check("45f the start grace stops a not-yet-created file reading as death",
          [d[0] for d in _v17_grace] == ["typing"]
          and load().get("typing_done") is False,
          _detail26([d[0] for d in _v17_grace]))

    _reset26()
    _v17_clock6 = _V17Clock(230000.0)
    mod.time = _v17_clock6
    _v17_out_a = os.path.join(_V17_TASKDIR, "tasks", "bconc1.output")
    _v17_out_b = os.path.join(_V17_TASKDIR, "tasks", "bconc2.output")
    _v17_write(_v17_out_a, "")
    _v17_write(_v17_out_b, "")
    tg17f = RecTG()
    mod._run_deferred(SDIR, tg17f, mod.on_prompt(SDIR, tg17f, prompt_text="all three"))
    for _tuid17, _out17, _desc17 in (("toolu_fixture000000000000000012", _v17_out_a, "watcher A"),
                                     ("toolu_fixture000000000000000013", _v17_out_b, "watcher B")):
        _v17_launch(tg17f, tinput=dict(_V17_INPUT, description=_desc17),
                    tuid=_tuid17,
                    tresponse=("Command running in background with ID: %s. Output is "
                               "being written to: %s."
                               % (os.path.basename(_out17)[:-len(".output")],
                                  _out17)))
    _step17(tg17f, "pre", "Bash", _V17_CODEX_INPUT, tuid="toolu_fixture000000000000000016")
    _step17(tg17f, "post", "Bash", _V17_CODEX_INPUT, tuid="toolu_fixture000000000000000016",
            tresponse={})
    _step17(tg17f, "pre", "Agent", _V17_AGENT_INPUT, tuid="toolu_fixture000000000000000010")
    _step17(tg17f, "post", "Agent", _V17_AGENT_INPUT, tuid="toolu_fixture000000000000000010",
            tresponse=_V17_AGENT_RESP)
    _v17_multi_stop = run_reply(tg17f, final=True)
    _v17_multi = load()
    _v17_open_bash = [s for s in (_v17_multi.get("steps") or [])
                      if s.get("bash_bg") and not s.get("done")]
    check("45g four concurrent lanes - two Bash, one Codex, one Agent - all hold",
          len(_v17_open_bash) == 2
          and mod._has_open_bash_bg(_v17_multi.get("steps"), _v17_clock6.now) is True
          and mod._has_open_codex_bg(_v17_multi.get("steps"), _v17_clock6.now) is True
          and mod._has_open_background_agents(_v17_multi.get("panel"),
                                              _v17_clock6.now) is True
          and [d[0] for d in _v17_multi_stop] == ["typing"]
          and _v17_multi.get("typing_done") is False,
          _detail26({"bash_open": len(_v17_open_bash),
                     "deferred": [d[0] for d in _v17_multi_stop]}))

    _v17_write(_v17_out_a, "SYNC_REPORT_READY\n\n[exited with code 0]\n")
    _v17_clock6.now += mod.CODEX_PROCESS_START_GRACE + 1.0
    _v17_partial_stop = run_reply(tg17f, final=True)
    _v17_partial = load()
    _v17_row_a = next((s for s in (_v17_partial.get("steps") or [])
                       if s.get("tuid") == "toolu_fixture000000000000000012"), {})
    _v17_row_b = next((s for s in (_v17_partial.get("steps") or [])
                       if s.get("tuid") == "toolu_fixture000000000000000013"), {})
    check("45g one lane finishing closes only itself and never the siblings",
          _v17_row_a.get("done") is True and _v17_row_a.get("outcome") == "completed"
          and _v17_row_b.get("done") is False
          and mod._has_open_codex_bg(_v17_partial.get("steps"),
                                     _v17_clock6.now) is True
          and [d[0] for d in _v17_partial_stop] == ["typing"]
          and _v17_partial.get("typing_done") is False,
          _detail26({"a": _v17_row_a.get("outcome"), "b": _v17_row_b.get("done")}))

    _reset26()
    _v17_clock7 = _V17Clock(260000.0)
    mod.time = _v17_clock7
    _v17_write(_V17_OUT, "")
    tg17g = RecTG()
    mod._run_deferred(SDIR, tg17g, mod.on_prompt(SDIR, tg17g, prompt_text="notify me"))
    _v17_launch(tg17g, tresponse=_V17_RESP_TEXT)
    run_reply(tg17g, final=True)
    _v17_notify_ops = mod.on_prompt(SDIR, tg17g, prompt_text=_V17_NOTIFY)
    mod._run_deferred(SDIR, tg17g, _v17_notify_ops)
    _v17_notified = load()
    _v17_row = _v17_step(_v17_notified)
    check("45h the notification turn closes the lane IN PLACE, not by reopening a box",
          _v17_row.get("done") is True
          and _v17_row.get("outcome") == "completed"
          and _v17_row.get("secs", 0) >= 0
          and mod._has_open_bash_bg(_v17_notified.get("steps"),
                                    _v17_clock7.now) is False
          and _v17_notify_ops is not None
          and not any(op[0] == "send" for op in _v17_notify_ops),
          _detail26({"row": _v17_row, "ops": [o[0] for o in (_v17_notify_ops or [])]}))

    _v17_fold = run_reply(tg17g, final=True)
    _v17_folded = load()
    check("45h with the lane closed the very next Stop folds the box normally",
          _v17_folded.get("typing_done") is True
          and [d[0] for d in _v17_fold] != ["typing"],
          _detail26({"typing_done": _v17_folded.get("typing_done"),
                     "deferred": [d[0] for d in _v17_fold]}))

    check("45h a killed/failed envelope writes the honest word, and a duplicate is inert",
          (lambda: [
              mod._apply_notification_close(
                  {"steps": [{"bash_bg": True, "done": False, "t0": 1.0,
                              "tuid": "T1", "cap": "background task · x"}],
                   "panel": []},
                  _V17_NOTIFY.replace(_V17_TUID, "T1").replace(
                      "<status>completed</status>", "<status>killed</status>"),
                  now=50.0)])()[0][:2] == (True, True),
          "killed envelope")
    _v17_dup_state = {"steps": [{"bash_bg": True, "done": False, "t0": 1.0,
                                 "tuid": "T2", "cap": "background task · y"}],
                      "panel": []}
    _v17_first = mod._apply_notification_close(
        _v17_dup_state, _V17_NOTIFY.replace(_V17_TUID, "T2"), now=50.0)
    _v17_second = mod._apply_notification_close(
        _v17_dup_state, _V17_NOTIFY.replace(_V17_TUID, "T2"), now=90.0)
    check("45h a repeat notification cannot re-close or re-stamp a closed lane",
          _v17_first[0] is True and _v17_first[1] is True
          and _v17_second[0] is True and _v17_second[1] is False
          and _v17_dup_state["steps"][0].get("secs") == 49.0,
          _detail26(_v17_dup_state["steps"][0]))

    check("45h a notification for an unknown id closes nothing at all",
          mod._apply_notification_close(
              {"steps": [{"bash_bg": True, "done": False, "t0": 1.0,
                          "tuid": "MINE", "cap": "background task · z"}],
               "panel": []},
              _V17_NOTIFY.replace(_V17_TUID, "SOMEBODY-ELSE"),
              now=50.0)[:2] == (False, False),
          "wrong id")

    _v17_spawns = []
    _v17_folds = []
    _v17_real_spawn = mod.spawn_animator
    _v17_real_fold = mod._fold_to_done

    def _v17_spawn(sdir=None):
        _v17_spawns.append(sdir)

    def _v17_fold_rec(sdir, tg):
        _v17_folds.append(sdir)
        return _v17_real_fold(sdir, tg)

    def _v17_crash(raise_after, respawn_min, with_lane):
        _reset26()
        del _v17_spawns[:]
        del _v17_folds[:]
        _v17_write(_V17_OUT, "")
        clock = _V17Clock(300000.0, off_path=_v17_off)
        mod.time = clock
        mod.ANIM_INTERVAL = 3
        mod.ANIM_MAX_RUN = 100000
        mod.ANIM_MAX_IDLE = 100000
        mod.MIN_EDIT_INTERVAL = 0
        mod.ANIM_RESPAWN_MIN_LIFE = respawn_min
        tgx = RecTG()
        mod._run_deferred(SDIR, tgx, mod.on_prompt(SDIR, tgx, prompt_text="crash"))
        if with_lane:
            _v17_launch(tgx, tresponse=_V17_RESP_TEXT)
        else:
            run_step(tgx, "pre", "Read", {"file_path": "/tmp/x"}, tuid="toolu_fixture000000000000000032")
            run_step(tgx, "post", "Read", {"file_path": "/tmp/x"}, tuid="toolu_fixture000000000000000032")
        mod.spawn_animator = _v17_spawn
        mod._fold_to_done = _v17_fold_rec
        clock.raise_at = clock.now + raise_after
        try:
            mod.run_animator(SDIR, tgx)
            died = False
        except RuntimeError:
            died = True
        finally:
            mod.spawn_animator = _v17_real_spawn
            mod._fold_to_done = _v17_real_fold
        return died, list(_v17_spawns), list(_v17_folds), load()

    _d17a, _s17a, _f17a, _st17a = _v17_crash(60.0, 30.0, True)
    check("45i an animator that dies with a live background task hands off",
          _d17a is True and len(_s17a) == 1 and _f17a == []
          and _st17a.get("folded") is not True
          and _st17a.get("typing_done") is False,
          _detail26({"spawns": len(_s17a), "folds": len(_f17a),
                     "folded": _st17a.get("folded")}))

    _d17b, _s17b, _f17b, _st17b = _v17_crash(6.0, 30.0, True)
    check("45i a death inside the first half-minute still never respawns",
          _d17b is True and _s17b == [] and len(_f17b) == 1,
          _detail26({"spawns": len(_s17b), "folds": len(_f17b)}))

    _d17c, _s17c, _f17c, _st17c = _v17_crash(60.0, 30.0, False)
    check("45i a death with NO background task still folds, exactly as before",
          _d17c is True and _s17c == [] and len(_f17c) == 1,
          _detail26({"spawns": len(_s17c), "folds": len(_f17c)}))

    _reset26()
    _v17_write(_V17_OUT, "")
    _v17_clock8 = _V17Clock(340000.0, off_path=_v17_off, stop_at=340000.0 + 4.0)
    mod.time = _v17_clock8
    mod.ANIM_INTERVAL = 3
    mod.ANIM_MAX_RUN = 100000
    mod.ANIM_MAX_IDLE = 60
    mod.MIN_EDIT_INTERVAL = 0
    tg17h = RecTG()
    mod._run_deferred(SDIR, tg17h, mod.on_prompt(SDIR, tg17h, prompt_text="idle"))
    _v17_launch(tg17h, tresponse=_V17_RESP_TEXT)
    _v17_clock8.now += 900.0
    _v17_clock8.stop_at = _v17_clock8.now + 4.0
    try:
        os.remove(_v17_off)
    except OSError:
        pass
    mod.run_animator(SDIR, tg17h)
    _v17_idle_state = load()
    check("45i the 30-minute idle backstop does not fold a live background task",
          _v17_idle_state.get("folded") is not True
          and _v17_idle_state.get("typing_done") is not True
          and len(tg17h.ops("typing")) >= 1,
          _detail26({"folded": _v17_idle_state.get("folded"),
                     "typing": len(tg17h.ops("typing"))}))

    _v17_write(_V17_OUT, "SYNC_REPORT_READY\n\n[exited with code 0]\n")
    _v17_clock8.now += 60.0
    _v17_clock8.stop_at = _v17_clock8.now + 90.0
    try:
        os.remove(_v17_off)
    except OSError:
        pass
    tg17h.calls.clear()
    mod.run_animator(SDIR, tg17h)
    check("45i once the task ends the backstop folds the box on its own",
          load().get("folded") is True,
          _detail26({"folded": load().get("folded")}))
    mod.ANIM_MAX_IDLE = _v17_real_idle
    mod.ANIM_MAX_RUN = _v17_real_run

    _v17_live_row = {"bash_bg": True, "done": False, "t0": 1.0,
                     "cap": "background task · Watch for sync worker report"}
    _v17_rows = mod._rich_lane_rows([_v17_live_row], [], now=61.0)
    _v17_done_rows = mod._rich_lane_rows(
        [dict(_v17_live_row, done=True, secs=125.0, outcome="completed")], [],
        now=200.0)
    _v17_kill_rows = mod._rich_lane_rows(
        [dict(_v17_live_row, done=True, secs=12.0, outcome="killed")], [], now=200.0)
    _v17_stale_rows = mod._rich_lane_rows(
        [dict(_v17_live_row, done=True, secs=7200.0, bash_stale=True)], [], now=1.0)
    check("45j a live lane draws its own table row, with its label and a ticking clock",
          len(_v17_rows) == 1 and _v17_rows[0][1] == "◉ live"
          and "background task" in _v17_rows[0][0]
          and _v17_rows[0][0].endswith("(Task)")
          and _v17_rows[0][2] == mod._rich_clock(60.0)
          and _v17_done_rows[0][1] == "✓ done"
          and _v17_kill_rows[0][1] == "✕ kill"
          and _v17_stale_rows[0][1] == "■ stop",
          _detail26([_v17_rows, _v17_done_rows, _v17_kill_rows, _v17_stale_rows]))

    check("45j the activity line names the wait instead of doubling the lane row",
          mod._rich_activity([_v17_live_row], 3) == "⏳ Waiting on background work"

          and mod._rich_activity(
              [_v17_live_row, {"done": False, "cap": "reading a file"}], 3)
          == "⏳ reading a file"

          and mod._rich_activity(
              [_v17_live_row, {"codex_bg": True, "done": False, "cap": "Codex Sol"}], 3)
          == "⏳ Waiting on the agent"
          and mod._rich_activity(
              [{"codex_bg": True, "done": False, "cap": "Codex Sol"}], 3)
          == "⏳ Waiting on the agent"
          and mod._rich_activity([], 3) == mod.thinking_line(3),
          _detail26([mod._rich_activity([_v17_live_row], 3),
                     mod._rich_activity(
                         [_v17_live_row,
                          {"codex_bg": True, "done": False, "cap": "C"}], 3)]))

    check("45j a finished lane still shows in the fold, so the recap stays honest",
          any("background task" in row[0]
              for row in mod._rich_lane_rows(
                  [dict(_v17_live_row, done=True, secs=1200.0,
                        outcome="completed")], [], now=9.0, fold=True)),
          "fold render")

    _reset26()
    _v17_clock9 = _V17Clock(400000.0)
    mod.time = _v17_clock9
    _v17_write(_V17_OUT, "")
    tg17i = RecTG()
    mod._run_deferred(SDIR, tg17i, mod.on_prompt(SDIR, tg17i, prompt_text="turn one"))
    _v17_launch(tg17i, tresponse=_V17_RESP_TEXT)
    run_reply(tg17i, final=True)
    _v17_clock9.now += 30.0
    mod._run_deferred(SDIR, tg17i, mod.on_prompt(SDIR, tg17i, prompt_text="turn two"))
    _v17_crossed = load()
    check("45k an open lane crosses the prompt boundary and still holds the new box",
          _v17_step(_v17_crossed).get("bash_bg") is True
          and _v17_step(_v17_crossed).get("done") is False
          and _v17_step(_v17_crossed).get("t0") == 400000.0
          and mod._has_open_bash_bg(_v17_crossed.get("steps"),
                                    _v17_clock9.now) is True
          and [d[0] for d in run_reply(tg17i, final=True)] == ["typing"],
          _detail26(_v17_step(_v17_crossed)))

    check("45k a carried lane counts as the fresh box, so a react cannot double it",
          mod._is_fresh_open_box(load(), _v17_clock9.now) is True
          and mod.on_react(SDIR, tg17i) == [],
          _detail26({"fresh": mod._is_fresh_open_box(load(), _v17_clock9.now)}))

    _reset26()
    _v17_clock10 = _V17Clock(430000.0)
    mod.time = _v17_clock10
    tg17j = RecTG()
    mod._run_deferred(SDIR, tg17j, mod.on_prompt(SDIR, tg17j, prompt_text="plain turn"))
    _step17(tg17j, "pre", "Read", {"file_path": "/tmp/a"}, tuid="toolu_fixture000000000000000033")
    _step17(tg17j, "post", "Read", {"file_path": "/tmp/a"}, tuid="toolu_fixture000000000000000033")
    _step17(tg17j, "pre", "Bash", {"command": "ls", "description": "list"},
            tuid="toolu_fixture000000000000000028")
    _step17(tg17j, "post", "Bash", {"command": "ls", "description": "list"},
            tuid="toolu_fixture000000000000000028", tresponse={"stdout": "a\n", "interrupted": False})
    _v17_plain = load()
    check("45k a turn with no background task gains not one new key or field",
          all(not any(k.startswith("bash_") for k in s)
              for s in (_v17_plain.get("steps") or []))
          and not any(k.startswith("bash_") for k in _v17_plain.keys())
          and all(s.get("done") is True for s in (_v17_plain.get("steps") or []))
          and [d[0] for d in run_reply(tg17j, final=True)] != ["typing"],
          _detail26(_v17_plain.get("steps")))

    _reset26()
    _v17_clock11 = _V17Clock(460000.0)
    mod.time = _v17_clock11
    tg17k = RecTG()
    mod._run_deferred(SDIR, tg17k, mod.on_prompt(SDIR, tg17k, prompt_text="refused"))
    _v17_launch(tg17k, tresponse={"stdout": "", "stderr": "denied",
                                  "interrupted": True})
    _v17_refused = load()
    check("45k an unacknowledged background launch closes on its own PostToolUse",
          (_v17_refused.get("steps") or [{}])[0].get("done") is True
          and "bash_bg" not in (_v17_refused.get("steps") or [{}])[0]
          and "bash_bg_requested" not in (_v17_refused.get("steps") or [{}])[0]
          and [d[0] for d in run_reply(tg17k, final=True)] != ["typing"],
          _detail26((_v17_refused.get("steps") or [{}])[0]))

    _v17_pure = {"bash_bg": True, "done": False, "t0": 1.0,
                 "bash_task_id": "bx", "bash_task_out": _V17_OUT}
    _v17_pure_copy = dict(_v17_pure)
    mod._has_open_bash_bg([_v17_pure], 2.0)
    mod._bash_bg_due(_v17_pure, 2.0)
    mod._bash_task_status(_v17_pure)
    mod._has_open_tool_step([_v17_pure], 2.0)
    mod._rich_lane_rows([_v17_pure], [], now=2.0)
    mod._rich_activity([_v17_pure], 1)
    check("45k the read-only helpers never mutate the row they are asked about",
          _v17_pure == _v17_pure_copy, _detail26(_v17_pure))

    _v17_tx = os.path.join(_V17_TASKDIR, "session-v17.jsonl")
    _v17_out_x = os.path.join(_V17_TASKDIR, "tasks", "bxx111.output")
    _v17_out_y = os.path.join(_V17_TASKDIR, "tasks", "byy222.output")
    _v17_write(_v17_tx, "\n".join([
        json.dumps({"type": "user", "message": {"content": [{
            "type": "tool_result",
            "content": ("Command running in background with ID: bxx111. Output is "
                        "being written to: %s. You will be notified when it "
                        "completes." % _v17_out_x)}]}}),
        json.dumps({"type": "user", "message": {"content": [{
            "type": "tool_result",
            "content": ("Command running in background with ID: byy222. Output is "
                        "being written to: %s." % _v17_out_y)}]}}),
    ]) + "\n")
    _v17_lane_x = {"bash_bg": True, "done": False, "t0": 1.0,
                   "bash_task_id": "bxx111", "tuid": "X"}
    _v17_lane_y = {"bash_bg": True, "done": False, "t0": 1.0,
                   "bash_task_id": "byy222", "tuid": "Y"}
    _v17_lane_z = {"bash_bg": True, "done": False, "t0": 1.0,
                   "bash_task_id": "bzz999", "tuid": "Z"}
    _v17_lanes = [_v17_lane_x, _v17_lane_y, _v17_lane_z]
    _v17_found = mod._bash_backfill_task_out(_v17_lanes, _v17_tx)
    check("45l each lane learns its OWN output path, never a sibling's",
          _v17_found == 2
          and _v17_lane_x.get("bash_task_out") == _v17_out_x
          and _v17_lane_y.get("bash_task_out") == _v17_out_y
          and "bash_task_out" not in _v17_lane_z
          and all(l.get("bash_out_probed") is True for l in _v17_lanes),
          _detail26([_v17_lane_x.get("bash_task_out"),
                     _v17_lane_y.get("bash_task_out"),
                     _v17_lane_z.get("bash_task_out")]))

    check("45l the probe is ONE read per lane, ever, and is inert on bad input",
          mod._bash_lanes_missing_out(_v17_lanes) == []
          and mod._bash_backfill_task_out(_v17_lanes, _v17_tx) == 0
          and mod._bash_backfill_task_out(
              [{"bash_bg": True, "done": False, "bash_task_id": "bxx111"}],
              os.path.join(_V17_TASKDIR, "no-such.jsonl")) == 0
          and mod._bash_backfill_task_out(
              [{"bash_bg": True, "done": False, "bash_task_id": "bxx111"}],
              None) == 0

          and mod._bash_lanes_missing_out([
              {"bash_bg": True, "done": True, "bash_task_id": "b1"},
              {"bash_bg": True, "done": False, "bash_task_id": "b2",
               "bash_task_out": "/x/tasks/b2.output"},
              {"bash_bg": True, "done": False},
              {"done": False, "bash_task_id": "b3"}]) == []

          and mod._bash_backfill_task_out(
              [{"bash_bg": True, "done": False, "bash_task_id": "b.*"}],
              _v17_tx) == 0,
          "truth table")

    _reset26()
    _v17_real_txpath = mod._bg_notify_transcript_path
    try:
        mod._bg_notify_transcript_path = lambda st: _v17_tx
        _v17_e2e_out = os.path.join(_V17_TASKDIR, "tasks", "be2e17.output")
        _v17_write(_v17_e2e_out, "")
        _v17_write(_v17_tx, json.dumps({"type": "user", "message": {"content": [{
            "type": "tool_result",
            "content": ("Command running in background with ID: be2e17. Output is "
                        "being written to: %s." % _v17_e2e_out)}]}}) + "\n")
        _v17_clock12 = _V17Clock(500000.0, off_path=_v17_off,
                                 stop_at=500000.0 + 9.0)
        mod.time = _v17_clock12
        mod.ANIM_INTERVAL = 3
        mod.ANIM_MAX_RUN = 100000
        mod.ANIM_MAX_IDLE = 100000
        mod.MIN_EDIT_INTERVAL = 0
        tg17l = RecTG()
        mod._run_deferred(SDIR, tg17l, mod.on_prompt(SDIR, tg17l, prompt_text="e2e"))
        _v17_launch(tg17l, tresponse={"stdout": "", "stderr": "", "isImage": False,
                                      "interrupted": False,
                                      "backgroundTaskId": "be2e17"})
        _v17_before = _v17_step(load())
        run_reply(tg17l, final=True)
        mod.run_animator(SDIR, tg17l)
        _v17_after = _v17_step(load())
        check("45l an animator tick learns the path a structured response never gave",
              "bash_task_out" not in _v17_before
              and _v17_before.get("bash_task_id") == "be2e17"
              and _v17_after.get("bash_task_out") == _v17_e2e_out
              and _v17_after.get("done") is False,
              _detail26({"before": _v17_before.get("bash_task_out"),
                         "after": _v17_after.get("bash_task_out")}))

        _v17_write(_v17_e2e_out, "\n[killed]\n")
        _v17_clock12.now += mod.CODEX_PROCESS_START_GRACE + 1.0
        _v17_kill_stop = run_reply(tg17l, final=True)
        _v17_killed_state = load()
        check("45l a killed task with no notification releases the box in seconds",
              mod._has_open_bash_bg(_v17_killed_state.get("steps"),
                                    _v17_clock12.now) is False
              and _v17_killed_state.get("typing_done") is True
              and [d[0] for d in _v17_kill_stop] != ["typing"]
              and _v17_step(_v17_killed_state, "bash_inferred").get("outcome")
              == "killed"
              and (_v17_clock12.now - 500000.0) < mod.CODEX_KEYLESS_HOLD_MAX,
              _detail26({"elapsed": _v17_clock12.now - 500000.0,
                         "step": _v17_step(_v17_killed_state, "bash_inferred")}))
    finally:
        mod._bg_notify_transcript_path = _v17_real_txpath
except Exception as _exc45:
    check("45 SECTION ABORTED - this build does not carry the v30.17 Bash lane",
          False, "%s: %s" % (type(_exc45).__name__, str(_exc45)[:400]))
finally:
    _v17_restore()
    try:
        shutil.rmtree(_V17_TASKDIR)
    except OSError:
        pass

_reset26()

print("\n===== #46 the effort tag on the context line (v30.20) =====")

_reset26()
_MODEL_STATE46 = os.path.join(STATE_DIR, "model-state")
_PROJ46 = tempfile.mkdtemp(prefix="tgstream-proj46-")

def _write_state46(text):

    if text is None:
        try:
            os.remove(_MODEL_STATE46)
        except OSError:
            pass
        return None
    with open(_MODEL_STATE46, "w", encoding="utf-8") as handle:
        handle.write(text)
    return _MODEL_STATE46

def _prompt46(tg, text, session_id=None):

    try:
        deferred = mod.on_prompt(SDIR, tg, prompt_text=text, session_id=session_id,
                                 project_dir=_PROJ46)
        mod._run_deferred(SDIR, tg, deferred)
        return ""
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, str(exc)[:180])

def _live46(state, fallback=False):
    return mod.render_rich_live(
        SDIR, state.get("steps") or [], state.get("proc_start") or 0.0, {},
        "effort tag", state.get("panel") or [], state.get("rich_plan") or [],
        {"items": [], "count": 0}, fallback=fallback, frame=1, state=state)

def _ctxline46(frame):
    return [line.rstrip() for line in frame.splitlines()][2]

try:
    _enable_rich26()

    _EMPTY_BAR46 = mod.CONTEXT_BAR_EMPTY * 8
    _FIFTEEN_BAR46 = mod.CONTEXT_BAR_FILL + mod.CONTEXT_BAR_EMPTY * 7
    _tags46 = [
        ("low", "L"), ("medium", "M"), ("high", "H"),
        ("xhigh", "XH"), ("max", "MX"),
    ]
    _mapped46 = {
        level: mod._context_line({"ctx_pct": 15, "ctx_model": "claude-fable-5-1",
                                  "ctx_effort": level})
        for level, _ in _tags46
    }
    check("46a all five effort levels render their exact tag after the model label",
          all(_mapped46[level] == "🧠 Fable 5.1 (%s) · %s 15%% context"
              % (tag, _FIFTEEN_BAR46) for level, tag in _tags46)
          and len(set(_mapped46.values())) == 5,
          _detail26(_mapped46))

    _long_tagged46 = mod._context_line(
        {"ctx_pct": 50, "ctx_model": "z" * 200, "ctx_effort": "xhigh"})
    _long_plain46 = mod._context_line({"ctx_pct": 50, "ctx_model": "z" * 200})
    _model_seg46 = _long_tagged46.split("🧠 ", 1)[1].split(" (XH) · ", 1)[0]
    check("46a the tag rides outside the 32-char model cap and moves nothing else",
          _long_tagged46 == _long_plain46.replace(" · ", " (XH) · ", 1)
          and _model_seg46 == _long_plain46.split("🧠 ", 1)[1].split(" · ", 1)[0]
          and len(_model_seg46) <= 32,
          _detail26({"tagged": _long_tagged46, "plain": _long_plain46,
                     "model_len": len(_model_seg46)}))

    _junk46 = ["turbo", "", "   ", "maxx", "mx", "MAXIMUM", "1", "max low",
               "max,high", "max\nlow", None, 7, 0, True, False, 1.5,
               ["max"], {"effort": "max"}, ("max",), "z" * 10240]
    _junk_lines46 = [
        mod._context_line({"ctx_pct": 15, "ctx_model": "claude-fable-5-1",
                           "ctx_effort": value})
        for value in _junk46
    ]
    _untagged46 = "🧠 Fable 5.1 · %s 15%% context" % _FIFTEEN_BAR46
    _pad46 = [mod._context_line({"ctx_pct": 15, "ctx_model": "claude-fable-5-1",
                                 "ctx_effort": value})
              for value in ("  max  ", "\tmax\n", "MAX", "Xhigh")]
    check("46b an unknown, empty or non-string effort renders the untagged line, never (?)",
          all(line == _untagged46 for line in _junk_lines46)
          and not any("(?" in line for line in _junk_lines46)
          and _pad46 == ["🧠 Fable 5.1 (MX) · %s 15%% context" % _FIFTEEN_BAR46] * 3
          + ["🧠 Fable 5.1 (XH) · %s 15%% context" % _FIFTEEN_BAR46],
          _detail26({"junk": _junk_lines46[:4], "padded": _pad46}))

    check("46b a state with no ctx_effort key renders exactly the v30.19 line",
          mod._context_line({"ctx_pct": 15, "ctx_model": "claude-fable-5-1"})
          == _untagged46,
          _detail26(mod._context_line({"ctx_pct": 15,
                                       "ctx_model": "claude-fable-5-1"})))

    _tg46 = RichRecTG()
    _write_state46("MODEL=fable\nEFFORT=max\n")
    _err46 = _prompt46(_tg46, "tag this turn", session_id="effort-session-46")
    _state46 = load()
    _rich_frame46 = _live46(_state46)
    _plain_frame46 = _live46(_state46, fallback=True)
    check("46c a UserPromptSubmit boundary stores the level and both live frames show it",
          _err46 == ""
          and _state46.get("ctx_effort") == "max"
          and _ctxline46(_rich_frame46) == "🧠 Fable (MX) · %s ??%% context" % _EMPTY_BAR46
          and _ctxline46(_plain_frame46) == _ctxline46(_rich_frame46)
          and _rich_frame46.splitlines()[1].startswith("👾 Working"),
          _detail26({"err": _err46, "effort": _state46.get("ctx_effort"),
                     "rich": _ctxline46(_rich_frame46),
                     "plain": _ctxline46(_plain_frame46)}))

    _write_state46("MODEL=fable\nEFFORT=low\n")
    _tg46b = RichRecTG()
    _err46b = _prompt46(_tg46b, "changed my effort", session_id="effort-session-46")
    _state46b = load()
    _write_state46("MODEL=fable\n")
    _tg46c = RichRecTG()
    _err46c = _prompt46(_tg46c, "no effort line now", session_id="effort-session-46")
    _state46c = load()
    check("46d a mid-session effort change lands on the next boundary, and so does its removal",
          _err46b == "" and _err46c == ""
          and _state46b.get("ctx_effort") == "low"
          and _ctxline46(_live46(_state46b))
          == "🧠 Fable (L) · %s ??%% context" % _EMPTY_BAR46
          and "ctx_effort" not in _state46c
          and _ctxline46(_live46(_state46c))
          == "🧠 Fable · %s ??%% context" % _EMPTY_BAR46,
          _detail26({"low": _state46b.get("ctx_effort"),
                     "cleared": "ctx_effort" in _state46c,
                     "line": _ctxline46(_live46(_state46c))}))

    _write_state46("MODEL=fable\nEFFORT=xhigh\n")
    _tg46d = RichRecTG()
    _err46d = _prompt46(_tg46d, "carry the tag", session_id="effort-session-46")
    _opened46 = load()
    _step_err46 = _step26(_tg46d, "pre", "Read",
                          {"file_path": "/tmp/effort-46.txt"}, "EFFORT-46",
                          session_id="effort-session-46")[1]
    _stepped46 = load()
    check("46e handle_step's full state rewrite preserves the effort key",
          _err46d == "" and _step_err46 == ""
          and _opened46.get("ctx_effort") == "xhigh"
          and _stepped46.get("ctx_effort") == "xhigh"
          and _ctxline46(_live46(_stepped46))
          == "🧠 Fable (XH) · %s ??%% context" % _EMPTY_BAR46,
          _detail26({"opened": _opened46.get("ctx_effort"),
                     "stepped": _stepped46.get("ctx_effort"),
                     "step_err": _step_err46}))

    _write_state46(None)
    _tg46e = RichRecTG()
    _err46e = _prompt46(_tg46e, "no model-state here", session_id="effort-session-46")
    _state46e = load()
    check("46f with no model-state the line stays exactly the unknown-furniture line",
          _err46e == ""
          and "ctx_effort" not in _state46e
          and "(" not in _ctxline46(_live46(_state46e))
          and mod._context_line({}) == "🧠 Unknown model · %s ??%% context" % _EMPTY_BAR46
          and mod._context_line(None)
          == "🧠 Unknown model · %s ??%% context" % _EMPTY_BAR46,
          _detail26({"line": _ctxline46(_live46(_state46e)),
                     "bare": mod._context_line({})}))

    _fold_steps46 = [{"fam": "files", "cap": "reading effort-46.txt",
                      "tuid": "FOLD-46", "done": True, "k": "", "aid": "",
                      "t0": 1000.0, "secs": 2.0}]
    _rich_fold46 = mod.render_rich_final(
        SDIR, _fold_steps46, 1000.0, mission="effort tag", panel=[], plan=[],
        outcome="done", tools={"items": [], "count": 0})[0]
    _plain_fold46 = mod.render_final(_fold_steps46, 1000.0, 0, "effort tag", [])[0]
    check("46g the folded recap carries no context line, so the tag cannot reach it",
          "🧠" not in (_rich_fold46 or "")
          and "(MX)" not in (_rich_fold46 or "")
          and "🧠" not in (_plain_fold46 or "")
          and "% context" not in (_rich_fold46 or "")
          and "% context" not in (_plain_fold46 or ""),
          _detail26({"rich": (_rich_fold46 or "")[:200],
                     "plain": (_plain_fold46 or "")[:200]}))

    _parse46 = []
    for text, want in (
            ("MODEL=fable\nEFFORT=max\n", "max"),
            ("MODEL=fable\nEFFORT=low\n", "low"),
            ("MODEL=fable\nEFFORT=medium\n", "medium"),
            ("MODEL=fable\nEFFORT=high\n", "high"),
            ("MODEL=fable\nEFFORT=xhigh\n", "xhigh"),
            ("EFFORT=MAX\n", "max"),
            ("effort=high\n", "high"),
            ("EFFORT=  max  \n", "max"),
            ("MODEL=fable\nEFFORT=high\nEFFORT=low\n", "high"),
            ("MODEL=fable\n", ""),
            ("", ""),
            ("EFFORT=\n", ""),
            ("EFFORT=   \n", ""),
            ("EFFORT=turbo\n", ""),
            ("EFFORT\n", ""),
            ("  EFFORT = high\n", ""),
            ("NOT_EFFORT=high\n", ""),
            ("max\n", ""),
            ("MODEL=fable\nEFFORT=max", "max")):
        _write_state46(text)
        _parse46.append((text[:24], mod._turn_context_effort(SDIR), want))
    check("46h EFFORT= is parsed exactly the way MODEL= is, and nothing else passes",
          all(got == want for _, got, want in _parse46),
          _detail26([row for row in _parse46 if row[1] != row[2]] or "all match"))

    _write_state46("EFFORT=" + "z" * 10240 + "\nEFFORT=max\n")
    _huge_raw46 = mod._read_context_model_state(SDIR)
    _huge_effort46 = mod._turn_context_effort(SDIR)
    _store46 = {}
    mod._store_context_effort(_store46, "z" * 10240)
    _store_after_junk46 = dict(_store46)
    mod._store_context_effort(_store46, "max")
    _store_after_good46 = dict(_store46)
    mod._store_context_effort(_store46, "turbo")
    check("46h a 10 KB effort line is bounded away and never reaches state",
          len(_huge_raw46) == mod.CONTEXT_MODEL_STATE_BYTES
          and _huge_effort46 == ""
          and _store_after_junk46 == {}
          and _store_after_good46 == {"ctx_effort": "max"}
          and "ctx_effort" not in _store46
          and mod._store_context_effort(None, "max") == "",
          _detail26({"raw_len": len(_huge_raw46), "effort": _huge_effort46,
                     "store": _store46}))

    _write_state46(None)
    _absent46 = mod._turn_context_effort(SDIR)
    os.makedirs(_MODEL_STATE46, exist_ok=True)
    _dir46 = mod._turn_context_effort(SDIR)
    os.rmdir(_MODEL_STATE46)
    _fifo46path = _MODEL_STATE46
    os.mkfifo(_fifo46path)
    _fifo46 = mod._turn_context_effort(SDIR)
    os.remove(_fifo46path)
    _write_state46("MODEL=fable\nEFFORT=max\n")
    _model_still46 = mod._configured_context_model(mod._read_context_model_state(SDIR))
    check("46h a missing, directory or FIFO model-state is no tag, and MODEL= is untouched",
          _absent46 == "" and _dir46 == "" and _fifo46 == ""
          and _model_still46 == "fable"
          and mod._configured_context_model("MODEL=claude-opus-5\nEFFORT=max\n")
          == "claude-opus-5"
          and mod._context_effort_tag("max") == " (MX)"
          and mod._context_effort_tag("turbo") == ""
          and mod._context_effort_label("HIGH") == "high"
          and sorted(mod.CONTEXT_EFFORT_TAGS)
          == ["high", "low", "max", "medium", "xhigh"],
          _detail26({"absent": _absent46, "dir": _dir46, "fifo": _fifo46,
                     "model": _model_still46}))
except Exception as _exc46:
    check("46 SECTION ABORTED - this build does not carry the v30.20 effort tag",
          False, "%s: %s" % (type(_exc46).__name__, str(_exc46)[:400]))
finally:
    try:
        os.remove(_MODEL_STATE46)
    except OSError:
        pass
    try:
        shutil.rmtree(_PROJ46)
    except OSError:
        pass
    _reset26()

print("\n===== #47 versioned lane labels (v30.21) =====")

_CACHE47 = os.path.join(SDIR, "models-cache.json")
_CODEX_HOME_HARNESS47 = os.environ.get("CODEX_HOME", "")
_REAL_RUN47 = mod.subprocess.run
_REAL_POPEN47 = mod.subprocess.Popen
_REAL_URLOPEN47 = mod.request.urlopen
_write_fleet_config(judge=True)

_MODELS47 = [
    {"id": "claude-opus-5-5", "created_at": "2026-09-21T16:24:00Z"},
    {"id": "claude-fable-5-1", "created_at": "2026-08-28T00:00:00Z"},
    {"id": "claude-opus-5", "created_at": "2026-07-24T00:00:00Z"},
    {"id": "claude-sonnet-5", "created_at": "2026-06-29T00:00:00Z"},
    {"id": "claude-fable-5", "created_at": "2026-06-07T00:00:00Z"},
    {"id": "claude-opus-4-8", "created_at": "2026-05-28T00:00:00Z"},
    {"id": "claude-haiku-4-5-20251001", "created_at": "2025-10-15T00:00:00Z"},
    {"id": "claude-sonnet-4-5-20250929", "created_at": "2025-09-29T00:00:00Z"},
]

class _FakeProc47:
    pid = 0

def _write_cache47(models=None, fetched_at=None, raw=None):

    if raw is None:
        raw = json.dumps({
            "fetched_at": (time.time() - 60) if fetched_at is None else fetched_at,
            "source": "https://api.anthropic.com/v1/models",
            "models": _MODELS47 if models is None else models})
    tmp = _CACHE47 + ".w47"
    with open(tmp, "w") as fh:
        fh.write(raw)
    os.replace(tmp, _CACHE47)

def _drop_cache47():
    try:
        if os.path.isdir(_CACHE47) and not os.path.islink(_CACHE47):
            os.rmdir(_CACHE47)
        elif os.path.lexists(_CACHE47):
            os.remove(_CACHE47)
    except OSError:
        pass

def _codex_home47(name, config=None):

    home = os.path.join(STATE_DIR, "codex-home-47-%s" % name)
    if config is not None:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "config.toml"), "w") as fh:
            fh.write(config)
    os.environ["CODEX_HOME"] = home
    return home

def _cap47(command):
    return mod._codex_bash_caption({"command": command})

def _row47(model, label="lane", done=False, **extra):
    row = {"tuid": "ROW47-%s" % label, "lab": label, "typ": "opus-executor",
           "mdl": model, "done": done, "t0": time.time() - 30}
    if done:
        row["secs"] = 12
    row.update(extra)
    return row

def _lane47(row=None, steps=None, now=None):
    rows = mod._rich_lane_rows(steps or [], [row] if row is not None else [],
                               now=time.time() if now is None else now)
    return rows[0][0] if rows else ""

def _passthrough_pgrep47(argv):
    return isinstance(argv, (list, tuple)) and list(argv[:2]) == ["/usr/bin/pgrep", "-f"]

_drop_cache47()

_codex_home47("none")
_caps47 = {
    "sol": _cap47("codex exec -m gpt-6-sol -o x-report.md"),
    "astra": _cap47(
        'cd "/Users/example/some dir" && /opt/homebrew/bin/codex exec -m gpt-6-astra '
        '-c model_reasoning_effort=max --sandbox read-only --skip-git-repo-check '
        '--color never -o "/private/tmp/sp/audit-report.md" - '
        '< "/private/tmp/sp/audit-brief.txt" > "/private/tmp/sp/audit-run.log" 2>&1'),
    "terra": _cap47("codex exec -m gpt-5.6-terra -o /tmp/architecture-report.md -"),
    "sol56": _cap47("codex exec -m gpt-5.6-sol -o /tmp/lane-report.md -"),
    "gpt55": _cap47("codex exec -m gpt-5.5 -o /tmp/plain-report.md -"),
    "long": _cap47("codex exec --model=gpt-6-sol -o /tmp/long-form-report.md -"),
}
check("47a codex exec -m gpt-6-sol -o x-report.md reads 'GPT-6 Sol · x'",
      _caps47["sol"] == "GPT-6 Sol · x", _detail26(_caps47))
check("47a -m gpt-6-astra, gpt-5.6-terra, gpt-5.6-sol and gpt-5.5 each spell their own model",
      _caps47["astra"] == "GPT-6 Astra · audit"
      and _caps47["terra"] == "GPT-5.6 Terra · architecture"
      and _caps47["sol56"] == "GPT-5.6 Sol · lane"
      and _caps47["gpt55"] == "GPT-5.5 · plain"
      and _caps47["long"] == "GPT-6 Sol · long-form",
      _detail26(_caps47))

_codex_home47("astra", 'model_reasoning_effort = "max"\n'
                       'model = "gpt-6-astra"  # the default\n'
                       'sandbox_mode = "workspace-write"\n\n'
                       '[profiles.fast]\nmodel = "gpt-5.5"\n')
_cfg47 = {
    "config": _cap47("codex exec -o y-report.md -"),
    "dash_c": _cap47("codex exec -c model=gpt-6-sol -o y-report.md -"),
    "config_quoted": _cap47("codex exec --config 'model=\"gpt-5.6-terra\"' -o y-report.md -"),
    "m_beats_c": _cap47("codex exec -c model=gpt-5.5 -m gpt-6-sol -o y-report.md -"),
    "git_m": _cap47('git commit -m "gpt-5.6-sol" && codex exec -o y-report.md - < b.txt'),
    "next_line_git": _cap47('codex exec -o y-report.md - < b.txt\ngit commit -m "gpt-5.6-sol"'),
    "semicolon_newline": _cap47('codex exec -o y-report.md -;\ngit commit -m "gpt-5.6-sol"'),
    "continued": _cap47('codex exec \\\n  -m gpt-6-sol \\\n  -o y-report.md -'),
    "resume_continued": _cap47(
        '/opt/homebrew/bin/codex exec resume \\\n  -m gpt-6-astra \\\n'
        '  -o "/tmp/y-report.md" \\\n  "$SID" - < f.txt > r.log 2>&1'),
    "profile": _cap47("codex exec -p fast -o y-report.md -"),
    "resume": _cap47("codex exec resume 019abc -o y-report.md -"),
    "oss": _cap47("codex exec --oss -o y-report.md -"),
    "inline_home": _cap47("CODEX_HOME=/elsewhere codex exec -o y-report.md -"),
}
check("47b no -m plus config.toml model=gpt-6-astra reads 'GPT-6 Astra'",
      _cfg47["config"] == "GPT-6 Astra · y", _detail26(_cfg47))
check("47b -c/--config model= outranks the config file, and -m outranks both",
      _cfg47["dash_c"] == "GPT-6 Sol · y"
      and _cfg47["config_quoted"] == "GPT-5.6 Terra · y"
      and _cfg47["m_beats_c"] == "GPT-6 Sol · y", _detail26(_cfg47))

check("47b a git -m elsewhere on the command line - before it, or on a later line - is never "
      "read as the model",
      _cfg47["git_m"] == _cfg47["next_line_git"] == _cfg47["semicolon_newline"]
      == "GPT-6 Astra · y", _detail26(_cfg47))
check("47b a line-continued invocation and an explicit -m on resume are read exactly",
      _cfg47["continued"] == "GPT-6 Sol · y"
      and _cfg47["resume_continued"] == "GPT-6 Astra · y", _detail26(_cfg47))
check("47b the config is never trusted where something else could pick the model (CONTROL)",
      _cfg47["profile"] == _cfg47["resume"] == _cfg47["oss"]
      == _cfg47["inline_home"] == "Codex · y", _detail26(_cfg47))

_codex_home47("none")
_nocfg47 = [_cap47("codex exec -o z-report.md -"),
            _cap47("codex exec --sandbox read-only prompt"),
            _cap47('codex exec -m "$MODEL" -o z-report.md -'),
            _cap47("codex exec -m o3 -o z-report.md -")]
check("47c no -m and no config reads the bare 'Codex', as does a model it cannot spell (CONTROL)",
      _nocfg47 == ["Codex · z", "Codex", "Codex · z", "Codex · z"], _detail26(_nocfg47))

_strip47 = [mod._codex_lane_label(v) for v in (
    "GPT-6 Sol · x", "GPT-6 Astra · audit", "GPT-5.6 Terra · architecture",
    "GPT-5.5 · plain", "Codex Mini · tiny")]
check("47d _codex_lane_label strips the new model prefix",
      _strip47 == ["x", "audit", "architecture", "plain", "tiny"], _detail26(_strip47))
_legacy47 = [mod._codex_lane_label(v) for v in (
    "Codex Sol · lane-research", "Codex Terra · architecture", "Codex · z", "")]
check("47d captions stored before v30.21 still strip exactly as they did (CONTROL)",
      _legacy47 == ["lane-research", "architecture", "z", "Codex"], _detail26(_legacy47))
_only47 = _lane47(steps=[{"cap": "GPT-6 Sol", "codex_bg": True, "codex_model": "gpt-6-sol",
                          "done": False, "t0": time.time() - 5}])
check("47d a caption that is only a GPT label reads 'Codex (GPT-6 Sol)', never the model twice",
      mod._codex_lane_label("GPT-6 Sol") == "Codex" and _only47 == "Codex (GPT-6 Sol)",
      _detail26([mod._codex_lane_label("GPT-6 Sol"), _only47]))

_write_cache47()
_claude47 = {
    "opus": _lane47(_row47("opus", "opus lane")),
    "fable": _lane47(_row47("fable", "fable lane")),
    "opus1m": _lane47(_row47("opus[1m]", "wide lane")),
    "sonnet": _lane47(_row47("sonnet", "sonnet lane")),
    "haiku": _lane47(_row47("haiku", "haiku lane")),
}
check("47e 'opus' with a resolvable cache reads 'Opus 5.5'",
      _claude47["opus"] == "opus lane (Opus 5.5)", _detail26(_claude47))
check("47e 'fable' with a resolvable cache reads 'Fable 5.1'",
      _claude47["fable"] == "fable lane (Fable 5.1)", _detail26(_claude47))
check("47e 'opus[1m]' counts as opus and reads 'Opus 5.5'",
      _claude47["opus1m"] == "wide lane (Opus 5.5)", _detail26(_claude47))
check("47e sonnet and haiku resolve too, and a dated snapshot id drops its date",
      _claude47["sonnet"] == "sonnet lane (Sonnet 5)"
      and _claude47["haiku"] == "haiku lane (Haiku 4.5)", _detail26(_claude47))
_drop_cache47()
_nocache47 = [_lane47(_row47("opus", "opus lane")), _lane47(_row47("fable", "fable lane"))]
check("47e 'opus' with no cache reads the bare 'Opus', exactly as before (CONTROL)",
      _nocache47 == ["opus lane (Opus)", "fable lane (Fable)"], _detail26(_nocache47))

_write_cache47()
_served47 = {
    "live": _lane47(_row47("opus", "served lane", srv="claude-opus-4-8[1m]")),
    "done_truth": _lane47(_row47("opus", "served lane", done=True,
                                 srv="claude-opus-5-5[1m]")),
    "done_no_truth": _lane47(_row47("opus", "served lane", done=True)),
    "named": _lane47(_row47("claude-opus-4-8", "named lane", done=True)),
}
_drop_cache47()
check("47f a truthful served id beats the cache",
      _served47["live"] == "served lane (Opus 4.8)", _detail26(_served47))
check("47f the served id, and a version the dispatch named, speak for a FINISHED row too",
      _served47["done_truth"] == "served lane (Opus 5.5)"
      and _served47["named"] == "named lane (Opus 4.8)", _detail26(_served47))
check("47f the cache never speaks for a finished row - it describes now, not what ran (CONTROL)",
      _served47["done_no_truth"] == "served lane (Opus)", _detail26(_served47))

_bad47 = {}
for _name47, _writer47 in (
        ("garbage", lambda: _write_cache47(raw="{not json")),
        ("empty", lambda: _write_cache47(raw="")),
        ("a bare list", lambda: _write_cache47(raw=json.dumps(_MODELS47))),
        ("no fetched_at", lambda: _write_cache47(raw=json.dumps({"models": _MODELS47}))),
        ("NaN fetched_at", lambda: _write_cache47(
            raw='{"fetched_at": NaN, "models": %s}' % json.dumps(_MODELS47))),
        ("bool fetched_at", lambda: _write_cache47(
            raw=json.dumps({"fetched_at": True, "models": _MODELS47}))),
        ("models not a list", lambda: _write_cache47(
            raw=json.dumps({"fetched_at": time.time(), "models": {"id": "claude-opus-5-5"}}))),
        ("unparseable created_at", lambda: _write_cache47(
            models=[{"id": "claude-opus-9", "created_at": "yesterday"}])),
        ("stale by 25 hours", lambda: _write_cache47(fetched_at=time.time() - 25 * 3600)),
        ("from the future", lambda: _write_cache47(fetched_at=time.time() + 86400)),
        ("oversized", lambda: _write_cache47(raw=json.dumps({
            "fetched_at": time.time(), "models": _MODELS47, "pad": "x" * (300 * 1024)}))),
        ("a directory", lambda: os.makedirs(_CACHE47)),
):
    _drop_cache47()
    _writer47()
    _bad47[_name47] = _lane47(_row47("opus", "opus lane"))
    _drop_cache47()
check("47g a corrupt, stale, future, oversized or odd cache fails open to the bare family (CONTROL)",
      len(_bad47) == 12 and all(v == "opus lane (Opus)" for v in _bad47.values()),
      _detail26(_bad47))

_write_cache47()
_hist_sid47 = "carry-session-47"
_hist_now47 = time.time()
_hist47 = mod._normalize_agent_history({
    "day": mod._et_day(_hist_now47), "total": 3, "session_totals": {_hist_sid47: 3},
    "rows": [
        {"id": "%s:agent:OLD-47" % _hist_sid47, "label": "pre-v30.21 row",
         "family": "Opus", "model": "opus", "t0": _hist_now47 - 900, "secs": 60,
         "outcome": "completed", "session_id": _hist_sid47,
         "day": mod._et_day(_hist_now47), "closed_at": _hist_now47 - 300},
        {"id": "%s:agent:FROZEN-47" % _hist_sid47, "label": "ran on opus 5",
         "family": "Opus 5", "model": "opus", "t0": _hist_now47 - 800, "secs": 61,
         "outcome": "completed", "session_id": _hist_sid47,
         "day": mod._et_day(_hist_now47), "closed_at": _hist_now47 - 200},
        {"id": "%s:codex:SOL-47" % _hist_sid47, "label": "sol audit",
         "family": "GPT-6 Sol", "model": "gpt-6-sol", "t0": _hist_now47 - 700,
         "secs": 62, "outcome": "completed", "session_id": _hist_sid47,
         "day": mod._et_day(_hist_now47), "closed_at": _hist_now47 - 100},
    ]}, now=_hist_now47)
_carry47 = [row[0] for row in mod._rich_history_lane_rows(
    [], [], _hist47, _hist_sid47, _hist_now47, 4)]
_drop_cache47()
check("47h a carried ledger row draws the family it froze at close - today's cache never relabels it",
      _carry47 == ["pre-v30.21 row (Opus)", "ran on opus 5 (Opus 5)",
                   "sol audit (GPT-6 Sol)"], _detail26(_carry47))

_net47 = []
_render_errors47 = []
_mtimes47 = {}

def _guard_run47(argv, *args, **kwargs):
    if _passthrough_pgrep47(argv):
        return _REAL_RUN47(argv, *args, **kwargs)
    _net47.append(("run", argv))
    raise AssertionError("a render ran a subprocess")

def _guard_popen47(argv, *args, **kwargs):
    _net47.append(("Popen", argv))
    raise AssertionError("a render spawned a process")

def _guard_urlopen47(*args, **kwargs):
    _net47.append(("urlopen", args))
    raise AssertionError("a render touched the network")

_render_sid47 = "render-session-47"
_render_now47 = time.time()
_render_panel47 = [
    _row47("opus", "live alias", background=True, history_session_id=_render_sid47),
    _row47("fable", "finished alias", done=True, history_session_id=_render_sid47),
]
_render_steps47 = [{"fam": "run", "cap": "GPT-6 Sol · lane47", "tuid": "CX47R",
                    "done": False, "k": "", "aid": "", "codex": True, "codex_bg": True,
                    "codex_model": "gpt-6-sol", "t0": _render_now47 - 40}]
_render_state47 = {"ctx_pct": 12, "ctx_model": "claude-opus-5-5",
                   "rich_session_id": _render_sid47,
                   "agent_history": mod._empty_agent_history(_render_now47)}
_nospawn_saved47 = os.environ.pop("STREAM_NO_SPAWN", None)
try:
    mod.subprocess.run = _guard_run47
    mod.subprocess.Popen = _guard_popen47
    mod.request.urlopen = _guard_urlopen47
    for _case47 in ("missing", "stale", "corrupt", "fresh"):
        _drop_cache47()
        if _case47 == "stale":
            _write_cache47(fetched_at=_render_now47 - 25 * 3600)
        elif _case47 == "corrupt":
            _write_cache47(raw="{corrupt")
        elif _case47 == "fresh":
            _write_cache47()
        _before47 = os.stat(_CACHE47).st_mtime_ns if os.path.exists(_CACHE47) else None
        for _fn47, _args47, _kw47 in (
                ("render_rich_live", (SDIR, _render_steps47, _render_now47 - 90, {}),
                 {"mission": "no network", "panel": _render_panel47, "plan": [],
                  "tools": {"items": [], "count": 0}, "state": _render_state47}),
                ("render_rich_live", (SDIR, _render_steps47, _render_now47 - 90, {}),
                 {"mission": "no network", "panel": _render_panel47, "plan": [],
                  "tools": {"items": [], "count": 0}, "fallback": True,
                  "state": _render_state47}),
                ("render_rich_final", (SDIR, _render_steps47, _render_now47 - 90),
                 {"mission": "no network", "panel": _render_panel47, "plan": [],
                  "outcome": "done", "agent_history": _render_state47["agent_history"]}),
                ("_agent_display_records",
                 (_render_steps47, _render_panel47, _render_state47["agent_history"],
                  _render_sid47), {"now": _render_now47}),
                ("_rich_lane_rows", (_render_steps47, _render_panel47),
                 {"now": _render_now47, "fold": True}),
        ):
            _value47, _err47 = _call26(_fn47, *_args47, **_kw47)
            if _err47:
                _render_errors47.append((_case47, _fn47, _err47))
        _mtimes47[_case47] = (
            _before47,
            os.stat(_CACHE47).st_mtime_ns if os.path.exists(_CACHE47) else None)
finally:
    mod.subprocess.run = _REAL_RUN47
    mod.subprocess.Popen = _REAL_POPEN47
    mod.request.urlopen = _REAL_URLOPEN47
    if _nospawn_saved47 is not None:
        os.environ["STREAM_NO_SPAWN"] = _nospawn_saved47
    _drop_cache47()
check("47i rendering with a missing, stale, corrupt or fresh cache makes no subprocess, spawn "
      "or network call, even with spawning enabled (CONTROL)",
      _net47 == [] and _render_errors47 == [],
      _detail26({"calls": _net47, "errors": _render_errors47}))
check("47i no render ever writes or claims the cache file (CONTROL)",
      len(_mtimes47) == 4 and _mtimes47["missing"] == (None, None)
      and all(before == after for before, after in _mtimes47.values()),
      _detail26(_mtimes47))

_reset26()
_enable_rich26()
_drop_cache47()
_spawned47 = []
_inline47 = []
_net47j = []

def _record_popen47(argv, *args, **kwargs):
    _spawned47.append((list(argv) if isinstance(argv, (list, tuple)) else [argv],
                       dict(kwargs)))
    return _FakeProc47()

def _record_run47(argv, *args, **kwargs):
    if _passthrough_pgrep47(argv):
        return _REAL_RUN47(argv, *args, **kwargs)
    _inline47.append(argv)
    raise AssertionError("the dispatch edge ran a subprocess inline")

def _record_urlopen47(*args, **kwargs):
    _net47j.append(args)
    raise AssertionError("the dispatch edge touched the network")

_nospawn_saved47 = os.environ.pop("STREAM_NO_SPAWN", None)
_tg47j = RichRecTG()
try:
    mod.subprocess.run = _record_run47
    mod.subprocess.Popen = _record_popen47
    mod.request.urlopen = _record_urlopen47
    _, _j_prompt_err47 = _prompt26(_tg47j, "dispatch an opus worker", "refresh-session-47")
    _, _j_pre_err47 = _step26(
        _tg47j, "pre", "Agent",
        {"description": "refresh probe", "subagent_type": "opus-executor",
         "model": "opus", "run_in_background": True},
        tuid="REFRESH-47A", session_id="refresh-session-47")
    _first47 = [c for c in _spawned47 if "models-refresh" in c[0]]
    _claim47 = (os.path.isfile(_CACHE47),
                os.path.getsize(_CACHE47) if os.path.isfile(_CACHE47) else None)
    _, _j_pre2_err47 = _step26(
        _tg47j, "pre", "Agent",
        {"description": "second probe", "subagent_type": "opus-executor",
         "model": "opus[1m]", "run_in_background": True},
        tuid="REFRESH-47B", session_id="refresh-session-47")
    _second47 = [c for c in _spawned47 if "models-refresh" in c[0]]
    _claim_lane47 = _lane47(_row47("opus", "claimed"))
finally:
    mod.subprocess.run = _REAL_RUN47
    mod.subprocess.Popen = _REAL_POPEN47
    mod.request.urlopen = _REAL_URLOPEN47
    if _nospawn_saved47 is not None:
        os.environ["STREAM_NO_SPAWN"] = _nospawn_saved47
check("47j an alias dispatch with no cache starts exactly ONE detached refresher",
      not any((_j_prompt_err47, _j_pre_err47)) and len(_first47) == 1
      and _first47[0][0][-2:] == ["models-refresh", _CACHE47]
      and _first47[0][0][1] == os.path.realpath(MOD_PATH)
      and _first47[0][1].get("start_new_session") is True
      and all(_first47[0][1].get(k) == mod.subprocess.DEVNULL
              for k in ("stdin", "stdout", "stderr")),
      _detail26({"spawned": _spawned47, "errors": (_j_prompt_err47, _j_pre_err47)}))
check("47j nothing runs inline - no curl, no security, no network - and a second dispatch "
      "inside the hour starts no second refresher",
      not _j_pre2_err47 and _inline47 == [] and _net47j == [] and len(_second47) == 1,
      _detail26({"inline": _inline47, "net": _net47j, "spawned": _spawned47}))
check("47j the claim is an empty placeholder, which every render reads as no cache",
      _claim47 == (True, 0) and _claim_lane47 == "claimed (Opus)",
      _detail26({"claim": _claim47, "lane": _claim_lane47}))
_drop_cache47()

_reset26()
_enable_rich26()
_drop_cache47()
_sid47k = "claude-lane-session-47"
_clock47 = _Clock26(time.time())
_real_time47 = mod.time.time
_agent_in47 = {"description": "Booklet audit", "subagent_type": "opus-executor",
               "model": "opus", "run_in_background": True}
_k_errs47 = []
try:
    mod.time.time = _clock47.time
    _write_cache47()
    _tg47k = RichRecTG()
    _k_errs47.append(_prompt26(_tg47k, "dispatch the booklet audit", _sid47k)[1])
    _clock47.advance(2)
    _k_errs47.append(_step26(_tg47k, "pre", "Agent", _agent_in47, tuid="AGENT-47K",
                             session_id=_sid47k)[1])
    _k_pre47 = load()
    _pre_frame47k = _text26(_render_live26(
        SDIR, _k_pre47.get("steps") or [], _k_pre47.get("proc_start"), {},
        mission="Booklet", panel=_k_pre47.get("panel"), plan=[],
        tools={"items": [], "count": 0}, state=_k_pre47))
    _clock47.advance(1)
    _k_errs47.append(_step26(
        _tg47k, "post", "Agent", _agent_in47, tuid="AGENT-47K",
        tresponse={"isAsync": True, "status": "async_launched", "agentId": "a47k",
                   "description": "Booklet audit",
                   "resolvedModel": "claude-opus-5-5[1m]"},
        session_id=_sid47k)[1])
    _row47k = dict(_panel_row31(load(), "AGENT-47K"))
    _drop_cache47()
    _clock47.advance(30)
    _k_live47 = load()
    _post_frame47k = _text26(_render_live26(
        SDIR, _k_live47.get("steps") or [], _k_live47.get("proc_start"), {},
        mission="Booklet", panel=_k_live47.get("panel"), plan=[],
        tools={"items": [], "count": 0}, state=_k_live47))
    _plain_frame47k = _text26(_render_live26(
        SDIR, _k_live47.get("steps") or [], _k_live47.get("proc_start"), {},
        mission="Booklet", panel=_k_live47.get("panel"), plan=[],
        tools={"items": [], "count": 0}, fallback=True, state=_k_live47))
    _clock47.advance(60)
    _k_errs47.append(_prompt26(_tg47k, _agent_notification31(
        "AGENT-47K", 'Agent "Booklet audit" finished', task_id="agent-task-47k"),
        _sid47k)[1])
    _after47k = load()
    _hist47k = [r for r in ((_after47k.get("agent_history") or {}).get("rows") or [])
                if str(r.get("id") or "").endswith("AGENT-47K")]
    _next_frame47k = _text26(_render_live26(
        SDIR, _after47k.get("steps") or [], _after47k.get("proc_start"), {},
        mission="Next question", panel=_after47k.get("panel"), plan=[],
        tools={"items": [], "count": 0}, state=_after47k))
    _fold47k = _text26(mod.render_rich_final(
        SDIR, [{"fam": "helpers", "cap": "Booklet audit", "tuid": "AGENT-47K",
                "done": True, "k": "subtitle", "aid": ""},
               {"fam": "files", "cap": "reading the booklet", "tuid": "R47K",
                "done": True, "k": "", "aid": ""}],
        _k_pre47.get("proc_start"), mission="Booklet",
        panel=[dict(_row47k, done=True, secs=93.0, outcome="completed")], plan=[],
        outcome="done", agent_history=_after47k.get("agent_history")))
finally:
    mod.time.time = _real_time47
check("47k an alias dispatch shows 'Opus 5.5' from the cache the instant it opens",
      not any(_k_errs47) and "| ◉ Booklet audit (Opus 5.5) |" in _pre_frame47k,
      _detail26({"errors": _k_errs47, "frame": _pre_frame47k}))
check("47k the PostToolUse stamps the served id on the row, and the label no longer needs "
      "the cache - live and plain fallback both",
      _row47k.get("srv") == "claude-opus-5-5[1m]"
      and "| ◉ Booklet audit (Opus 5.5) |" in _post_frame47k
      and "- ◉ Booklet audit (Opus 5.5) · " in _plain_frame47k
      and mod._rich_markup_balanced(_post_frame47k) is True
      and mod._rich_frame_fits(_post_frame47k) is True,
      _detail26({"row": _row47k, "live": _post_frame47k, "plain": _plain_frame47k}))
check("47k the close freezes 'Opus 5.5' into the ledger and the next box's carried row reads it",
      len(_hist47k) == 1 and _hist47k[0].get("family") == "Opus 5.5"
      and "Booklet audit (Opus 5.5)" in _next_frame47k,
      _detail26({"history": _hist47k, "next": _next_frame47k}))
check("47k the fold names the model too",
      "| ✓ Booklet audit (Opus 5.5) |" in _fold47k
      and mod._rich_markup_balanced(_fold47k) is True,
      _detail26(_fold47k))

_reset26()
_enable_rich26()
_codex_home47("none")
_sid47l = "codex-lane-session-47"
_tg47l = RichRecTG()
_codex_in47 = {
    "command": ('S="/private/tmp/tgstream-47"\n'
                "/opt/homebrew/bin/codex exec -m gpt-6-sol -c model_reasoning_effort=max "
                "--sandbox read-only --skip-git-repo-check --color never "
                '-o "$S/x-report.md" - < "$S/x-brief.txt" > "$S/x-run.log" 2>&1'),
    "description": "Sol lane", "run_in_background": True}
_l_errs47 = [_prompt26(_tg47l, "dispatch sol", _sid47l)[1]]
_l_errs47.append(_step26(_tg47l, "pre", "Bash", _codex_in47, tuid="CX47L",
                         session_id=_sid47l)[1])
_l_errs47.append(_step26(_tg47l, "post", "Bash", _codex_in47, tuid="CX47L",
                         session_id=_sid47l)[1])
_st47l = load()
_step47l = next((dict(s) for s in (_st47l.get("steps") or [])
                 if s.get("tuid") == "CX47L"), {})
_l_now47 = time.time()
_live47l = _text26(_render_live26(
    SDIR, _st47l.get("steps") or [], _st47l.get("proc_start"), {}, mission="Sol",
    panel=_st47l.get("panel"), plan=[], tools={"items": [], "count": 0}, state=_st47l))
_plain47l = _text26(_render_live26(
    SDIR, _st47l.get("steps") or [], _st47l.get("proc_start"), {}, mission="Sol",
    panel=_st47l.get("panel"), plan=[], tools={"items": [], "count": 0},
    fallback=True, state=_st47l))
_tree47l = mod._agent_tree_lines(mod._agent_display_records(
    _st47l.get("steps") or [], _st47l.get("panel") or [], _st47l.get("agent_history"),
    _sid47l, now=_l_now47)[0])
_l_errs47.append(_prompt26(_tg47l, _done_notification22("CX47L", "task-47l"),
                           _sid47l)[1])
_after47l = load()
_hist47l = [r for r in ((_after47l.get("agent_history") or {}).get("rows") or [])
            if str(r.get("id") or "").endswith("CX47L")]
_next47l = _text26(_render_live26(
    SDIR, _after47l.get("steps") or [], _after47l.get("proc_start"), {},
    mission="Next", panel=_after47l.get("panel"), plan=[],
    tools={"items": [], "count": 0}, state=_after47l))
_fold47l = _text26(mod.render_rich_final(
    SDIR, [dict(_step47l, done=True, secs=75.0, codex_terminal_status="completed")],
    _st47l.get("proc_start"), mission="Sol", panel=[], plan=[], outcome="done",
    agent_history=_after47l.get("agent_history")))
check("47l a background codex exec -m gpt-6-sol records its model and reads 'GPT-6 Sol · x'",
      not any(_l_errs47) and _step47l.get("cap") == "GPT-6 Sol · x"
      and _step47l.get("codex_model") == "gpt-6-sol" and _step47l.get("codex_bg") is True,
      _detail26({"errors": _l_errs47, "step": _step47l}))
check("47l the live table, the plain fallback and the roster all say 'x (GPT-6 Sol)'",
      "| ◉ x (GPT-6 Sol) |" in _live47l
      and "- ◉ x (GPT-6 Sol) · " in _plain47l
      and any("x (GPT-6 Sol) · running" in line for line in _tree47l),
      _detail26({"live": _live47l, "plain": _plain47l, "tree": _tree47l}))
check("47l the completed lane is ledgered as GPT-6 Sol and folds as GPT-6 Sol",
      len(_hist47l) == 1 and _hist47l[0].get("family") == "GPT-6 Sol"
      and _hist47l[0].get("model") == "gpt-6-sol" and _hist47l[0].get("label") == "x"
      and "| ✓ x (GPT-6 Sol) |" in _fold47l,
      _detail26({"history": _hist47l, "fold": _fold47l}))
check("47l the next box's carried row still reads 'x (GPT-6 Sol)'",
      "x (GPT-6 Sol)" in _next47l, _detail26(_next47l))

try:

    _write_cache47()
    _fam47 = {
        "opus": mod._agent_family("opus", "", live=True),
        "fable": mod._agent_family("fable", "", live=True),
        "opus1m": mod._agent_family("opus[1m]", "", live=True),
        "sonnet": mod._agent_family("sonnet", "", live=True),
        "haiku": mod._agent_family("haiku", "", live=True),
        "mythos": mod._agent_family("mythos", "", live=True),
        "not_live": mod._agent_family("opus", ""),
        "served": mod._agent_family("opus", "", served="claude-opus-4-8[1m]", live=True),
        "type_only": mod._agent_family("", "fable-executor", live=True),
        "inherit": mod._agent_family("inherit", "opus-executor", live=True),
        "opusplan": mod._agent_family("opusplan", "", live=True),
        "codex": mod._agent_family("gpt-6-astra", "worker"),
        "legacy_codex": mod._agent_family("Codex", "Codex"),
    }
    _drop_cache47()
    _fam47["no_cache"] = mod._agent_family("opus", "", live=True)
    check("47m _agent_family: served id, then a named version, then the cache for a LIVE alias, "
          "then the bare family",
          _fam47 == {"opus": "Opus 5.5", "fable": "Fable 5.1", "opus1m": "Opus 5.5",
                     "sonnet": "Sonnet 5", "haiku": "Haiku 4.5", "mythos": "Mythos",
                     "not_live": "Opus", "served": "Opus 4.8", "type_only": "Fable",
                     "inherit": "Opus", "opusplan": "Opus", "codex": "GPT-6 Astra",
                     "legacy_codex": "Codex", "no_cache": "Opus"},
          _detail26(_fam47))

    _cl47 = [mod._claude_version_label(v) for v in (
        "claude-opus-5-5[1m]", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5",
        "claude-haiku-4-5-20251001", "claude-opus-4-5-20251101", "opus", "opus[1m]",
        "<synthetic>", "gpt-6-sol", "", None, 7, "claude-opus-" + "5" * 80)]
    check("47n _claude_version_label reuses the context-line formatter and proves a version or "
          "returns ''",
          _cl47 == ["Opus 5.5", "Opus 5.5", "Fable 5.1", "Sonnet 5", "Haiku 4.5", "Opus 4.5",
                    "", "", "", "", "", "", "", ""], _detail26(_cl47))
    _cx47 = [mod._codex_model_label(v) for v in (
        "gpt-6-sol", "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5",
        "gpt-5.3-codex-spark", "gpt-4o-2024-08-06", "codex-mini", "codex", "o3", "",
        None, "gpt-" + "9" * 80)]
    check("47n _codex_model_label is one generic rule: gpt-<ver>-<name>, gpt-<ver>, codex-*, "
          "else Codex",
          _cx47 == ["GPT-6 Sol", "GPT-6 Astra", "GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.5",
                    "GPT-5.3 Codex Spark", "GPT-4o", "Codex Mini", "Codex", "Codex",
                    "Codex", "Codex", "Codex"], _detail26(_cx47))

    _drop_cache47()
    _o_missing = mod._models_cache_models(SDIR)
    _write_cache47()
    _o_good = mod._models_cache_models(SDIR)
    _o_newest = [mod._models_cache_newest(f, SDIR) for f in
                 ("opus", "fable", "sonnet", "haiku", "mythos", "gpt")]
    _write_cache47(fetched_at=time.time() - mod.MODELS_CACHE_TTL - 5)
    _o_stale = mod._models_cache_models(SDIR)
    _drop_cache47()
    check("47o the cache reader returns the fresh list and nothing else; newest is the highest "
          "created_at per family word",
          _o_missing == [] and len(_o_good) == len(_MODELS47) and _o_stale == []
          and _o_newest == ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5",
                            "claude-haiku-4-5-20251001", "", ""]
          and mod.MODELS_CACHE_FILE == "models-cache.json"
          and mod.MODELS_CACHE_TTL == 24 * 3600,
          _detail26({"good": _o_good, "newest": _o_newest}))
    check("47o the parse is memoised on the file's identity, bounded to one entry",
          len(mod._MODELS_CACHE_MEMO) <= 1, _detail26(list(mod._MODELS_CACHE_MEMO)))

    _spawn47p = []

    def _popen47p(argv, *args, **kwargs):
        _spawn47p.append(("SPAWN", list(argv), dict(kwargs)))
        return _FakeProc47()

    def _run47p(argv, *args, **kwargs):
        if _passthrough_pgrep47(argv):
            return _REAL_RUN47(argv, *args, **kwargs)
        _spawn47p.append(("INLINE", argv, dict(kwargs)))
        raise AssertionError("the refresh policy ran a subprocess inline")

    _nospawn_saved47 = os.environ.get("STREAM_NO_SPAWN")
    try:
        mod.subprocess.Popen = _popen47p
        mod.subprocess.run = _run47p
        _drop_cache47()
        os.environ["STREAM_NO_SPAWN"] = "1"
        _p_nospawn = mod._models_cache_maybe_refresh(SDIR)
        _p_nospawn_file = os.path.lexists(_CACHE47)
        os.environ.pop("STREAM_NO_SPAWN", None)
        _p_missing = mod._models_cache_maybe_refresh(SDIR)
        _p_missing_file = (os.path.isfile(_CACHE47),
                           os.path.getsize(_CACHE47) if os.path.isfile(_CACHE47) else None)
        _p_again = mod._models_cache_maybe_refresh(SDIR)
        _old47 = time.time() - 2 * 3600
        _write_cache47()
        os.utime(_CACHE47, (_old47, _old47))
        _p_fresh = mod._models_cache_maybe_refresh(SDIR)
        _write_cache47(fetched_at=time.time() - 30 * 3600)
        os.utime(_CACHE47, (_old47, _old47))
        _p_stale = mod._models_cache_maybe_refresh(SDIR)
        _p_stale_mtime = os.stat(_CACHE47).st_mtime
        _p_stale_again = mod._models_cache_maybe_refresh(SDIR)
        _drop_cache47()
        os.makedirs(_CACHE47)
        _p_dir = mod._models_cache_maybe_refresh(SDIR)
        _drop_cache47()
    finally:
        mod.subprocess.Popen = _REAL_POPEN47
        mod.subprocess.run = _REAL_RUN47
        if _nospawn_saved47 is not None:
            os.environ["STREAM_NO_SPAWN"] = _nospawn_saved47
        _drop_cache47()
    _spawns47p = [c for c in _spawn47p if c[0] == "SPAWN"]
    check("47p STREAM_NO_SPAWN=1 disables the refresher outright and leaves no claim file",
          _p_nospawn is False and _p_nospawn_file is False,
          _detail26((_p_nospawn, _p_nospawn_file)))
    check("47p a missing cache is claimed as an empty file and refreshed once; asking again "
          "inside the hour starts nothing",
          _p_missing is True and _p_missing_file == (True, 0) and _p_again is False,
          _detail26((_p_missing, _p_missing_file, _p_again)))
    check("47p a fresh cache is left alone, a stale one is claimed and refreshed once, a "
          "directory is never fought",
          _p_fresh is False and _p_stale is True
          and abs(_p_stale_mtime - time.time()) < 120 and _p_stale_again is False
          and _p_dir is False,
          _detail26((_p_fresh, _p_stale, _p_stale_mtime, _p_stale_again, _p_dir)))
    check("47p every spawn is the detached models-refresh process and nothing ever ran inline",
          len(_spawns47p) == 2
          and all(c[1][-2:] == ["models-refresh", _CACHE47]
                  and c[2].get("start_new_session") is True for c in _spawns47p)
          and not [c for c in _spawn47p if c[0] == "INLINE"],
          _detail26(_spawn47p))

    _FAKE_KEY47 = "fake-keychain-value-47-" + "0" * 24
    _API_BODY47 = json.dumps({
        "data": [dict(m, type="model", display_name="Model %d" % i)
                 for i, m in enumerate(_MODELS47)],
        "has_more": False, "first_id": "claude-opus-5-5",
        "last_id": "claude-sonnet-4-5-20250929"}).encode("utf-8")
    _q_calls = []
    _q_curl = {"rc": 0, "body": _API_BODY47}

    class _Result47:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def _run47q(argv, *args, **kwargs):
        _q_calls.append((list(argv), kwargs.get("input"), kwargs.get("timeout")))
        if argv[0] == "/usr/bin/security":
            return _Result47(0, (_FAKE_KEY47 + "\n").encode("utf-8"))
        if argv[0] == "/usr/bin/curl":
            return _Result47(_q_curl["rc"], _q_curl["body"])
        raise AssertionError("unexpected subprocess %r" % (argv,))

    try:
        mod.subprocess.run = _run47q
        _drop_cache47()
        _q_ok = mod._models_refresh_main(_CACHE47)
        with open(_CACHE47) as fh:
            _q_text = fh.read()
        _q_written = json.loads(_q_text)
        _q_curl["rc"] = 22
        _q_fail = mod._models_refresh_main(_CACHE47)
        with open(_CACHE47) as fh:
            _q_after_fail = fh.read()
        _q_curl["rc"], _q_curl["body"] = 0, (
            b'{"type": "error", "error": {"type": "authentication_error"}}')
        _q_env = mod._models_refresh_main(_CACHE47)
        with open(_CACHE47) as fh:
            _q_after_env = fh.read()
        _q_before_wrong = len(_q_calls)
        _q_wrong = mod._models_refresh_main(os.path.join(SDIR, "state.json"))
        _q_wrong_calls = len(_q_calls) - _q_before_wrong
    finally:
        mod.subprocess.run = _REAL_RUN47
    _q_security = [c for c in _q_calls if c[0][0] == "/usr/bin/security"]
    _q_curls = [c for c in _q_calls if c[0][0] == "/usr/bin/curl"]
    _q_strays = [n for n in os.listdir(SDIR) if n.startswith("models-cache.json.tmp")]
    check("47q the refresher writes {fetched_at, source, models:[{id, created_at}]} and the "
          "key is nowhere in the file",
          _q_ok is True
          and sorted(_q_written) == ["fetched_at", "models", "source"]
          and _q_written["models"] == _MODELS47
          and abs(_q_written["fetched_at"] - time.time()) < 60
          and _q_written["source"] == "https://api.anthropic.com/v1/models"
          and _FAKE_KEY47 not in _q_text,
          _detail26(_q_written))
    check("47q the key comes from the keychain and reaches curl on STDIN, never in any argv",
          len(_q_security) == 3 and all(c[0] == [
              "/usr/bin/security", "find-generic-password",
              "-s", "nexera-telebots-judge", "-w"] for c in _q_security)
          and len(_q_curls) == 3
          and all(_FAKE_KEY47 not in " ".join(c[0]) for c in _q_calls)
          and all(c[0][-2:] == ["--config", "-"] and "--max-time" in c[0]
                  and (c[2] or 0) > 0 for c in _q_curls)
          and all(("x-api-key: " + _FAKE_KEY47).encode() in (c[1] or b"")
                  and b"anthropic-version: 2023-06-01" in (c[1] or b"")
                  and b"https://api.anthropic.com/v1/models?limit=100" in (c[1] or b"")
                  for c in _q_curls),
          _detail26([(c[0], bool(c[1]), c[2]) for c in _q_calls]))
    check("47q a failed fetch or an error envelope writes nothing and keeps the last good cache",
          _q_fail is False and _q_after_fail == _q_text
          and _q_env is False and _q_after_env == _q_text and _q_strays == [],
          _detail26({"fail": _q_fail, "env": _q_env, "strays": _q_strays}))
    check("47q it refuses any path not named models-cache.json before touching the keychain",
          _q_wrong is False and _q_wrong_calls == 0, _detail26((_q_wrong, _q_wrong_calls)))
    _drop_cache47()
    _q_proc = _REAL_RUN47(
        [sys.executable, os.path.realpath(MOD_PATH), "models-refresh",
         os.path.join(SDIR, "no-such-dir", "models-cache.json")],
        stdin=mod.subprocess.DEVNULL, stdout=mod.subprocess.PIPE,
        stderr=mod.subprocess.PIPE, timeout=30)
    check("47q the models-refresh mode exits cleanly ahead of every channel gate and writes "
          "nothing when its folder is missing",
          _q_proc.returncode == 0 and _q_proc.stdout == b"" and _q_proc.stderr == b""
          and not os.path.lexists(os.path.join(SDIR, "no-such-dir")),
          _detail26((_q_proc.returncode, _q_proc.stdout[:200], _q_proc.stderr[:200])))

    _panel47r = [{"tuid": "R1", "done": False}, {"tuid": "R2", "done": True},
                 {"tuid": "", "done": False}]
    _r_ok = mod._panel_record_served_model(_panel47r, "R1",
                                           {"resolvedModel": "claude-fable-5-1"})
    _r_done = mod._panel_record_served_model(_panel47r, "R2",
                                             {"resolvedModel": "claude-opus-5-5"})
    _r_junk = [mod._panel_record_served_model([{"tuid": "J", "done": False}], "J", resp)
               for resp in ({"resolvedModel": "opus"}, {"resolvedModel": "<synthetic>"},
                            {"resolvedModel": "claude-opus-" + "5" * 80},
                            {"resolvedModel": 5}, {}, "resolvedModel: claude-opus-5-5",
                            None)]
    _r_idless = mod._panel_record_served_model(
        _panel47r, None, {"resolvedModel": "claude-opus-5-5[1m]"})
    check("47r the served id lands only on the matching OPEN row, and only as a versioned "
          "Claude id",
          _r_ok is True and _panel47r[0].get("srv") == "claude-fable-5-1"
          and _r_done is False and "srv" not in _panel47r[1]
          and _r_junk == [False] * 7
          and _r_idless is True and _panel47r[2].get("srv") == "claude-opus-5-5[1m]",
          _detail26({"panel": _panel47r, "junk": _r_junk}))
except Exception as _exc47:
    check("47 SECTION ABORTED - this build does not carry the v30.21 versioned lane labels",
          False, "%s: %s" % (type(_exc47).__name__, str(_exc47)[:400]))
finally:
    mod.subprocess.run = _REAL_RUN47
    mod.subprocess.Popen = _REAL_POPEN47
    mod.request.urlopen = _REAL_URLOPEN47
    os.environ["STREAM_NO_SPAWN"] = "1"
    if _CODEX_HOME_HARNESS47:
        os.environ["CODEX_HOME"] = _CODEX_HOME_HARNESS47
    _drop_cache47()
    _reset26()

print("\n===== #48 public configuration and local-day contract =====")
_metadata_node48 = ast.parse("def fixture():\n    return 1\n").body[0]
_metadata_hash48 = _behavior_hash(_metadata_node48)
_metadata_node48.type_params = []
_metadata_node48._fields = tuple(_metadata_node48._fields) + ("type_params",)
check("48a empty Python declaration metadata cannot change an AST golden",
      _behavior_hash(_metadata_node48) == _metadata_hash48)
check("48a no fixed bot identities remain", mod.BOT_IDENTITIES == {})
check("48a the date resolver uses the system local timezone", mod.ET_ZONE is None)
_saved_tz48 = os.environ.get("TZ")
_days48 = {}
try:
    for _zone48 in ("UTC", "Pacific/Honolulu", "Asia/Tokyo"):
        os.environ["TZ"] = _zone48
        time.tzset()
        _days48[_zone48] = mod._et_day(1609459200)
finally:
    if _saved_tz48 is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _saved_tz48
    time.tzset()
check("48a day boundaries follow the configured local timezone",
      _days48 == {"UTC": "2021-01-01", "Pacific/Honolulu": "2020-12-31",
                  "Asia/Tokyo": "2021-01-01"}, repr(_days48))

_saved_nospawn48 = os.environ.pop("STREAM_NO_SPAWN", None)
_saved_state48 = os.environ.get("TELEGRAM_STATE_DIR")
_saved_run48, _saved_popen48 = mod.subprocess.run, mod.subprocess.Popen
_calls48 = []


def _no_io48(*args, **kwargs):
    _calls48.append((args, kwargs))
    raise AssertionError("Disabled model extras attempted external I/O")


try:
    mod.subprocess.run = _no_io48
    mod.subprocess.Popen = _no_io48
    _drop_cache47()
    _disabled_results48 = []
    for _option48 in (False, None, "true", 1):
        _write_fleet_config(judge=_option48)
        _disabled_results48.append((mod._models_cache_maybe_refresh(SDIR),
                                    mod._models_refresh_main(_CACHE47)))
    check("48b model extras are off unless the config explicitly contains true",
          _disabled_results48 == [(False, False)] * 4
          and _calls48 == [] and not os.path.lexists(_CACHE47),
          repr(_disabled_results48))
    _write_fleet_config(judge=True)
    os.environ.pop("TELEGRAM_STATE_DIR", None)
    check("48b the detached refresher is inert outside a bot session",
          mod._models_refresh_main(_CACHE47) is False and _calls48 == [])
    os.environ["TELEGRAM_STATE_DIR"] = _saved_state48

    def _missing_key48(argv, *args, **kwargs):
        _calls48.append(list(argv))
        if list(argv) != ["/usr/bin/security", "find-generic-password", "-s",
                          "nexera-telebots-judge", "-w"]:
            raise AssertionError("A missing optional key must never reach the network")
        return types.SimpleNamespace(returncode=44, stdout=b"")

    mod.subprocess.run = _missing_key48
    _calls48.clear()
    check("48c an absent optional key makes no fetch and writes no cache",
          mod._models_refresh_main(_CACHE47) is False
          and len(_calls48) == 1 and not os.path.lexists(_CACHE47))
    check("48c optional key lookup has no account pin",
          mod.MODELS_KEYCHAIN_ACCOUNT == ""
          and mod.MODELS_KEYCHAIN_SERVICE == "nexera-telebots-judge"
          and "-a" not in _calls48[0])
finally:
    mod.subprocess.run, mod.subprocess.Popen = _saved_run48, _saved_popen48
    if _saved_nospawn48 is not None:
        os.environ["STREAM_NO_SPAWN"] = _saved_nospawn48
    if _saved_state48 is not None:
        os.environ["TELEGRAM_STATE_DIR"] = _saved_state48
    _write_fleet_config()
    _drop_cache47()

print("\n" + "=" * 60)
if FAILS:
    print("FAILED: %d" % len(FAILS))
    for f in FAILS:
        print("   - " + f)
else:
    print("ALL PASS: %d checks; %d explicit skip" % (CHECK_COUNT, len(SKIPS)))
print("=" * 60)

try:
    shutil.rmtree(STATE_DIR)
except OSError:
    pass

sys.exit(1 if FAILS else 0)
