#!/usr/bin/env python3
"""Offline IMAP response and backend authorization regression tests."""

import importlib.machinery
import importlib.util
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

if os.environ.get("TELEBOTS_TEST") != "1" or os.environ.get("TELEGRAM_BOT_TOKEN"):
    raise SystemExit("Run through the scrubbed sandbox wrapper.")

SOURCE = Path(__file__).resolve().parents[2] / "engine/fleet-email"
loader = importlib.machinery.SourceFileLoader("fleet_email_regression", str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
email_backend = importlib.util.module_from_spec(spec)
loader.exec_module(email_backend)


class FakeIMAP:
    capabilities = ("MOVE",)

    def __init__(self, message):
        self.message = message
        self.fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def select(self, *_args, **_kwargs):
        return "OK", [b""]

    def uid(self, operation, *args):
        if operation == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in range(1, 101))]
        if operation == "fetch":
            self.fetched.append((args[0], args[1]))
            return "OK", [(b"BODY[]", self.message)]
        raise AssertionError(operation)


class FakeAuthority:
    def __init__(self, response):
        self.response = response
        self.proof = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sendall(self, value):
        self.proof = json.loads(value)

    def recv(self, _limit):
        return json.dumps(self.response).encode()


class EmailBackendRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mail-backend-", dir=os.environ["TELEBOTS_SANDBOX_ROOT"])
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(email_backend, "FLEET_HOME", self.home).start()
        patch.object(email_backend, "CONFIG", self.home / "email.json").start()
        patch.object(email_backend, "password", return_value="fixture-only").start()
        self.config = {"username": "me@example.test", "archive_mailbox": "Archive"}

    def test_five_cards_fetch_only_five_recent_eligible_messages(self):
        client = FakeIMAP(b"From: Sender <sender@example.test>\r\nSubject: Sample\r\n\r\nBody")
        patch.object(email_backend, "imap_client", return_value=client).start()
        email_backend.atomic_json(self.home / "inbox-snoozed.json", {"100": 9999999999})
        cards = email_backend.builtin("inbox", {"limit": 5}, self.config)
        self.assertEqual([card["id"] for card in cards], ["99", "98", "97", "96", "95"])
        self.assertEqual([uid for uid, _ in client.fetched], ["99", "98", "97", "96", "95"])
        self.assertTrue(all("<0." in query for _, query in client.fetched))

    def test_read_has_full_plain_body_and_reply_all_metadata(self):
        body = "A" * 2000 + " END"
        raw = ("From: Sender <sender@example.test>\r\nTo: Me <me@example.test>\r\n"
               "Cc: Copy <copy@example.test>\r\nReply-To: Replies <reply@example.test>\r\n"
               "Message-Id: <m@example.test>\r\nIn-Reply-To: <prior@example.test>\r\n"
               "References: <root@example.test> <prior@example.test>\r\n"
               "Date: Fri, 2 Oct 2026 12:00:00 -0400\r\nSubject: Long\r\n"
               "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode()
        client = FakeIMAP(raw)
        patch.object(email_backend, "imap_client", return_value=client).start()
        message = email_backend.builtin("read", {"id": "1"}, self.config)
        self.assertEqual(message["preview"], body[:1600])
        self.assertEqual(message["body"], body)
        self.assertIn("copy@example.test", message["cc"])
        self.assertIn("me@example.test", message["to"])
        self.assertIn("reply@example.test", message["reply_to"])
        self.assertEqual(message["message_id"], "<m@example.test>")
        self.assertEqual(message["in_reply_to"], "<prior@example.test>")
        self.assertIn("<root@example.test>", message["references"])
        self.assertIn("-0400", message["date"])
        self.assertFalse(message["truncated"])

    def test_html_only_body_is_readable_and_oversized_response_refused(self):
        raw = (b"From: Sender <sender@example.test>\r\nSubject: HTML\r\n"
               b"Content-Type: text/html; charset=utf-8\r\n\r\n"
               b"<p>Hello &amp; goodbye</p><script>ignore()</script><p>Second line</p>")
        client = FakeIMAP(raw)
        patch.object(email_backend, "imap_client", return_value=client).start()
        message = email_backend.builtin("read", {"id": "1"}, self.config)
        self.assertIn("Hello & goodbye", message["body"])
        self.assertIn("Second line", message["body"])
        self.assertNotIn("ignore()", message["body"])
        client.message = b"From: sender@example.test\r\n\r\n" + b"x" * 300000
        with self.assertRaises(ValueError):
            email_backend.builtin("read", {"id": "1"}, self.config)

    def test_truncated_read_receipt_is_not_full(self):
        state = self.home / "state"
        state.mkdir()
        email_backend.atomic_json(self.home / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        raw = b"From: sender@example.test\r\n\r\n" + b"x" * (email_backend.MAX_BODY_CHARS + 1)
        patch.object(email_backend, "imap_client", return_value=FakeIMAP(raw)).start()
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}):
            message = email_backend.builtin("read", {"id": "1"}, self.config)
        self.assertTrue(message["truncated"])
        record = email_backend.load_json(state / "mail-reads" / (message["read_receipt"] + ".json"), {})
        self.assertFalse(record["full"])
        self.assertTrue(record["truncated"])

    def test_direct_send_refuses_a_forged_consumed_audit_record(self):
        state = self.home / "state"
        state.mkdir()
        email_backend.atomic_json(self.home / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        email_backend.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        value = {"to": ["demo@example.invalid"], "subject": "DEMO", "body": "No send"}
        consent_id = "a" * 24
        email_backend.atomic_json(state / "consents.json", {consent_id: {
            "id": consent_id, "chat": "123", "message": 42, "phase": "consumed",
            "expires": 9999999999999, "email": email_backend.canonical_email(value),
            "digest": email_backend.digest(value)}})
        request = {"bot": "main", "consent_id": consent_id, "email": value}
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state)}):
            with self.assertRaises(ValueError):
                email_backend.checked_send(request)

    def test_custom_adapter_receives_each_non_send_operation_payload(self):
        adapter = self.home / "fake-adapter"
        adapter.write_text("#!/bin/sh\nexit 0\n")
        adapter.chmod(0o700)
        email_backend.atomic_json(self.home / "email.json", {"enabled": True, "backend": str(adapter)})
        operations = (
            ("test", {}, {}),
            ("inbox", {"limit": 4}, {"limit": 4}),
            ("read", {"id": "17"}, {"id": "17"}),
            ("archive", {"id": "18"}, {"id": "18"}),
            ("snooze", {"id": "19", "hours": 24}, {"id": "19", "hours": 24}),
        )
        for operation, request, expected in operations:
            with self.subTest(operation=operation):
                child = subprocess.CompletedProcess([str(adapter), operation], 0, '{"status":"ok"}', "")
                with (patch.object(email_backend.subprocess, "run", return_value=child) as run,
                      patch.object(email_backend.sys, "stdin", io.StringIO(json.dumps(request))),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(email_backend.main(["fleet-email", operation]), 0)
                self.assertEqual(run.call_args.args[0], [str(adapter), operation])
                self.assertEqual(json.loads(run.call_args.kwargs["input"]), expected)

    def test_custom_adapter_opaque_id_supports_read_archive_snooze_and_reply(self):
        adapter = self.home / "fake-adapter"
        adapter.write_text("#!/bin/sh\nexit 0\n")
        adapter.chmod(0o700)
        email_backend.atomic_json(self.home / "email.json", {"enabled": True, "backend": str(adapter)})
        item_id = "message-abc"
        read = {"id": item_id, "reply_to": "sender@example.invalid", "subject": "Subject"}
        for operation, request, output in (
            ("read", {"id": item_id}, read),
            ("archive", {"id": item_id}, {"status": "archived"}),
            ("snooze", {"id": item_id, "hours": 24}, {"status": "snoozed"}),
        ):
            with self.subTest(operation=operation):
                child = subprocess.CompletedProcess([str(adapter), operation], 0, json.dumps(output), "")
                with (patch.object(email_backend.subprocess, "run", return_value=child) as run,
                      patch.object(email_backend.sys, "stdin", io.StringIO(json.dumps(request))),
                      contextlib.redirect_stdout(io.StringIO()) as captured):
                    self.assertEqual(email_backend.main(["fleet-email", operation]), 0)
                self.assertEqual(run.call_args.args[0], [str(adapter), operation])
                self.assertEqual(json.loads(run.call_args.kwargs["input"]), request)
                self.assertNotIn("shell", run.call_args.kwargs)
                if operation == "read":
                    read = json.loads(captured.getvalue())

        # A reply starts with that opaque-ID read, then uses the normal exact-email
        # send payload. The adapter receives JSON on stdin, never shell text.
        reply = {"to": [read["reply_to"]], "cc": [], "bcc": [],
                 "subject": "Re: " + read["subject"], "body": "Offline reply"}
        state = self.home / "state"
        state.mkdir()
        consent = "a" * 24
        request = {"bot": "main", "consent_id": consent, "email": reply}

        def accept_reply(*args, **kwargs):
            self.assertEqual(json.loads(kwargs["input"]), request)
            self.assertNotIn("shell", kwargs)
            email_backend.atomic_json(state / "mail-attempts" / consent, {"state": "smtp-accepted"})
            return subprocess.CompletedProcess(args[0], 0, '{"ok":true,"status":"smtp-accepted"}', "")

        with (patch.object(email_backend, "checked_send", return_value=(reply, state)),
              patch.object(email_backend.subprocess, "run", side_effect=accept_reply) as run,
              patch.object(email_backend.sys, "stdin", io.StringIO(json.dumps(request))),
              contextlib.redirect_stdout(io.StringIO()) as captured):
            self.assertEqual(email_backend.main(["fleet-email", "send"]), 0)
        self.assertEqual(run.call_args.args[0], [str(adapter), "send"])
        self.assertTrue(json.loads(captured.getvalue())["ok"])

    def test_custom_adapter_rejects_empty_or_control_character_ids(self):
        for item_id in ("", "  ", "message\nabc", "message\x00abc", "message\u2028abc"):
            with self.subTest(item_id=repr(item_id)), self.assertRaises(ValueError):
                email_backend.adapter_payload("read", {"id": item_id})

    def test_authority_comes_from_private_state_file_and_requires_hmac(self):
        state = self.home / "state"
        state.mkdir()
        email_backend.atomic_json(self.home / "fleet.json", {"version": 2, "bots": [
            {"key": "main", "state_dir": str(state)}]})
        email_backend.atomic_json(state / "access.json", {"dmPolicy": "allowlist", "allowFrom": ["123"]})
        secret = "a" * 64
        email_backend.atomic_json(state / "email-authority.json", {"port": 11111, "secret": secret})
        value = {"to": ["demo@example.invalid"], "subject": "DEMO", "body": "No send"}
        nonce = "b" * 64
        request = {"bot": "main", "consent_id": "c" * 24, "email": value,
                   "chat": "123", "message": 42, "digest": email_backend.digest(value), "nonce": nonce}
        mac = hmac.new(bytes.fromhex(secret), f"{nonce}:{request['digest']}".encode(), hashlib.sha256).hexdigest()
        accepted = FakeAuthority({"ok": True, "mac": mac})
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state), "TELEBOTS_EMAIL_AUTHORITY_PORT": "22222"}), \
             patch.object(email_backend.socket, "create_connection", return_value=accepted) as connect:
            self.assertEqual(email_backend.checked_send(request)[0], email_backend.canonical_email(value))
            connect.assert_called_once_with(("127.0.0.1", 11111), timeout=3)
        self.assertEqual(accepted.proof["nonce"], nonce)
        forged = FakeAuthority({"ok": True})
        with patch.dict(os.environ, {"TELEGRAM_STATE_DIR": str(state), "TELEBOTS_EMAIL_AUTHORITY_PORT": "22222"}), \
             patch.object(email_backend.socket, "create_connection", return_value=forged):
            with self.assertRaises(ValueError):
                email_backend.checked_send(request)


if __name__ == "__main__":
    unittest.main()
