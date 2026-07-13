---
name: fleet-ops
description: Day-two operations for a claude-telegram-fleet install. Use when the user wants to add a bot to the fleet, rotate a bot token, read the watchdog logs, park or re-enable a bot, turn a hook off with a kill switch, or test a hook safely. Covers the exact file paths and the safe procedures.
---

Operational knowledge for running a fleet after it is set up. `$FLEET_HOME`
defaults to `~/.claude/telegram-fleet`. State directories are at
`~/.claude/channels/fleet-<key>/`.

## Add a bot

1. Create the bot in @BotFather and copy its token.
2. Add an entry to `$FLEET_HOME/fleet.json` (schema in
   `config/fleet.example.json`): `name`, `key` (matching
   `^[a-z0-9][a-z0-9-]{0,30}$`, unique), `workdir`, `state_dir` of
   `~/.claude/channels/fleet-<key>`, and an emoji.
3. Create the state directory and write the token:
   ```
   mkdir -p ~/.claude/channels/fleet-<key>
   printf 'TELEGRAM_BOT_TOKEN=%s\n' "<token>" > ~/.claude/channels/fleet-<key>/.env
   chmod 600 ~/.claude/channels/fleet-<key>/.env
   ```
   Never echo the token back or write it anywhere else.
4. Seed `access.json` with `dmPolicy` `pairing` and an empty `allowFrom`.
5. Pair and lock down: DM the bot, `/telegram:access pair <code>`, then
   `/telegram:access policy allowlist`.
6. Start it: `$FLEET_HOME/bin/fleet-launch <key>`. The watchdog picks it up on
   its next pass; no script edits are needed.

## Rotate a token

1. In @BotFather, pick the bot and choose Revoke current token. The old token
   dies immediately.
2. Write the new token into `~/.claude/channels/fleet-<key>/.env` (chmod 600),
   or run `/telegram:configure <new-token>` from that bot.
3. Restart the bot so the poller reloads the token:
   `$FLEET_HOME/bin/fleet-fresh <pid> <workdir>` or send `/fresh`.

## Read the logs

- Watchdog: `$FLEET_HOME/logs/watchdog.log` (restarts, health decisions).
- To see what the watchdog would do without acting:
  `$FLEET_HOME/bin/watchdog.sh --dry-run`.

## Park or re-enable a bot

- Park one bot (stop the watchdog from restarting it) by creating its disable
  marker under `$FLEET_HOME/watchdog-state/`.
- Re-enable it by removing that marker.

## Kill switches (turn a hook off, no restart)

State-dir switches live in `~/.claude/channels/fleet-<key>/` and affect only
that bot:

| File | Turns off |
|---|---|
| `stream.off` | the live progress box |
| `ack.off` | the instant reaction ack |
| `tldr_hook_off` | the whole numbered-TLDR check |
| `tldr_judge_off` | only the API judge (free checks stay on) |
| `reply_guard_off` | the reply-delivery guarantee |

One switch is global: `$FLEET_HOME/deliver-nudge.off` turns off the
document-delivery nudge, which runs in every session.

Create one with `touch`; remove the file to restore the hook.

## Test a hook safely

Never start a second Claude session against a live bot to test it; a duplicate
poller kills the real one. Test the streamer offline with `STREAM_DRY=1` and
`STREAM_NO_SPAWN=1` set, which blocks real API calls and the animator spawn. The
hook test harnesses under `tests/` also run fully offline.
