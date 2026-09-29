# Architecture

Two layers, cleanly separated.

- Layer A, the plugin: hooks, skills, and slash commands. Installed through the
  Claude Code plugin system, versioned and removable. It never edits your
  `settings.json`.
- Layer B, the daemon: a launchd watchdog and the snapshot/restore engines.
  Installed by the installer, because a plugin's runtime is session-scoped and
  cannot own a boot-time daemon or remove one on uninstall.

## Data flow

Inbound, a Telegram message becoming a Claude turn:

```
your phone (Telegram DM to your bot)
   |
   v
Telegram Bot API (cloud), long-poll getUpdates
   |
   v
server.ts  =  the poller / Bun MCP worker  (one per bot)
   |  1. access gate: read access.json, drop / pair / deliver
   |  2. write liveness: state_dir/poller.heartbeat and state_dir/last-inbound
   |  3. intercept /fresh, /model, /opus, /fable, /esc, /context, /usage, and
   |     permission replies before the model sees them (the command intercepts
   |     are the optional patches; /esc, the shortcuts, and the reports exist
   |     only there), and register the "/" command menu on every poller start
   |  4. download attachments to state_dir/inbox/, describe them in the message meta
   |  5. emit an MCP notification to the session
   v
claude --channels plugin:telegram@claude-plugins-official  (the session)
   |  wraps the notification as a user turn, then the hooks fire in order:
   |     telegram-ack.py      instant emoji reaction
   |     telegram-fresh.sh    idle-path /fresh
   |     telegram-resume.sh   idle-path /resume, cross-bot picker
   |     telegram-model.sh    idle-path /model
   |     tg-stream.py         opens the live "Working" box
   |     tg-reply-guard.py    arms the delivery guarantee for this turn
   v
the model works. PreToolUse and PostToolUse stream progress (tg-stream.py),
tldr-nudge.py checks the reply, tg-reply-guard.py records that a reply went out.
```

Outbound, a Claude reply reaching Telegram:

```
model calls the reply tool  ->  server.ts  ->  Telegram Bot API  ->  your phone
```

## Process tree per bot

```
Terminal.app window (one per bot)
  claude ... --channels plugin:telegram@claude-plugins-official     the session
    bun run ... start                                               the MCP wrapper
      bun server.ts                                                 the poller
```

A bot is healthy when the session PID and the poller PID are both alive and the
poller heartbeat is fresh (under 180 seconds old). Telegram allows exactly one
`getUpdates` consumer per token, so there must be exactly one poller per bot. Two
pollers on one token produce a 409 Conflict and a fight over the token, which is
the single most common failure and is covered in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Supervision loop

```
launchd (KeepAlive)  ->  watchdog-loop.sh  ->  watchdog.sh (one pass)  every ~60s
   each pass, for each bot: check session PID, poller PID, and heartbeat freshness
     unhealthy past the grace window, not in cooldown, under the hourly breaker
       ->  fleet-fresh <pid> <workdir> [resume_uuid] quiet
             ->  kill the stale session (PID, start time and workdir guarded;
                 skipped when the bot is already back)
                   ->  open a new Terminal window running fleet-launch
                         ->  read model-state, exec claude --channels ... resumed
```

Snapshot and Restore share a fleet transaction lock and a saved generation ID.
An interrupted snapshot rolls back only switches whose inode still belongs to it.
Restore archives only its processed generation after current health checks pass.
The public engines reject duplicate bot keys and shared workdirs or state directories.
Restart and Restore require fresh heartbeats. An omitted restart argument preserves the selected model;
recognized idle-hook and mid-task `/fresh` requests still reset it. Explicit
`fresh-reset` always resets, and launch dry-runs preserve model-state.
Custom FLEET_HOME and FLEET_CONFIG paths are resolved once and carried into
Terminal launches and child hooks, including paths containing spaces.

The loop uses a KeepAlive wrapper rather than a launchd `StartInterval` timer on
purpose: interval timers can stop firing after the machine sleeps, and a keepalive
loop does not depend on that timer.

## FLEET_HOME

The installer creates `~/.claude/telegram-fleet/` (override with `FLEET_HOME`):

```
~/.claude/telegram-fleet/
  fleet.json            the one config file
  bin/                  all engine and daemon scripts (uninstall = remove this dir)
    fleet-config        reads fleet.json for every other script
    fleet-launch        launch a bot in its workdir with token and state dir
    fleet-fresh         kill and relaunch a bot, guarded by PID and workdir
    fleet-model         switch model and effort, resume into the same session
    fleet-esc           press one verified Escape in a bot's window (/esc)
    fleet-context       report a session's live context usage (/context)
    fleet-usage         report fleet-wide token burn (/usage)
    fleet-sessions      enumerate sessions across all bots for the resume picker
    fleet-snapshot      record running sessions before a reboot
    fleet-restore       resume the exact conversations after login
    fleet-doctor        version and liveness checks
    watchdog.sh         one health pass
    watchdog-loop.sh    the keepalive loop
  logs/                 watchdog.log and friends
  permission-stance     one of: safe, scoped, full
```

## State directories

One per bot, at `~/.claude/channels/fleet-<key>/`. The `fleet-` prefix keeps
these clear of the stock plugin's default `telegram` directory, so a fleet
install coexists with an existing stock setup and never hijacks it. Contents:

```
.env               TELEGRAM_BOT_TOKEN, chmod 600
access.json        dmPolicy and allowFrom (the numeric user IDs)
model-state        MODEL and EFFORT, sticky across restarts
poller.heartbeat   updated every poll; the watchdog reads its age
last-inbound       updated on each inbound message
inbox/             downloaded attachments (grows over time)
stream/            the live-streamer's state and locks
```

## Kill switches

Every hook can be turned off with a file, no restart needed. State-dir switches
sit inside a bot's `~/.claude/channels/fleet-<key>/` directory and affect only
that bot:

| File | Turns off |
|---|---|
| `stream.off` | the live progress box |
| `ack.off` | the instant reaction ack |
| `tldr_hook_off` | the whole numbered-TLDR check |
| `tldr_judge_off` | only the API judge, keeping the free checks |
| `reply_guard_off` | the reply-delivery guarantee |

One switch is global, because its hook runs in every session, not just bots:

| File | Turns off |
|---|---|
| `~/.claude/telegram-fleet/deliver-nudge.off` | the document-delivery nudge |

To create one: `touch ~/.claude/channels/fleet-main/stream.off`. To restore the
hook: remove the file.

## Testing without a live bot

Never start a second Claude session against a live bot to test it; a duplicate
poller will kill the real one. Test the streamer offline with `STREAM_DRY=1` and
`STREAM_NO_SPAWN=1` set, which suppresses real API calls and the animator spawn.
The hook test harnesses under `tests/` run fully offline.
