---
description: Install the launchd watchdog that keeps the Telegram bots alive and restarts a dropped one into the same conversation. Shows the exact plist and launchctl commands before running them. Use when the user wants always-on bots or asks to install the watchdog daemon.
allowed-tools: Bash, Read
---

Install the launchd watchdog for the fleet. This is a boot-time system change, so
show the user exactly what you will do and get an explicit yes before running it.

`$FLEET_HOME` defaults to `~/.claude/telegram-fleet`. The agent label is
`com.claude-fleet.watchdog` and the plist goes to
`~/Library/LaunchAgents/com.claude-fleet.watchdog.plist`.

Steps:

1. Confirm the watchdog scripts are installed: `$FLEET_HOME/bin/watchdog.sh` and
   `$FLEET_HOME/bin/watchdog-loop.sh` should exist. If not, run the installer or
   `/telegram-fleet:setup` first.

2. Show the user the plist you will render from
   `daemon/com.claude-fleet.watchdog.plist.template`, with `__FLEET_HOME__`
   replaced by the real path. Print it so they can read it.

3. Show the exact commands you will run, then wait for a yes:

   ```
   mkdir -p ~/Library/LaunchAgents
   sed "s|__FLEET_HOME__|$FLEET_HOME|g" \
     daemon/com.claude-fleet.watchdog.plist.template \
     > ~/Library/LaunchAgents/com.claude-fleet.watchdog.plist
   launchctl bootout gui/$(id -u)/com.claude-fleet.watchdog 2>/dev/null || true
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude-fleet.watchdog.plist
   ```

   The `bootout` first makes it idempotent: it clears any already-loaded copy
   before loading the new one.

4. Verify it loaded: `launchctl print gui/$(id -u)/com.claude-fleet.watchdog`
   should succeed, and `$FLEET_HOME/logs/watchdog.log` should show a heartbeat
   within a minute or two.

Note for the user: the watchdog uses a KeepAlive loop, not a launchd interval
timer, because interval timers can stop firing after the Mac sleeps. It restarts
a dead bot into its existing conversation and identifies processes by exact PID
with an environment match, never by name pattern.
