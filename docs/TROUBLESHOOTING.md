# Troubleshooting

Start with `~/.claude/telegram-fleet/bin/fleet-doctor`. It checks the official plugin version, overlay, bot config, token validity, duplicate tokens, Claude Code version and per-bot health. Then run `~/.claude/telegram-fleet/bin/fleet-checklist <key>` for a per-feature result. Report each row as `Passed`, `Failed`, `Needs your action` or `Not configured` with its reason; an installed file alone does not prove phone delivery.

## The bot does not reply or pairing never arrives

Check the bot's MCP log and its `<state_dir>/logs/poller-events.log`. A poller error may be in the MCP log, not in the visible Terminal window. Check `fleet-doctor` for `getMe`, the session, poller and heartbeat. If Bun is missing, install it from [Bun's instructions](https://bun.sh/docs/installation) and restart this bot. If the token is invalid, revoke or replace it through BotFather and a hidden local prompt; never paste it into chat.

Telegram permits one `getUpdates` consumer per token. A 409 Conflict often means the same token is in two bot configs or two channel sessions are polling it. `fleet-doctor` checks duplicate tokens. Identify any extra process by exact PID, state directory and parent before stopping it. Never kill by a name pattern. A 401 response usually means the token was revoked or copied incorrectly.

## Overlay is not OK

`poller/verify-overlay.sh --status` returns `OK`, `STOCK`, `MISSING_MODULE`, `TAMPERED`, `VERSION_MISMATCH:<version>` or `NOT_INSTALLED`. The installer and doctor explain the same status. An unsupported upstream version must not be patched by force. When Anthropic changes the official plugin, Telebots extras pause and the watchdog switches to PID-only checks; the stock channel may still reply. Update this project when it supports that version. If the same 0.0.7 slot was replaced by stock, the guarded overlay installer can reapply the seam after checking its hash.

The overlay is required for mid-task controls, the 13-command menu, tap callbacks, reactions and poller voice. The old 0.0.6 patch scripts no longer apply. If the plugin was installed while an ordinary Claude Code session was open, run `/reload-plugins` in that session to load this project's slash commands; restart only the bot being checked if its poller needs the new overlay.

## A command or picker does nothing

The phone controls work in a private chat with an allowed user. An unauthorized or stale tap is refused. `/resume` needs transcripts; labels come from the opening of each chat and do not require an API key. A handoff picker needs at least two configured bots. `/inbox` shows a setup hint until email passes `fleet-email test`.

For `/esc` and `/compact`, macOS must grant Terminal.app Automation consent; `/esc` and some compact paths also need Accessibility. Run `fleet-checklist --consent` while at the Mac. In System Settings > Privacy & Security, review Automation and Accessibility if a prompt was missed. The engines refuse an unverified window or an unsent prompt instead of typing into another app. Read `<state_dir>/logs/esc.log` or `compact.log` for the exact reason.

`/usage` can show token burn while plan limits are unavailable if the Claude Code credential or Anthropic usage endpoint cannot be read. `/compact` may answer "Not enough messages to compact"; that proves the command path but means the session is too short. `fleet-checklist` accepts that specific result for its compact row.

## Watchdog reports unhealthy, or recovery loops

The watchdog supervises only bots with `.enrolled` and without `stopped`. It checks exact session and poller PIDs plus the heartbeat when the overlay is `OK`. Run the installed watchdog script with `--dry-run` and read `$FLEET_HOME/logs/watchdog.log`. A grace period, cooldown and breaker stop repeated launches. If an upstream update removed the overlay, PID-only mode avoids a false heartbeat failure. The Mac must be awake and logged in, and a real Telegram round trip is needed to prove recovery.

Snapshot and restore use exact conversations. If restore refuses a snapshot, keep it for inspection rather than deleting it. An offline sandbox check does not establish sleep, login or reboot behavior on your Mac.

## Voice, email or phone page fails

For voice, check `autotranscribe.off`, ffmpeg and the local backend, then read `<state_dir>/logs/voice-transcribe.log`. On Apple Silicon, install uv and mlx-whisper; the first run downloads a model. Intel Macs need the slower fallback. A failed local transcript should leave the voice note available for the model's fallback path.

For email, run `fleet-email test`. It checks the configured IMAP and SMTP account without sending. An app password must be in Keychain under service `telebots-email`, not in `email.json`. OAuth-only providers need a separate backend. An email send that reached an uncertain SMTP state is not retried automatically. See [EMAIL.md](EMAIL.md).

For the optional phone page, confirm Telebots Bar is enabled, both devices are on the intended Tailscale network, the page is bound to the Tailscale interface on port 8477 and its bearer token is valid. The page does not share Telegram's pairing access. See [menubar/README.md](../menubar/README.md).
