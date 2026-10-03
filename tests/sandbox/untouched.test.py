#!/usr/bin/env python3
"""The untouched gate ignores live state and names protected changes."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import runner


class UntouchedSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="telebots-untouched-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.plist = self.home / "Library/LaunchAgents/com.example.watchdog.plist"
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("original plist\n")
        self.server = self.home / ".claude/plugins/cache/claude-plugins-official/telegram/0.0.7/server.ts"
        self.server.parent.mkdir(parents=True)
        self.server.write_text("stock server\n")
        self.marker = self.server.with_name(".in_use")
        self.marker.write_text("first\n")
        self.log = self.home / ".claude/telegram-fleet/menubar.log"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("first\n")

    def snapshot(self) -> dict[str, object]:
        process_list = subprocess.CompletedProcess([], 0, stdout="")
        with patch.object(runner.subprocess, "run", return_value=process_list):
            return runner.snapshot(self.home, None)

    def test_runtime_file_changes_pass(self) -> None:
        before = self.snapshot()
        self.marker.write_text("second\n")
        self.log.write_text("second\n")
        session = self.home / ".claude/channels/bot/session.json"
        session.parent.mkdir(parents=True)
        session.write_text("live session\n")
        after = self.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(runner.changed_items(before, after), [])

    def test_protected_plist_change_reports_exact_file(self) -> None:
        before = self.snapshot()
        self.plist.write_text("changed plist and size\n")
        after = self.snapshot()
        self.assertEqual(runner.changed_items(before, after), [str(self.plist)])

    def test_plugin_server_change_reports_exact_file(self) -> None:
        before = self.snapshot()
        self.server.write_text("other server\n")
        after = self.snapshot()
        self.assertEqual(runner.changed_items(before, after), [str(self.server)])


if __name__ == "__main__":
    unittest.main()
