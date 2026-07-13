---
description: Remove the launchd watchdog daemon for the Telegram fleet, leaving the bots and plugin in place. Shows the exact launchctl commands first. Use when the user wants to stop the always-on watchdog without uninstalling everything.
allowed-tools: Bash, Read
---

Remove the launchd watchdog while leaving the bots, the plugin, and all state in
place. This stops the automatic restart behavior only. Show the commands and get
a yes before running them.

The agent label is `com.claude-fleet.watchdog` and the plist is at
`~/Library/LaunchAgents/com.claude-fleet.watchdog.plist`.

Show the user these commands, then wait for a yes:

```
launchctl bootout gui/$(id -u)/com.claude-fleet.watchdog 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.claude-fleet.watchdog.plist
```

The `bootout` stops and unloads the agent; the `rm` removes the plist so it does
not reload at next login. The `|| true` keeps it from erroring if the agent was
already unloaded.

After running:

- Confirm it is gone: `launchctl print gui/$(id -u)/com.claude-fleet.watchdog`
  should now fail.
- Remind the user that the bots keep running until their sessions end, but a
  dropped bot will no longer restart itself. They can reinstall the watchdog any
  time with `/telegram-fleet:install-daemon`.
