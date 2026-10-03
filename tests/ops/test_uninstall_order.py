#!/usr/bin/env python3
"""Offline lifecycle ordering with fake launchctl and restore scripts."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["TELEBOTS_SANDBOX_ROOT"])
if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper.")


class UninstallOrder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fleet = self.home / ".claude/telegram-fleet"
        self.state = self.fleet / "watchdog-state"
        self.lock = self.state / "watchdog.lock"
        self.pid = self.state / "watchdog-loop.pid"
        self.lock.mkdir(parents=True)
        self.pid.write_text("424242\n")
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        self.plist = agents / "com.claude-fleet.watchdog.plist"
        self.plist.write_text("sandbox plist\n")
        self.repo = self.root / "repo"
        (self.repo / "poller").mkdir(parents=True)
        shutil.copy2(REPO / "uninstall.sh", self.repo / "uninstall.sh")
        self.trace = self.root / "trace"
        restore = self.repo / "poller/restore-stock.sh"
        restore.write_text('''#!/bin/bash
if [ "${1:-}" = --preflight ]; then
  if [ -f "$TEST_ROOT/restored" ]; then echo verified >> "$TRACE"; echo STOCK
  else echo preflight >> "$TRACE"; echo RESTORE; fi
else
  echo restore >> "$TRACE"
  touch "$TEST_ROOT/restored"
fi
''')
        restore.chmod(0o700)
        bindir = self.root / "bin"
        bindir.mkdir()
        launchctl = bindir / "launchctl"
        launchctl.write_text('''#!/bin/bash
echo bootout >> "$TRACE"
[ "${FAIL_STOP:-0}" = 1 ] && exit 1
(sleep 1; rm -f "$PID_FILE"; rmdir "$LOCK_DIR"; echo pass-ended >> "$TRACE") >/dev/null 2>&1 &
exit 0
''')
        launchctl.chmod(0o700)
        self.env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                        TELEGRAM_STATE_DIR=str(self.home / ".claude/channels/test"),
                        PATH=str(bindir) + ":" + os.environ["PATH"],
                        TEST_ROOT=str(self.root), TRACE=str(self.trace),
                        PID_FILE=str(self.pid), LOCK_DIR=str(self.lock))

    def run_uninstall(self, fail_stop=False):
        return subprocess.run(["/bin/bash", str(self.repo / "uninstall.sh"), "--yes"],
                              env=dict(self.env, FAIL_STOP="1" if fail_stop else "0"),
                              capture_output=True, text=True, timeout=10)

    def test_waits_for_watchdog_pass_before_restoring_and_verifies_stock(self):
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(),
                         ["preflight", "bootout", "pass-ended", "restore", "verified"])

    def test_failed_watchdog_stop_aborts_before_restore_or_removal(self):
        result = self.run_uninstall(fail_stop=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.trace.read_text().splitlines(), ["preflight", "bootout"])
        self.assertTrue(self.plist.exists())
        self.assertTrue(self.fleet.exists())


if __name__ == "__main__":
    unittest.main()
