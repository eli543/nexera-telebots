# Changelog

Changes are grouped by release. This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Terminal window lookups in `fleet-compact`, `fleet-esc` and `fleet-fresh` walk one snapshot of window ids. Walking the windows by z-order index skipped a window raised during the walk, which read as a blank screen or a missing window.
- `fleet-compact` no longer ends a compaction on one bad look at the screen. It looks again before it believes a picker, typed text or an unreadable frame, asks Claude Code's session registry (`status` and `waitingFor`) about a frame it cannot read, and gives a stalled Terminal up to two minutes. An unreadable frame is saved to `compact-last-unreadable.txt` at mode 0600.
- `fleet-compact` waits for a menu or question to close instead of refusing. A leftover menu gets one lone Esc through the TIOCSTI ioctl, and only when the registry shows the bot idle or waiting on `dialog open`; for any other waiting reason it asks for `/esc` and never presses Esc itself. The acknowledgement is held until the way is clear.
- Unsent text in the input box is cleared with one ctrl-c, saved to `compact-cleared-box.txt` at mode 0600 and quoted back in the chat. A paste placeholder, a box taller than 16 rows and text that keeps changing are left alone. Claude Code's `Try "..."` hint reads as an empty box. The in-flight guard now refuses only while the bot is busy.
- `/esc` first injects a lone Esc with TIOCSTI, which needs no focus change and no keystroke permission, then falls back to the keystroke path. The keystroke osascript runs under an outside time limit and looks every 0.2 seconds, for up to two seconds, for Terminal to come forward.
- `fleet-fresh` signals nothing until Terminal answers. It waits up to five minutes, says so once and then refuses, leaving the old session running. It opens the new window as its own osascript under a 40-second outside limit, activates Terminal afterwards in the background, retries a failed open for up to eight minutes after checking for a session a late open started, and sends a notice when a restart does not come back, unless quiet.
- `fleet-restore` opens each window before it activates Terminal, and the activation runs in the background.
- No engine relies on an AppleScript `with timeout` as a bound: a frozen Terminal held osascript well past one.
- Known limit: the TIOCSTI paths need the engine to run inside the bot's own terminal session. The poller starts engines in a new session, so until that changes `/esc` uses its keystroke path and `/compact` asks for `/esc` when a leftover menu is open.

## [0.3.0] - Pending release

Prepared Friday, October 2, 2026. The release date will be set after integration and the real-phone acceptance test.

### Added

- The 13-command Telegram menu: `/esc`, `/fresh`, `/update`, `/key`, `/inbox`, `/opus`, `/fable`, `/model`, `/effort`, `/context`, `/compact`, `/usage` and `/resume`.
- Tap bubbles, the first-reply quick bar, a model-then-effort picker and Close on every picker.
- Seven reaction actions and `/key`, with a local notes folder in place of a private knowledge base.
- A live work box based on streamer v30.22, with the 🎯 mission line and a Plan built from real task updates.
- Poller-side local voice transcription, with an optional warm model, a local cold fallback and an explicit failure path.
- Tappable `/resume` with labels based on each conversation's opening topic, including a no-key fallback.
- A context nudge at 60, 70, 80 and 90 percent, plus a picker for handoff to another configured bot.
- Optional `/inbox` cards backed by the user's own IMAP/SMTP account. Sending uses an exact preview and server-checked two-tap consent.
- Setup and operations engines: `fleet-add-bot`, `fleet-start`, `fleet-pair`, `fleet-checklist`, `fleet-email`, `fleet-update`, `fleet-compact`, `fleet-state`, `fleet-handoff`, `fleet-resume-open` and `fleet-context-pct`.
- An upstream plugin hash watch and an offline sandbox runner in CI.
- Telebots Bar as an optional Mac menu bar layer, with a phone page over Tailscale.
- One attended Pair/Ignore dialog on the Mac after a new bot receives a pairing code. Pair rechecks the sender, creation time and policy from the pairing request snapshot. `fleet-pair --lock` remains the manual fallback.
- Setup rows for a history-preserving model switch, reopening a chat, blank `/fresh`, agent-attested handoff, full inbox reply, quiet recovery, reboot restore and phone rescue. Handoff receipts must match, and P13 passes only after the user confirms the note in the target bot chat. Real-device rows still say when phone proof is needed.

### Changed

- The nine optional patches for official Telegram plugin 0.0.6 are replaced by one hash-checked seam diff for plugin 0.0.7 plus the project's own Telebots module. The local stock file is backed up for uninstall.
- Install starts with an empty bot list. It neither creates a bot nor loads the watchdog before pairing. The watchdog manages enrolled bots and falls back to PID-only health checks when an upstream plugin update removes the overlay.
- The phone setup now uses a hidden token prompt, a new Terminal window, bot-specific local pairing, macOS consent while the person is at the Mac and a setup checklist.
- `/fresh` starts blank; `/update` reloads the installed build without downloading and preserves the conversation when available. "⚡ Back and ready" is recorded as accepted only when Telegram confirms it.
- `/opus` and `/fable` keep the current or default effort unless the person explicitly selects another effort.
- The docs state that the Mac must stay awake, logged in and online; automatic recovery resumes a chat active within 30 minutes and otherwise starts blank. Restart is quiet. Stall detection logs locally; `options.stall_phone_alerts=true` enables one phone alert per suspected stall and is off by default.
- The project is scoped to macOS 14 or newer. Bot names and count come from config; the docs state BotFather's pacing and account limits.
- `scoped` is documented as Claude Code's `--permission-mode auto`.

### Fixed

- `/usage` deduplicates transcript usage by message and request and includes subagent transcripts in fleet token burn.
- Slash commands use installed engine paths rather than the location of the clone. Hooks are inert outside bot sessions.
- Doctor and troubleshooting guidance cover overlay status, token duplication, exact logs and Claude Code version gates.
- The installer carries the overlay verifier and patch files into the installed fleet. Doctor accepts the empty bot list that installation creates.
- Rich message HTTP 404 falls back to plain text. Email sends require the active poller's one-time nonce for the exact digest and full SMTP acceptance; disk consent receipts are audit only.
- Checklist Escape, compact, resume-label and watchdog rows read the producer's actual log path and success wording. The ops tests run real engines inside blocked-command sandboxes instead of manufacturing success strings.
- The sandbox's untouched check matches protected process commands, so a short-lived child PID cannot look like a changed daemon. Its summary names the shared roots and suite reuse behind the 42 grouped scenario results.
- Bot tokens stay out of local API process arguments. Full inbox reads return bounded bodies and reply headers.
- OAuth credentials for usage checks now enter curl through stdin, not curl arguments. A source scan covers credential headers in engine, daemon and hook curl calls.
- Tap labels reject newlines and other control characters. `/fresh` and `/model` parse structured hook input and reject synthetic button, reaction and email confirmation origins before reading the command body.
- Every custom email adapter operation gets its documented payload. Custom adapters accept opaque message IDs for reads, archives and snoozes, including the read that begins a reply. The built-in IMAP backend still requires numeric UIDs. A truncated inbox read records `full=false`, and the full-reply checklist row requires a complete read.
- Email consent checks use a mode-0600 session authority file and an HMAC response. The normal poller workflow enforces two taps; shell permission in any stance can reach mail credentials or the authority file directly.
- Handoff receipts use sender-issued one-use delivery IDs and are labeled agent-attested. Editing a preview or waiting beyond one hour requires ID renewal and a fresh preview; the old ID is cancelled. Matching receipts are evidence of the agents' actions; the user confirms the note in the destination chat before checklist P13 passes.
- The voice guide now states that `autotranscribe.off` disables poller transcription for one bot; the local transcriber chooses an available local backend. The stored `voice` option does not select or disable the poller backend.
- Fleet notices use one shared sender that distinguishes Telegram acceptance from an attempted or failed send. The update checklist accepts only a confirmed Back and ready response. Telebots Bar reads the engine's accepted, attempted or failed ping status while judging reconnect health separately. The watchdog keeps separate records for stall observed and phone notification accepted; the Mac and phone dashboards show observed stalls even when phone alerts are off, and show `phone notified` only for a matching accepted alert.
- Uninstall stops the watchdog, waits for its PID file and watchdog repair lock to clear, then rechecks the plugin slot before deciding whether to restore it. It verifies the final stock hash whenever the pinned 0.0.7 slot is present and aborts if that check fails. A failed watchdog stop aborts teardown.
- Setup and operations copy uses the Pair/Ignore dialog as the normal path, documents the manual pairing fallback and the fleet log locations. `/watch` ends when its session restarts, and `/update` describes the installed build.
- The dashboard changes have source and fixture checks; Telebots Bar was not built or launched in this fix round. The other release checks are offline. Release date and real Mac and phone acceptance remain pending.

### Removed

- The experimental `platform/windows/` folder and its support promise. A separate cross-platform product is outside this release.
- The optional 0.0.6 patch set and the old setup instructions for applying it.

## [0.2.0] - Tuesday, July 14, 2026

The original phone-control release on official Telegram plugin 0.0.6. Its poller extensions were optional patches; version 0.3.0 replaces that packaging.

### Added

- `/esc` interrupt with a verified Terminal window and exact PID guard.
- `/opus` and `/fable` shortcuts that kept the conversation.
- `/context` and `/usage` meters, and an eight-command Telegram menu.
- Setup guidance for the installer, bot pairing and daemon layer.

## [0.1.0] - Sunday, July 12, 2026

First public release, built on Anthropic's official Telegram channel plugin 0.0.6 and its `--channels` path.

### Added

- Live progress, acknowledgement reactions, a reply guard, numbered TLDR checks and document delivery nudges.
- Local voice notes, `/fresh`, `/resume`, `/model`, watchdog supervision and exact-conversation snapshot and restore.
- A config-driven fleet, installer and uninstaller, plus offline hook checks.
