---
description: Full teardown of nexera-telebots. Removes the watchdog, stops bot sessions, uninstalls the plugin, and removes FLEET_HOME, keeping tokens and transcripts unless the user asks to purge. Use when the user wants to remove or uninstall the Telegram fleet.
allowed-tools: Bash, Read
---

Walk the user through removing nexera-telebots. Confirm each destructive
step. The repo ships `uninstall.sh`, which does this end to end; you can run it or
guide the user through the equivalent commands. `$FLEET_HOME` defaults to
`~/.claude/telegram-fleet`.

Tell the user what will happen, then run the steps they approve.

## What gets removed

1. The watchdog daemon:

   ```
   launchctl bootout gui/$(id -u)/com.claude-fleet.watchdog 2>/dev/null || true
   rm -f ~/Library/LaunchAgents/com.claude-fleet.watchdog.plist
   ```

2. Running bot sessions, stopped by exact PID after verifying each process's
   working directory. Never pattern-kill. If unsure, have the user close the
   bots' Terminal windows instead.

3. The plugin: `claude plugin uninstall telegram-fleet@nexera-telebots`.
   Ask whether to also remove `telegram@claude-plugins-official`; keep it if
   other setups use it.

4. `$FLEET_HOME` itself (engines, logs, config).

## What is kept by default

- Per-bot state directories under `~/.claude/channels/fleet-<key>/`, which hold
  the tokens and allowlists.
- Claude Code's own transcripts under `~/.claude/projects/`.

To remove the state directories too, run the shipped script with the purge flag:
`./uninstall.sh --purge`. Confirm first; it removes tokens and cannot be undone.

## Preferred path

Offer to run the shipped script, which does all of the above with confirmations:

```
./uninstall.sh            # keeps tokens and transcripts
./uninstall.sh --purge    # also removes state directories
./uninstall.sh --dry-run  # print every action without executing
```

## After removal

Removing this project does not revoke any Telegram token. Remind the user to
rotate a token in @BotFather (Revoke current token) if it was ever exposed.
