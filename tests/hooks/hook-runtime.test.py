#!/usr/bin/env python3
"""Offline hook integration checks. Run through the scrubbed sandbox wrapper."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugin/hooks"
sys.path.insert(0, str(HOOKS))
if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper, without a token in the environment.")


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HOOKS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Hooks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hooks-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.fleet = self.root / "fleet"
        self.workdir = self.root / "work"
        for path in (self.state, self.fleet / "bin", self.workdir):
            path.mkdir(parents=True)
        self.env = dict(os.environ, TELEGRAM_STATE_DIR=str(self.state), FLEET_HOME=str(self.fleet))
        self.env.pop("TELEGRAM_BOT_TOKEN", None)
        self.configure()
        (self.state / "access.json").write_text(json.dumps({"allowFrom": ["4242"]}))
        (self.state / "model-state").write_text("MODEL=sonnet\n")
        self.transcript = self.root / "turn.jsonl"
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {
            "model": "claude-sonnet", "usage": {"input_tokens": 20_000}}}) + "\n" +
            json.dumps({"type": "assistant", "message": {
                "model": "claude-sonnet", "usage": {"input_tokens": 122_000}}}) + "\n")
        helper = self.fleet / "bin/fleet-context-pct"
        helper.write_text("""#!/usr/bin/env python3
import json,sys
def context_cap(served, state): return 200000
if __name__ == '__main__':
    rows=[json.loads(line) for line in open(sys.argv[1]) if line.strip()]
    usages=[row['message']['usage']['input_tokens'] for row in rows if row.get('type')=='assistant']
    print(round(usages[-1]/context_cap('', '')*100))
""")
        helper.chmod(0o755)

    def configure(self, bots=1, thresholds="60,70,80,90", judge=False):
        rows = [{"name": "Main", "key": "main", "state_dir": str(self.state), "workdir": str(self.workdir)}]
        if bots > 1:
            rows.append({"name": "Helper", "key": "helper", "state_dir": str(self.root / "second"),
                         "workdir": str(self.root / "second-work")})
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": rows,
            "options": {"guardian_thresholds": thresholds, "tldr_judge": judge}}))

    def run_hook(self, file, event=None, args=(), extra=None, state=True):
        env = dict(self.env)
        if not state:
            env.pop("TELEGRAM_STATE_DIR", None)
        env.update(extra or {})
        interpreter = "/bin/bash" if file.endswith(".sh") else sys.executable
        return subprocess.run([interpreter, str(HOOKS / file), *args],
            input=json.dumps(event or {}), capture_output=True, text=True, env=env)

    def event(self, **extra):
        return {"session_id": "session-fixture", "transcript_path": str(self.transcript), **extra}

    def test_every_hook_registration_is_inert_without_state(self):
        env = dict(self.env, CLAUDE_PLUGIN_ROOT=str(ROOT / "plugin"))
        env.pop("TELEGRAM_STATE_DIR", None)
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        groups = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
        count = 0
        for event, entries in groups.items():
            for entry in entries:
                for hook in entry["hooks"]:
                    result = subprocess.run(["/bin/bash", "-c", hook["command"]], input="{}",
                        capture_output=True, text=True, env=env)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), (event, hook))
                    count += 1
        self.assertGreaterEqual(count, 17)
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_direct_hooks_are_inert_without_state(self):
        files = {"telegram-fresh.sh": (), "telegram-model.sh": (), "telegram-resume.sh": (),
                 "context-guardian.sh": (), "compact-notify.sh": ("pre",),
                 "record-session-id.sh": (), "telegram-ack.py": (), "tldr-nudge.py": (),
                 "tg-deliver-nudge.py": (), "tg-reply-guard.py": ("prompt",)}
        for file, args in files.items():
            result = self.run_hook(file, {"tool_name": "Write", "tool_input": {"file_path": "/tmp/report.pdf"}}, args, state=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), file)

    def test_synthetic_slash_labels_never_run_fresh_or_model(self):
        config = self.fleet / "bin/fleet-config"
        config.write_text("#!/usr/bin/env python3\nimport sys\n"
                          "print('main' if sys.argv[1] == 'bot-by-state' else '" + str(self.workdir) + "')\n")
        config.chmod(0o700)
        for name in ("fleet-fresh", "fleet-model"):
            engine = self.fleet / "bin" / name
            engine.write_text("#!/bin/sh\nprintf 'called\\n' >> '" + str(self.root / "engine-calls") + "'\n")
            engine.chmod(0o700)
        for via, command, hook in (("button", "/fresh", "telegram-fresh.sh"),
                                   ("reaction", "/model", "telegram-model.sh"),
                                   ("email_confirm", "/fresh", "telegram-fresh.sh"),
                                   ("button", "/model opus", "telegram-model.sh")):
            prompt = f'<channel source="plugin:telegram:telegram" chat_id="4242" via="{via}" message_id="9">{command}</channel>'
            result = self.run_hook(hook, self.event(prompt=prompt))
            self.assertEqual(result.returncode, 0, (via, command, result.stderr))
        self.assertFalse((self.root / "engine-calls").exists())

    def test_judge_is_opt_in_and_uses_unpinned_service(self):
        tn = module("tldr-nudge")
        with patch.dict(os.environ, self.env, clear=True), patch.object(tn.subprocess, "run") as key:
            self.assertTrue(tn._judge_off(str(self.state)))
            self.assertIsNone(tn.number_reason("A large response. " * 200, str(self.state)))
            key.assert_not_called()
            self.configure(judge=True)
            self.assertFalse(tn._judge_off(str(self.state)))
            key.return_value.stdout = "synthetic-key"
            self.assertEqual(tn._api_key(), "synthetic-key")
            argv = key.call_args.args[0]
            self.assertIn("nexera-telebots-judge", argv)
            self.assertNotIn("-a", argv)
            (self.state / "tldr_judge_off").touch()
            self.assertTrue(tn._judge_off(str(self.state)))

    def test_reply_guard_once_and_success_marker(self):
        self.transcript.write_text(json.dumps({"type": "user", "message": {"content": "Status?"}}) + "\n" +
            json.dumps({"type": "assistant", "message": {"content": "The requested local work is complete and its checks have passed."}}) + "\n")
        self.run_hook("tg-reply-guard.py", args=("prompt",))
        result = self.run_hook("tg-reply-guard.py", self.event(), ("stop",))
        self.assertEqual(json.loads(result.stdout)["decision"], "block")
        self.assertEqual(self.run_hook("tg-reply-guard.py", self.event(), ("stop",)).stdout, "")
        self.run_hook("tg-reply-guard.py", args=("prompt",))
        self.run_hook("tg-reply-guard.py", args=("reply",))
        self.assertEqual(self.run_hook("tg-reply-guard.py", self.event(), ("stop",)).stdout, "")

    def test_reply_guard_cooldown_skips_only_live_flood_ban(self):
        guard = module("tg-reply-guard")
        (self.state / "stream").mkdir()
        for seconds, expected in ((3600, True), (5, False), (-1, False), (900000, False)):
            (self.state / "stream/ratelimit.json").write_text(json.dumps({"cooldown": {"4242": {"until": time.time()+seconds}}}))
            self.assertEqual(guard._cooldown_until(str(self.state)) > 0, expected)

    def test_guardian_61_and_91_single_bot_cells(self):
        for percent in (61, 91):
            self.transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": percent * 2000}}}) + "\n")
            result = self.run_hook("context-guardian.sh", self.event(), extra={"CC_GUARDIAN_DRYRUN": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            dry = json.loads(result.stdout)
            self.assertEqual(dry["threshold"], 60 if percent == 61 else 90)
            row = dry["payload"]["reply_markup"]["inline_keyboard"][0]
            self.assertEqual([(b["text"], b["callback_data"]) for b in row],
                             [("Keep going", "qr:0:0"), ("Compact now", "qr:0:1")])
            self.assertFalse(list(self.state.glob("guardian-nudged-*")))

    def test_guardian_two_bots_and_threshold_override(self):
        self.configure(bots=2, thresholds="55,75")
        result = self.run_hook("context-guardian.sh", self.event(), extra={"CC_GUARDIAN_DRYRUN": "1"})
        data = json.loads(result.stdout)
        self.assertEqual(data["threshold"], 55)
        row = data["payload"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in row], ["ho:pick", "qr:0:1", "qr:0:2"])
        (self.state / "guardian-thresholds").write_text("1")
        result = self.run_hook("context-guardian.sh", self.event(), extra={"CC_GUARDIAN_DRYRUN": "1"})
        self.assertEqual(json.loads(result.stdout)["threshold"], 1)

    def test_guardian_marks_all_crossed_once_and_logs_no_payload(self):
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 182000}}}) + "\n")
        guardian = module("context-guardian")
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(self.event()))), patch.object(guardian, "telegram", return_value={"ok": True}) as send:
            guardian.main()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(len(list(self.state.glob("guardian-nudged-session-fixture-*"))), 4)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(self.event()))), patch.object(guardian, "telegram") as send:
            guardian.main()
            send.assert_not_called()
        log = (self.state / "logs/guardian.log").read_text()
        self.assertIn("reason=8", log)
        self.assertNotIn("Context at", log)

    def test_guardian_failed_delivery_keeps_thresholds_for_retry(self):
        guardian = module("context-guardian")
        bot = {"name": "Main"}
        with patch.dict(os.environ, self.env, clear=True), patch.object(guardian, "telegram", return_value={"ok": False}) as send:
            for _ in range(2):
                guardian.nudge(self.state, bot, 1, "retry-session", 91, {})
            self.assertEqual(send.call_count, 2)
            self.assertFalse(list(self.state.glob("guardian-nudged-*")))
            send.return_value = {"ok": True}
            guardian.nudge(self.state, bot, 1, "retry-session", 91, {})
            self.assertEqual(len(list(self.state.glob("guardian-nudged-retry-session-*"))), 4)
            guardian.nudge(self.state, bot, 1, "retry-session", 91, {})
            self.assertEqual(send.call_count, 3)

    def test_guardian_missing_chat_keeps_thresholds(self):
        guardian = module("context-guardian")
        (self.state / "access.json").unlink()
        with patch.dict(os.environ, self.env, clear=True), patch.object(guardian, "telegram") as send:
            guardian.nudge(self.state, {"name": "Main"}, 1, "retry-session", 91, {})
            send.assert_not_called()
        self.assertFalse(list(self.state.glob("guardian-nudged-*")))

    def test_guardian_lock_prevents_concurrent_duplicate_alerts(self):
        guardian = module("context-guardian")
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"ok": True}
        with patch.dict(os.environ, self.env, clear=True), patch.object(guardian, "telegram", side_effect=send) as api:
            first = threading.Thread(target=guardian.nudge, args=(self.state, {"name": "Main"}, 1, "race", 91, {}), daemon=True)
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                guardian.nudge(self.state, {"name": "Main"}, 1, "race", 91, {})
                self.assertEqual(api.call_count, 1)
                self.assertFalse(list(self.state.glob("guardian-nudged-*")))
            finally:
                release.set()
                first.join(5)
            self.assertFalse(first.is_alive())
            guardian.nudge(self.state, {"name": "Main"}, 1, "race", 91, {})
            self.assertEqual(api.call_count, 1)
        self.assertEqual(len(list(self.state.glob("guardian-nudged-race-*"))), 4)

    def test_compact_pre_post_dry_run_and_rearm(self):
        dry = {"CC_COMPACT_DRYRUN": "1"}
        result = self.run_hook("compact-notify.sh", self.event(trigger="auto"), ("pre",), dry)
        self.assertIn("at 61%", json.loads(result.stdout)["text"])
        before = json.loads((self.state / "compact-started").read_text())
        self.assertEqual(before["pct_before"], "61")
        (self.state / "compact-inflight").touch()
        (self.state / "guardian-nudged-session-fixture-60").touch()
        with self.transcript.open("a") as handle:
            import datetime
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            handle.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": stamp,
                                     "compactMetadata": {"postTokens": 10000}}) + "\n")
        result = self.run_hook("compact-notify.sh", self.event(source="compact"), ("post",), dry)
        lines = result.stdout.splitlines()
        self.assertIn("from 61% to about 15%", json.loads(lines[0])["text"])
        self.assertIn("Do not repeat", lines[1])
        self.assertFalse((self.state / "compact-started").exists())
        self.assertFalse((self.state / "compact-inflight").exists())
        self.assertFalse(list(self.state.glob("guardian-nudged-*")))

    def test_compact_ignores_historical_boundary_for_fresh_summary(self):
        with self.transcript.open("a") as handle:
            handle.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2020-01-01T00:00:00Z",
                                     "compactMetadata": {"postTokens": 160000}}) + "\n")
        dry = {"CC_COMPACT_DRYRUN": "1"}
        self.run_hook("compact-notify.sh", self.event(trigger="auto"), ("pre",), dry)
        with self.transcript.open("a") as handle:
            handle.write(json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "s" * 8000}}) + "\n")
        result = self.run_hook("compact-notify.sh", self.event(source="compact"), ("post",), dry)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertIn("about 11%", payload["text"])
        self.assertNotIn("about 90%", payload["text"])
        self.assertIn("after_source=summary", (self.state / "logs/compact.log").read_text())

    def test_compact_ignores_historical_boundary_and_summary_without_fresh_data(self):
        with self.transcript.open("a") as handle:
            handle.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2020-01-01T00:00:00Z",
                                     "compactMetadata": {"postTokens": 160000}}) + "\n")
            handle.write(json.dumps({"type": "user", "isCompactSummary": True, "message": {"content": "s" * 8000}}) + "\n")
        dry = {"CC_COMPACT_DRYRUN": "1"}
        self.run_hook("compact-notify.sh", self.event(trigger="auto"), ("pre",), dry)
        result = self.run_hook("compact-notify.sh", self.event(source="compact"), ("post",), dry)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertIn("context was 61% before it", payload["text"])
        self.assertNotIn("about", payload["text"])
        self.assertIn("after_source=unknown", (self.state / "logs/compact.log").read_text())

    def test_compact_manual_and_subagent_paths(self):
        dry = {"CC_COMPACT_DRYRUN": "1"}
        result = self.run_hook("compact-notify.sh", self.event(trigger="manual"), ("pre",), dry)
        self.assertEqual(result.stdout, "")
        marker = (self.state / "compact-started").read_bytes()
        for mode, event in (("pre", self.event(trigger="auto", agent_id="side-agent")),
                            ("post", self.event(source="compact", agent_id="side-agent"))):
            result = self.run_hook("compact-notify.sh", event, (mode,), dry)
            self.assertEqual(result.stdout, "")
            self.assertEqual((self.state / "compact-started").read_bytes(), marker)
        self.assertIn("reason=15", (self.state / "logs/compact.log").read_text())

    def test_compact_followup_fresh_boundary_and_timeout(self):
        (self.state / ".env").write_text("TELEGRAM_BOT_TOKEN=synthetic-test-value\n")
        now = time.time()
        import datetime
        stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        with self.transcript.open("a") as handle:
            handle.write(json.dumps({"subtype": "compact_boundary", "timestamp": stamp, "compactMetadata": {"postTokens": 10000}}) + "\n")
        env = {"CC_COMPACT_DRYRUN": "1", "CC_FU_CHAT": "4242", "CC_FU_MSG": "9", "CC_FU_BOT": "Main",
               "CC_FU_TRANSCRIPT": str(self.transcript), "CC_FU_START": str(now), "CC_COMPACT_FOLLOWUP_WAIT": "0"}
        result = self.run_hook("compact-notify.sh", args=("followup",), extra=env)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["method"], "editMessageText")
        self.assertIn("about 15%", payload["text"])
        env["CC_FU_START"] = str(now + 60)
        result = self.run_hook("compact-notify.sh", args=("followup",), extra=env)
        self.assertEqual(result.stdout, "")
        self.assertIn("reason=13", (self.state / "logs/compact.log").read_text())

    def test_session_id_is_recorded_without_subagent_overwrite(self):
        self.run_hook("record-session-id.sh", {"session_id": "main-id"})
        self.assertEqual((self.state / "session.id").read_text(), "main-id\n")
        for event in ({"session_id": "child-id", "agent_id": "child"}, {"session_id": "bad\nvalue"}):
            self.run_hook("record-session-id.sh", event)
            self.assertEqual((self.state / "session.id").read_text(), "main-id\n")

    def install_lister(self):
        lister = self.fleet / "bin/fleet-sessions"
        lister.write_text("""#!/usr/bin/env python3
import json,os,sys
with open(os.environ['STUB_LOG'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='render':
    if os.environ.get('STUB_FAIL_RENDER'): sys.exit(1)
    print(json.dumps({'style': sys.argv[-1], 'text': '1. Main - Test the report', 'reply_markup': {'inline_keyboard': [[{'text':'Close','callback_data':'rs:close'}]]}}))
elif sys.argv[1]=='list': print('1. Main - Test the report')
elif sys.argv[1]=='resolve': print('00000000-0000-4000-8000-000000000001')
""")
        lister.chmod(0o755)
        engine = self.fleet / "bin/fleet-resume-open"
        engine.write_text("#!/bin/sh\nexit 0\n")
        engine.chmod(0o755)
        return {"CC_RESUME_DRYRUN": "1", "STUB_LOG": str(self.root / "lister.jsonl")}

    def channel(self, text):
        return self.event(prompt='<channel source="plugin:telegram:telegram" chat_id="4242" message_id="9">'+text+'</channel>')

    def test_resume_stub_lister_contract_and_generation(self):
        env = self.install_lister()
        result = self.run_hook("telegram-resume.sh", self.channel("/resume"), extra=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Test the report", json.loads(result.stdout)["payload"]["text"])
        args = json.loads((self.root / "lister.jsonl").read_text().splitlines()[0])
        self.assertEqual(args, ["render", "--limit", "20", "--exclude-uuid", "session-fixture", "--bot", "main", "--style", "reply"])
        result = self.run_hook("telegram-resume.sh", self.channel("/resume 07 gabc123"), extra=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["args"], ["00000000-0000-4000-8000-000000000001", str(self.workdir), "4242"])
        self.assertEqual(json.loads((self.root / "lister.jsonl").read_text().splitlines()[-1]), ["resolve", "7", "--bot", "main", "--gen", "abc123"])

    def test_resume_fallback_and_callback_text_is_not_a_command(self):
        env = {**self.install_lister(), "STUB_FAIL_RENDER": "1"}
        result = self.run_hook("telegram-resume.sh", self.channel("/resume"), extra=env)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("reply_markup", json.loads(result.stdout)["payload"])
        event = self.channel("/resume")
        event["prompt"] = event["prompt"].replace(' message_id=', ' via="button" message_id=')
        result = self.run_hook("telegram-resume.sh", event, extra=env)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_resume_caps_identity_uses_read_only_process_query(self):
        resume = module("telegram-resume")
        (self.state / "caps").write_text("resume-buttons=1\npid=9999\n")
        (self.state / "bot.pid").write_text("9999\n")
        with patch.object(resume.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "9999\n", "")) as run:
            self.assertTrue(resume.inline_capability(self.state))
            self.assertEqual(run.call_args.args[0], ["/bin/ps", "-p", "9999", "-o", "pid="])
        (self.state / "bot.pid").write_text("10000\n")
        with patch.object(resume.subprocess, "run") as run:
            self.assertFalse(resume.inline_capability(self.state))
            run.assert_not_called()

    def test_ack_reaction_is_mocked_and_contains_no_message_body_log(self):
        ack = module("telegram-ack")
        with patch.dict(os.environ, self.env, clear=True), patch.object(ack, "telegram", return_value={"ok": True}) as send:
            self.assertTrue(ack.set_reaction("synthetic", "4242", "9", "👀"))
            self.assertEqual(send.call_args.args[0], "setMessageReaction")
            self.assertEqual(send.call_args.args[1]["reaction"], [{"type": "emoji", "emoji": "👀"}])
        self.assertNotIn("synthetic", (self.state / "logs/ack.log").read_text())
        self.assertIn("ACK ok=true", (self.state / "logs/ack.log").read_text())

    def test_api_override_requires_test_mode_and_loopback(self):
        helper = module("telebots_hooklib")
        for url, allowed in (("http://127.0.0.1:8765", True), ("http://localhost:8765", True),
                             ("http://[::1]:8765", True), ("https://example.test", False),
                             ("http://user:pass@localhost:8765", False)):
            with patch.dict(os.environ, {**self.env, "TELEBOTS_TEST": "1", "TELEBOTS_API_BASE": url}, clear=True):
                self.assertEqual(helper.api_base() is not None, allowed)
        with patch.dict(os.environ, {**self.env, "TELEBOTS_TEST": "0", "TELEBOTS_API_BASE": "http://127.0.0.1:8765"}, clear=True):
            self.assertEqual(helper.api_base(), "https://api.telegram.org")
        with patch.dict(os.environ, self.env, clear=True), patch.object(helper.request, "build_opener") as network:
            self.assertEqual(helper.telegram("sendMessage", {}, token="synthetic"), {"ok": False})
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
