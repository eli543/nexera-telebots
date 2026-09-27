# Telebots Bar

A native macOS menu bar monitor for a Telebots fleet. One dot per bot sits in the
menu bar, filled when that bot is connected and hollow when it is not, next to a
count such as `6/6` that turns red the moment any bot drops. Click it for a Liquid
Glass panel (a material fallback before macOS 26):

- one row per bot, in fleet order: a status dot, `heard 12s ago`, `No poller` or
  `Down`, and a Reconnect button; click a row for its session pid, poller pid,
  launch mode, last heartbeat and the last three lines of its launch log
- Reconnect all down, prominent only while something is down
- the watchdog switch, plus Refresh, Open log and Quit

It refreshes every 15 seconds and every time the panel opens. Swift and AppKit
with SwiftUI inside, no third-party code, no Dock icon.

## Try it, then install it

```
./build.sh                                   # builds ~/Applications/Telebots Bar.app (ad-hoc signed)
open ~/Applications/"Telebots Bar.app"       # a one-off run
./install.sh --dry-run                       # shows the LaunchAgent it would write, changes nothing
./install.sh                                 # always on: starts at login, comes back after a crash
./uninstall.sh                               # unloads the agent, removes the plist and the app
```

`install.sh` stops a running copy by its exact pid, writes
`~/Library/LaunchAgents/com.claude-fleet.menubar.plist` (RunAtLoad, and KeepAlive
only after a crash, so Quit in the panel still quits), and loads it. Needs Xcode
or the Command Line Tools for `swiftc`.

## What it reads

The same things the engines read, and nothing hardcoded:

- `fleet.json`, found the way `fleet-config` finds it: `$FLEET_CONFIG`, then
  `$FLEET_HOME/fleet.json`, then `~/.claude/telegram-fleet/fleet.json`. Bot names,
  keys, workdirs and state dirs all come from there.
- The watchdog's health test, done natively: a bot is connected when a Claude
  session process and a Telegram poller process (`server.ts`) both run with that
  bot's `TELEGRAM_STATE_DIR`, and `poller.heartbeat` is no older than the
  watchdog's limit. No heartbeat file means the pid check alone, as in the
  watchdog. Session but no poller, or a stale heartbeat, is `No poller`; no
  session is `Down`.

An install whose watchdog or restart engine lives somewhere else can say so in an
optional `menubar` block in `fleet.json` (other tools ignore it):

```json
"menubar": {
  "fresh_engine": "~/fleet/bin/my-fresh.sh",
  "watchdog_state_dir": "~/fleet/watchdog-state",
  "watchdog_log": "~/fleet/watchdog.log",
  "heartbeat_max_age": 300,
  "resume_source": "session-id",
  "time_zone": "America/New_York"
}
```

| Key | Default |
|---|---|
| `fresh_engine` | `$FLEET_HOME/bin/fleet-fresh`, else `fleet-fresh` on the PATH |
| `watchdog_state_dir` | `$FLEET_HOME/watchdog-state` |
| `watchdog_log` | `$FLEET_HOME/logs/watchdog.log` (what Open log opens) |
| `heartbeat_max_age` | `180` seconds, the watchdog's limit |
| `resume_source` | `latest-transcript`: the newest transcript in the bot's project dir. `session-id`: the id in `<state_dir>/session.id` when its transcript exists |
| `time_zone` | `America/New_York`, for the action log and the heartbeat clock |

## What Reconnect does

Exactly what the watchdog's restart does, through the same engine and the same
four arguments:

```
/bin/bash <fresh engine> <session pid, or 999999 when the session is down> <workdir> <resume id> quiet
```

- The resume id keeps the conversation. With no saved conversation it passes
  `fresh-keep`: a new, empty session on the same model.
- The engine does the kill, and only the exact pid whose working directory is
  that bot's workdir, so a sibling bot is never touched. The app never kills
  anything itself.
- A bot that is connected is never restarted without asking: the panel shows
  "Restart <Name> even though it is connected? It keeps the conversation." with
  Cancel as the default.
- The engine opens the new bot window in Terminal, so the first real Reconnect
  asks macOS once for permission to control Terminal. The app asks before the
  engine runs, so a declined prompt restarts nothing.
- It makes one attempt and then waits up to 45 seconds for the new poller; press
  Reconnect again if a bot does not come back.

Reconnect all down runs the same thing for every bot that is not connected, one
at a time.

## The watchdog switch

Off creates `<key>.disabled` in the watchdog state dir for every bot, the kill
switch the snapshot engine uses; on removes them, like restore. Nothing else is
touched, including the crash-loop breaker's own markers. When only some bots are
switched off, the panel says which.

## Command line

```
Telebots Bar --dump                          # the panel as text, for the fleet right now
Telebots Bar --reconnect <bot> --dry-run     # log the exact engine command, run nothing
Telebots Bar --reconnect <bot>               # reconnect a bot that is not connected
Telebots Bar --toggle-watchdog --dry-run     # log the files it would create or remove
Telebots Bar --render-preview <file.png>     # panel in light and dark, plus the menu bar icon
Telebots Bar --dry-run                       # run the menu bar app with every action logged only
```

The binary is `~/Applications/Telebots Bar.app/Contents/MacOS/Telebots Bar`.
`TELEBOTS_BAR_DRY_RUN=1` works like `--dry-run`. `--demo` shows a panel with
made-up states, clearly labelled DEMO, for screenshots only.

## Files

Every action the app takes goes to `$FLEET_HOME/menubar.log`, one line each,
stamped in the configured time zone. It reads no bot tokens, sends nothing to
Telegram, and writes nothing else unless you flip the watchdog switch or press
Reconnect.

If the icon does not show up, a menu bar organizer may be keeping new items in
its hidden section; drag Telebots Bar into view.
