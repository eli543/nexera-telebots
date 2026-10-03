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

    def test_uninstall_and_untouched_guarantees_match_implementation(self):
        text = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[0]
        self.assertIn("rechecks the plugin slot", text)
        self.assertIn("final stock hash", text)
        self.assertIn("process commands", text)
        self.assertNotIn("pins known launchd labels", text)

    def test_handoff_receipts_do_not_claim_user_confirmation(self):
        text = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[0]
        self.assertIn("user confirms the note", text)
        self.assertNotIn("A handoff passes only when both pollers", text)

    def test_custom_adapter_ids_are_described_without_changing_imap_uids(self):
        text = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[0]
        self.assertIn("opaque message IDs", text)
        self.assertIn("numeric UIDs", text)

    def test_edited_handoff_requires_a_new_id_and_preview(self):
        text = (ROOT / "CHANGELOG.md").read_text().split("## [0.2.0]", 1)[0]
        self.assertIn("ID renewal and a fresh preview", text)
        self.assertIn("old ID is cancelled", text)


if __name__ == "__main__":
    unittest.main()
