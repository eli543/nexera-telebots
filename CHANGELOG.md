# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-public polish pass.

### Added

- `SETUP-WITH-CLAUDE.md`: a guided install written to an assisting Claude, so
  dropping the repo link into Claude and saying "set this up for me" stands up
  the first bot end to end with a verify gate at each step, explains every
  component as it goes, then repeats for more bots. Linked from the top of the
  README.

### Changed

- `/usage` and `/context` meter legends restyled to the minimal form: the
  colored squares are gone from the legend line under each bar (they stay inside
  the bar itself). The legend now reads `↳ X% used · Y% left · resets ...` and
  `↳ X% full · U used · F free of CAP`.
- Install docs corrected: only the installer lays down the engine, so the
  three-line plugin path is now honest that you also run `./install.sh`, and a
  guard in `/telegram-fleet:setup` stops early if the engine is not installed.
- README states it is built and provided by Nexera Intelligence, with a short
  About section; the badge row is trimmed to platform and license; and
  attribution was added to the plugin and marketplace metadata and NOTICE.

## [0.2.0] - 2026-07-14

The phone-control release: interrupt a busy bot, switch models in one tap, read
live meters, and get a real "/" menu. All four features ship as optional
`server.ts` patches (06-09) plus three new config-driven engines; nothing in the
zero-patch core changed behavior.

### Added

- `/esc` interrupt (patch 06 + `engine/fleet-esc`): press a real, verified
  Escape in the requesting bot's Terminal window so the current turn stops and
  a queued message is read immediately. No restart, no context loss. Atomic
  activate-front-verify before the single keystroke (macOS delivers keys to the
  frontmost application), per-tty debounce, a global keystroke lock, focus
  restore, and honest in-chat failure reporting.
- `/opus` and `/fable` one-tap model shortcuts (patch 07): menu taps cannot
  carry arguments, so the two common switches are commands of their own,
  routing into the existing `/model` machinery with the conversation kept.
- `/context` live context meter (patch 08 + `engine/fleet-context`): used
  tokens vs the window cap with a two-tone bar, read from the session
  transcript's newest usage record, computed and sent entirely outside the
  model - works mid-task and costs the session zero context.
- `/usage` plan limits + fleet burn (patch 08 + `engine/fleet-usage`): your
  real plan limits first - session and weekly percentages with local-time
  resets, fetched from the same endpoint the terminal `/usage` screen uses,
  with your own Claude Code OAuth token read from the Keychain at call time
  (never logged or stored; the undocumented endpoint fails soft to a one-line
  notice) - then measured token burn for today plus the trailing 7 days across
  every bot in `fleet.json`, per-bot and per-model split. Both meters use the
  orange/black emoji bar style, the machine's local timezone, and state their
  remaining limits honestly (the per-category context table is terminal-only).
- Real Telegram "/" command menu (patch 09): replaces the stock dead
  `/start` `/help` `/status` entries with the fleet's actual commands (`esc`,
  `fresh`, `opus`, `fable`, `model`, `context`, `usage`, `resume`),
  re-registered on every poller start, plus a documented one-off curl for
  instant effect.

### Changed

- README restructured: a complete Setup section now sits at the top - the
  one-bot-one-session-one-window design rationale, bot creation and pairing,
  the restart family, and the command menu.
- Patch tooling now applies and verifies all nine patches with an updated
  fully-patched reference hash; `verify-patches.sh` reports the four new
  patches individually.
- SECURITY.md documents exactly what `/esc` can and cannot press and the macOS
  Automation/Accessibility consent it sits behind. TROUBLESHOOTING, FAQ,
  ARCHITECTURE, and the fleet-ops skill cover the new commands.

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

[Unreleased]: https://github.com/eli543/nexera-telebots/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/eli543/nexera-telebots/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/eli543/nexera-telebots/releases/tag/v0.1.0
