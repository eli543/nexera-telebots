"""Public setup instructions describe the behavior that ships."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DocsCopyTests(unittest.TestCase):
    def test_dialog_is_the_normal_pairing_path(self):
        for path in ("README.md", "plugin/commands/setup.md", "plugin/skills/fleet-ops/SKILL.md"):
            with self.subTest(path=path):
                copy = (ROOT / path).read_text()
                self.assertIn("Pair/Ignore dialog", copy)
                self.assertIn("If the dialog is unavailable", copy)

    def test_log_map_and_watch_lifetime(self):
        ops = (ROOT / "plugin/skills/fleet-ops/SKILL.md").read_text()
        self.assertIn("`$FLEET_HOME/logs/`", ops)
        self.assertIn("fresh.log", ops.split("`$FLEET_HOME/logs/`", 1)[1].split("\n", 1)[0])
        self.assertIn("`<state_dir>/logs/`", ops)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("a session restart ends the watch", readme)

    def test_update_fixture_matches_installed_build_copy(self):
        actual = json.loads((ROOT / "poller/ux.json").read_text())
        fixture = json.loads((ROOT / "tests/sandbox/fixtures/ux.json").read_text())
        description = lambda value: next(item["description"] for item in value["commands"] if item["command"] == "update")
        self.assertEqual(description(fixture), description(actual))


if __name__ == "__main__":
    unittest.main()
