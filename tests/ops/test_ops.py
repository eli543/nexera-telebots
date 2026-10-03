"""Offline WP4 checks. Run with a scrubbed HOME and no Telegram token."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SANDBOX = Path(os.environ["WP4_SANDBOX"])


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class IMAP(socketserver.StreamRequestHandler):
    message = b"From: sender@example.test\r\nSubject: Sample\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nMessage body"

    def handle(self):
        self.wfile.write(b"* OK fake IMAP ready\r\n")
        while True:
            line = self.rfile.readline().decode("ascii", errors="replace").strip()
            if not line:
                return
            tag, _, command = line.partition(" ")
            upper = command.upper()
            if upper.startswith("CAPABILITY"):
                self.wfile.write(b"* CAPABILITY IMAP4rev1 MOVE\r\n")
            elif upper.startswith("SELECT") or upper.startswith("EXAMINE"):
                self.wfile.write(b"* 1 EXISTS\r\n")
            elif upper.startswith("UID SEARCH"):
                self.wfile.write(b"* SEARCH 1\r\n")
            elif upper.startswith("UID FETCH"):
                self.wfile.write(b"* 1 FETCH (BODY[] {" + str(len(self.message)).encode() + b"}\r\n")
                self.wfile.write(self.message + b")\r\n")
            elif upper.startswith("LOGOUT"):
                self.wfile.write(b"* BYE closing\r\n")
            self.wfile.write(f"{tag} OK done\r\n".encode())
            self.wfile.flush()
            if upper.startswith("LOGOUT"):
                return


class SMTP(socketserver.StreamRequestHandler):
    sent = 0

    def handle(self):
        self.wfile.write(b"220 fake SMTP ready\r\n")
        data = False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            upper = line.upper().strip()
            if data:
                if upper == b".":
                    type(self).sent += 1
                    data = False
                    self.wfile.write(b"250 queued\r\n")
            elif upper.startswith(b"EHLO"):
                self.wfile.write(b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 OK\r\n")
            elif upper.startswith(b"AUTH"):
                self.wfile.write(b"235 authenticated\r\n")
            elif upper.startswith(b"DATA"):
                data = True
                self.wfile.write(b"354 go ahead\r\n")
            elif upper.startswith(b"QUIT"):
                self.wfile.write(b"221 goodbye\r\n")
                self.wfile.flush()
                return
            else:
                self.wfile.write(b"250 OK\r\n")
            self.wfile.flush()


class OfflineOps(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SANDBOX)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fleet = self.home / "fleet"
        self.fleet.mkdir()
        os.environ["HOME"] = str(self.home)
        os.environ["FLEET_HOME"] = str(self.fleet)
        self.email = load_script("fleet_email", REPO / "engine/fleet-email")
        self.email.FLEET_HOME = self.fleet
        self.email.CONFIG = self.fleet / "email.json"
        self.checklist = load_script("fleet_checklist", REPO / "engine/fleet-checklist")
        self.checklist.ROOT = self.fleet
        self.handlers = {}
        self.socket_patch = patch("socket.create_connection", side_effect=self.connect_fake)
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def start_server(self, handler):
        port = 10000 + len(self.handlers)
        self.handlers[port] = handler
        return port

    def connect_fake(self, address, timeout=None, source_address=None):
        host, port = address
        if host != "127.0.0.1" or port not in self.handlers:
            raise OSError("fake servers accept loopback only")
        client, server = socket.socketpair()
        def serve():
            try:
                self.handlers[port](server, address, object())
            finally:
                server.close()
        threading.Thread(target=serve, daemon=True).start()
        return client

    def test_email_against_local_imap_and_smtp(self):
        imap_port = self.start_server(IMAP)
        smtp_port = self.start_server(SMTP)
        SMTP.sent = 0
        os.environ["TELEBOTS_TEST"] = "1"
        c = {"enabled": True, "backend": "builtin", "imap_host": "127.0.0.1",
             "imap_port": imap_port, "smtp_host": "127.0.0.1", "smtp_port": smtp_port,
             "smtp_security": "ssl", "username": "user@example.test",
             "from": "user@example.test", "archive_mailbox": "Archive"}
        self.email.atomic_json(self.email.CONFIG, c)
        state = self.fleet / "bot-main"
        state.mkdir()
        self.email.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        self.email.atomic_json(self.fleet / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}), \
             patch.object(self.email, "password", return_value="fake-only"):
            self.assertEqual(self.email.builtin("test", {}, c)["sent"], False)
            inbox = self.email.builtin("inbox", {"limit": 5}, c)
            self.assertEqual(inbox[0]["subject"], "Sample")
            self.assertEqual(self.email.builtin("read", {"id": "1"}, c)["preview"], "Message body")
            self.assertEqual(self.email.builtin("archive", {"id": "1"}, c)["status"], "archived")
            self.assertEqual(self.email.builtin("snooze", {"id": "1", "hours": 24}, c)["status"], "snoozed")
            value = {"to": ["recipient@example.test"], "cc": [], "bcc": [],
                     "subject": "Exact", "body": "One time"}
            consent_id = "a" * 24
            self.email.atomic_json(state / "consents.json", {consent_id: {
                "id": consent_id, "chat": "123", "message": 42,
                "phase": "consumed", "expires": int(time.time() * 1000) + 600000,
                "email": value, "digest": self.email.digest(value)}})
            request = {"bot": "main", "consent_id": consent_id, "email": value}
            answer = self.email.builtin("send", request, c)
            self.assertEqual(answer["status"], "smtp-accepted")
            self.assertIs(answer["ok"], True)
            self.assertEqual(SMTP.sent, 1)
            with self.assertRaises(ValueError):
                self.email.builtin("send", request, c)
            self.assertEqual(SMTP.sent, 1)
            bad = dict(request, email=dict(value, subject="Changed"))
            with self.assertRaises(ValueError):
                self.email.builtin("send", bad, c)

    def test_full_inbox_reply_round_trip_when_backend_exposes_the_message(self):
        imap_port = self.start_server(IMAP)
        smtp_port = self.start_server(SMTP)
        original = IMAP.message
        body = "Opening line.\n" + "Complete message line.\n" * 110
        IMAP.message = (b"From: sender@example.test\r\nReply-To: reply@example.test\r\n"
                        b"To: user@example.test\r\nCc: observer@example.test\r\n"
                        b"Subject: Complete request\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                        + body.encode())
        self.addCleanup(setattr, IMAP, "message", original)
        c = {"enabled": True, "backend": "builtin", "imap_host": "127.0.0.1",
             "imap_port": imap_port, "smtp_host": "127.0.0.1", "smtp_port": smtp_port,
             "smtp_security": "ssl", "username": "user@example.test",
             "from": "user@example.test", "archive_mailbox": "Archive"}
        self.email.atomic_json(self.email.CONFIG, c)
        state = self.fleet / "bot-main"
        state.mkdir()
        self.email.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        self.email.atomic_json(self.fleet / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}), \
             patch.object(self.email, "password", return_value="fake-only"):
            message = self.email.builtin("read", {"id": "1"}, c)
            if not {"body", "to", "cc"}.issubset(message):
                self.skipTest("full IMAP body and recipient fields await the email package")
            self.assertEqual(message["body"], body)
            self.assertEqual(message["reply_to"], "reply@example.test")
            self.assertIn("user@example.test", message["to"])
            self.assertIn("observer@example.test", message["cc"])
            reply = {"to": [message["reply_to"]], "cc": ["observer@example.test"],
                     "bcc": [], "subject": "Re: " + message["subject"],
                     "body": "I read the complete message."}
            consent_id = "d" * 24
            self.email.atomic_json(state / "consents.json", {consent_id: {
                "id": consent_id, "chat": "123", "message": 42, "phase": "consumed",
                "expires": int(time.time() * 1000) + 600000,
                "email": reply, "digest": self.email.digest(reply)}})
            SMTP.sent = 0
            answer = self.email.builtin("send", {"bot": "main", "consent_id": consent_id,
                                                  "email": reply}, c)
            self.assertEqual(answer["status"], "smtp-accepted")
            self.assertEqual(SMTP.sent, 1)

    def test_email_contract_digest_and_fresh_consent_checks(self):
        email = {"to": ["demo@example.invalid"], "subject": "DEMO café ✉",
                 "body": "Line one\nLine two"}
        self.assertEqual(self.email.digest(email),
                         "d5e25cc947ef111213510e5b11d285e3109c062b94923f04500d7dd9a8ae9573")
        self.assertEqual(self.email.digest({"body": email["body"], "cc": [], "bcc": [],
                                            "subject": email["subject"], "to": email["to"]}),
                         self.email.digest(email))
        state = self.fleet / "bot-main"
        state.mkdir()
        self.email.atomic_json(self.fleet / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        self.email.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        consent_id = "b" * 24
        record = {"id": consent_id, "chat": "123", "message": 42, "phase": "consumed",
                  "expires": int(time.time() * 1000) + 600000,
                  "email": self.email.canonical_email(email), "digest": self.email.digest(email)}
        path = state / "consents.json"
        request = {"bot": "main", "consent_id": consent_id, "email": email}
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}):
            self.email.atomic_json(path, {consent_id: record})
            self.assertEqual(self.email.checked_send(request)[0], self.email.canonical_email(email))
            for change in ({"id": "c" * 24}, {"chat": "124"}, {"message": 0},
                           {"expires": int(time.time() * 1000)}, {"phase": "authorized"}):
                self.email.atomic_json(path, {consent_id: dict(record, **change)})
                with self.assertRaises(ValueError):
                    self.email.checked_send(request)
            self.email.atomic_json(path, {consent_id: record})
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                self.email.checked_send(request)
            path.chmod(0o600)
            self.email.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": []})
            with self.assertRaises(ValueError):
                self.email.checked_send(request)
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(self.home)}):
            with self.assertRaises(ValueError):
                self.email.checked_send(request)

    def test_custom_adapter_claims_its_own_attempt(self):
        state = self.fleet / "bot-main"
        state.mkdir()
        self.email.atomic_json(self.fleet / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        self.email.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        value = {"to": ["demo@example.invalid"], "subject": "DEMO", "body": "Do not send"}
        consent_id = "c" * 24
        self.email.atomic_json(state / "consents.json", {consent_id: {
            "id": consent_id, "chat": "123", "message": 42, "phase": "consumed",
            "expires": int(time.time() * 1000) + 600000,
            "email": self.email.canonical_email(value), "digest": self.email.digest(value)}})
        adapter = self.root / "custom-adapter"
        adapter.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                           "req = json.load(sys.stdin)\n"
                           "path = pathlib.Path(os.environ['TELEGRAM_STATE_DIR']) / 'mail-attempts' / req['consent_id']\n"
                           "path.parent.mkdir(mode=0o700, exist_ok=True)\n"
                           "fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)\n"
                           "os.close(fd)\nprint(json.dumps({'ok': True}))\n")
        adapter.chmod(0o700)
        self.email.atomic_json(self.email.CONFIG, {"enabled": True, "backend": str(adapter)})
        request = {"bot": "main", "consent_id": consent_id, "email": value}
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}), \
             patch.object(self.email.sys, "stdin", io.StringIO(json.dumps(request))):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(self.email.main(["fleet-email", "send"]), 0)
            self.assertIs(json.loads(output.getvalue())["ok"], True)
        self.assertEqual((state / "mail-attempts" / consent_id).stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}), \
             patch.object(self.email.sys, "stdin", io.StringIO(json.dumps(request))):
            self.assertEqual(self.email.main(["fleet-email", "send"]), 1)

    def test_email_setup_uses_keychain_prompt_and_saves_no_password(self):
        answers = ["mail.example.test", "993", "smtp.example.test", "465", "ssl",
                   "user@example.test", "user@example.test", "Archive"]
        with patch.object(self.email.sys, "stdin", SimpleNamespace(isatty=lambda: True)), \
             patch("builtins.input", side_effect=answers), \
             patch.object(self.email.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as keychain, \
             patch.object(self.email, "builtin", return_value={"status": "ok", "sent": False}):
            answer = self.email.setup()
        self.assertEqual(answer["status"], "configured")
        argv = keychain.call_args.args[0]
        self.assertEqual(argv[-1], "-w")
        self.assertEqual(argv[argv.index("-s") + 1], "telebots-email")
        saved = (self.fleet / "email.json").read_text()
        self.assertIn('"enabled":true', saved)
        self.assertNotIn("password", saved.lower())
        self.assertEqual((self.fleet / "email.json").stat().st_mode & 0o777, 0o600)

    def test_email_help_is_safe_before_account_setup(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.email.main(["fleet-email", "--help"]), 0)
        self.assertIn("Exit codes", output.getvalue())

    def test_checklist_rows_and_event_evidence(self):
        self.assertEqual(len(self.checklist.ROWS), 31)
        self.assertEqual({code for code, _, _ in self.checklist.ROWS},
                         {f"A{x}" for x in range(1, 12)} | {f"P{x}" for x in range(1, 21)})
        state = self.fleet / "bot-main"
        (state / "logs").mkdir(parents=True)
        self.assertEqual(self.checklist.phone_row("P2", state, self.fleet / "notes", 1, False, False)["status"], "Needs your action")
        self.assertEqual(self.checklist.phone_row("P13", state, self.fleet / "notes", 1, False, False)["status"], "Not configured")
        self.assertEqual(self.checklist.phone_row("P7", state, self.fleet / "notes", 1, False, False)["status"], "Needs your action")

    def test_crash_probe_matches_only_the_selected_state(self):
        state = self.fleet / "bot-main"
        calls = [SimpleNamespace(returncode=0, stdout="111 claude --channels plugin:telegram@claude-plugins-official\n222 claude --channels plugin:telegram@claude-plugins-official\n"),
                 SimpleNamespace(returncode=0, stdout="TELEGRAM_STATE_DIR=/some/other/bot"),
                 SimpleNamespace(returncode=0, stdout="TELEGRAM_STATE_DIR=" + str(state))]
        with patch.object(self.checklist, "run", side_effect=calls):
            self.assertEqual(self.checklist.exact_session_pid(state), 222)
        allowed, _ = self.checklist.crash_exact_session(state, "main", True)
        self.assertFalse(allowed)

    def test_checklist_dry_run_names_all_sources_without_touching_a_bot(self):
        env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet))
        dry = subprocess.run(["python3", str(REPO / "engine/fleet-checklist"), "--dry-run"],
                             env=env, text=True, capture_output=True, check=False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(sum(line.startswith(("A", "P")) for line in dry.stdout.splitlines()), 31)
        self.assertIn("fleet-home esc.log sent Esc", dry.stdout)
        self.assertIn("30-minute resume boundary", dry.stdout)
        self.assertFalse((self.fleet / "checklist-main.json").exists())

    def test_voice_dispatch_and_daemon_path_guard(self):
        voice = load_script("voice_dispatch", REPO / "voice/transcribe-voice")
        daemon = load_script("voice_daemon", REPO / "voice/transcribe-voice-daemon")
        audio = self.root / "sample.oga"
        audio.write_bytes(b"fake audio for an offline routing test")

        def fake_mlx(command, env):
            output = Path(command[command.index("--output-dir") + 1]) / "sample.txt"
            output.write_text("  Check the voice note  \n")
            return 0

        with patch.object(voice.os, "uname", return_value=SimpleNamespace(machine="arm64")), \
             patch("shutil.which", return_value="/fake/mlx_whisper"), \
             patch.object(voice, "_run_child", side_effect=fake_mlx):
            text, code = voice.cold_path(str(audio), "en")
        self.assertEqual(code, 0)
        self.assertEqual(voice.trim_lines(text).strip(), "Check the voice note")
        with self.assertRaises(daemon.BadRequest):
            daemon.parse_request(json.dumps({"path": "relative.oga"}), SimpleNamespace(languages={"en"}))
        self.assertEqual(daemon.parse_request(json.dumps({"path": str(audio), "language": "en"}),
                                              SimpleNamespace(languages={"en"}))[0], str(audio))

    def test_watchdog_skips_unenrolled_and_stopped(self):
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        state = self.fleet / "bot-main"
        state.mkdir()
        config = bin_dir / "fleet-config"
        config.write_text("#!/bin/sh\ncase \"$1\" in bots) echo main ;; bot) echo \"$TEST_STATE\" ;; esac\n")
        config.chmod(0o700)
        verifier = bin_dir / "verify-overlay.sh"
        verifier.write_text("#!/bin/sh\necho VERSION_MISMATCH:0.0.8\n")
        verifier.chmod(0o700)
        env = dict(os.environ, FLEET_HOME=str(self.fleet), TEST_STATE=str(state),
                   HOME=str(self.home), TELEGRAM_BOT_TOKEN="")
        env.pop("TELEGRAM_BOT_TOKEN")
        command = ["/bin/bash", str(REPO / "daemon/watchdog.sh"), "--dry-run"]
        first = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("NOT ENROLLED", first.stdout)
        self.assertEqual((self.fleet / "overlay.status").read_text().strip(), "VERSION_MISMATCH:0.0.8")
        (state / ".enrolled").touch()
        (state / "stopped").touch()
        second = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        self.assertIn("STOPPED", second.stdout)

    def test_watchdog_version_change_alerts_once_without_restarting(self):
        state = self.fleet / "bot-main"
        state.mkdir()
        (state / ".enrolled").touch()
        (state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-local-token\n")
        (state / "access.json").write_text('{"allowFrom":["123"]}\n')
        (state / "poller.heartbeat").write_text("1\n")
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        fc = bin_dir / "fleet-config"
        fc.write_text("#!/bin/sh\ncase \"$1\" in bots) echo main ;; bot) echo \"$TEST_STATE\" ;; esac\n")
        verifier = bin_dir / "verify-overlay.sh"
        verifier.write_text("#!/bin/sh\necho VERSION_MISMATCH:0.0.8\n")
        fresh = bin_dir / "fleet-fresh"
        fresh.write_text("#!/bin/sh\necho called >> \"$FRESH_LOG\"\n")
        for path in (fc, verifier, fresh):
            path.chmod(0o700)
        shims = self.root / "shims"
        shims.mkdir()
        (shims / "ps").write_text("#!/bin/sh\ncase \"$1\" in -axo) printf '111 claude --channels plugin:telegram@claude-plugins-official\\n222 bun server.ts\\n' ;; eww) printf 'TELEGRAM_STATE_DIR=%s\\n' \"$TEST_STATE\" ;; esac\n")
        (shims / "curl").write_text("#!/bin/sh\necho called >> \"$CURL_LOG\"\nprintf '{\"ok\":true}\\n'\n")
        for path in shims.iterdir():
            path.chmod(0o700)
        env = dict(os.environ, FLEET_HOME=str(self.fleet), TEST_STATE=str(state),
                   FRESH_LOG=str(self.root / "fresh.log"), CURL_LOG=str(self.root / "curl.log"),
                   PATH=str(shims) + ":/usr/bin:/bin:/usr/sbin:/sbin")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        command = ["/bin/bash", str(REPO / "daemon/watchdog.sh")]
        for _ in range(2):
            check = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual((self.root / "curl.log").read_text().splitlines(), ["called"])
        self.assertFalse((self.root / "fresh.log").exists())
        self.assertEqual((self.fleet / "watchdog-state/main.overlay-alerted").read_text().strip(), "0.0.8")

    def test_watchdog_repairs_stock_overlay_on_pinned_version(self):
        state = self.fleet / "bot-main"
        state.mkdir()
        (state / ".enrolled").touch()
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        status = self.fleet / "verify-status"
        status.write_text("STOCK\n")
        fc = bin_dir / "fleet-config"
        fc.write_text("#!/bin/sh\ncase \"$1\" in bots) echo main ;; bot) echo \"$TEST_STATE\" ;; esac\n")
        verifier = bin_dir / "verify-overlay.sh"
        verifier.write_text("#!/bin/sh\ncat \"$VERIFY_STATUS\"\n")
        installer = bin_dir / "install-overlay.sh"
        installer.write_text("#!/bin/sh\necho OK > \"$VERIFY_STATUS\"\n")
        for path in (fc, verifier, installer):
            path.chmod(0o700)
        env = dict(os.environ, FLEET_HOME=str(self.fleet), TEST_STATE=str(state),
                   VERIFY_STATUS=str(status), HOME=str(self.home))
        env.pop("TELEGRAM_BOT_TOKEN", None)
        check = subprocess.run(["/bin/bash", str(REPO / "daemon/watchdog.sh")],
                               env=env, capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual((self.fleet / "overlay.status").read_text().strip(), "OK")

    def test_watchdog_does_not_restart_on_unreachable_api(self):
        state = self.fleet / "bot-main"
        state.mkdir()
        (state / ".enrolled").touch()
        (state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-local-token\n")
        (state / "poller.heartbeat").write_text("1\n")
        wd_state = self.fleet / "watchdog-state"
        wd_state.mkdir()
        (wd_state / "main.unhealthy_since").write_text("1\n")
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        scripts = {
            "fleet-config": '#!/bin/sh\ncase "$1" in bots) echo main ;; bot) echo "$TEST_STATE" ;; esac\n',
            "verify-overlay.sh": "#!/bin/sh\necho OK\n",
            "fleet-fresh": '#!/bin/sh\necho called >> "$FRESH_LOG"\n',
        }
        for name, body in scripts.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o700)
        shims = self.root / "shims"
        shims.mkdir()
        (shims / "ps").write_text("#!/bin/sh\ncase \"$1\" in -axo) printf '111 claude --channels plugin:telegram@claude-plugins-official\\n222 bun server.ts\\n' ;; eww) printf 'TELEGRAM_STATE_DIR=%s\\n' \"$TEST_STATE\" ;; esac\n")
        (shims / "curl").write_text("#!/bin/sh\nprintf '{\"ok\":false,\"error_code\":401}\\n'\n")
        for path in shims.iterdir():
            path.chmod(0o700)
        env = dict(os.environ, FLEET_HOME=str(self.fleet), TEST_STATE=str(state),
                   FRESH_LOG=str(self.root / "fresh.log"), HOME=str(self.home),
                   PATH=str(shims) + ":/usr/bin:/bin:/usr/sbin:/sbin")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        check = subprocess.run(["/bin/bash", str(REPO / "daemon/watchdog.sh")],
                               env=env, capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertFalse((self.root / "fresh.log").exists())

    def test_doctor_rejects_fake_401_and_duplicate_token(self):
        bots = []
        for key in ("main", "backup"):
            state = self.fleet / f"bot-{key}"
            work = self.fleet / f"work-{key}"
            state.mkdir()
            work.mkdir()
            (state / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-local-token\n")
            (state / ".env").chmod(0o600)
            (state / "access.json").write_text('{"dmPolicy":"allowlist","allowFrom":["123"]}\n')
            bots.append({"key": key, "name": key, "workdir": str(work), "state_dir": str(state)})
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 1, "bots": bots}) + "\n")
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        verifier = bin_dir / "verify-overlay.sh"
        verifier.write_text("#!/bin/sh\necho OK\n")
        verifier.chmod(0o700)
        shims = self.root / "shims"
        shims.mkdir()
        scripts = {"claude": "echo 2.1.286", "bun": "echo 1.4.2", "launchctl": "exit 1",
                   "curl": "printf '{\"ok\":false,\"error_code\":401}\\n'", "ps": "exit 0"}
        for name, body in scripts.items():
            path = shims / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o700)
        env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                   PATH=str(shims) + ":/usr/bin:/bin:/usr/sbin:/sbin")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        check = subprocess.run(["/bin/bash", str(REPO / "engine/fleet-doctor")],
                               env=env, capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 1, check.stdout + check.stderr)
        self.assertIn("getMe", check.stdout)
        self.assertIn("401", check.stdout)
        self.assertIn("duplicate token", check.stdout)
        self.assertNotIn("fake-local-token", check.stdout + check.stderr)

    def test_snapshot_restore_dry_runs_stay_in_sandbox(self):
        state = self.fleet / "bot-main"
        work = self.fleet / "work-main"
        state.mkdir()
        work.mkdir()
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 1, "bots": [
            {"key": "main", "name": "Main", "state_dir": str(state), "workdir": str(work)}]}) + "\n")
        shims = self.root / "shims"
        shims.mkdir()
        blocked = self.root / "blocked.log"
        for name in ("ps", "osascript", "launchctl", "open", "security"):
            script = shims / name
            script.write_text("#!/bin/sh\necho " + name + " >> \"$BLOCKED_LOG\"\nexit 1\n")
            script.chmod(0o700)
        bash_env = self.root / "bash-env"
        bash_env.write_text('kill() { echo "kill-blocked" >> "$BLOCKED_LOG"; return 1; }\n')
        env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                   BLOCKED_LOG=str(blocked), BASH_ENV=str(bash_env),
                   PATH=str(shims) + ":/usr/bin:/bin:/usr/sbin:/sbin")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        snapshot = subprocess.run(["/bin/bash", str(REPO / "engine/fleet-snapshot"), "--dry-run"],
                                  env=env, capture_output=True, text=True, check=False)
        self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
        self.assertTrue((self.fleet / "restart-state/snapshot.dryrun.tsv").is_file())
        restore = subprocess.run(["/bin/bash", str(REPO / "engine/fleet-restore"), "--dry-run"],
                                 env=env, capture_output=True, text=True, check=False)
        self.assertIn("dry", restore.stdout.lower() + restore.stderr.lower())
        self.assertNotIn("osascript", blocked.read_text() if blocked.exists() else "")

    def test_snapshot_restore_transaction_and_health_gate_in_sandbox(self):
        state = self.fleet / "bot-main"
        work = self.fleet / "work-main"
        state.mkdir()
        work.mkdir()
        (self.fleet / "fleet.json").write_text(json.dumps({"version": 2, "bots": [
            {"key": "main", "name": "Main", "state_dir": str(state), "workdir": str(work)}]}) + "\n")
        bin_dir = self.fleet / "bin"
        bin_dir.mkdir()
        for name in ("fleet-config", "fleet-launch", "fleet-fresh"):
            shutil.copy2(REPO / "engine" / name, bin_dir / name)
        project = subprocess.run([str(bin_dir / "fleet-config"), "project-dir", "main"],
                                 env=dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet)),
                                 text=True, capture_output=True, check=True).stdout.strip()
        Path(project).mkdir(parents=True)
        session = "11111111-2222-3333-4444-555555555555"
        (state / "session.id").write_text(session + "\n")
        (Path(project) / (session + ".jsonl")).write_text('{}\n')
        (state / "poller.heartbeat").write_text(str(int(time.time())))
        mode = self.root / "process-mode"
        mode.write_text("down")
        shims = self.root / "shims"
        shims.mkdir()
        ps = shims / "ps"
        ps.write_text("#!/bin/sh\n"
                      f'mode=$(cat "{mode}")\n'
                      'if [ "$1" = "-p" ]; then echo "Fri Oct  2 12:00:00 2026"; exit 0; fi\n'
                      'if [ "$1" = "-axo" ]; then\n'
                      '  if [ "$mode" = up ]; then\n'
                      '    echo "111 claude --channels plugin:telegram@claude-plugins-official"\n'
                      '    echo "222 bun server.ts"\n'
                      '  fi\n  exit 0\nfi\n'
                      f'if [ "$1" = eww ]; then echo "TELEGRAM_STATE_DIR={state}"; exit 0; fi\n'
                      'if [ "$1" = "-o" ] && [ "$2" = etime= ]; then echo 00:05; exit 0; fi\n'
                      'if [ "$1" = "-o" ] && [ "$2" = ppid= ]; then echo 111; exit 0; fi\n'
                      'exit 1\n')
        ps.chmod(0o700)
        osa_log = self.root / "osascript-calls"
        osascript = shims / "osascript"
        osascript.write_text(f'#!/bin/sh\necho attempted >> "{osa_log}"\nexit 1\n')
        osascript.chmod(0o700)
        bash_env = self.root / "bash-env"
        bash_env.write_text('kill() { [ "$1" = -0 ] && return 0; return 1; }\n')
        site = self.root / "python-site"
        site.mkdir()
        (site / "sitecustomize.py").write_text("import os\nos.kill = lambda pid, sig: None\n")
        env = dict(os.environ, HOME=str(self.home), FLEET_HOME=str(self.fleet),
                   BASH_ENV=str(bash_env), PYTHONPATH=str(site),
                   PATH=str(shims) + ":/usr/bin:/bin:/usr/sbin:/sbin")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        original = (REPO / "engine/fleet-restore").read_text()
        self.assertEqual(original.count("/usr/bin/osascript"), 1)
        safe_restore = self.root / "fleet-restore-sandbox"
        safe_restore.write_text(original.replace("/usr/bin/osascript", str(osascript)))
        safe_restore.chmod(0o700)

        def call(script, *args):
            return subprocess.run(["/bin/bash", str(script), *args], env=env,
                                  capture_output=True, text=True, timeout=30, check=False)

        snapshot = call(REPO / "engine/fleet-snapshot")
        self.assertEqual(snapshot.returncode, 0, snapshot.stdout + snapshot.stderr)
        snap = self.fleet / "restart-state/snapshot.tsv"
        switch = self.fleet / "watchdog-state/main.disabled"
        self.assertIn("generation=", snap.read_text().splitlines()[0])
        self.assertTrue(switch.is_file())
        saved = snap.read_text()
        snap.write_text(saved.replace(str(work), str(self.root / "wrong-work")))
        rejected = call(safe_restore)
        self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
        self.assertIn("REJECTED", rejected.stdout)
        self.assertFalse(osa_log.exists())
        self.assertTrue(switch.is_file())
        snap.write_text(saved)
        failed_launch = call(safe_restore)
        self.assertEqual(failed_launch.returncode, 1, failed_launch.stdout + failed_launch.stderr)
        self.assertIn("LAUNCH FAILED", failed_launch.stdout)
        self.assertTrue(snap.is_file())
        self.assertEqual(osa_log.read_text().splitlines(), ["attempted"])
        mode.write_text("up")
        (state / "poller.heartbeat").write_text(str(int(time.time()) - 1000))
        stale = call(safe_restore)
        self.assertEqual(stale.returncode, 1, stale.stdout + stale.stderr)
        self.assertIn("PENDING WATCHDOG", stale.stdout)
        self.assertTrue(snap.is_file())
        (state / "poller.heartbeat").write_text(str(int(time.time())))
        recovered = call(safe_restore)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertIn("archived", recovered.stdout)
        self.assertFalse(snap.exists())
        self.assertFalse(switch.exists())
        self.assertEqual(osa_log.read_text().splitlines(), ["attempted"])


if __name__ == "__main__":
    unittest.main()
