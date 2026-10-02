#!/usr/bin/env python3
"""Exercise the installed-data rules renderer in a scrubbed temporary home."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/hooks/telebots-rules.sh"
UX_SOURCE = ROOT / "poller/ux.json"
# Contract C2 fixture used until the poller package is merged. Once present,
# the real UX file is used by these same consistency and rendering tests.
UX_FIXTURE = {
    "reactions": [
        {"emoji": "❤", "phrase": "Save everything to my notes", "key_line": "❤️ save everything to your notes folder"},
        {"emoji": "👍", "phrase": "Yes, go ahead", "key_line": "👍 yes, go ahead"},
        {"emoji": "👎", "phrase": "No, stop", "key_line": "👎 no, stop"},
        {"emoji": "🔥", "phrase": "Priority: put this at the top of my list", "key_line": "🔥 put this at the top of your list"},
        {"emoji": "👏", "phrase": "Wrap it up: done, then save everything", "key_line": "👏 finish and save everything"},
        {"emoji": "😁", "phrase": "Explain this simply", "key_line": "😁 explain this simply"},
        {"emoji": "🥰", "phrase": "Watch this", "key_line": "🥰 watch this for a change"},
    ],
    "commands": [],
    "quick_bar": [["/esc", "/model"], ["/usage", "/fresh"]],
    "key_extra": [],
}


class RulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            raise RuntimeError("Run rules tests with a scrubbed environment and no token.")
        sandbox = os.environ.get("SANDBOX_ROOT")
        temp = os.environ.get("TMPDIR")
        if not sandbox or not temp or not Path(temp).resolve().is_relative_to(Path(sandbox).resolve()):
            raise RuntimeError("Run rules tests through the sandbox wrapper.")
        cls.ux = json.loads(UX_SOURCE.read_text()) if UX_SOURCE.exists() else UX_FIXTURE

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rules-", dir=os.environ["TMPDIR"])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.fleet = self.home / ".claude/telegram-fleet"
        self.state = self.home / ".claude/channels/fleet-garden"
        self.fleet.mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            # macOS's /usr/bin/python3 launcher creates xcrun_db in TMPDIR.
            # Keep that toolchain cache in the harness temp directory, outside
            # the per-test home whose no-write property is checked below.
            "TMPDIR": os.environ["TMPDIR"],
            "FLEET_HOME": str(self.fleet),
            "TELEGRAM_STATE_DIR": str(self.state),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.config = {
            "version": 2,
            "bots": [{"key": "garden", "name": "Garden", "emoji": "🌿",
                      "workdir": "~/claude-fleet/garden", "state_dir": "~/.claude/channels/fleet-garden"}],
            "options": {"notes_dir": "~/Telebots Notes"},
        }
        self.save_config()
        self.save_ux(self.ux)

    def save_config(self):
        (self.fleet / "fleet.json").write_text(json.dumps(self.config), encoding="utf-8")

    def save_ux(self, data):
        (self.fleet / "ux.json").write_text(json.dumps(data), encoding="utf-8")

    def render(self, expected=0):
        before = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        result = subprocess.run(["/bin/bash", str(HOOK)], env=self.env, input="{}", text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        after = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "The renderer must not write any state.")
        if expected:
            self.assertEqual(result.stdout, "")
            self.assertIn("could not render valid rules", result.stderr)
            return result
        self.assertEqual(result.stderr, "")
        output = json.loads(result.stdout)
        hook_output = output["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "SessionStart")
        context = hook_output["additionalContext"]
        self.assertLessEqual(len(context), 9000)
        self.assertLessEqual(len(result.stdout.rstrip("\n")), 10000)
        return context

    def test_inactive_exits_before_reading_files_or_starting_python(self):
        self.env.pop("TELEGRAM_STATE_DIR")
        self.env["PATH"] = "/missing-path"
        self.env["FLEET_HOME"] = str(self.base / "not-created")
        result = subprocess.run(["/bin/bash", str(HOOK)], env=self.env, input="not json", capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertFalse((self.base / "not-created").exists())

    def test_empty_state_is_inactive(self):
        self.env["TELEGRAM_STATE_DIR"] = ""
        result = subprocess.run(["/bin/bash", str(HOOK)], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_resolves_bot_and_notes_from_config(self):
        context = self.render()
        print("Normal rendered rules: " + str(len(context)) + " characters", flush=True)
        self.assertIn('You are "Garden"', context)
        self.assertIn("fleet of 1 bot(s)", context)
        self.assertIn(str(self.home / "Telebots Notes"), context)
        self.assertNotRegex(context, r"\{\{[A-Z_]+\}\}")

    def test_default_install_root_and_version_one_options(self):
        self.env.pop("FLEET_HOME")
        self.config["version"] = 1
        self.config.pop("options")
        self.save_config()
        self.assertIn(str(self.home / "Telebots Notes"), self.render())

    def test_unknown_bot_and_missing_config_use_neutral_defaults(self):
        (self.fleet / "fleet.json").unlink()
        context = self.render()
        self.assertIn('You are "Claude"', context)
        self.assertIn("fleet of 1 bot(s)", context)

    def test_many_arbitrary_bots_and_path_normalization(self):
        self.config["bots"] = [{"key": "bot-" + str(i), "name": "Node " + str(i),
                               "state_dir": str(self.home / ("state-" + str(i)))} for i in range(40)]
        self.config["bots"][23]["state_dir"] = str(self.state) + "/../fleet-garden/"
        self.config["bots"][23]["name"] = "東京 helper & analyst"
        self.config["options"]["notes_dir"] = "~/Notes & Projects/Shared"
        self.save_config()
        context = self.render()
        self.assertIn('You are "東京 helper & analyst"', context)
        self.assertIn("fleet of 40 bot(s)", context)
        self.assertIn(str(self.home / "Notes & Projects/Shared"), context)

    def test_explicit_config_override(self):
        override = self.base / "other-fleet.json"
        self.config["bots"][0]["name"] = "Custom config"
        override.write_text(json.dumps(self.config), encoding="utf-8")
        self.env["FLEET_CONFIG"] = str(override)
        self.assertIn('You are "Custom config"', self.render())

    def test_reaction_phrases_and_legend_are_identical_to_ux(self):
        context = self.render()
        for row in self.ux["reactions"]:
            expected = "- " + row["emoji"] + ": " + json.dumps(row["phrase"], ensure_ascii=False)
            expected += "; /key: " + json.dumps(row["key_line"], ensure_ascii=False)
            self.assertIn(expected, context)

    def test_reads_changed_ux_without_a_second_hardcoded_table(self):
        ux = copy.deepcopy(self.ux)
        ux["reactions"][0]["phrase"] = 'Store the selected work | include "results"'
        ux["reactions"][0]["key_line"] = "❤ store selected work"
        self.save_ux(ux)
        context = self.render()
        self.assertIn(json.dumps(ux["reactions"][0]["phrase"]), context)
        self.assertIn(ux["reactions"][0]["key_line"], context)

    def test_placeholder_text_in_config_is_not_reprocessed(self):
        self.config["bots"][0]["name"] = "{{FLEET_SIZE}}"
        self.save_config()
        self.assertIn('You are "{{FLEET_SIZE}}"', self.render())

    def test_duplicate_state_refuses_ambiguous_identity(self):
        self.config["bots"].append(dict(self.config["bots"][0], key="other"))
        self.save_config()
        self.render(expected=1)

    def test_invalid_or_missing_ux_emits_no_partial_rules(self):
        for value in ("{", "[]", '{"reactions": []}'):
            with self.subTest(value=value):
                (self.fleet / "ux.json").write_text(value)
                self.render(expected=1)
        (self.fleet / "ux.json").unlink()
        self.render(expected=1)

    def test_invalid_reaction_rows_refuse(self):
        for patch in ({"emoji": "❤️"}, {"emoji": "👍"}, {"phrase": "line\nbreak"}, {"key_line": None}):
            with self.subTest(patch=patch):
                ux = copy.deepcopy(self.ux)
                ux["reactions"][0].update(patch)
                self.save_ux(ux)
                self.render(expected=1)

    def test_large_valid_config_fits_the_cap(self):
        self.config["bots"][0]["name"] = "N" * 128
        self.config["options"]["notes_dir"] = str(self.home / ("notes-" + "x" * 500))
        self.save_config()
        self.render()

    def test_exact_cap_and_one_character_over(self):
        initial = self.render()
        self.config["bots"][0]["name"] = "N" * (len("Garden") + 9000 - len(initial))
        self.save_config()
        self.assertEqual(len(self.render()), 9000)
        self.config["bots"][0]["name"] += "X"
        self.save_config()
        self.render(expected=1)
        print("Boundary rendered rules: 9000 characters; 9001 rejected", flush=True)

    def test_oversized_config_refuses_without_leaking_its_value(self):
        private_value = "DO-NOT-ECHO-THIS-" * 1000
        self.config["bots"][0]["name"] = private_value
        self.save_config()
        result = self.render(expected=1)
        self.assertNotIn("DO-NOT-ECHO-THIS", result.stderr)

    def test_malformed_fleet_is_not_reported_as_default_identity(self):
        (self.fleet / "fleet.json").write_text('{"bots": null}')
        self.render(expected=1)


if __name__ == "__main__":
    print("UX consistency source: " + ("poller/ux.json" if UX_SOURCE.exists() else "contract C2 fixture (poller package not merged)"), flush=True)
    unittest.main(verbosity=2)
