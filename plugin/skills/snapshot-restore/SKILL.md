---
name: snapshot-restore
description: Preserve and restore the Telegram fleet across a planned Mac restart or macOS update, so the bots come back resumed into their exact prior conversations instead of fresh. Use WHENEVER the user says they are about to restart, reboot, shut down, or update the Mac and want the bots kept, or afterward when they want the bots brought back in the state they were in.
---

Keep the bots' conversations across a planned reboot. A restart kills the
processes but never touches the on-disk transcripts, so the job is: record each
bot's session now, relaunch resumed after login. Two phases.

`$FLEET_HOME` defaults to `~/.claude/telegram-fleet`.

## Phase 1: snapshot (before the restart, bots up)

Run `$FLEET_HOME/bin/fleet-snapshot`. It records each running bot's current
session id to `$FLEET_HOME/restart-state/snapshot.tsv` and parks the watchdog so
it cannot spawn blank sessions during boot.

- Preview with `$FLEET_HOME/bin/fleet-snapshot --dry-run`.
- Run this, then let the user reboot.
- Do not run it from inside a bot you are about to restart out from under.

## Phase 2: restore (after login, bots down)

Run `$FLEET_HOME/bin/fleet-restore`. It reads the snapshot, relaunches each bot
resumed into its exact conversation, skips any already up, leaves manually
disabled bots down, re-enables the watchdog, and archives the snapshot.

- Preview with `$FLEET_HOME/bin/fleet-restore --dry-run`.
- Run it from a plain terminal, not from inside one of the bots it relaunches.
- If there is no snapshot file, there is nothing to restore; start the bots
  normally with `$FLEET_HOME/bin/fleet-launch <key>`.

## What to tell the user

- Full history resumes. An action a bot was mid-run at the reboot instant stops;
  send "continue" to that bot.
- This is the opposite of `/fresh`, which deliberately starts a bot blank.
- Why not rely on the watchdog: the watchdog only resumes a bot whose transcript
  was touched recently, and its launchd agent runs at login, where it could race
  a manual restore. The snapshot path closes both gaps for a planned reboot.
