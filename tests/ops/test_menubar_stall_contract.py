"""Parse-level check of watchdog markers consumed by the Mac and phone views."""

import os
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["TELEBOTS_SANDBOX_ROOT"])
if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper.")


class MenubarStallContract(unittest.TestCase):
    def source_contract(self):
        swift = (ROOT / "menubar/Fleet.swift").read_text()
        warning = swift.split("static func stallSuspected", 1)[1].split("static func stallPhoneNotified", 1)[0]
        notified = swift.split("static func stallPhoneNotified", 1)[1].split("static func indicators", 1)[0]
        suffix = lambda body: re.search(r'bot\.key \+ "(\.stall_[a-z]+)"', body).group(1)
        return suffix(warning), suffix(notified), warning, notified

    def test_dashboard_warning_and_notification_use_distinct_markers(self):
        observed, alerted, warning, notified = self.source_contract()
        self.assertEqual(observed, ".stall_observed")
        self.assertEqual(alerted, ".stall_alerted")
        self.assertIn("firstLineDigits", warning)
        self.assertIn("firstLineDigits", notified)
        self.assertIn("alerted == observed", notified)

        watchdog = (ROOT / "daemon/watchdog.sh").read_text()
        self.assertIn('observed="$STATE/$key.stall_observed"', watchdog)
        self.assertIn('flag="$STATE/$key.stall_alerted"', watchdog)
        main = (ROOT / "menubar/main.swift").read_text()
        self.assertIn('ind.stallSuspected && ind.stallPhoneNotified', main)
        self.assertIn('parts.append("phone notified")', main)
        phone = (ROOT / "menubar/PhonePage.swift").read_text()
        self.assertIn("bot.detail_line", phone)
        self.assertIn("bot.stall ? ' · stall suspected'", phone)

    def test_observation_survives_disabled_alerts_and_stale_markers(self):
        observed_suffix, alerted_suffix, _, _ = self.source_contract()
        with tempfile.TemporaryDirectory(dir=SANDBOX) as temp:
            state = Path(temp)
            observed, alerted = state / ("main" + observed_suffix), state / ("main" + alerted_suffix)
            observed.write_text("200\n")
            def dashboard(session_started):
                inbound = int(observed.read_text().strip()) if observed.exists() else 0
                warning = inbound >= session_started
                alerted_inbound = int(alerted.read_text().strip()) if alerted.exists() else 0
                return warning, warning and alerted_inbound == inbound
            self.assertEqual(dashboard(100), (True, False))
            alerted.write_text("150\n")
            self.assertEqual(dashboard(100), (True, False))
            alerted.write_text("200\n")
            self.assertEqual(dashboard(100), (True, True))
            self.assertEqual(dashboard(250), (False, False))


if __name__ == "__main__":
    unittest.main()
