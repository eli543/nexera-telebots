# Troubleshooting

Start with `/telegram-fleet:doctor`. It checks the Claude Code version, Bun,
the official plugin, the config, and per-bot liveness, and prints a green or red
table. Most problems below show up there first.

## A bot stops replying, or you see 409 Conflict in the logs

Telegram allows exactly one `getUpdates` consumer per bot token. Two pollers on
the same token fight over it and one gets a 409 Conflict. This is the most common
failure.

Causes and fixes:

- Two sessions for one bot. You started the bot twice, or a stale session never
  died. Find the pollers for that bot and keep one:
  `ps -axo pid,command | grep server.ts`. Identify the extra one by its working
  directory, then `kill <pid>` the specific PID. Never pattern-kill.
- The same token in two `.env` files. Two bots must never share a token. Check
  each `~/.claude/channels/fleet-<key>/.env`.
- A leftover poller from a crash. `/fresh` on the affected bot, or let the
  watchdog restart it, clears it.

## The channel never connects

Almost always Bun. The official plugin's MCP server runs on Bun, and without it
the channel fails quietly.

- Confirm Bun is installed: `bun --version`. If missing:
  `curl -fsSL https://bun.sh/install | bash`, then restart the session.
- Confirm you launched with the channel flag. The session must run
  `claude --channels plugin:telegram@claude-plugins-official`. The fleet launcher
  does this for you; a hand-started `claude` does not.
- Confirm the token is set. Each bot reads `TELEGRAM_BOT_TOKEN` from its `.env`.
  `/telegram:configure` (with no argument) reports whether a token is present.

## Claude pauses partway through an unattended task

The `safe` permission stance uses normal permission prompts, and a prompt pauses
the session until you answer. From your phone this looks like the bot going quiet
mid-task.

- Where your Claude Code version and the plugin support the permission relay,
  approval prompts are forwarded to Telegram and you answer them there.
- Otherwise, either return to the terminal to approve, or reconsider your stance.
  `scoped` pre-allows a common read and edit set so routine work does not pause.
  `full` never pauses but removes the guardrails; read [SECURITY.md](../SECURITY.md)
  before choosing it.

## The watchdog says a bot is unhealthy but it looks fine

Health is three things, not one: the session PID alive, the poller PID alive, and
the poller heartbeat fresh (under 180 seconds). A bot can have a live process yet
a dead poll loop, for example after a clean stop or a persistent 409. That is
exactly the "alive but silent" case the heartbeat catches.

- Check the heartbeat age: compare `stat -f %m ~/.claude/channels/fleet-<key>/poller.heartbeat`
  to now. If it is stale, the poll loop is dead even though the process is up; a
  restart fixes it.
- If the watchdog is restarting the same bot repeatedly, the crash-loop breaker
  disables it after too many restarts in an hour and alerts you. Fix the root
  cause (usually a bad token or a missing working directory), then remove the
  bot's disable marker under `~/.claude/telegram-fleet/watchdog-state/`.
- To see what the watchdog would do without it acting, run
  `~/.claude/telegram-fleet/bin/watchdog.sh --dry-run`.

## /fresh, /model, or /resume typed in Telegram did nothing

- `/resume` is idle-only. Typed while the bot is mid-task, it folds into the
  running turn and does nothing. Wait for the bot to finish, then resend.
- `/fresh` and `/model` work mid-task only if the optional patches are applied.
  Without them, both still work when the bot is idle. See
  [../patches/README.md](../patches/README.md).
- Check the command spelling. `/model` takes a class (`opus`, `sonnet`, `fable`,
  `haiku`) and an optional effort (`low`, `medium`, `high`, `xhigh`, `max`). An
  unknown token is rejected and nothing changes.

## After a Claude Code or plugin update, the optional patches are gone

Expected. The mid-task `/fresh` and `/model` interceptors are patches on the
official plugin's `server.ts`, which lives in a version-pinned cache directory. A
plugin update writes a new cache directory and your patches do not carry over.

- The idle-path commands keep working with no patches, so the core is unaffected.
- To restore mid-task interception, rerun `patches/apply-patches.sh`. It checks
  the plugin version, backs up the stock file, and applies the patches in order.
- `patches/verify-patches.sh` reports which patches are currently present.

This is why the patches are optional and quarantined rather than part of the core
install.

## Voice notes are not transcribed

- Check the `voice` option in `fleet.json`. `auto` uses mlx on Apple Silicon and
  faster-whisper elsewhere, and disables voice if neither is installed.
- On Apple Silicon, confirm `mlx_whisper` is on the path:
  `uv tool install mlx-whisper`, plus `brew install ffmpeg`.
- Elsewhere, `pip install faster-whisper`.
- The first transcription downloads the model and is slow; later ones are fast.

## Something else

- Read the recent watchdog log: `~/.claude/telegram-fleet/logs/watchdog.log`.
- The `fleet-ops` skill documents the kill switches, safe testing with
  `STREAM_DRY=1`, and reading state per bot.
- If a hook is misbehaving, turn it off with its kill switch (see
  [ARCHITECTURE.md](ARCHITECTURE.md)) and the bot keeps running without it.
