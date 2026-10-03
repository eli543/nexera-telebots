# Changelog

Changes are grouped by release. This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No entries yet.

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

### Changed

- The nine optional patches for official Telegram plugin 0.0.6 are replaced by one hash-checked seam diff for plugin 0.0.7 plus the project's own Telebots module. The local stock file is backed up for uninstall.
- Install starts with an empty bot list. It neither creates a bot nor loads the watchdog before pairing. The watchdog manages enrolled bots and falls back to PID-only health checks when an upstream plugin update removes the overlay.
- The phone setup now uses a hidden token prompt, a new Terminal window, bot-specific pairing, macOS consent while the person is at the Mac and a feature checklist.
- `/fresh` and `/update` send "⚡ Back and ready" with a Continue bubble after a resumed restart.
- The project is scoped to macOS 14 or newer. Bot names and count come from config; the docs state BotFather's pacing and account limits.
- `scoped` is documented as Claude Code's `--permission-mode auto`.

### Fixed

- `/usage` deduplicates transcript usage by message and request and includes subagent transcripts in fleet token burn.
- Slash commands use installed engine paths rather than the location of the clone. Hooks are inert outside bot sessions.
- Doctor and troubleshooting guidance cover overlay status, token duplication, exact logs and Claude Code version gates.
- The installer carries the overlay verifier and patch files into the installed fleet. Doctor accepts the empty bot list that installation creates.
- Rich message HTTP 404 falls back to plain text. Email sends require the exact, fresh consent record and full SMTP acceptance.

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
