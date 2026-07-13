---
description: Restore the Telegram bots after a Mac restart or macOS update, resuming each into its exact prior conversation. Use after login when the user wants the bots brought back in the state they were in before the reboot.
allowed-tools: Bash
---

The Mac has restarted and the user wants the bots back in their exact prior
conversations. Run the restore engine now, after login, with the bots down.

Run `$FLEET_HOME/bin/fleet-restore` (`$FLEET_HOME` defaults to
`~/.claude/telegram-fleet`).

What it does:

- Reads `$FLEET_HOME/restart-state/snapshot.tsv` (written by
  `/telegram-fleet:snapshot` before the reboot).
- Relaunches each snapshotted bot resumed into its exact conversation.
- Skips any bot that is already up, and leaves a manually disabled bot down.
- Re-enables the watchdog and archives the snapshot file.

Guidance to give the user:

- To preview without changing anything, run `$FLEET_HOME/bin/fleet-restore --dry-run`.
- If there is no snapshot file, there is nothing to restore. That happens if
  `/telegram-fleet:snapshot` was not run before the reboot; in that case
  start the bots normally with `$FLEET_HOME/bin/fleet-launch <key>`.
- Do not run this from inside one of the bots it will relaunch. Run it from a
  plain terminal session.
- Full history resumes. Any action a bot was mid-run at the reboot stops; send
  "continue" to that bot.
