"""Checklist evidence from real engines with external Mac commands blocked."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["TELEBOTS_SANDBOX_ROOT"])


class ChecklistProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fleet = self.home / ".claude/telegram-fleet"
        self.work = self.root / "work"
        self.state = self.root / "state"
        self.shims = self.root / "shims"
        self.tmp = self.root / "tmp"
        for path in (self.fleet / "bin", self.work, self.state, self.shims, self.tmp):
            path.mkdir(parents=True, exist_ok=True)
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [{
            "key": "main", "name": "Main", "workdir": str(self.work), "state_dir": str(self.state)}]}))
        (self.state / "access.json").write_text('{"dmPolicy":"allowlist","allowFrom":["123"]}\n')
        (self.state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-sandbox-only\n")
        shutil.copy2(REPO / "engine/fleet-config", self.fleet / "bin/fleet-config")
        for name, body in {
            "ps": '#!/bin/sh\ncase "$*" in *comm=*) echo claude ;; *tty=*) echo ttys999 ;; esac\n',
            "lsof": '#!/bin/sh\nprintf "COMMAND PID USER FD TYPE NAME\\nclaude 999 1 cwd DIR %s\\n" "$BOT_WORK"\n',
            "osascript": '#!/bin/sh\nif [ "$1" = "-e" ]; then echo Terminal; else [ "${OSA_TOUCH_COMPACT:-0}" = 1 ] && touch "$COMPACT_STARTED"; echo OK; fi\n',
            "curl": '#!/bin/sh\nprintf "{\\\"ok\\\":true}\\n"\n',
        }.items():
            path = self.shims / name
            path.write_text(body)
            path.chmod(0o700)
        self.bash_env = self.root / "bash-env"
        self.bash_env.write_text('kill() { [ "$1" = -0 ]; }\n')
        self.env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                        PATH=str(self.shims) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                        BASH_ENV=str(self.bash_env), BOT_WORK=str(self.work),
                        COMPACT_STARTED=str(self.state / "compact-started"),
                        TELEBOTS_TEST="1")
        self.env.pop("TELEGRAM_BOT_TOKEN", None)
        loader = importlib.machinery.SourceFileLoader("fleet_checklist_producer", str(REPO / "engine/fleet-checklist"))
        spec = importlib.util.spec_from_loader("fleet_checklist_producer", loader)
        self.checklist = importlib.util.module_from_spec(spec)
        loader.exec_module(self.checklist)
        self.checklist.ROOT = self.fleet

    def safe_engine(self, name):
        source = (REPO / "engine" / name).read_text()
        source = source.replace("/tmp/", str(self.tmp) + "/")
        source = source.replace("/usr/bin/osascript", str(self.shims / "osascript"))
        source = source.replace("/usr/bin/curl", str(self.shims / "curl"))
        self.assertNotIn("/usr/bin/osascript", source)
        self.assertNotIn("/usr/bin/curl", source)
        target = self.root / name
        target.write_text(source)
        target.chmod(0o700)
        return target

    def call(self, script, *args, extra=None):
        return subprocess.run(["/bin/bash", str(script), *map(str, args)],
                              env={**self.env, **(extra or {})}, cwd=self.root,
                              capture_output=True, text=True, timeout=25, check=False)

    def row(self, code, started):
        return self.checklist.phone_row(code, self.state, self.root / "notes", 1,
                                        False, False, started)["status"]

    def test_escape_and_compact_receipts_come_from_executed_engines(self):
        esc = self.safe_engine("fleet-esc")
        self.assertIn(str(self.shims / "osascript"), esc.read_text())
        self.assertEqual(self.call(self.shims / "osascript", "-e").stdout.strip(), "Terminal")
        started = int(time.time()) - 1
        dry = self.call(esc, 999999, self.work, 123, extra={"CC_ESC_DRYRUN": "1"})
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.row("P9", started), "Needs your action")
        landed = self.call(esc, 999999, self.work, 123)
        self.assertEqual(landed.returncode, 0, (self.fleet / "logs/esc.log").read_text())
        self.assertEqual(self.row("P9", started), "Passed")

        compact = self.safe_engine("fleet-compact")
        frame = self.root / "frame.txt"
        frame.write_text("─" * 24 + "\n❯ \n" + "─" * 24 + "\n⏵⏵ permission mode\n")
        dry = self.call(compact, 999999, self.work, extra={
            "CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
        self.assertEqual(dry.returncode, 0, (self.fleet / "logs/compact.log").read_text())
        self.assertEqual(self.row("P11", started), "Needs your action")
        landed = self.call(compact, 999999, self.work, extra={
            "CC_COMPACT_FRAME_FILE": str(frame), "OSA_TOUCH_COMPACT": "1"})
        self.assertEqual(landed.returncode, 0, (self.fleet / "logs/compact.log").read_text())
        self.assertEqual(self.row("P11", started), "Passed", repr(self.checklist.engine_run(self.state, "compact", started)))
        self.assertFalse((self.state / "logs/esc.log").exists())
        self.assertFalse((self.state / "logs/compact.log").exists())

    def test_watchdog_dry_run_does_not_prove_recovery_or_phone_delivery(self):
        (self.state / ".enrolled").touch()
        watchdog = REPO / "daemon/watchdog.sh"
        result = self.call(watchdog, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would restart", result.stdout)
        row = self.checklist.phone_row("P15", self.state, self.root / "notes", 1,
                                        False, True, int(time.time()) - 1)
        self.assertEqual(row["status"], "Needs your action")
        self.assertIn("30 minutes", row["detail"])

    def test_update_ready_ping_without_restart_does_not_pass(self):
        update = self.safe_engine("fleet-update")
        started = int(time.time()) - 1
        ready = self.call(update, 999999, self.work, 123, extra={
            "CC_UPDATE_READY_DRYRUN": "1", "TELEBOTS_API_BASE": "http://127.0.0.1:12345"})
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertIn("TEST ready ping only", (self.state / "logs/update.log").read_text())
        self.assertEqual(self.row("P8", started), "Needs your action")


if __name__ == "__main__":
    unittest.main()
