# Telebots Bar

A native macOS menu bar monitor for a Telebots fleet. It is quiet when all is well
and says exactly what is wrong when something is not. Swift, AppKit and SwiftUI,
no third-party code, no Dock icon.

In the menu bar:

- one dot per bot, filled when connected and hollow when not, next to a count
  such as `6/6` that turns red when any bot is not connected
- a shield with a slash when auto-recovery (the watchdog) is off or not running
- an antenna with a slash when Telegram is unreachable from this Mac

Click it for a Liquid Glass panel (a material fallback before macOS 26):

- a header with the fleet count and a ring, plus one short red line only when
  auto-recovery is off or Telegram is unreachable
- one row per bot, in fleet order: a status dot, the poller heartbeat age (the
  poller's own liveness ping, not the time of the last message), or `No poller` /
  `Down` in red, and a Reconnect button. A second line shows the model and version,
  effort and context used (`Fable 5.1 · max · 41% ctx`), plus restarts in the last
  hour when there were any and `stall suspected` when the watchdog flagged one.
  Click a row for its uptime and a Fresh start button
- Reconnect all down, only while something is down
- the Watchdog switch (auto-recovery on or off), Snapshot, Restore and Refresh
- a `?` legend that explains every mark and holds the Quit link

It refreshes every 15 seconds and whenever the panel opens; context usage is read
at most once a minute per bot. Every control has a tooltip.

## Try it, then install it

```
./build.sh                                   # builds ~/Applications/Telebots Bar.app (ad-hoc signed)
open ~/Applications/"Telebots Bar.app"       # a one-off run
./install.sh --dry-run                       # shows the LaunchAgent it would write, changes nothing
./install.sh                                 # always on: starts at login, comes back after a crash
./uninstall.sh                               # unloads the agent, removes the plist and the app
```

`build.sh` keeps its intermediates in `menubar/.build.noindex`, which Spotlight
skips, so only the app in `~/Applications` shows up in search. `install.sh` stops a
running copy by its exact pid, writes
`~/Library/LaunchAgents/com.claude-fleet.menubar.plist` (RunAtLoad, and KeepAlive
only after a crash, so Quit still quits), and loads it. Needs Xcode or the Command
Line Tools for `swiftc`.

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
- The model from the newest reply in the session's transcript, named from its id
  (`claude-opus-5-5` reads `Opus 5.5`, `claude-fable-5-1` reads `Fable 5.1`), or the
  launch alias (`Opus`) until the first reply; effort from the session's arguments,
  or the bot's saved `model-state` when it is down; context used from the same
  transcript record, the fleet's own formula; restarts from the watchdog's
  `<key>.restart_log`; the stall flag from `<key>.stall_alerted`, counted only for
  the session running now.
- The watchdog's last pass and network check (`watchdog.last_pass` and
  `net.good_since` in its state folder). A watchdog that does not record them is
  covered by an unauthenticated check of `api.telegram.org` once a minute.

An install whose watchdog or engines live somewhere else can say so in an optional
`menubar` block in `fleet.json` (other tools ignore it):

```json
"menubar": {
  "fresh_engine": "~/fleet/bin/my-fresh.sh",
  "watchdog_state_dir": "~/fleet/watchdog-state",
  "watchdog_log": "~/fleet/watchdog.log",
  "heartbeat_max_age": 300,
  "resume_source": "session-id",
  "fresh_start_arg": "fresh-reset",
  "snapshot_engine": "~/fleet/bin/my-snapshot.sh",
  "restore_engine": "~/fleet/bin/my-restore.sh",
  "snapshot_file": "~/fleet/restart-state/snapshot.tsv",
  "time_zone": "America/New_York"
}
```

| Key | Default |
|---|---|
| `fresh_engine` | `$FLEET_HOME/bin/fleet-fresh`, else `fleet-fresh` on the PATH |
| `watchdog_state_dir` | `$FLEET_HOME/watchdog-state` |
| `watchdog_log` | `$FLEET_HOME/logs/watchdog.log` |
| `heartbeat_max_age` | `180` seconds, the watchdog's limit |
| `resume_source` | `latest-transcript`: the newest transcript in the bot's project dir. `session-id`: the id in `<state_dir>/session.id` when its transcript exists |
| `fresh_start_arg` | empty: the engines' own fresh start (the `/fresh` path) |
| `snapshot_engine` | `$FLEET_HOME/bin/fleet-snapshot` |
| `restore_engine` | `$FLEET_HOME/bin/fleet-restore` |
| `snapshot_file` | `$FLEET_HOME/restart-state/snapshot.tsv` |
| `time_zone` | `America/New_York`, for the action log, uptimes and confirmations |

An option this build does not know, or a value it cannot use, is logged once as a
warning and ignored, so a config newer than the app still shows the fleet.

## What the buttons do

Reconnect runs exactly what the watchdog's restart does, through the same engine
and the same four arguments:

```
/bin/bash <fresh engine> <session pid, or 999999 when the session is down> <workdir> <resume id> quiet
```

- The resume id keeps the conversation. With no saved conversation it passes
  `fresh-keep`: a new, empty session on the same model.
- The engine does the kill, and only the exact pid whose working directory is
  that bot's workdir, so a sibling bot is never touched. The app never kills
  anything itself.
- A connected bot is never restarted without asking: "Restart <Name> even though
  it is connected? It keeps the conversation." with Cancel as the default.
- The engine opens the new bot window in Terminal, so the first real Reconnect
  asks macOS once for permission to control Terminal. The app asks before the
  engine runs, so a declined prompt restarts nothing.
- One attempt, then up to 45 seconds of waiting for the new poller; a spinner in
  the row while it runs and a check mark when the bot is back.

Fresh start (in an expanded row) runs the same engine with the fresh-start
argument, so the bot comes back with a new, empty conversation. It always asks
first.

Reconnect all down runs Reconnect for every bot that is not connected, one at a
time.

Snapshot runs the fleet snapshot engine: it records every bot's current session
so a reboot or macOS update can bring each one back, and it pauses auto-recovery
until Restore, so the red header line and the shield mark stay until then. A
short confirmation such as "Snapshot saved · 6 sessions · 12:41pm ET" follows and
stays as Restore's tooltip.

Restore runs the fleet restore engine: it reopens every bot that is not running in
its saved conversation, skips bots that are running, turns auto-recovery back on
and archives the snapshot. It is disabled while no snapshot is saved.

The Watchdog switch: off creates `<key>.disabled` in the watchdog state folder for
every bot, the same kill switch the snapshot engine uses; on removes them, like
restore. Nothing else is touched, including the crash-loop breaker's own markers.

## Command line

```
Telebots Bar --dump                          # the panel as text, for the fleet right now
Telebots Bar --reconnect <bot> --dry-run     # log the exact engine command, run nothing
Telebots Bar --reconnect <bot>               # reconnect a bot that is not connected
Telebots Bar --fresh-start <bot> --dry-run   # log the fresh-start command (a real one is panel only)
Telebots Bar --snapshot --dry-run            # log the snapshot command and what a real run does
Telebots Bar --restore --dry-run             # the same for restore
Telebots Bar --toggle-watchdog --dry-run     # log the files it would create or remove
Telebots Bar --render-preview <file.png>     # panel in light and dark, plus the menu bar states
Telebots Bar --dry-run                       # run the menu bar app with every action logged only
```

The binary is `~/Applications/Telebots Bar.app/Contents/MacOS/Telebots Bar`.
`TELEBOTS_BAR_DRY_RUN=1` works like `--dry-run`. `--demo` shows a panel with
made-up states, clearly labelled DEMO, for screenshots only.

## Files

Every action the app takes goes to `$FLEET_HOME/menubar.log`, one line each,
stamped in the configured time zone. It reads no bot tokens, sends nothing to
Telegram, and changes nothing unless you press a button or flip the switch.
