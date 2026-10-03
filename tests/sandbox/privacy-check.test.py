#!/usr/bin/env python3
"""Regression checks for Git-scoped privacy scanning and sandbox copying."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from repo_files import copy_files, git_paths


HERE = Path(__file__).resolve().parent


class PrivacyFileListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="telebots-privacy-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        source = root / "source"
        self.clone = root / "clone"
        (source / "tests/sandbox").mkdir(parents=True)
        (source / "menubar").mkdir()
        for name in ("privacy-check.py", "privacy-allowlist.txt", "repo_files.py"):
            shutil.copy2(HERE / name, source / "tests/sandbox" / name)
        (source / ".gitignore").write_text("__pycache__/\nnode_modules/\nout/\n.env\n")
        (source / "menubar/.gitignore").write_text(".build.noindex/\n")
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test", "-c",
             "user.email=test@example.invalid", "commit", "-qm", "Seed privacy test"],
            check=True,
        )
        subprocess.run(["git", "clone", "-q", str(source), str(self.clone)], check=True)
        self.marker = "/" + "Users/someone\n"
        self.ignored = [
            Path("menubar/.build.noindex/x.plist"),
            Path("node_modules/x.plist"),
            Path("out/x.plist"),
            Path(".env"),
        ]
        for relative in self.ignored:
            path = self.clone / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.marker)

    def gate(self) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("TELEBOTS_SANDBOX_ROOT", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(self.clone / "tests/sandbox/privacy-check.py")],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_ignored_home_path_is_neither_scanned_nor_copied(self) -> None:
        result = self.gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Privacy gate passed.", result.stdout)
        staged = Path(self.temp.name) / "staged"
        copy_files(self.clone, staged, git_paths(self.clone))
        for relative in self.ignored:
            self.assertFalse((staged / relative).exists())

    def test_untracked_home_path_is_scanned_and_copied(self) -> None:
        untracked = self.clone / "menubar/new.plist"
        untracked.write_text(self.marker)
        result = self.gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("menubar/new.plist:1: home path", result.stdout)
        for relative in self.ignored:
            self.assertNotIn(str(relative), result.stdout)
        staged = Path(self.temp.name) / "staged"
        copy_files(self.clone, staged, git_paths(self.clone))
        self.assertEqual((staged / "menubar/new.plist").read_text(), self.marker)
        for relative in self.ignored:
            self.assertFalse((staged / relative).exists())


if __name__ == "__main__":
    unittest.main()
