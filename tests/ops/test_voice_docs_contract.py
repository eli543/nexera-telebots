"""Keep the voice guide aligned with the poller and local transcriber."""

import os
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper.")


class VoiceDocsContract(unittest.TestCase):
    def test_documented_switch_and_backend_match_the_producers(self):
        guide = (ROOT / "voice/README.md").read_text()
        poller = (ROOT / "poller/telebots.ts").read_text()
        transcriber = (ROOT / "voice/transcribe-voice").read_text()
        self.assertIn("autotranscribe.off", poller)
        self.assertIn("autotranscribe.off", guide)
        self.assertIn("ask_daemon(", transcriber)
        self.assertIn("cold_path(", transcriber)
        self.assertIn("warm daemon", guide)
        self.assertIn("installed local backend", guide)
        self.assertIn("does not select or disable", guide)
        self.assertNotIn("`fleet-config option voice` selects the backend", guide)
        self.assertNotIn("The `voice` option in `fleet.json` selects the behavior", guide)
        self.assertNotIn("force the mlx backend", guide)
        self.assertNotIn("force the faster-whisper backend", guide)


if __name__ == "__main__":
    unittest.main()
