"""Checklist evidence from real engines with external Mac commands blocked."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["TELEBOTS_SANDBOX_ROOT"])


class ChecklistProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.fleet = self.home / ".claude/telegram-fleet"
        self.work = self.root / "work"
        self.state = self.root / "state"
        self.shims = self.root / "shims"
        self.tmp = self.root / "tmp"
        for path in (self.fleet / "bin", self.work, self.state, self.shims, self.tmp):
            path.mkdir(parents=True, exist_ok=True)
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [{
            "key": "main", "name": "Main", "workdir": str(self.work), "state_dir": str(self.state)}]}))
        (self.state / "access.json").write_text('{"dmPolicy":"allowlist","allowFrom":["123"]}\n')
        (self.state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-sandbox-only\n")
        shutil.copy2(REPO / "engine/fleet-config", self.fleet / "bin/fleet-config")
        for name, body in {
            "ps": '#!/bin/sh\ncase "$*" in *comm=*) echo claude ;; *tty=*) echo ttys999 ;; esac\n',
            "lsof": '#!/bin/sh\nprintf "COMMAND PID USER FD TYPE NAME\\nclaude 999 1 cwd DIR %s\\n" "$BOT_WORK"\n',
            "osascript": '#!/bin/sh\nif [ "$1" = "-e" ]; then echo Terminal; else [ "${OSA_TOUCH_COMPACT:-0}" = 1 ] && touch "$COMPACT_STARTED"; echo OK; fi\n',
            "curl": '#!/bin/sh\nprintf "{\\\"ok\\\":true}\\n"\n',
        }.items():
            path = self.shims / name
            path.write_text(body)
            path.chmod(0o700)
        self.bash_env = self.root / "bash-env"
        self.bash_env.write_text('kill() { [ "$1" = -0 ]; }\n')
        self.env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                        PATH=str(self.shims) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                        BASH_ENV=str(self.bash_env), BOT_WORK=str(self.work),
                        COMPACT_STARTED=str(self.state / "compact-started"),
                        TELEBOTS_TEST="1")
        self.env.pop("TELEGRAM_BOT_TOKEN", None)
        loader = importlib.machinery.SourceFileLoader("fleet_checklist_producer", str(REPO / "engine/fleet-checklist"))
        spec = importlib.util.spec_from_loader("fleet_checklist_producer", loader)
        self.checklist = importlib.util.module_from_spec(spec)
        loader.exec_module(self.checklist)
        self.checklist.ROOT = self.fleet

    def safe_engine(self, name):
        source = (REPO / "engine" / name).read_text()
        source = source.replace("/tmp/", str(self.tmp) + "/")
        source = source.replace("/usr/bin/osascript", str(self.shims / "osascript"))
        source = source.replace("/usr/bin/curl", str(self.shims / "curl"))
        self.assertNotIn("/usr/bin/osascript", source)
        self.assertNotIn("/usr/bin/curl", source)
        target = self.root / name
        target.write_text(source)
        target.chmod(0o700)
        return target

    def call(self, script, *args, extra=None):
        return subprocess.run(["/bin/bash", str(script), *map(str, args)],
                              env={**self.env, **(extra or {})}, cwd=self.root,
                              capture_output=True, text=True, timeout=25, check=False)

    def row(self, code, started):
        return self.checklist.phone_row(code, self.state, self.root / "notes", 1,
                                        False, False, started)["status"]

    def test_escape_and_compact_receipts_come_from_executed_engines(self):
        esc = self.safe_engine("fleet-esc")
        self.assertIn(str(self.shims / "osascript"), esc.read_text())
        self.assertEqual(self.call(self.shims / "osascript", "-e").stdout.strip(), "Terminal")
        started = int(time.time()) - 1
        dry = self.call(esc, 999999, self.work, 123, extra={"CC_ESC_DRYRUN": "1"})
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.row("P9", started), "Needs your action")
        landed = self.call(esc, 999999, self.work, 123)
        self.assertEqual(landed.returncode, 0, (self.fleet / "logs/esc.log").read_text())
        self.assertEqual(self.row("P9", started), "Passed")

        compact = self.safe_engine("fleet-compact")
        frame = self.root / "frame.txt"
        frame.write_text("─" * 24 + "\n❯ \n" + "─" * 24 + "\n⏵⏵ permission mode\n")
        dry = self.call(compact, 999999, self.work, extra={
            "CC_COMPACT_DRYRUN": "1", "CC_COMPACT_FRAME_FILE": str(frame)})
        self.assertEqual(dry.returncode, 0, (self.fleet / "logs/compact.log").read_text())
        self.assertEqual(self.row("P11", started), "Needs your action")
        landed = self.call(compact, 999999, self.work, extra={
            "CC_COMPACT_FRAME_FILE": str(frame), "OSA_TOUCH_COMPACT": "1"})
        self.assertEqual(landed.returncode, 0, (self.fleet / "logs/compact.log").read_text())
        self.assertEqual(self.row("P11", started), "Passed", repr(self.checklist.engine_run(self.state, "compact", started)))
        self.assertFalse((self.state / "logs/esc.log").exists())
        self.assertFalse((self.state / "logs/compact.log").exists())

    def test_watchdog_dry_run_does_not_prove_recovery_or_phone_delivery(self):
        (self.state / ".enrolled").touch()
        watchdog = REPO / "daemon/watchdog.sh"
        result = self.call(watchdog, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would restart", result.stdout)
        row = self.checklist.phone_row("P15", self.state, self.root / "notes", 1,
                                        False, True, int(time.time()) - 1)
        self.assertEqual(row["status"], "Needs your action")
        self.assertIn("30 minutes", row["detail"])

    def test_update_ready_ping_without_restart_does_not_pass(self):
        update = self.safe_engine("fleet-update")
        started = int(time.time()) - 1
        ready = self.call(update, 999999, self.work, 123, extra={
            "CC_UPDATE_READY_DRYRUN": "1", "TELEBOTS_API_BASE": "http://127.0.0.1:12345"})
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertIn("TEST ready ping only", (self.state / "logs/update.log").read_text())
        self.assertEqual(self.row("P8", started), "Needs your action")

    def test_handoff_requires_a_matching_receipt_from_the_receiving_poller(self):
        started = int(time.time()) - 1
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        peer = self.root / "second-state"
        (self.state / "logs").mkdir()
        (peer / "logs").mkdir(parents=True)
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [
            {"key": "main", "name": "Main", "workdir": str(self.work), "state_dir": str(self.state)},
            {"key": "second", "name": "Second", "workdir": str(self.root / "second-work"), "state_dir": str(peer)},
        ]}))
        note_id = "a" * 24
        source = self.state / "logs/poller-events.log"
        source.write_text(f"{stamp} HANDOFF-SENT id={note_id} source=main target=second\n")
        row = lambda: self.checklist.phone_row("P13", self.state, self.root / "notes", 2, False, False, started)["status"]
        self.assertEqual(row(), "Needs your action")
        (peer / "logs/poller-events.log").write_text(f"{stamp} HANDOFF-RECEIVED id={'b' * 24} source=main target=second\n")
        self.assertEqual(row(), "Needs your action")
        (peer / "logs/poller-events.log").write_text(f"{stamp} HANDOFF-RECEIVED id={note_id} source=main target=second\n")
        self.assertEqual(row(), "Needs your action")
        delivery = "d" * 24
        source.write_text(f"{stamp} HANDOFF-ISSUED id={note_id} source=main target=second delivery={delivery}\n"
                          f"{stamp} HANDOFF-SENT id={note_id} source=main target=second delivery={delivery}\n")
        (peer / "logs/poller-events.log").write_text(
            f"{stamp} HANDOFF-RECEIVED id={note_id} source=main target=second delivery={delivery}\n")
        deliveries = self.fleet / "handoffs/deliveries"
        deliveries.mkdir(parents=True)
        record = deliveries / (delivery + ".json")
        record.write_text(json.dumps({
            "note_id": note_id, "source": "main", "target": "second", "phase": "received",
            "issued_at": int(time.time() * 1000), "delivery_id": delivery}))
        record.chmod(0o600)
        self.assertEqual(row(), "Passed")

    def test_inbox_reply_requires_a_linked_full_read_and_smtp_receipt(self):
        started = int(time.time()) - 1
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        (self.state / "logs").mkdir()
        reads = self.state / "mail-reads"
        reads.mkdir()
        read_id, consent_id = "b" * 24, "c" * 24
        receipt = reads / (read_id + ".json")
        receipt.write_text(json.dumps({"id": "7", "full": True, "from": "sender@example.test",
                                       "reply_to": "reply@example.test", "to": "user@example.test",
                                       "cc": "observer@example.test"}))
        receipt.chmod(0o600)
        (self.fleet / "email.json").write_text('{"enabled":true,"from":"user@example.test"}')
        (self.state / "consents.json").write_text(json.dumps({consent_id: {
            "phase": "consumed", "email": {"to": ["reply@example.test"], "cc": ["observer@example.test"],
                                              "bcc": [], "subject": "Re: Request", "body": "Offline fixture"}}}))
        poller = self.state / "logs/poller-events.log"
        poller.write_text(f"{stamp} EMAIL-CONFIRM id={consent_id}\n{stamp} EMAIL-SEND id={consent_id} status=sent\n")
        row = lambda: self.checklist.phone_row("P14", self.state, self.root / "notes", 1, True, False, started)["status"]
        self.assertEqual(row(), "Needs your action")
        poller.write_text(poller.read_text() + f"{stamp} EMAIL-REPLY id={consent_id} read={read_id} status=sent\n")
        self.assertEqual(row(), "Passed")
        (self.state / "consents.json").write_text(json.dumps({consent_id: {
            "phase": "consumed", "email": {"to": ["wrong@example.test"], "cc": ["observer@example.test"],
                                              "bcc": [], "subject": "Re: Request", "body": "Offline fixture"}}}))
        self.assertEqual(row(), "Needs your action")

    def test_stall_phone_alert_is_opt_in_and_only_sends_once(self):
        (self.state / ".enrolled").touch()
        (self.state / "last-inbound").write_text(str(int(time.time()) - 601))
        (self.state / "poller.heartbeat").write_text(str(int(time.time())))
        (self.fleet / "bin/verify-overlay.sh").write_text('#!/bin/sh\necho OK\n')
        (self.fleet / "bin/verify-overlay.sh").chmod(0o700)
        (self.shims / "ps").write_text('#!/bin/sh\ncase "$1" in\n'
                                        '  -axo) printf "111 claude --channels plugin:telegram@claude-plugins-official\\n222 bun server.ts\\n" ;;\n'
                                        '  eww) printf "TELEGRAM_STATE_DIR=%s\\n" "$TEST_STATE" ;;\n'
                                        'esac\n')
        (self.shims / "curl").write_text('#!/bin/sh\necho called >> "$CURL_LOG"\nprintf "{\\"ok\\":true}\\n"\n')
        config = self.fleet / "fleet.json"
        fleet = json.loads(config.read_text())
        started = {"TEST_STATE": str(self.state), "CURL_LOG": str(self.root / "curl.log")}
        watchdog = REPO / "daemon/watchdog.sh"
        self.assertEqual(self.call(watchdog, extra=started).returncode, 0)
        self.assertFalse((self.root / "curl.log").exists())
        fleet["options"] = {"stall_phone_alerts": True}
        config.write_text(json.dumps(fleet))
        (self.fleet / "watchdog-state/main.stall_alerted").unlink()
        for _ in range(2):
            self.assertEqual(self.call(watchdog, extra=started).returncode, 0)
        self.assertEqual((self.root / "curl.log").read_text().splitlines(), ["called"])
        self.assertFalse((self.fleet / "watchdog-state/main.restart_log").exists())


if __name__ == "__main__":
    unittest.main()
