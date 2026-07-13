---
description: Snapshot the running Telegram bots before a planned Mac restart or macOS update, so they can be resumed into their exact conversations afterward. Use when the user says they are about to restart, reboot, shut down, or update their Mac and want the bots preserved.
allowed-tools: Bash
---

The user is about to restart, update, or shut down the Mac and wants the bots to
come back into their exact conversations, not fresh. Run the snapshot engine now,
while the bots are still up.

Run `$FLEET_HOME/bin/fleet-snapshot` (`$FLEET_HOME` defaults to
`~/.claude/telegram-fleet`).

What it does:

- Records each bot's current session id (the newest transcript in its project
  directory) plus whether it was running, to
  `$FLEET_HOME/restart-state/snapshot.tsv`. The processes will not survive the
  reboot, but that file will.
- Parks the watchdog by marking each bot disabled, so it cannot start blank
  sessions during boot before the restore runs.

Guidance to give the user:

- To preview without changing anything, run `$FLEET_HOME/bin/fleet-snapshot --dry-run`.
- After the Mac is back and they have logged in, they run
  `/telegram-fleet:restore` (or `$FLEET_HOME/bin/fleet-restore`) to bring
  the bots back resumed.
- Full history resumes. Any action a bot was mid-run at the restart instant
  stops; they send "continue" to that bot.
- Do not run this from inside a bot session that you are about to restart out
  from under. Run it, then reboot.
