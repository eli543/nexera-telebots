# Optional email backend

`/inbox` is always in the Telegram command menu. Without an email account, it shows one setup hint and sends no mail. The built-in backend uses your own IMAP account for reading and SMTP account for sending. This project does not supply an email account.

## Set up on the Mac

1. Confirm your provider permits IMAP and SMTP with an app password. Enable the provider's required account security setting first.
2. Run `~/.claude/telegram-fleet/bin/fleet-email setup`. You do this at the Mac: enter the app password into the hidden local prompt. Do not paste it into a Claude chat, command argument or config file.
3. Run `~/.claude/telegram-fleet/bin/fleet-email test`. It checks login and mailbox selection without sending an email. Then run the email row in `fleet-checklist <key>`.

`$FLEET_HOME/email.json` holds only non-secret settings: enabled state, backend, IMAP and SMTP hosts and ports, SMTP security (`ssl` or `starttls`), username, From address and archive mailbox. The password lives in macOS Keychain under service `telebots-email`, account equal to the configured username. The backend must not log the password. A custom executable may replace the built-in backend if it implements the same local operations.

| Provider | Built-in backend position |
|---|---|
| Gmail | Possible when the account permits [IMAP and an app password](https://support.google.com/mail/answer/7126229). Google may restrict app passwords for managed accounts. |
| iCloud Mail | Uses [IMAP/SMTP and an app-specific password](https://support.apple.com/en-ie/102525). |
| Fastmail | Uses [IMAP/SMTP and an app password](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports); its Basic plan does not include third-party IMAP/SMTP access. |
| Yahoo Mail | Possible where the account permits an app password and IMAP/SMTP; run `fleet-email test` to confirm. |
| Outlook.com or Microsoft 365 | The built-in app-password backend does not implement [Microsoft's OAuth flow](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth). Use a separate custom backend or leave email off. |

Provider settings can change. The local `test` result is the gate for this account. Archive requires the IMAP server to support UID MOVE; the backend refuses instead of silently deleting or copying when it is unavailable. Snooze is local and does not depend on a provider snooze feature.

## Inbox and send flow

`/inbox` asks the backend for recent messages and shows cards with Reply, Archive and Snooze 1d. Each card contains a subject, sender and at most 1,600 characters of preview text. `fleet-email read` fetches a separate bounded full message: up to 262,144 source bytes and 100,000 body characters, plus To, Cc, Reply-To, Message-Id, In-Reply-To and References. Its `truncated` flag warns when the limit was reached. When run from a configured bot, the read returns a `read_receipt`; pass that ID to `email_preview` when replying so the checklist can link the full read to the send. Reading and archiving use the configured account. A reply is shown as an exact preview before any send. The `email_preview` tool posts To, Cc, Bcc, subject and body, followed by `Send it`, Edit and Hold controls. Tapping `Send it` changes the keyboard in place to Cancel on the left and Confirm send on the right. That first tap sends nothing.

Confirm send authorizes one exact email for ten minutes. The active poller process keeps the consent authority and checks the digest of the canonical recipients, subject and body; `email_send` must submit the identical content and matching consent ID. For the send attempt, the poller issues a short-lived one-time nonce for that digest on stdin. `fleet-email` reads the endpoint and per-session secret from a mode-0600 file in the bot state directory, then requires an HMAC response from the loopback authority before SMTP. The nonce never enters process arguments. A changed preview, expiry, replay or edit refuses the send. The backend claims one attempt before SMTP and never retries an uncertain result. A reaction, handoff preview or ordinary `Send it` bubble cannot authorize email. An assistant with shell permission in any stance could reach the mail credentials or authority file directly, so the boundary is as strong as your permission stance makes shell access.

`consents.json` under the bot's state directory is a mode-0600 audit record only. Editing it cannot create send authority. `mail-reads/` holds bounded read receipts with reply headers, and `mail-attempts/` holds one-attempt send records; neither is consent. The checklist compares the sent To/Cc addresses with the latest message's Reply-To, To and Cc, excluding your own address. It does not infer a Bcc recipient. A sent message may still remain in Claude Code's transcript because the assistant prepared it; treat that transcript as private. [SECURITY.md](../SECURITY.md) covers the wider data flow.

## Custom backend contract

Set `backend` in `email.json` to an absolute executable path. Call that adapter only through the installed `fleet-email` wrapper, with JSON on stdin; it returns JSON on stdout and exits nonzero on failure. Operations are `inbox`, `read`, `archive`, `snooze`, `send` and `test`. The wrapper asks the live poller's loopback authority to spend the exact one-time nonce before invoking a custom adapter's `send`. It forwards only the bot key, consent ID and exact email fields to the adapter. The adapter must create a mode-0600 `mail-attempts/<consent-id>` record with create-once semantics before its own SMTP or API attempt, and return `ok=true` only after full acceptance. The wrapper refuses a missing attempt record and never retries an uncertain result. Neither a custom adapter nor a fabricated `consents.json` entry can authorize a send through this wrapper. Do not expose credentials in stdout or logs.
