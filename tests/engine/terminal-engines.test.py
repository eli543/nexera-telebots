"""Offline checks for the Terminal engines: window lookups, settled looks, the lone
Esc, stranded box text, a stalled Terminal, and a bounded window open.

Nothing here reaches Terminal.app. Each engine runs from a copy whose absolute
osascript path points at a fake Terminal in a private shims directory, next to
fake ps, lsof and curl. The only terminal any engine touches is a pty this test
opens itself, and every process the test signals is one it started.
"""

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unittest


REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "engine"
SANDBOX = Path(os.environ.get("TELEBOTS_SANDBOX_ROOT") or tempfile.gettempdir())
ON_MAC = sys.platform == "darwin"

RULE = "─" * 40
NO_TAB_RE = re.compile(r'return "(__[A-Z_]+__)"')

# The exact window walk every Terminal lookup must use: one snapshot of the window
# ids, one try per window. Walking `windows` by index skips a window raised mid-walk.
INDEX_WALK = (
    "set windowIds to id of every window",
    "repeat with i from 1 to count of windowIds",
    "set w to window id (item i of windowIds)",
    "repeat with oneTab in tabs of w",
)

FAKE_TERMINAL = r'''#!/usr/bin/python3
"""Stand-in for /usr/bin/osascript: a fake Terminal driven by files in $FAKE."""
import json, os, re, subprocess, sys, time

FAKE = os.environ["FAKE"]


def path(name):
    return os.path.join(FAKE, name)


def has(name):
    return os.path.exists(path(name))


def read(name, default=""):
    try:
        with open(path(name), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return default


def write(name, text):
    with open(path(name), "w", encoding="utf-8") as fh:
        fh.write(text)


def event(kind, **extra):
    extra.update(kind=kind, t=time.time())
    with open(path("events.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(extra) + "\n")


def hang(kind):
    with open(path("hung-pids"), "a") as fh:
        fh.write("%d\n" % os.getpid())
    event(kind + "-hang")
    time.sleep(float(read("hang-seconds", "8").strip() or 8))
    sys.exit(1)


def terminal_down():
    mode = read("terminal-down").strip()
    if mode == "hang":
        hang("terminal")
    if mode:
        sys.stderr.write("execution error: Terminal got an error: AppleEvent timed out. (-1712)\n")
        sys.exit(1)


def start_session():
    mode_file = os.environ.get("RESTORE_MODE_FILE")
    if mode_file:
        with open(mode_file, "w") as fh:
            fh.write("up")
        with open(os.environ["RESTORE_HEARTBEAT"], "w") as fh:
            fh.write(str(int(time.time())))
        return
    child = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    with open(path("spawned-pids"), "a") as fh:
        fh.write("%d\n" % child.pid)
    procs = json.loads(read("procs.json", "{}"))
    procs[str(child.pid)] = {
        "comm": "claude", "tty": "??", "cwd": os.environ["FAKE_WORKDIR"],
        "lstart": "Mon Nov  2 22:00:00 2026",
        "command": "claude --channels plugin:telegram@claude-plugins-official --name telebots-main",
    }
    write("procs.json", json.dumps(procs))


args = sys.argv[1:]
if args and args[0] == "-":
    script = sys.stdin.read()
    argv = args[1:]
else:
    parts, i = [], 0
    while i < len(args) and args[i] == "-e":
        parts.append(args[i + 1])
        i += 2
    if i < len(args) and args[i] == "--":
        i += 1
    script = "\n".join(parts)
    argv = args[i:]
os.makedirs(path("scripts"), exist_ok=True)
with open(path("scripts/%d-%d.applescript" % (time.time_ns(), os.getpid())), "w") as fh:
    fh.write(script)

if "keystroke theText" in script or "key code 53" in script:
    event("keystroke", argv=argv)
    terminal_down()
    if has("keystroke-hangs"):
        hang("keystroke")
    print(read("keystroke-result", "OK").strip() or "OK")
elif "frontmost is true" in script and "Terminal" not in script:
    print("Finder")
elif "set frontmost of process" in script:
    event("restore-focus")
elif script.strip() == 'tell application "Terminal" to activate':
    event("activate")
    if has("activate-hangs"):
        hang("activate")
elif 'tell application "Terminal" to count windows' in script:
    event("terminal-check")
    terminal_down()
    print(3)
elif "do script (item 1 of argv)" in script:
    event("open", argv=argv)
    plan = json.loads(read("open-plan", '["ok-up"]'))
    step = plan.pop(0) if len(plan) > 1 else plan[0]
    write("open-plan", json.dumps(plan))
    if step == "hang":
        hang("open")
    if step in ("ok-up", "late"):
        start_session()
    if step in ("late", "fail"):
        sys.stderr.write("execution error: Terminal got an error: AppleEvent timed out. (-1712)\n")
        sys.exit(1)
    print("tab 1 of window id 42")
elif "history of oneTab" in script or "history of" in script:
    event("read")
    terminal_down()
    if has("no-tab"):
        print(re.search(r'return "(__[A-Z_]+__)"', script).group(1))
        sys.exit(0)
    frames = json.loads(read("frames.json", "[]"))
    frame = frames[0] if frames else ""
    if len(frames) > 1:
        frames.pop(0)
        write("frames.json", json.dumps(frames))
    sys.stdout.write(frame)
elif "ASCII character 3" in script:
    event("ctrl-c")
    terminal_down()
    if has("after-ctrl-c"):
        write("frames.json", json.dumps([read("after-ctrl-c")]))
    print("OK")
elif "do script theText in oneTab" in script:
    event("text", text=argv[1] if len(argv) > 1 else "")
    terminal_down()
    if has("deliver-nowindow"):
        print("NOWINDOW")
        sys.exit(0)
    if has("started-flag"):
        with open(read("started-flag").strip(), "w") as fh:
            fh.write("{}")
    print("OK")
elif 'do script "" in oneTab' in script:
    event("return")
    print("OK")
elif "do script killer in oneTab" in script:
    event("clear")
    print("OK")
else:
    event("unknown")
    sys.exit(2)
'''

FAKE_PS = r'''#!/usr/bin/python3
"""Fake ps: answers for the test's fake processes, real /bin/ps for anything else."""
import json, os, sys

FAKE = os.environ["FAKE"]
try:
    procs = json.load(open(os.path.join(FAKE, "procs.json")))
except (OSError, ValueError):
    procs = {}


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


args = sys.argv[1:]
if args[:1] == ["-axo"]:
    # A real process table is never empty, and the engines read an empty one as a failure.
    print("1 /sbin/launchd")
    for pid, info in procs.items():
        if not alive(pid):
            continue
        if args[1] == "pid=,comm=":
            print(pid, info["comm"])
        elif args[1] == "pid=,command=":
            print(pid, info["command"])
    sys.exit(0)
pid = args[args.index("-p") + 1] if "-p" in args else None
if pid in procs:
    if not alive(pid):
        sys.exit(1)
    fields = [args[i + 1].rstrip("=") for i, a in enumerate(args) if a == "-o"]
    print(" ".join(str(procs[pid].get(f, "")) for f in fields))
    sys.exit(0)
os.execv("/bin/ps", ["/bin/ps"] + args)
'''

FAKE_LSOF = r'''#!/usr/bin/python3
"""Fake lsof: the cwd of the test's fake processes, nothing for anything else."""
import json, os, sys

FAKE = os.environ["FAKE"]
try:
    procs = json.load(open(os.path.join(FAKE, "procs.json")))
except (OSError, ValueError):
    procs = {}
args = sys.argv[1:]
pid = args[args.index("-p") + 1] if "-p" in args else None
info = procs.get(pid)
if not info:
    sys.exit(1)
try:
    os.kill(int(pid), 0)
except ProcessLookupError:
    sys.exit(1)
except PermissionError:
    pass
if "-Fn" in args:
    print("p%s\nfcwd\nn%s" % (pid, info["cwd"]))
else:
    print("COMMAND PID USER FD TYPE NAME")
    print("claude %s user cwd DIR %s" % (pid, info["cwd"]))
'''

FAKE_CURL = r'''#!/usr/bin/python3
"""Fake curl: records the Telegram text it was asked to send, never the token."""
import json, os, sys, time

sys.stdin.read()
args = sys.argv[1:]
text = None
for i, arg in enumerate(args[:-1]):
    if arg == "--data-urlencode" and args[i + 1].startswith("text="):
        text = args[i + 1][5:]
with open(os.path.join(os.environ["FAKE"], "messages.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"text": text, "t": time.time()}) + "\n")
print('{"ok":true,"result":{"message_id":1}}')
'''

# Runs the engine as the leader of a new session whose controlling terminal is
# the test's own pty, the way an engine started from inside a bot session has
# that bot's terminal.
IN_SESSION = (
    "import fcntl, os, sys, termios\n"
    "os.setsid()\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "fcntl.ioctl(fd, termios.TIOCSCTTY, 0)\n"
    "os.close(fd)\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


def idle_frame():
    return "earlier output\n" + RULE + "\n❯ \n" + RULE + "\n  ⏵⏵ accept edits on (shift+tab to cycle)\n"


def typed_frame(*rows):
    body = "\n".join(["❯ " + rows[0]] + ["  " + r for r in rows[1:]])
    return "earlier output\n" + RULE + "\n" + body + "\n" + RULE + "\n  ⏵⏵ accept edits on (shift+tab to cycle)\n"


def busy_frame():
    return ("✶ Working…\n" + RULE + "\n❯ \n" + RULE +
            "\n  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt\n")


def picker_frame():
    return ("Select model\n\n ❯ 1. Default (recommended)\n   2. Opus\n   3. Haiku\n\n"
            " Enter to confirm · Esc to exit\n")


def hint_frame():
    # One ctrl-c on an empty box swaps the footer for this hint: no footer, no box.
    return "earlier output\n" + RULE + "\n❯ \n" + RULE + "\n  Press Ctrl-C again to exit\n"


def torn_frame():
    return "earlier output\n" + RULE + "\n❯ \n─────── half drawn\n"


class EngineHarness(unittest.TestCase):
    """One disposable fleet with one bot, fake Mac commands and engine copies."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="term-", dir=SANDBOX)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fleet = self.home / ".claude/telegram-fleet"
        self.work = self.root / "work"
        self.state = self.root / "state"
        self.shims = self.root / "shims"
        self.tmp = self.root / "tmp"
        self.fake = self.root / "fake"
        for path in (self.fleet / "bin", self.work, self.state / "logs", self.shims, self.tmp, self.fake,
                     self.home / ".claude/sessions"):
            path.mkdir(parents=True, exist_ok=True)
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [{
            "key": "main", "name": "Main", "workdir": str(self.work), "state_dir": str(self.state)}]}))
        (self.state / "access.json").write_text('{"dmPolicy":"allowlist","allowFrom":["123"]}\n')
        (self.state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-sandbox-only\n")
        for name in ("fleet-config", "fleet-telegram-send.sh", "fleet-launch"):
            shutil.copy2(ENGINE / name, self.fleet / "bin" / name)
        for name, body in (("osascript", FAKE_TERMINAL), ("ps", FAKE_PS), ("lsof", FAKE_LSOF), ("curl", FAKE_CURL)):
            shim = self.shims / name
            shim.write_text(body)
            shim.chmod(0o700)
        (self.fake / "procs.json").write_text("{}")
        self.children = []
        self.addCleanup(self.cleanup)
        self.env = {
            "HOME": str(self.home), "FLEET_HOME": str(self.fleet), "TMPDIR": str(self.tmp),
            "PATH": str(self.shims) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "TELEBOTS_TEST": "1", "TELEBOTS_SANDBOX_ROOT": str(SANDBOX),
            "FAKE": str(self.fake), "FAKE_WORKDIR": str(self.work), "LANG": "en_US.UTF-8",
        }

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
        for name in ("spawned-pids", "hung-pids"):
            listing = self.fake / name
            if not listing.exists():
                continue
            for line in listing.read_text().split():
                pid = int(line)
                try:
                    # Only processes the fake Terminal itself started: verify before signaling.
                    command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                             capture_output=True, text=True).stdout
                    if "sleep 60" in command or str(self.shims) in command or "osascript" in command:
                        os.kill(pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
        self.temp.cleanup()

    # ---- the fake world -----------------------------------------------------------
    def target(self, tty_name="ttys999"):
        """A live process the engines treat as the bot's claude session."""
        child = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        threading.Thread(target=child.wait, daemon=True).start()   # reap it the moment it dies
        procs = json.loads((self.fake / "procs.json").read_text())
        procs[str(child.pid)] = {
            "comm": "claude", "tty": tty_name, "cwd": str(self.work),
            "lstart": "Mon Nov  2 21:00:00 2026",
            "command": "claude --channels plugin:telegram@claude-plugins-official --name telebots-main",
        }
        (self.fake / "procs.json").write_text(json.dumps(procs))
        return child

    def frames(self, *frames):
        (self.fake / "frames.json").write_text(json.dumps(list(frames)))

    def registry(self, pid, status, waiting_for=None):
        data = {"pid": pid, "status": status}
        if waiting_for:
            data["waitingFor"] = waiting_for
        (self.home / ".claude/sessions" / (str(pid) + ".json")).write_text(json.dumps(data))

    def events(self, kind=None):
        path = self.fake / "events.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if kind is None or row["kind"] == kind]

    def messages(self):
        path = self.fake / "messages.jsonl"
        return [json.loads(line)["text"] for line in path.read_text().splitlines()] if path.exists() else []

    def scripts(self):
        folder = self.fake / "scripts"
        return [p.read_text() for p in sorted(folder.iterdir())] if folder.exists() else []

    def when(self, condition, action, timeout=30):
        """Run action once condition holds: the fake user reacts to what the engine did."""
        stop = threading.Event()

        def watch():
            end = time.monotonic() + timeout
            while time.monotonic() < end and not stop.is_set():
                try:
                    if condition():
                        action()
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(0.05)
        threading.Thread(target=watch, daemon=True).start()
        self.addCleanup(stop.set)

    def said(self, text):
        return any(text in m for m in self.messages())

    # ---- engine copies ------------------------------------------------------------
    def engine(self, name, constants=None, replace=None):
        source = (ENGINE / name).read_text()
        # /tmp/ first: the sandbox path itself may contain /tmp/.
        source = source.replace("/tmp/", str(self.tmp) + "/")
        source = source.replace("/usr/bin/osascript", str(self.shims / "osascript"))
        # Whole-second sleeps run ten times faster. Fractional ones stay: run_with_timeout
        # counts its deadline in steps of its own sleep. Waits measured with date stay as set.
        source = re.sub(r"\bsleep (\d+)\b(?!\.)", lambda m: "sleep %g" % (int(m.group(1)) / 10), source)
        for key, value in (constants or {}).items():
            source, count = re.subn(r"^%s=\d+" % key, "%s=%s" % (key, value), source, flags=re.M)
            self.assertEqual(count, 1, key)
        for old, new in (replace or {}).items():
            self.assertIn(old, source)
            source = source.replace(old, new)
        self.assertNotIn("/usr/bin/osascript", source)
        copy = self.root / name
        copy.write_text(source)
        copy.chmod(0o700)
        return copy

    def run_engine(self, script, *args, pty_name=None, timeout=60, extra=None):
        command = ["/bin/bash", str(script), *map(str, args)]
        if pty_name:
            command = ["/usr/bin/python3", "-c", IN_SESSION, "/dev/" + pty_name] + command
        started = time.monotonic()
        result = subprocess.run(command, env={**self.env, **(extra or {})}, cwd=self.root,
                                capture_output=True, text=True, timeout=timeout)
        result.elapsed = time.monotonic() - started
        return result

    def log(self, name):
        path = self.fleet / "logs" / (name + ".log")
        return path.read_text() if path.exists() else ""

    def assert_index_walks(self):
        """Every script the engine sent that looks up a window uses the exact walk."""
        for script in self.scripts():
            self.assertNotIn("repeat with w in windows", script)
            self.assertNotIn("contents of", script)
            if "tabs of" in script:
                for line in INDEX_WALK:
                    self.assertIn(line, script)


class PtyWatcher:
    """A pty the test owns, read raw. Optionally closes the fake menu on an Esc."""

    def __init__(self, harness, close_menu_with=None):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.name = os.path.basename(os.ttyname(self.slave))
        self.got = b""
        self.harness = harness
        self.close_menu_with = close_menu_with
        self.stop = False
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()
        harness.addCleanup(self.close)

    def read(self):
        while not self.stop:
            try:
                ready = select.select([self.slave], [], [], 0.05)[0]
                if not ready:
                    continue
                chunk = os.read(self.slave, 64)
            except OSError:
                return
            if not chunk:
                return
            self.got += chunk
            if b"\x1b" in chunk and self.close_menu_with is not None:
                self.harness.frames(self.close_menu_with)

    def close(self):
        self.stop = True
        self.thread.join(timeout=2)
        for fd in (self.slave, self.master):
            try:
                os.close(fd)
            except OSError:
                pass


def ioctl_allowed_in_session():
    """True when this kernel lets a session leader inject into its own pty."""
    if not ON_MAC:
        return False
    master, slave = pty.openpty()
    try:
        tty.setraw(slave)
        name = os.ttyname(slave)
        probe = ("import fcntl, termios, os, sys\n"
                 "fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)\n"
                 "fcntl.ioctl(fd, termios.TIOCSTI, b'x')\n")
        result = subprocess.run(["/usr/bin/python3", "-c", IN_SESSION, name, "/usr/bin/python3", "-c", probe, name],
                                capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(slave)
        os.close(master)


class StaticTerminalScriptTests(unittest.TestCase):
    """Source checks that need no Mac."""

    ENGINES = ("fleet-compact", "fleet-esc", "fleet-fresh", "fleet-restore")

    def heredocs(self, source):
        """(command line, body) for every quoted heredoc in a shell script."""
        out = []
        lines = source.split("\n")
        for i, line in enumerate(lines):
            m = re.search(r"<<'([A-Z]+)'", line)
            if not m:
                continue
            body = []
            for later in lines[i + 1:]:
                if later == m.group(1):
                    break
                body.append(later)
            out.append((line, "\n".join(body)))
        return out

    def test_every_window_lookup_walks_a_snapshot_of_window_ids(self):
        for name in self.ENGINES:
            source = (ENGINE / name).read_text()
            with self.subTest(engine=name):
                self.assertNotIn("repeat with w in windows", source)
                self.assertNotRegex(source, r"window id \(contents of")
                for _command, body in self.heredocs(source):
                    if "tabs of" in body and 'tell application "Terminal"' in body:
                        for line in INDEX_WALK:
                            self.assertIn(line, body)
        compact = (ENGINE / "fleet-compact").read_text()
        probe = compact[compact.index("OSA = '''"):compact.index("end run'''")]
        for line in INDEX_WALK:
            self.assertIn(line, probe)

    def test_no_applescript_timeout_is_relied_on_as_a_bound(self):
        for name in self.ENGINES:
            source = (ENGINE / name).read_text()
            for command, body in self.heredocs(source):
                if "with timeout" in body:
                    with self.subTest(engine=name, command=command.strip()):
                        self.assertIn("osa_limited", command)
            self.assertNotRegex(source, r"-e '[^']*with timeout")

    def test_the_window_opens_before_activate_and_nothing_waits_on_activate(self):
        for name in ("fleet-fresh", "fleet-restore"):
            source = (ENGINE / name).read_text()
            with self.subTest(engine=name):
                activations = [line.strip() for line in source.split("\n")
                               if "activate" in line and "osascript" in line]
                self.assertEqual(activations, [
                    "/usr/bin/osascript -e 'tell application \"Terminal\" to activate' >/dev/null 2>&1 &"])
                opens = [i for i, line in enumerate(source.split("\n")) if "do script (item 1 of argv)" in line]
                launch = [i for i, line in enumerate(source.split("\n")) if activations[0] in line.strip()]
                self.assertTrue(opens and launch and max(opens) < launch[0])
                for _command, body in self.heredocs(source):
                    self.assertNotRegex(body, r"(?m)^\s*activate\s*$")
        fresh = (ENGINE / "fleet-fresh").read_text()
        self.assertIn('run_with_timeout "$OPEN_TIMEOUT" OPEN_OUT /usr/bin/osascript', fresh)
        self.assertRegex(fresh, r"(?m)^OPEN_TIMEOUT=40\b")

    def test_lone_esc_is_never_sent_through_do_script(self):
        for name in ("fleet-compact", "fleet-esc"):
            source = (ENGINE / name).read_text()
            with self.subTest(engine=name):
                self.assertIn("termios.TIOCSTI", source)
                self.assertNotIn("ASCII character 27", source)
                self.assertNotRegex(source, r"do script[^\n]*(\\x1b|\\033|character id 27)")


@unittest.skipUnless(ON_MAC, "the Terminal engines are macOS shell engines")
class CompactTests(EngineHarness):
    FAST = {"RELOOK_QUICK": 1, "RELOOK_UNREADABLE": 2, "LOOSE_AFTER": 1, "MODAL_WAIT": 3,
            "MODAL_POLL": 1, "STALL_WAIT": 2, "BUSY_POLL": 1, "PROOF_WAIT": 3, "SETTLE_WAIT": 1,
            "KEYSTROKE_LIMIT": 2}

    def compact(self, constants=None, pty_name=None, replace=None):
        script = self.engine("fleet-compact", {**self.FAST, **(constants or {})}, replace)
        (self.fake / "started-flag").write_text(str(self.state / "compact-started"))
        target = self.target(pty_name or "ttys999")
        result = self.run_engine(script, target.pid, self.work, pty_name=pty_name)
        return target, result

    def assert_delivered(self, result):
        log = self.log("compact")
        self.assertEqual(result.returncode, 0, log)
        self.assertIn("=== done (path A delivered a 8-character /compact", log)
        self.assertEqual([e["text"] for e in self.events("text")], ["/compact"])

    def test_one_unreadable_look_does_not_end_a_compaction(self):
        self.frames(torn_frame(), idle_frame())
        _target, result = self.compact()
        self.assert_delivered(result)
        self.assertEqual(self.messages(), ["Compacting Main now. I'll ping when it's done."])
        evidence = self.state / "compact-last-unreadable.txt"
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.assertTrue(evidence.read_text().startswith("read=ok "))
        self.assert_index_walks()

    def test_footer_hint_on_an_empty_box_reads_idle_with_the_registry(self):
        self.frames(hint_frame())
        target = self.target()
        self.registry(target.pid, "idle")
        script = self.engine("fleet-compact", self.FAST)
        (self.fake / "started-flag").write_text(str(self.state / "compact-started"))
        self.assert_delivered(self.run_engine(script, target.pid, self.work))
        self.assertIn("reading it as IDLE", self.log("compact"))

    def test_registry_busy_turns_an_unknown_screen_into_a_wait(self):
        self.frames(torn_frame())
        target = self.target()
        self.registry(target.pid, "busy")

        def turn_ends():
            self.registry(target.pid, "idle")
            self.frames(idle_frame())
        self.when(lambda: self.said("is mid-task"), turn_ends)
        script = self.engine("fleet-compact", self.FAST)
        (self.fake / "started-flag").write_text(str(self.state / "compact-started"))
        self.assert_delivered(self.run_engine(script, target.pid, self.work))
        self.assertIn("Main is mid-task. I'll compact the moment this turn ends.", self.messages())

    def test_stranded_text_is_cleared_once_and_quoted_back(self):
        self.frames(typed_frame("half a thought about the report"))
        (self.fake / "after-ctrl-c").write_text(idle_frame())
        _target, result = self.compact()
        self.assert_delivered(result)
        self.assertEqual(len(self.events("ctrl-c")), 1)
        quoted = [m for m in self.messages() if m.startswith("I cleared unsent text")]
        self.assertEqual(len(quoted), 1)
        self.assertTrue(quoted[0].endswith("\n\nhalf a thought about the report"))
        saved = self.state / "compact-cleared-box.txt"
        self.assertEqual(saved.read_text(), "half a thought about the report\n")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assert_index_walks()

    def test_text_that_cannot_be_moved_safely_is_left_alone(self):
        tall = typed_frame(*["row %d of a long draft" % n for n in range(18)])
        typing = [typed_frame("h" * n) for n in range(1, 60)]
        cases = {"paste placeholder": [typed_frame("[Pasted text #1 +42 lines]")],
                 "taller than a box shows": [tall],
                 "still changing": typing}
        for label, frames in cases.items():
            with self.subTest(label):
                for name in ("events.jsonl", "messages.jsonl"):
                    (self.fake / name).unlink(missing_ok=True)
                self.frames(*frames)
                _target, result = self.compact()
                self.assertEqual(result.returncode, 11, self.log("compact"))
                self.assertEqual(self.events("ctrl-c"), [])
                self.assertEqual(self.events("text"), [])
                self.assertTrue(self.messages()[-1].startswith("Can't compact Main:"))

    def test_text_that_survives_ctrl_c_is_a_placeholder(self):
        self.frames(typed_frame("Ask anything"))
        _target, result = self.compact()
        self.assert_delivered(result)
        self.assertEqual(len(self.events("ctrl-c")), 1)
        self.assertFalse(any(m.startswith("I cleared unsent text") for m in self.messages()))
        self.assertIn("Claude Code's placeholder", self.log("compact"))

    def test_try_hint_reads_as_an_empty_box_but_real_text_does_not(self):
        script = self.engine("fleet-compact", self.FAST)
        frame = self.root / "frame.txt"
        for text, word in (('Try "write a test for <filepath>"', "IDLE"), ("Try harder", "TYPING:Try harder")):
            with self.subTest(text):
                frame.write_text(typed_frame(text))
                result = self.run_engine(script, 999998, self.work, extra={
                    "CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
                self.assertEqual(result.returncode, 0)
                self.assertIn("DRYRUN screen state on /dev/ttys999: " + word + "\n", self.log("compact"))

    def test_dry_run_saves_no_evidence(self):
        script = self.engine("fleet-compact", self.FAST)
        frame = self.root / "frame.txt"
        frame.write_text(torn_frame())
        result = self.run_engine(script, 999998, self.work, extra={
            "CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
        self.assertEqual(result.returncode, 0)
        self.assertIn("DRYRUN screen state on /dev/ttys999: UNREADABLE", self.log("compact"))
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [".env", "access.json", "logs"])

    @unittest.skipUnless(ioctl_allowed_in_session(), "this kernel refuses TIOCSTI even in session")
    def test_leftover_menu_gets_one_lone_esc_and_the_ack_waits_for_it(self):
        for waiting in (("idle", None), ("waiting", "dialog open")):
            with self.subTest(registry=waiting):
                for name in ("events.jsonl", "messages.jsonl", "frames.json"):
                    (self.fake / name).unlink(missing_ok=True)
                watcher = PtyWatcher(self, close_menu_with=idle_frame())
                self.frames(picker_frame())
                script = self.engine("fleet-compact", self.FAST)
                (self.fake / "started-flag").write_text(str(self.state / "compact-started"))
                target = self.target(watcher.name)
                self.registry(target.pid, *waiting)
                result = self.run_engine(script, target.pid, self.work, pty_name=watcher.name)
                self.assert_delivered(result)
                self.assertEqual(watcher.got, b"\x1b")
                self.assertEqual(self.messages(), ["Compacting Main now. I'll ping when it's done."])
                self.assertIn("lone Esc by ioctl on /dev/" + watcher.name, self.log("compact"))
                for script_text in self.scripts():
                    self.assertNotIn("character 27", script_text)

    @unittest.skipUnless(ioctl_allowed_in_session(), "this kernel refuses TIOCSTI even in session")
    def test_a_question_never_gets_an_esc(self):
        for waiting in ("input needed", "permission prompt"):
            with self.subTest(waiting_for=waiting):
                for name in ("events.jsonl", "messages.jsonl", "frames.json"):
                    (self.fake / name).unlink(missing_ok=True)
                watcher = PtyWatcher(self)
                self.frames(picker_frame())
                script = self.engine("fleet-compact", {**self.FAST, "MODAL_WAIT": 30})
                (self.fake / "started-flag").write_text(str(self.state / "compact-started"))
                target = self.target(watcher.name)
                self.registry(target.pid, "waiting", waiting)
                self.when(lambda: self.said("Tap /esc to close it"), lambda: self.frames(idle_frame()))
                result = self.run_engine(script, target.pid, self.work, pty_name=watcher.name)
                self.assert_delivered(result)
                self.assertEqual(watcher.got, b"")
                said = self.messages()
                self.assertEqual(len(said), 2, (said, self.log("compact")))
                self.assertIn("Tap /esc to close it", said[0])
                self.assertEqual(said[1], "Compacting Main now. I'll ping when it's done.")

    def test_a_refused_ioctl_asks_for_esc_and_never_claims_one(self):
        watcher = PtyWatcher(self)   # a real pty, but the engine is not in its session
        self.frames(picker_frame())
        script = self.engine("fleet-compact", self.FAST)
        target = self.target(watcher.name)
        self.registry(target.pid, "idle")
        result = self.run_engine(script, target.pid, self.work)
        self.assertEqual(result.returncode, 9, self.log("compact"))
        self.assertEqual(watcher.got, b"")
        said = self.messages()
        self.assertIn("Tap /esc to close it", said[0])
        self.assertFalse(any("I pressed Esc" in m for m in said))
        self.assertTrue(said[-1].startswith("Gave up compacting Main"))
        self.assertNotIn("Compacting Main now", " ".join(said))

    def test_a_stalled_terminal_gets_its_wait(self):
        (self.fake / "terminal-down").write_text("error")
        self.frames(idle_frame())
        self.when(lambda: len(self.events("read")) >= 3, lambda: (self.fake / "terminal-down").unlink())
        _target, result = self.compact()
        self.assert_delivered(result)
        self.assertFalse(any("hasn't answered" in m for m in self.messages()))

    def test_a_terminal_that_stays_stalled_is_refused_plainly(self):
        (self.fake / "terminal-down").write_text("error")
        _target, result = self.compact()
        self.assertEqual(result.returncode, 12, self.log("compact"))
        self.assertIn("Terminal on the Mac hasn't answered", self.messages()[-1])
        self.assertEqual(self.events("text"), [])

    def test_a_frozen_read_is_cut_off_from_outside(self):
        (self.fake / "terminal-down").write_text("hang")
        (self.fake / "hang-seconds").write_text("4")
        script = self.engine("fleet-compact", self.FAST, {"timeout=8,": "timeout=1,"})
        target = self.target()
        result = self.run_engine(script, target.pid, self.work)
        self.assertEqual(result.returncode, 12, self.log("compact"))
        self.assertIn("read=timeout", self.log("compact"))

    def test_no_window_for_the_tty_is_refused_plainly(self):
        (self.fake / "no-tab").write_text("1")
        _target, result = self.compact()
        self.assertEqual(result.returncode, 12, self.log("compact"))
        self.assertIn("can't find a Terminal window", self.messages()[-1])

    def test_the_in_flight_marker_holds_back_only_a_busy_bot(self):
        for frames, code in ((idle_frame(), 0), (busy_frame(), 7)):
            with self.subTest(code=code):
                for name in ("events.jsonl", "messages.jsonl"):
                    (self.fake / name).unlink(missing_ok=True)
                (self.state / "compact-inflight").write_text(str(int(time.time())))
                self.frames(frames)
                _target, result = self.compact()
                self.assertEqual(result.returncode, code, self.log("compact"))
                if code == 7:
                    self.assertEqual(self.messages(), ["A compaction is already in flight for Main."])
                    self.assertEqual(self.events("text"), [])

    def test_the_keystroke_fallback_is_bounded_from_outside(self):
        (self.fake / "deliver-nowindow").write_text("1")
        (self.fake / "keystroke-hangs").write_text("1")
        self.frames(idle_frame())
        _target, result = self.compact()
        self.assertEqual(result.returncode, 10, self.log("compact"))
        self.assertIn("path B (keystroke) result: TIMEOUT", self.log("compact"))
        self.assertIn("Terminal stopped answering partway", self.messages()[-1])
        self.assertLess(result.elapsed, 20)


@unittest.skipUnless(ON_MAC, "the Terminal engines are macOS shell engines")
class EscTests(EngineHarness):
    def esc(self, tty_name="ttys999", in_session=False, constants=None):
        script = self.engine("fleet-esc", constants)
        target = self.target(tty_name)
        return self.run_engine(script, target.pid, self.work, 123,
                               pty_name=tty_name if in_session else None)

    @unittest.skipUnless(ioctl_allowed_in_session(), "this kernel refuses TIOCSTI even in session")
    def test_esc_goes_in_by_ioctl_without_touching_terminal(self):
        watcher = PtyWatcher(self)
        result = self.esc(watcher.name, in_session=True)
        log = self.log("esc")
        self.assertEqual(result.returncode, 0, log)
        time.sleep(0.2)
        self.assertEqual(watcher.got, b"\x1b")
        self.assertIn("lone Esc by ioctl: OK", log)
        self.assertIn("=== done (sent Esc to /dev/%s) ===" % watcher.name, log)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.messages(), [])

    def test_esc_falls_back_to_the_keystroke_when_the_kernel_refuses(self):
        # The bot's real pty, but the engine runs outside that session, the way a
        # detached spawn starts it: the kernel refuses the ioctl.
        watcher = PtyWatcher(self)
        result = self.esc(watcher.name)
        log = self.log("esc")
        self.assertEqual(result.returncode, 0, log)
        time.sleep(0.2)
        self.assertEqual(watcher.got, b"")
        self.assertIn("Permission denied", log)
        self.assertIn("falling back to the keystroke path", log)
        self.assertIn("=== done (sent Esc to /dev/%s) ===" % watcher.name, log)
        self.assertEqual(len(self.events("keystroke")), 1)
        script = next(s for s in self.scripts() if "key code 53" in s)
        self.assertIn("repeat 10 times", script)
        self.assertIn("delay 0.2", script)
        self.assert_index_walks()

    def test_esc_keystroke_path_is_bounded_from_outside(self):
        (self.fake / "keystroke-hangs").write_text("1")
        (self.fake / "hang-seconds").write_text("20")
        result = self.esc(constants={"KEYSTROKE_LIMIT": 2})
        log = self.log("esc")
        self.assertEqual(result.returncode, 10, log)
        self.assertIn("=== done (TIMEOUT after 2s) ===", log)
        self.assertLess(result.elapsed, 10)
        self.assertEqual(self.messages(), [
            "The interrupt didn't land - Terminal on the Mac isn't answering. Your message will be read when the current task finishes."])


@unittest.skipUnless(ON_MAC, "the Terminal engines are macOS shell engines")
class FreshTests(EngineHarness):
    FAST = {"TERMINAL_WAIT": 2, "OPEN_WAIT": 3, "OPEN_TIMEOUT": 1, "POLLER_WAIT": 3}

    def fresh(self, quiet=False, constants=None, replace=None):
        script = self.engine("fleet-fresh", {**self.FAST, **(constants or {})},
                             {"terminal_answers 5": "terminal_answers 1", **(replace or {})})
        target = self.target("??")
        args = [target.pid, self.work, "fresh-keep"] + (["quiet"] if quiet else [])
        return target, self.run_engine(script, *args)

    def alive(self, child):
        return child.poll() is None

    def test_a_frozen_terminal_leaves_the_working_session_alone(self):
        for quiet in (False, True):
            with self.subTest(quiet=quiet):
                for name in ("events.jsonl", "messages.jsonl"):
                    (self.fake / name).unlink(missing_ok=True)
                (self.fake / "terminal-down").write_text("hang")
                (self.fake / "hang-seconds").write_text("3")
                target, result = self.fresh(quiet=quiet)
                self.assertEqual(result.returncode, 8, self.log("fresh"))
                self.assertTrue(self.alive(target))
                self.assertEqual(self.events("open"), [])
                said = self.messages()
                if quiet:
                    self.assertEqual(said, [])
                else:
                    self.assertEqual(len(said), 2, said)
                    self.assertIn("isn't answering, so Main stays up as it is for now", said[0])
                    self.assertIn("Main is still running as it was. Nothing changed.", said[1])

    def test_the_restart_goes_ahead_once_terminal_answers(self):
        (self.fake / "terminal-down").write_text("error")
        self.when(lambda: self.said("isn't answering"), lambda: (self.fake / "terminal-down").unlink())
        target, result = self.fresh()
        self.assertEqual(result.returncode, 0, self.log("fresh"))
        self.assertFalse(self.alive(target))
        self.assertEqual(len(self.events("open")), 1)
        self.assertEqual(self.messages()[-1], "⚡ Back and ready")

    def test_a_hung_open_is_cut_off_and_retried(self):
        (self.fake / "open-plan").write_text('["hang", "ok-up"]')
        (self.fake / "hang-seconds").write_text("6")
        _target, result = self.fresh()
        self.assertEqual(result.returncode, 0, self.log("fresh"))
        self.assertEqual(len(self.events("open")), 2)
        self.assertIn("osascript exit=124", self.log("fresh"))

    def test_a_late_open_is_found_before_any_second_window(self):
        (self.fake / "open-plan").write_text('["late"]')
        _target, result = self.fresh()
        self.assertEqual(result.returncode, 0, self.log("fresh"))
        self.assertEqual(len(self.events("open")), 1)
        self.assertIn("ran after all", self.log("fresh"))

    def test_a_window_that_never_opens_is_reported_unless_quiet(self):
        for quiet in (False, True):
            with self.subTest(quiet=quiet):
                for name in ("events.jsonl", "messages.jsonl"):
                    (self.fake / name).unlink(missing_ok=True)
                (self.fake / "open-plan").write_text('["fail"]')
                # The engine's own 45-step wait for a session adds nothing to this case.
                _target, result = self.fresh(quiet=quiet, replace={"$(seq 1 45)": "$(seq 1 5)"})
                self.assertEqual(result.returncode, 6, self.log("fresh"))
                self.assertGreater(len(self.events("open")), 1)
                self.assertIn("gave up opening the new window", self.log("fresh"))
                said = self.messages()
                if quiet:
                    self.assertEqual(said, [])
                else:
                    self.assertEqual(len(said), 1, said)
                    self.assertIn("Terminal never opened the new window", said[0])

    def test_activate_runs_after_the_open_and_nothing_waits_on_it(self):
        (self.fake / "activate-hangs").write_text("1")
        (self.fake / "hang-seconds").write_text("15")
        _target, result = self.fresh()
        self.assertEqual(result.returncode, 0, self.log("fresh"))
        self.assertLess(result.elapsed, 12)
        for _ in range(50):
            if self.events("activate"):
                break
            time.sleep(0.1)
        kinds = [e["kind"] for e in self.events() if e["kind"] in ("open", "activate")]
        self.assertEqual(kinds, ["open", "activate"])
        self.assert_index_walks()


@unittest.skipUnless(ON_MAC, "the Terminal engines are macOS shell engines")
class RestoreTests(EngineHarness):
    def test_restore_opens_first_and_activates_in_the_background(self):
        state = self.fleet / "bot-main"
        work = self.fleet / "work-main"
        state.mkdir()
        work.mkdir()
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [
            {"key": "main", "name": "Main", "state_dir": str(state), "workdir": str(work)}]}) + "\n")
        for name in ("fleet-snapshot", "fleet-fresh"):
            shutil.copy2(ENGINE / name, self.fleet / "bin" / name)
        env = {**self.env, "HOME": str(self.home)}
        project = subprocess.run([str(self.fleet / "bin/fleet-config"), "project-dir", "main"], env=env,
                                 text=True, capture_output=True, check=True).stdout.strip()
        Path(project).mkdir(parents=True)
        session = "11111111-2222-3333-4444-555555555555"
        (state / "session.id").write_text(session + "\n")
        (Path(project) / (session + ".jsonl")).write_text("{}\n")
        mode = self.root / "process-mode"
        mode.write_text("down")
        heartbeat = state / "poller.heartbeat"
        heartbeat.write_text(str(int(time.time())))
        # The process table as test_ops models it: down until Terminal opens the window.
        (self.shims / "ps").write_text(
            "#!/bin/sh\n"
            f'mode=$(cat "{mode}")\n'
            'if [ "$1" = "-p" ]; then echo "Fri Oct  2 12:00:00 2026"; exit 0; fi\n'
            'if [ "$1" = "-axo" ]; then\n'
            '  if [ "$mode" = up ]; then\n'
            '    echo "111 claude --channels plugin:telegram@claude-plugins-official"\n'
            '    echo "222 bun server.ts"\n'
            '  fi\n  exit 0\nfi\n'
            f'if [ "$1" = eww ]; then echo "TELEGRAM_STATE_DIR={state}"; exit 0; fi\n'
            'if [ "$1" = "-o" ] && [ "$2" = etime= ]; then echo 00:05; exit 0; fi\n'
            'if [ "$1" = "-o" ] && [ "$2" = ppid= ]; then echo 111; exit 0; fi\n'
            'exit 1\n')
        bash_env = self.root / "bash-env"
        bash_env.write_text('kill() { [ "$1" = -0 ] && return 0; return 1; }\n')
        site = self.root / "python-site"
        site.mkdir()
        (site / "sitecustomize.py").write_text("import os\nos.kill = lambda pid, sig: None\n")
        env.update(BASH_ENV=str(bash_env), PYTHONPATH=str(site), RESTORE_MODE_FILE=str(mode),
                   RESTORE_HEARTBEAT=str(heartbeat))
        snapshot = subprocess.run(["/bin/bash", str(ENGINE / "fleet-snapshot")], env=env,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
        (self.fake / "activate-hangs").write_text("1")
        (self.fake / "hang-seconds").write_text("15")
        restore = self.engine("fleet-restore")
        started = time.monotonic()
        result = subprocess.run(["/bin/bash", str(restore)], env=env, capture_output=True,
                                text=True, timeout=60)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("archived", result.stdout)
        self.assertLess(elapsed, 12)
        for _ in range(50):
            if self.events("activate"):
                break
            time.sleep(0.1)
        kinds = [e["kind"] for e in self.events() if e["kind"] in ("open", "activate")]
        self.assertEqual(kinds, ["open", "activate"])


if __name__ == "__main__":
    unittest.main()
