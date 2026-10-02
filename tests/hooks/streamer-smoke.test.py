#!/usr/bin/env python3
"""Loopback-only transport and lifecycle checks for the live streamer port.

The optional --require-404-fallback check records a known contract conflict:
v30.22 retries unsupported rich endpoints instead of switching to plain text.
"""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs


REQUIRE_404_FALLBACK = "--require-404-fallback" in sys.argv
if REQUIRE_404_FALLBACK:
    sys.argv.remove("--require-404-fallback")

SPEC = importlib.util.spec_from_file_location(
    "streamer", Path(__file__).resolve().parents[2] / "plugin/hooks/tg-stream.py")
streamer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(streamer)


class MockTelegram(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        params = {key: values[0] for key, values in parse_qs(body.decode()).items()}
        method = self.path.rsplit("/", 1)[-1]
        self.server.calls.append((method, params))
        code = self.server.rich_code if method == "sendRichMessage" else 200
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        result = {"ok": True, "result": {"message_id": len(self.server.calls) + 100}}
        if code != 200:
            result = {"ok": False, "error_code": code,
                      "description": "mock API refusal", "parameters": {"retry_after": 6}}
        self.wfile.write(json.dumps(result).encode())


class StreamerSmoke(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.environ.get("TELEBOTS_TEST"), "1", "Use a scrubbed sandbox")
        self.tmp = tempfile.TemporaryDirectory(prefix="streamer-smoke-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "bot"
        self.sdir = self.state / "stream"
        self.sdir.mkdir(parents=True)
        (self.state / "rich-stream").touch()
        self.fleet = self.root / "fleet"
        self.fleet.mkdir()
        (self.fleet / "fleet.json").write_text(json.dumps({"bots": [{
            "key": "main", "name": "Main", "emoji": "🤖", "state_dir": str(self.state)
        }], "options": {}}))
        self.env = patch.dict(os.environ, {
            "FLEET_HOME": str(self.fleet), "TELEGRAM_STATE_DIR": str(self.state),
            "STREAM_NO_SPAWN": "1", "STREAM_DRY": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = HTTPServer(("127.0.0.1", 0), MockTelegram)
        self.server.rich_code = 200
        self.server.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tg = streamer.Telegram("TEST_ONLY", "101", False, sdir=str(self.sdir))
        # An in-process test object only; production reads no API override.
        self.tg.base = "http://127.0.0.1:%s/botTEST_ONLY/" % self.server.server_port
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def prompt(self):
        deferred = streamer.on_prompt(str(self.sdir), self.tg, "Check the demo project",
                                      session_id="synthetic-session")
        streamer._run_deferred(str(self.sdir), self.tg, deferred)

    def test_mission_plan_progress_and_fold(self):
        self.prompt()
        st = streamer.load_state(str(self.sdir))
        self.assertTrue(st.get("rich_box"))
        frame = json.loads(next(params["rich_message"] for method, params in self.server.calls
                                if method == "sendRichMessage"))["markdown"]
        self.assertIn("🎯", frame)
        self.assertEqual(streamer.bot_identity(str(self.sdir)), ("Main", "🤖"))
        (self.state / "mission.txt").write_text("Check the demo project")
        for mode, response in [("pre", None), ("post", {"task": {"id": "1", "subject": "Inspect project"}})]:
            deferred = streamer.handle_step(str(self.sdir), self.tg, mode, "TaskCreate",
                {"subject": "Inspect project", "activeForm": "Inspecting project"},
                "create-1", None, None, tresponse=response, session_id="synthetic-session")
            streamer._run_deferred(str(self.sdir), self.tg, deferred)
        st = streamer.load_state(str(self.sdir))
        self.assertEqual(st["rich_plan"][0]["subject"], "Inspect project")
        deferred = streamer.handle_step(str(self.sdir), self.tg, "post", "TaskUpdate",
            {"taskId": "1", "status": "completed"}, "update-1", None, None,
            tresponse={"success": True, "taskId": "1", "updatedFields": ["status"]},
            session_id="synthetic-session")
        streamer._run_deferred(str(self.sdir), self.tg, deferred)
        st = streamer.load_state(str(self.sdir))
        self.assertEqual(st["rich_plan"][0]["status"], "completed")
        live = streamer.render_rich_live(str(self.sdir), st["steps"], st["proc_start"],
            st.get("agents", {}), "Check the demo project", st.get("panel"),
            st["rich_plan"], state=st)
        self.assertIn("Plan", live)
        self.assertIn("Inspect project", live)
        self.assertTrue(self.tg.edit_rich(st["msgid"], live)["ok"])
        streamer.record_reply(str(self.sdir), self.tg, final=False)
        deferred = streamer.record_reply(str(self.sdir), self.tg, final=True)
        streamer._run_deferred(str(self.sdir), self.tg, deferred)
        st = streamer.load_state(str(self.sdir))
        self.assertTrue(st.get("typing_done"))
        frames = [json.loads(params["rich_message"])["markdown"]
                  for method, params in self.server.calls if "rich_message" in params]
        self.assertTrue(any("What I did" in frame or "Completed" in frame or "Done" in frame
                            for frame in frames), frames[-1][:400])
        print("PASS: loopback mission, TaskCreate/TaskUpdate Plan, progress and final fold")

    def test_content_refusal_uses_plain_fallback(self):
        self.server.rich_code = 400
        self.prompt()
        methods = [method for method, params in self.server.calls]
        self.assertIn("sendMessage", methods)
        self.assertFalse(streamer.load_state(str(self.sdir)).get("rich_box"))
        print("PASS: rich content refusal uses plain fallback")

    def test_404_retains_live_retry_behavior(self):
        self.server.rich_code = 404
        self.prompt()
        st = streamer.load_state(str(self.sdir))
        methods = [method for method, params in self.server.calls]
        if REQUIRE_404_FALLBACK:
            self.assertIn("sendMessage", methods,
                          "F.2 requires 404 fallback; pinned v30.22 does not provide it")
        else:
            self.assertNotIn("sendMessage", methods)
            self.assertTrue(st.get("rich_pending_open"))
            print("OBSERVED: HTTP 404 leaves a pending rich open; F.2 fallback is unmet")

    def test_429_cooldown_prevents_another_request(self):
        self.server.rich_code = 429
        frame = streamer.render_rich_live(str(self.sdir), [], time.time(), {}, "Test cooldown")
        result = self.tg.send_rich(frame)
        self.assertEqual(result["error_class"], "rate_limit")
        before = len(self.server.calls)
        self.assertGreater(self.tg.cooldown_remaining(), 0)
        self.tg.send_rich(frame)
        self.assertEqual(len(self.server.calls), before)
        print("PASS: HTTP 429 arms cooldown and suppresses a second request")


if __name__ == "__main__":
    unittest.main(verbosity=2)
