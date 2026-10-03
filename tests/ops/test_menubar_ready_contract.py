"""Parse-level contract for the engine's Back and ready log vocabulary."""

import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper.")


class MenubarReadyContract(unittest.TestCase):
    def source_contract(self):
        swift = (ROOT / "menubar/Fleet.swift").read_text()
        return swift.split("static func engineReadyEvidence", 1)[1].split("static func describe", 1)[0]

    def test_dashboard_reads_current_status_vocabulary(self):
        body = self.source_contract()
        engine = (ROOT / "engine/fleet-fresh").read_text()
        self.assertIn("back-online ping status=$status", engine)
        self.assertIn("back-online ping status=failed", engine)
        self.assertIn('"back-online ping status="', body)
        for status in ("accepted", "attempted", "failed"):
            with self.subTest(status=status):
                self.assertIn(f'case "{status}":', body)
        self.assertNotIn("back-online ping sent", body)
        self.assertIn('line == "=== done (exit=0) ==="', body)

    def test_status_is_scoped_to_exact_restart_and_completion(self):
        body = self.source_contract()
        self.assertIn('ours = line.hasSuffix(marker); reported = nil', body)
        marker = " restart pid=123 cwd=/sandbox/bot ==="
        def observed(log):
            ours = False
            reported = None
            for line in log.splitlines():
                if line.startswith("=== ") and "restart pid=" in line:
                    ours = line.endswith(marker)
                    reported = None
                if ours:
                    match = re.search(r"back-online ping status=([^ ]+)", line)
                    if match and match.group(1) in ("accepted", "attempted", "failed"):
                        reported = match.group(1)
                if ours and line == "=== done (exit=0) ===" and reported:
                    return reported
            return None
        prefix = "=== Saturday restart pid=123 cwd=/sandbox/bot ===\n"
        suffix = "\n=== done (exit=0) ===\n"
        for status in ("accepted", "attempted", "failed"):
            with self.subTest(status=status):
                self.assertEqual(observed(prefix + "back-online ping status=" + status + suffix), status)
        self.assertIsNone(observed(prefix + "back-online ping status=accepted\n"))
        self.assertIsNone(observed("=== Saturday restart pid=999 cwd=/sandbox/bot ===\nback-online ping status=accepted" + suffix))
        self.assertIsNone(observed(prefix + "back-online ping sent" + suffix))


if __name__ == "__main__":
    unittest.main()
