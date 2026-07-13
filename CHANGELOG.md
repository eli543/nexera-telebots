# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-12

First public release. Built on Anthropic's official Telegram channel plugin
(`telegram@claude-plugins-official`, v0.0.6) and the sanctioned `--channels`
path.

### Added

- Live progress streaming: an animated status message that edits in place while
  Claude works and folds into a tap-to-expand recap. Zero model tokens.
- Guaranteed instant reaction ack on every inbound message, content-aware.
- Reply guarantee: a Stop hook so a turn cannot end silently in the terminal.
- Enforced structured replies: a deterministic numbered-TLDR check plus an
  optional Haiku judge, off by default.
- Document-delivery nudge: when Claude writes a document, it is handed to you as
  a tappable file.
- Voice-note prompts: local transcription with mlx-whisper on Apple Silicon and
  a faster-whisper fallback elsewhere.
- `/fresh`, `/resume`, and `/model` from Telegram, including a cross-bot resume
  picker. Idle-path commands need no plugin patches; mid-task interception is
  available as optional patches.
- Self-healing watchdog: a launchd daemon that restarts a dropped bot into the
  same conversation, with exact-PID kills, debounce, cooldown, and a crash-loop
  breaker.
- Snapshot and restore across a macOS reboot, resuming your exact conversations.
- Managed multi-bot fleet: one config file, N identical bots sharing one brain.
- Installer and uninstaller for the daemon layer, plus a plugin marketplace for
  the hooks, skills, and slash commands.
- Optional patches for mid-task `/fresh` and `/model`, quarantined with
  re-apply and restore tooling.
- Offline test harnesses for the hooks and a CI workflow (shellcheck, bash -n,
  py_compile, and a secret scan).

[0.1.0]: https://github.com/eli543/claude-telegram-fleet/releases/tag/v0.1.0
