"""Offline checks for the command and restart engines."""

import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
TOKEN = "12345678:" + "A" * 35
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


class Handler(BaseHTTPRequestHandler):
    calls = []
    getme_ok = True
    config_at_getme = None
    getme_configs = []

    def log_message(self, *_args):
        pass

    def reply(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.config_at_getme:
            self.getme_configs.append(json.loads(Path(self.config_at_getme).read_text()))
        self.reply({"ok": self.getme_ok, "result": {"is_bot": True, "username": "sandbox_test_bot"}})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self.calls.append({"path": self.path.split("/")[-1], "form": parse_qs(body)})
        if self.path.endswith("/v1/messages"):
            self.reply({"content": [{"type": "text", "text": "Weekly report setup"}]})
        else:
            self.reply({"ok": True, "result": {"message_id": 1}})


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.calls = []
        Handler.getme_ok = True
        Handler.getme_configs = []
        sandbox = os.environ["TELEBOTS_SANDBOX_ROOT"]
        self.temp = tempfile.TemporaryDirectory(prefix="engine-case-", dir=sandbox)
        self.base = Path(self.temp.name)
        self.home = self.base / "home.with.dot"
        self.home.mkdir()
        self.fleet = self.home / ".claude" / "telegram-fleet"
        self.fleet.mkdir(parents=True)
        self.config = self.fleet / "fleet.json"
        self.data = {"version": 2, "bots": [], "options": {"rich_stream": True}}
        self.save_config()
        Handler.config_at_getme = self.config
        self.env = os.environ.copy()
        self.env.update({"HOME": str(self.home), "FLEET_HOME": str(self.fleet),
                         "FLEET_CONFIG": str(self.config), "TELEBOTS_TEST": "1",
                         "TELEBOTS_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
                         "TELEBOTS_ANTHROPIC_API_BASE": f"http://127.0.0.1:{self.server.server_port}"})
        self.env.pop("TELEGRAM_BOT_TOKEN", None)
        self.env.pop("TELEGRAM_STATE_DIR", None)

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")

    def bot(self, key="alpha"):
        workdir = self.home / "claude-fleet" / key
        workdir.mkdir(parents=True, exist_ok=True)
        state = self.home / ".claude" / "channels" / ("fleet-" + key)
        state.mkdir(parents=True, exist_ok=True)
        (state / "logs").mkdir(exist_ok=True)
        (state / ".env").write_text("TELEGRAM_BOT_TOKEN=" + TOKEN + "\n", encoding="utf-8")
        (state / "access.json").write_text(json.dumps({"dmPolicy": "pairing", "allowFrom": ["42"],
                                                       "groups": {}, "pending": {}}), encoding="utf-8")
        self.data["bots"].append({"name": key.capitalize(), "key": key, "workdir": str(workdir),
                                  "state_dir": str(state), "emoji": "🤖"})
        self.save_config()
        return workdir, state

    def run_engine(self, name, *args, expect=0, extra=None):
        env = self.env.copy()
        env.update(extra or {})
        result = subprocess.run([str(ENGINE / name), *map(str, args)], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expect, f"{name} exit {result.returncode}: {result.stdout} {result.stderr}")
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result

    def project(self, key="alpha"):
        result = self.run_engine("fleet-config", "project-dir", key)
        path = Path(result.stdout.strip())
        path.mkdir(parents=True, exist_ok=True)
        return path

    def transcript(self, uuid=UUID_A, key="alpha", messages=None):
        records = messages or [
            {"type": "user", "message": {"content": "Hi"}},
            {"type": "user", "message": {"content": "Build a weekly report for the sales team"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "I will make the report from the sales data."}]}},
        ]
        path = self.project(key) / (uuid + ".jsonl")
        path.write_text("\n".join(json.dumps(m) for m in records) + "\n", encoding="utf-8")
        return path

    def test_fleet_config_accepts_empty_v2_and_legacy_v1(self):
        self.run_engine("fleet-config", "validate")
        self.assertEqual(self.run_engine("fleet-config", "bots").stdout, "")
        self.assertEqual(self.run_engine("fleet-config", "option", "voice").stdout.strip(), "auto")
        self.data["version"] = 1
        self.save_config()
        self.run_engine("fleet-config", "validate", expect=1)
        work, _state = self.bot()
        self.run_engine("fleet-config", "validate")
        project = self.run_engine("fleet-config", "project-dir", "alpha").stdout.strip()
        self.assertNotIn(".", Path(project).name)
        self.assertIn("home-with-dot", project)

    def test_add_bot_validates_before_config_and_hides_token(self):
        token_file = self.base / "test-token"
        token_file.write_text(TOKEN, encoding="utf-8")
        settings = {"CC_ADD_BOT_TOKEN_FILE": str(token_file)}
        Handler.getme_ok = False
        self.run_engine("fleet-add-bot", "alpha", expect=1, extra=settings)
        self.assertEqual(json.loads(self.config.read_text())["bots"], [])
        self.assertFalse((self.home / ".claude" / "channels" / "fleet-alpha").exists())
        Handler.getme_ok = True
        out = self.run_engine("fleet-add-bot", "alpha", extra=settings)
        self.assertIn("@sandbox_test_bot ready", out.stdout)
        self.assertEqual([row["bots"] for row in Handler.getme_configs], [[], []])
        state = self.home / ".claude" / "channels" / "fleet-alpha"
        self.assertEqual((state / ".env").stat().st_mode & 0o777, 0o600)
        self.assertTrue((state / "rich-stream").exists())
        self.assertEqual(json.loads(self.config.read_text())["bots"][0]["key"], "alpha")
        self.run_engine("fleet-add-bot", "beta", expect=1, extra=settings)
        self.assertEqual(len(json.loads(self.config.read_text())["bots"]), 1)
        self.data = json.loads(self.config.read_text())
        self.bot("beta")
        self.bot("gamma")
        paced = self.run_engine("fleet-add-bot", "delta", expect=1, extra=settings)
        self.assertIn("BotFather may slow", paced.stdout)
        self.assertEqual(len(json.loads(self.config.read_text())["bots"]), 3)

    def test_pair_uses_selected_state_and_enrolls_only_after_valid_code(self):
        _work, state = self.bot("alpha")
        _other_work, other = self.bot("beta")
        data = json.loads((state / "access.json").read_text())
        data["pending"]["ABC123"] = {"senderId": "77", "chatId": "77",
                                     "expiresAt": int(time.time() * 1000) + 60_000}
        (state / "access.json").write_text(json.dumps(data))
        self.run_engine("fleet-pair", "alpha", "BAD999", "--lock", expect=3,
                        extra={"TELEGRAM_STATE_DIR": str(other)})
        self.assertFalse((state / ".enrolled").exists())
        self.run_engine("fleet-pair", "alpha", "ABC123", "--lock",
                        extra={"TELEGRAM_STATE_DIR": str(other)})
        updated = json.loads((state / "access.json").read_text())
        self.assertEqual(updated["dmPolicy"], "allowlist")
        self.assertIn("77", updated["allowFrom"])
        self.assertNotIn("ABC123", updated["pending"])
        self.assertEqual((state / "approved" / "77").read_text().strip(), "77")
        self.assertTrue((state / ".enrolled").exists())
        self.assertEqual(json.loads((other / "access.json").read_text())["allowFrom"], ["42"])

    def test_pair_rejects_expired_code_without_enrollment(self):
        _work, state = self.bot()
        access = json.loads((state / "access.json").read_text())
        access["pending"]["OLD123"] = {"senderId": "77", "chatId": "77", "expiresAt": 1}
        (state / "access.json").write_text(json.dumps(access))
        self.run_engine("fleet-pair", "alpha", "OLD123", "--lock", expect=3)
        self.assertEqual(json.loads((state / "access.json").read_text()), access)
        self.assertFalse((state / ".enrolled").exists())

    def test_start_offers_one_local_dialog_for_new_code_and_pairs_only_on_pair(self):
        _work, state = self.bot()
        shims = self.base / "pair-shims"
        shims.mkdir()
        dialog_log = self.base / "pair-dialog.log"
        dialog = shims / "osascript"
        dialog.write_text('#!/bin/sh\nprintf "dialog\\n" >> "$PAIR_DIALOG_LOG"\nprintf "%s\\n" "$PAIR_DECISION"\n')
        dialog.chmod(0o700)
        extra = {"CC_START_PAIR_ONLY": "1", "CC_START_PAIR_WAIT": "3",
                 "PAIR_DIALOG_LOG": str(dialog_log), "PAIR_DECISION": "Pair",
                 "PATH": str(shims) + ":" + self.env["PATH"]}

        def pending(code):
            time.sleep(0.3)
            access = json.loads((state / "access.json").read_text())
            access["pending"][code] = {"senderId": "77", "chatId": "77",
                                        "createdAt": int(time.time() * 1000),
                                        "expiresAt": int(time.time() * 1000) + 60_000}
            (state / "access.json").write_text(json.dumps(access))

        writer = threading.Thread(target=pending, args=("a1b2c3",))
        writer.start()
        result = self.run_engine("fleet-start", "alpha", extra=extra)
        writer.join()
        self.assertIn("Paired bot alpha and locked", result.stdout)
        self.assertEqual(dialog_log.read_text().splitlines(), ["dialog"])
        self.assertTrue((state / ".enrolled").exists())
        self.assertEqual(json.loads((state / "access.json").read_text())["allowFrom"], ["42", "77"])

        (state / ".enrolled").unlink()
        access = json.loads((state / "access.json").read_text())
        access["dmPolicy"] = "pairing"
        (state / "access.json").write_text(json.dumps(access))
        writer = threading.Thread(target=pending, args=("d4e5f6",))
        writer.start()
        ignored = self.run_engine("fleet-start", "alpha", extra={**extra, "PAIR_DECISION": "Ignore"})
        writer.join()
        self.assertIn("pairing ignored", ignored.stdout)
        self.assertEqual(dialog_log.read_text().splitlines(), ["dialog", "dialog"])
        self.assertFalse((state / ".enrolled").exists())
        self.assertIn("d4e5f6", json.loads((state / "access.json").read_text())["pending"])

    def test_start_launch_fresh_update_and_ready_bubbles(self):
        work, state = self.bot()
        start = self.run_engine("fleet-start", "alpha", extra={"CC_START_DRYRUN": "1"})
        self.assertIn("Terminal window", start.stdout)
        launch = self.run_engine("fleet-launch", "alpha", expect=0, extra={"CC_LAUNCH_DRYRUN": "1"})
        self.assertIn("--name telebots-alpha", launch.stdout)
        (self.fleet / "overlay.status").write_text("VERSION_MISMATCH:0.0.8\n")
        self.run_engine("fleet-fresh", "999999", work, UUID_A, extra={"CC_FRESH_DRYRUN": "1"})
        fresh_log = (self.fleet / "logs" / "fresh.log").read_text()
        self.assertIn("proof mode=pid-only", fresh_log)
        self.assertIn("Continue bubble: yes", fresh_log)
        self.run_engine("fleet-fresh", "999999", work, UUID_A, extra={"CC_FRESH_READY_DRYRUN": "1"})
        sent = Handler.calls[-1]["form"]
        self.assertEqual(sent["text"], ["⚡ Back and ready"])
        self.assertIn("Continue", sent["reply_markup"][0])
        self.transcript()
        (state / "session.id").write_text(UUID_A + "\n")
        continued = self.run_engine("fleet-start", "alpha", "continue", extra={"CC_START_DRYRUN": "1"})
        self.assertIn("resume=" + UUID_A, continued.stdout)
        update = self.run_engine("fleet-update", "999999", work, "42", extra={"CC_UPDATE_DRYRUN": "1"})
        self.assertIn("target=" + UUID_A, update.stdout)
        self.run_engine("fleet-update", "999999", work, "42", extra={"CC_UPDATE_READY_DRYRUN": "1"})
        self.assertIn("Continue", Handler.calls[-1]["form"]["reply_markup"][0])

    def test_start_refuses_a_named_session_for_the_same_bot(self):
        work, _state = self.bot()
        fakebin = self.base / "probe-shims"
        fakebin.mkdir()
        ps = fakebin / "ps"
        ps.write_text("#!/bin/sh\nprintf '%s\\n' '999998 claude --channels plugin:telegram@claude-plugins-official --name telebots-alpha'\n")
        ps.chmod(0o700)
        lsof = fakebin / "lsof"
        lsof.write_text("#!/bin/sh\nprintf 'n%s\\n' '" + str(work) + "'\n")
        lsof.chmod(0o700)
        out = self.run_engine("fleet-start", "alpha", expect=7,
                              extra={"CC_START_DRYRUN": "1", "PATH": str(fakebin) + ":" + self.env["PATH"]})
        self.assertIn("already has a running session", out.stderr)

    def test_usage_dedupes_request_and_reads_subagents_with_budget(self):
        work, _state = self.bot()
        now = datetime.now(timezone.utc).isoformat()
        base = {"type": "assistant", "timestamp": now, "requestId": "req1",
                "message": {"id": "msg1", "model": "claude-sonnet", "usage":
                            {"output_tokens": 30, "input_tokens": 10, "cache_creation_input_tokens": 5}}}
        path = self.transcript(messages=[dict(base, uuid=str(i)) for i in range(3)])
        subdir = path.parent / UUID_A / "subagents"
        subdir.mkdir(parents=True)
        sub = dict(base, uuid="sub1", requestId="req2")
        sub["message"] = {**base["message"], "id": "msg2", "usage":
                          {"output_tokens": 70, "input_tokens": 20, "cache_creation_input_tokens": 0}}
        (subdir / "worker.jsonl").write_text(json.dumps(sub) + "\n")
        limits = {"limits": [{"kind": "session", "percent": 30,
                              "resets_at": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()},
                             {"kind": "weekly_all", "percent": 10,
                              "resets_at": (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()}]}
        fixture = self.base / "limits.json"
        fixture.write_text(json.dumps(limits))
        self.run_engine("fleet-usage", work, "42", extra={"CC_USAGE_DRYRUN": "1", "CC_USAGE_LIMITS_FILE": str(fixture)})
        report = (self.fleet / "logs" / "usage.log").read_text()
        self.assertIn("FLEET BURN TODAY: 100 tokens across 2 responses", report)
        self.assertIn("subagents included", report)
        self.assertIn("over budget", report)
        self.assertIn("WEEK - ALL MODELS", report)

    def test_sessions_render_opening_labels_and_optional_key(self):
        self.bot()
        self.transcript()
        result = self.run_engine("fleet-sessions", "render", "--limit", "20", "--bot", "alpha", "--style", "inline")
        data = json.loads(result.stdout)
        self.assertEqual(data["count"], 1)
        self.assertIn("Build a weekly report", data["text"])
        self.assertEqual(data["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "rs:" + UUID_A)
        self.assertIn("Refresh", json.dumps(data["reply_markup"]))
        resolve = self.run_engine("fleet-sessions", "resolve-uuid", UUID_A, "--bot", "alpha")
        self.assertIn(UUID_A, resolve.stdout)
        result = self.run_engine("fleet-sessions", "render", "--bot", "alpha", "--style", "inline",
                                 extra={"ANTHROPIC_API_KEY_TLDR": "fake-test-key"})
        self.assertIn("Weekly report setup", json.loads(result.stdout)["text"])
        self.assertEqual(Handler.calls[-1]["path"], "messages")
        self.assertTrue((self.fleet / "logs" / "sessions.log").read_text().count("RENDER count=1 labels=1") >= 2)

    def test_sessions_cap_at_twenty_and_keep_locked_topic(self):
        self.bot()
        for number in range(22):
            uuid = f"{number + 1:08d}-1111-4111-8111-111111111111"
            self.transcript(uuid=uuid, messages=[
                {"type": "user", "message": {"content": "Build a distinct report " + str(number)}},
                {"type": "user", "message": {"content": "For the weekly meeting"}},
                {"type": "user", "message": {"content": "Include the sales total"}},
            ])
        rendered = json.loads(self.run_engine("fleet-sessions", "render", "--limit", "20",
                                              "--bot", "alpha", "--style", "inline").stdout)
        self.assertEqual(rendered["count"], 20)
        buttons = [cell for row in rendered["reply_markup"]["inline_keyboard"]
                   for cell in row if cell["callback_data"].startswith("rs:")]
        self.assertEqual(len(buttons), 20)
        self.assertTrue(all(len(cell["callback_data"]) <= 64 for cell in buttons))
        cached = json.loads((self.fleet / "resume-labels.json").read_text())
        self.assertTrue(all(item["locked"] for item in cached.values()))

    def test_context_percent_state_resume_and_handoff_fail_closed(self):
        work, state = self.bot()
        other_work, _other_state = self.bot("beta")
        record = {"type": "assistant", "message": {"model": "claude-sonnet", "usage":
                  {"input_tokens": 100000, "cache_read_input_tokens": 50000,
                   "cache_creation_input_tokens": 0}}}
        transcript = self.transcript(messages=[record])
        (state / "session.id").write_text(UUID_A + "\n")
        (state / "poller.heartbeat").write_text(str(int(time.time())))
        (self.fleet / "overlay.status").write_text("OK\n")
        pct = self.run_engine("fleet-context-pct", transcript)
        self.assertEqual(pct.stdout.strip(), "75")
        state_report = self.run_engine("fleet-state", "--json")
        rows = json.loads(state_report.stdout)
        self.assertEqual(rows[0]["context_pct"], 75)
        self.assertTrue(rows[0]["up"])
        (self.fleet / "overlay.status").write_text("VERSION_MISMATCH:0.0.8\n")
        fakebin = self.base / "health-probes"
        fakebin.mkdir()
        ps = fakebin / "ps"
        ps.write_text("#!/bin/sh\nprintf '%s\\n' '999998 claude --channels plugin:telegram@claude-plugins-official --name telebots-alpha'\n")
        ps.chmod(0o700)
        lsof = fakebin / "lsof"
        lsof.write_text("#!/bin/sh\nprintf 'n%s\\n' '" + str(work) + "'\n")
        lsof.chmod(0o700)
        degraded = json.loads(self.run_engine("fleet-state", "--json", extra={
            "PATH": str(fakebin) + ":" + self.env["PATH"]}).stdout)
        self.assertTrue(degraded[0]["up"])
        self.run_engine("fleet-resume-open", UUID_A, work, "42", expect=9,
                        extra={"CC_RESUME_DRYRUN": "1"})
        resume = self.run_engine("fleet-resume-open", UUID_A, work, "42",
                                 extra={"CC_RESUME_DRYRUN": "1", "CC_RESUME_TEST_PID": "999998"})
        self.assertIn("DRYRUN would copy", resume.stdout)
        self.run_engine("fleet-handoff", "fresh", "alpha", expect=3,
                        extra={"CC_HANDOFF_DRYRUN": "1", "TELEGRAM_STATE_DIR": str(state)})
        handoff = self.run_engine("fleet-handoff", "fresh", "beta",
                                  extra={"CC_HANDOFF_DRYRUN": "1", "CC_HANDOFF_TEST_PID": "999998",
                                         "TELEGRAM_STATE_DIR": str(state)})
        self.assertIn("no note delivered", handoff.stdout)

    def test_compact_classifier_uses_fixture_without_osascript(self):
        work, _state = self.bot()
        rule = "─" * 24
        frame = self.base / "idle-frame.txt"
        frame.write_text(rule + "\n❯ \n" + rule + "\n⏵⏵ permission mode\n")
        self.run_engine("fleet-compact", "999998", work, "keep\nall facts PASSWORD=private-test-value",
                        extra={"CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
        log = (self.fleet / "logs" / "compact.log").read_text()
        self.assertIn("DRYRUN screen state on /dev/ttys999: IDLE", log)
        self.assertIn("focus sanitized", log)
        self.assertNotIn("private-test-value", log)
        frame.write_text("No, exit\nEnter to select\n")
        self.run_engine("fleet-compact", "999998", work, extra={"CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
        self.assertIn("PICKER", (self.fleet / "logs" / "compact.log").read_text())

    def test_every_engine_help_names_arguments_and_exit_codes(self):
        engines = ["fleet-config", "fleet-context", "fleet-esc", "fleet-fresh", "fleet-launch",
                   "fleet-model", "fleet-usage", "fleet-sessions", "fleet-start", "fleet-add-bot",
                   "fleet-pair", "fleet-update", "fleet-compact", "fleet-resume-open",
                   "fleet-state", "fleet-context-pct", "fleet-handoff"]
        for name in engines:
            with self.subTest(name=name):
                out = self.run_engine(name, "--help")
                self.assertIn("exit", (out.stdout + out.stderr).lower())


if __name__ == "__main__":
    unittest.main()
