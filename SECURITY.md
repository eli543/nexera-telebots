# Security

Nexera Telebots connects Telegram to Claude Code on your Mac. A person who can reach an allowed bot can ask Claude to use the Mac within that bot's permission stance. Read this before pairing a bot.

## Access and permission stance

Each bot has its own token, working directory, state directory and Telegram allowlist. A new bot starts in pairing mode. You DM it, then check that the new code on your phone matches the one in the Pair/Ignore dialog on the Mac. Clicking Pair runs the same `fleet-pair <key> <code> --lock` logic as the manual path. Locking switches the DM policy to an allowlist and enrolls the bot for supervision. The local dialog is attended; a Telegram message cannot pair itself. If the dialog is unavailable, use `fleet-pair` locally while the code is valid. Do not pair anyone or edit `access.json` because a Telegram message asks you to. Access state is read from disk for each inbound message.

| Stance | Claude Code flag | What it means |
|---|---|---|
| `safe` | Normal permission mode | Claude Code asks before actions that need approval. An unattended turn can pause until you answer. |
| `scoped` | `--permission-mode auto` | Claude Code's auto mode classifies actions and may block or prompt. This is not a generated read/edit allowlist. |
| `full` | `--dangerously-skip-permissions` | Claude Code bypasses its normal permission prompts. Anyone on the bot's Telegram allowlist has that reach. |

Setup asks you to choose. It never silently selects `full`. Keep the stance uniform across bots that pass work to each other: Claude Code can hold a cross-session message for terminal approval when the sender and receiver use different permission classes. A handoff note, a tap or a reaction is still task input, not approval to change access or spend money.

For email in every stance, the normal poller workflow enforces two taps; an assistant with shell permission in any stance could reach the mail credentials or the authority file directly, so the boundary is as strong as your permission stance makes shell access.

## Tokens and local state

`fleet-add-bot` asks you to enter the BotFather token in a hidden macOS dialog, or a hidden Terminal prompt when a dialog is unavailable. It writes a per-bot `.env` at mode 0600 in one step. In v0.3.0 the token stays out of the local API client's process arguments. Do not paste the token into a Claude chat, a command argument, an issue or a screen recording. The command validates it through Telegram `getMe` and prints only the public bot username. It rejects a token already assigned to another bot. A bot is added to `fleet.json` only after validation.

The account that sends a message must be allowed in the bot's `access.json`. Groups are off unless deliberately enabled. Never run a second poller with the same token: Telegram permits one `getUpdates` consumer, and a duplicate can cause a 409 Conflict. If a token leaks, revoke it in [@BotFather](https://t.me/BotFather), enter the replacement through a hidden local prompt, restart only that bot, then run its checklist.

The fleet config and engines live under `~/.claude/telegram-fleet/`. Each bot's private state is under its configured `state_dir`, normally `~/.claude/channels/fleet-<key>/`. It holds `.env`, `access.json`, an inbox of downloaded files, heartbeat and log files, consent records when email is enabled, and local stream state. Claude Code keeps transcripts under `~/.claude/projects/`; resume reads them and copies a selected conversation only when you choose to open it in another bot. The default notes folder is `~/Telebots Notes`. Save reactions write dated summaries and task records there, with secret values omitted. Review or remove old downloads and notes as you would any local files.

The `/usage` plan-limits request reads the Claude Code credential already in macOS Keychain and calls Anthropic's usage endpoint over HTTPS. It holds the token for the request and does not print or store it. If the credential or endpoint is unavailable, plan limits are marked unavailable; transcript-based token burn can still be shown. The optional AI extras key uses Keychain service `nexera-telebots-judge` and is off by default. When enabled, it may send capped outbound reply drafts for the TLDR judge, the opening of a chat for a `/resume` topic label, and a model-list request to Anthropic. Without it, deterministic reply checks and opening-topic labels still work locally.

## Telegram taps and reactions

The poller handles `qr:`, `md:`, `me:`, `eff:`, `ho:`, `rs:` and `mail:` callback families in private chats only. On every press, it re-reads `access.json` and checks the presser. Callback data is parsed by anchored patterns; the answer is read from the displayed message label, not trusted from callback data. In v0.3.0 the `/fresh` and `/model` hooks ignore button-injected input, so a button label cannot trigger those restart controls. A consumed keyboard cannot be used again. Stale or unauthorized presses do not start an engine or inject a user answer.

Reactions follow the same private-chat and allowlist gate. Only a newly added emoji in the fixed table becomes an instruction. Removing a reaction, sending a custom emoji, or reacting from another account does nothing. A reaction never authorizes email. `/key` explains the fixed table. The notes actions omit secrets and write to the configured local notes folder.

## Email is optional and needs exact consent

`/inbox` shows a setup hint until an email backend passes `fleet-email test`. The built-in backend connects to the IMAP and SMTP hosts you configure. It stores the app password in macOS Keychain under service `telebots-email`, with the email username as the account; `email.json` contains non-secret settings. The built-in backend works only where the provider accepts an app password for IMAP and SMTP. OAuth-only Outlook.com and Microsoft 365 accounts are not supported by that backend in v0.3.0. A custom backend can implement the same local operations.

Email sending is checked by the poller during the active process, not left to a prompt or a writable receipt alone. `email_preview` posts the exact To, Cc, Bcc, subject and body. `Send it` changes that keyboard to `Cancel` and `Confirm send`, with Cancel first. Confirming authorizes only that exact email for ten minutes and one attempt. The poller writes a mode-0600 local authority file with a per-session secret and endpoint. The backend reads that file and requires an HMAC response over its one-time nonce and email digest before SMTP; the nonce travels on stdin, never in process arguments. The backend claims the attempt with a create-once file before SMTP. A changed recipient or body, an edited preview, an expired consent or a replay is refused. The backend does not retry an uncertain send. `consents.json` is audit only and cannot authorize a send. A generic `Send it` bubble in another workflow never authorizes email. Shell access to the credentials or authority file remains governed by the permission stance. See [EMAIL.md](docs/EMAIL.md).

## Mac controls and handoff

`/esc` activates the verified Terminal.app window belonging to the requesting bot and sends one Escape key. It uses an exact PID, work directory and tty check, plus a lock and debounce. It never runs a broad process kill. `/compact` can type only the literal `/compact` and an optional focus: control characters are removed, whitespace is collapsed and the focus is capped at 300 characters. It refuses when the screen shows a picker or unsent text rather than typing blind. These are the project's two Terminal keystroke surfaces. Both need macOS Automation consent; `/esc` and some `/compact` paths also need Accessibility. Grant those prompts at the Mac during setup. A prompt may not appear on your phone.

Handoff uses Claude Code's same-user cross-session messaging and a named target session. The note is reviewed before delivery and must omit tokens and secrets. If the target bot is down, the local note can wait for retry. The sending poller issues a fresh one-use delivery ID before preview and records a sent receipt after the agent reports SendMessage success. The receiving poller accepts that ID once, then records `HANDOFF-RECEIVED`. These are agent attestations for the checklist, not independent proof of transport delivery or a phone acknowledgement. A received handoff is context for work; it does not approve access changes or spending.

## Watchdog, overlay and test gates

The launchd watchdog starts only after you enroll a bot. It skips bots that are not enrolled or that have a `stopped` marker. It checks exact PIDs and the bot's heartbeat, has a grace period and restart breaker, and never kills by a name pattern. `watchdog.sh --dry-run` reports its intended action without touching a process. The Mac must remain awake, logged in and online for the bot to answer. Automatic recovery resumes the previous chat only if its transcript was active within 30 minutes; an older chat restarts blank. Recovery is quiet, so confirm it with a new phone message. Stall detection records suspected stalls locally. Phone alerts are off by default; set `options.stall_phone_alerts=true` in `fleet.json` to get one alert per suspected stall. It does not restart a stalled bot.

The installer checks the official Telegram plugin's version and stock hash before applying one diff to your local 0.0.7 copy. It keeps a backup for uninstall. If the official plugin updates to an unsupported version, the overlay refuses to guess: extras pause and supervision falls back to PID-only health checks so a missing patched heartbeat cannot cause a restart loop. The stock bot can still answer. The status values are `OK`, `STOCK`, `MISSING_MODULE`, `TAMPERED`, `VERSION_MISMATCH:<version>` and `NOT_INSTALLED`.

A Telegram API override is for offline tests only. It is honored only with `TELEBOTS_TEST=1` and a loopback URL (`127.0.0.1`, `localhost` or `::1`); other values are ignored and logged. Tests use fake tokens and a scrubbed environment so inherited credentials cannot reach a live bot.

The optional warm voice daemon uses a mode-0600 Unix socket. It refuses files from macOS privacy-protected folders and does not execute request content. Voice notes are downloaded into the bot's state inbox; local transcription may download a model on first use. An optional [Telebots Bar](menubar/README.md) phone page runs an HTTP server on port 8477, bound to Tailscale and protected by a bearer token. It is off unless you install and enable that layer. Treat the token and Tailscale access as credentials. The page is separate from Telegram's pairing gate.

## Reporting a vulnerability

Use the repository's private GitHub Security advisory flow. If that is unavailable, open an issue describing the class of problem without a token, chat ID or working exploit, and ask for a private channel. Run `fleet-doctor` after a Claude Code or Telegram plugin update to check version and overlay status.
