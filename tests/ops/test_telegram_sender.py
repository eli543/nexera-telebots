"""Scrubbed, offline transport receipts for fleet engine notices."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["TELEBOTS_SANDBOX_ROOT"])


class TelegramSenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        curl = self.root / "curl"
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$ARGS"\ncat > "$CONFIG"\n'
                        'printf "%s\\n" "$RESPONSE"\nexit "$CURL_EXIT"\n')
        curl.chmod(0o700)
        self.env = dict(os.environ, PATH=f"{self.root}:/usr/bin:/bin", ARGS=str(self.root / "argv"),
                        CONFIG=str(self.root / "config"), RESPONSE='{"ok":true}', CURL_EXIT="0",
                        TELEBOTS_TEST="1", TELEBOTS_API_BASE="http://127.0.0.1:12345")
        self.env.pop("TELEGRAM_BOT_TOKEN", None)

    def send(self, response, curl_exit="0"):
        command = '. "engine/fleet-telegram-send.sh"; telegram_send "fixture-token" "123" "hello"'
        return subprocess.run(["/bin/bash", "-c", command], cwd=REPO,
                              env={**self.env, "RESPONSE": response, "CURL_EXIT": curl_exit},
                              capture_output=True, text=True, check=False)

    def test_sender_distinguishes_accepted_attempted_and_failed(self):
        accepted = self.send('{"ok":true}')
        self.assertEqual((accepted.returncode, accepted.stdout.strip()), (0, "accepted"))
        self.assertNotIn("fixture-token", (self.root / "argv").read_text())
        self.assertIn("fixture-token", (self.root / "config").read_text())
        rejected = self.send('{"ok":false,"error_code":429}')
        self.assertEqual((rejected.returncode, rejected.stdout.strip()), (1, "attempted"))
        failed = self.send('', "7")
        self.assertEqual((failed.returncode, failed.stdout.strip()), (2, "failed"))

    def test_all_engine_notice_paths_use_shared_sender(self):
        for path in ("engine/fleet-fresh", "engine/fleet-update", "engine/fleet-compact", "daemon/watchdog.sh"):
            with self.subTest(path=path):
                self.assertIn("fleet-telegram-send.sh", (REPO / path).read_text())


if __name__ == "__main__":
    unittest.main()
