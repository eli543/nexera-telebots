# Windows support (experimental, untested)

> UNTESTED. These scripts are a good-faith port of the macOS daemon layer,
> written to spec and never run on real hardware. They need a community tester
> before anyone should trust them. Do not run them on a machine you cannot afford
> to babysit. If you try them, please open an issue with what worked and what did
> not.

## What ports and what does not

- The hooks (the Claude-facing behavior: streaming, ack, TLDR, reply-guard,
  deliver-nudge) are portable Python with no third-party dependencies. On
  Windows, the one known gap is the file lock: the streamer uses `fcntl`, which
  is POSIX only, and falls back to a portable lock shim on import failure. That
  fallback is the least-tested path.
- The daemon layer does not port directly. macOS uses launchd, the Keychain,
  `osascript`, and Terminal.app. Windows has none of those. These scripts mirror
  the logic with Task Scheduler and Windows Terminal instead.

## The three scripts

- `launch-channel.ps1` starts one bot: reads the token from that bot's `.env`,
  sets `TELEGRAM_STATE_DIR`, and launches `claude --channels` in a new Windows
  Terminal tab.
- `watchdog.ps1` runs one health pass over the fleet: for each bot in
  `fleet.json`, it checks whether the session and poller processes are alive and
  restarts an unhealthy bot through `launch-channel.ps1`. It identifies processes
  by a verified command-line match, never by a broad name kill.
- `register-task.ps1` registers a Scheduled Task that runs `watchdog.ps1` on a
  loop at logon.

## Config

These read the same `fleet.json` as the macOS layer, at
`%USERPROFILE%\.claude\telegram-fleet\fleet.json` (override with the `FLEET_HOME`
environment variable). Per-bot state directories live under
`%USERPROFILE%\.claude\channels\fleet-<key>\`.

## Known gaps versus macOS

- No exact-conversation snapshot/restore across a reboot yet; that path uses
  macOS specifics that need a Windows rewrite.
- No window-grid tiling.
- Voice uses `transcribe-voice-faster` (faster-whisper); the mlx backend is Apple
  Silicon only.
- The permission relay and permission-stance behavior follow Claude Code's own
  Windows support, which is outside this repo.

## Wanted

A tester on real Windows with Claude Code and Bun installed. A working report,
with fixes, would let this graduate out of experimental. Good first issue.
