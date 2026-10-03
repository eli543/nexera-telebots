"""Release notes stay aligned with the verified v0.3.0 fixes."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ChangelogReleaseTests(unittest.TestCase):
    def test_pending_release_names_fixes_and_limits(self):
        text = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[0]
        for phrase in ("curl arguments", "control characters", "custom email adapter",
                       "watchdog repair", "session authority file", "shell permission",
                       "agent-attested", "truncated", "Telegram acceptance",
                       "stall observed", "pairing request snapshot", "real-phone"):
            with self.subTest(phrase=phrase):
                self.assertTrue(phrase in text, f"missing release note: {phrase}")


if __name__ == "__main__":
    unittest.main()
