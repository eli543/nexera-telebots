# Architecture

Nexera Telebots runs on macOS through Anthropic's official `telegram@claude-plugins-official` channel. Each configured bot has one Claude Code session in one Terminal.app window and one Telegram poller. The project adds a version-checked local overlay, a plugin of hooks and skills, and optional launchd supervision.

## The overlay

Anthropic's official Telegram plugin 0.0.7 remains installed through Claude Code. `poller/install-overlay.sh` checks its version and stock SHA-256, keeps a local backup, applies `poller/seam-0.0.7.patch` and copies the project's `telebots.ts` module and `ux.json`. The seam contains only the hooks the module cannot attach from outside, including callback order, reply button markup, inbound interception, heartbeat and the poller guard. `poller/README.md` lists the changes to the Apache-2.0 file.

The seam loads the module with a guarded import. If the module fails, the stock poller can still handle a normal Telegram conversation. The heartbeat remains in the seam. `poller/verify-overlay.sh --status` prints one of `OK`, `STOCK`, `MISSING_MODULE`, `TAMPERED`, `VERSION_MISMATCH:<version>` or `NOT_INSTALLED`. It exits successfully only for `OK`. Uninstall restores the stock backup.

A new upstream plugin version does not get an unreviewed patch. Telebots extras pause, the watchdog switches to PID-only checks and reports the version change once per bot. A stock bot can still work while the seam is updated.

## Message and reply path

```text
Telegram DM -> official plugin poller -> access gate -> Telebots intercept or Claude Code turn
                                                       -> plugin hooks and rules
Claude reply -> official plugin reply tool -> Telegram DM
```

The poller reads the bot's `access.json` on inbound messages. It intercepts Telegram controls before a busy Claude turn can consume them as ordinary chat. It sends an MCP notification for normal messages, including voice transcripts when a local backend succeeds. The plugin's hooks add the acknowledgement, live work box, reply guard, context guardian and rendered Telebots rules. The Plan in the box comes from real task updates; it is empty if a turn creates no tasks. Rich messages fall back to plain messages when the Telegram server rejects them.

The poller registers 13 commands from `ux.json` on start. `/key` and the rules use the same table. It reads fleet config at use time for `/resume` and handoff, so adding a bot does not require a fixed-name table.

| Callback prefix | Purpose |
|---|---|
| `qr:` | Quick reply and checklist bubbles |
| `md:` and `me:` | Model, then effort picker |
| `eff:` | Effort picker |
| `ho:` | Handoff target picker |
| `rs:` | Resume picker |
| `mail:` | Exact email preview and confirmation |
| `perm:` | The official plugin's permission replies |

Telebots callback handlers register before the official `perm:` handler. Every Telebots press is checked as a private chat against the current allowlist. The label is read from the displayed message, and a used keyboard is cleared. [SECURITY.md](../SECURITY.md) describes the gates.

## Process and supervision

```text
Terminal.app window
  Claude Code --channels plugin:telegram@claude-plugins-official --name telebots-<key>
    Bun MCP worker
      official server.ts plus local seam and telebots.ts

launchd -> watchdog loop -> one health pass -> exact bot restart when needed
```

Each bot has a different token, work directory, state directory and session name. Telegram permits one poller per token. The watchdog manages only bots with an `.enrolled` marker and no `stopped` marker. It checks session PID, poller PID and heartbeat, uses a grace period, cooldown and breaker, and restarts the exact bot through `fleet-fresh`. It never kills by a name pattern. If overlay status is not `OK`, PID-only checks avoid treating the stock plugin's missing Telebots heartbeat as a dead bot. The Mac must be awake and logged in.

Snapshot and restore use a fleet transaction lock and generation ID. Restore validates the snapshot before changing sessions, and archives a generation only after the relevant bots pass health checks. A sandbox test is not proof of a reboot restore on a real Mac.

## Files and logs

`FLEET_HOME` defaults to `~/.claude/telegram-fleet/`. `fleet.json` holds bot names, keys, workdirs, state dirs and non-secret options. The installer copies engines into `bin/`, the menu table into `ux.json`, and writes logs and snapshots under that home. `email.json` holds non-secret account settings; the optional app password stays in Keychain.

A bot state directory normally follows `~/.claude/channels/fleet-<key>/` and contains:

| File | Purpose |
|---|---|
| `.env` | Token, mode 0600 |
| `access.json` | Pairing policy and allowed numeric user IDs |
| `.enrolled` | The bot finished pairing and may be supervised |
| `stopped` | A person parked the bot; watchdog skips it |
| `rich-stream` | Enable rich work box transport |
| `autotranscribe.off` | Disable poller voice transcription |
| `guardian-thresholds` | Per-bot context nudge thresholds |
| `poller.heartbeat`, `last-inbound` | Liveness and inbound timestamps |
| `inbox/`, `stream/` | Downloads and work box state |
| `logs/` | Poller, engine and voice evidence |

`logs/poller-events.log` contains time, event name and non-secret fields, never message text or a token. The checklist reads this file and the engine logs to prove observed behavior. Downloaded attachments and notes remain local files and need ordinary retention care.

## Kill switches and testing

Per-bot hook switches include `stream.off`, `ack.off`, `tldr_hook_off`, `tldr_judge_off` and `reply_guard_off`. `autotranscribe.off` disables poller voice. The global document-delivery switch is `$FLEET_HOME/deliver-nudge.off`. A switch changes one feature without stopping the poller; restore it by removing the marker.

Offline tests must use `tests/sandbox/sbenv`, a fake home, fake tokens, a loopback Telegram API and blocking command shims. Never run a second poller on a live bot token. See [CHECKLIST.md](CHECKLIST.md) for the separate, human-assisted phone acceptance.
