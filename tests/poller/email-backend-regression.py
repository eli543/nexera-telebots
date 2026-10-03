#!/usr/bin/env python3
"""Offline IMAP response and backend authorization regression tests."""

import importlib.machinery
import importlib.util
import contextlib
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


if __name__ == "__main__":
    unittest.main()
