# Telebots Bar

A native macOS menu bar monitor for a Telebots fleet. It is quiet when all is well
and says exactly what is wrong when something is not. Swift, AppKit and SwiftUI,
no third-party code, no Dock icon.

Build with Xcode or Command Line Tools that include the macOS 26 SDK. The
compiled app runs on macOS 14 or newer; glass effects use a material fallback
before macOS 26.

In the menu bar:

- one dot per bot, filled when connected and hollow when not, next to a count
  such as `6/6` that turns red when any bot is not connected
- a shield with a slash when auto-recovery (the watchdog) is off, paused for a bot
  by the watchdog's own crash-loop breaker, not running, or has no state folder at all
- an antenna with a slash when Telegram is unreachable from this Mac

Click it for a Liquid Glass panel (a material fallback before macOS 26):

- a header with the fleet count and a ring, plus one short red line only when
  auto-recovery is off, paused or unavailable, or Telegram is unreachable
- one row per bot, in fleet order: a status dot, the poller heartbeat age (the
  poller's own liveness ping, not the time of the last message), or `No poller` /
  `Down` in red, and a Reconnect button. A second line shows the model and version,
  effort and context used (`Fable 5.1 · max · 41% ctx`), plus restarts in the last
  hour when there were any, `stall suspected` when the watchdog flagged one, and
  `auto-recovery paused` when the watchdog's crash-loop breaker stopped restarting
  that bot (its tooltip says since when and why).
  When an action on a bot did not work, a red line under it says why and stays
  until the bot is back or you try again. Click a row for its uptime and a Fresh
  start button
- Reconnect all down, only while something is down
- the Watchdog switch (auto-recovery on or off), Snapshot, Restore and Refresh
- a `?` legend that explains every mark and holds the Quit link

It refreshes every 15 seconds and whenever the panel opens, off the main thread, so
the panel never waits on a process scan; context usage is read at most once a
minute per bot. The legend and the rows scroll once they would run past the screen.
Every control has a tooltip, rows expand from VoiceOver too, and the panel's own
open and close animation follows Reduce Motion.

## Try it, then install it

```
./build.sh                                   # builds and verifies .build.noindex/staged/Telebots Bar.app only
open .build.noindex/staged/"Telebots Bar.app"   # a one-off run of the staged copy (quits if one already runs)
./install.sh --dry-run                       # shows the LaunchAgent it would write, changes nothing
./install.sh                                 # installs it: starts at login, comes back after a crash
./uninstall.sh                               # unloads the agent, removes the plist and the app
```

`build.sh` only stages: it compiles, signs and verifies the app in
`menubar/.build.noindex/staged` (Spotlight skips the folder, so only the app in
`~/Applications` shows up in search) and touches nothing installed or running.
`build.sh --swap` puts the staged app into `~/Applications`, and requires the LaunchAgent to be unloaded, even between retries. It holds the
shared instance lock throughout the copy and swap and refuses any running installed
or staged copy, including a demo; `install.sh` is the way to update an
installed copy, since it stops the agent first. The swap never deletes the
installed app first: the new one is copied in under a temporary name and verified,
the old one moves aside to `menubar/.build.noindex/previous` (kept until the next
swap; a plain build leaves it alone), and the new one is renamed into place.
(`--no-install` and `--swap-only` still work as older names for the two modes.)

If bootstrap or verification fails, `install.sh` automatically restores the
previous bundle and plist and restarts the previously loaded service. It exits
nonzero even after successful recovery. A failed recovery reports its remaining
backup location. The matched backup is `menubar/.build.noindex/previous-install/`
(`Telebots Bar.app`, `agent.plist`, and `was-loaded`). A previously absent bundle or
plist is restored to its absent state. A previously unloaded service stays unloaded.

For a manual rollback after a successful install, from `menubar/`:

```sh
launchctl bootout gui/$(id -u)/com.claude-fleet.menubar
python3 app-lock.py --restore-files \
  .build.noindex/previous-install "$HOME/Applications/Telebots Bar.app" \
  "$HOME/Library/LaunchAgents/com.claude-fleet.menubar.plist"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.claude-fleet.menubar.plist"
launchctl print gui/$(id -u)/com.claude-fleet.menubar
```

Use your original `TELEBOTS_BAR_APP_DIR` path if customized. Bootstrap only if
`previous-install/was-loaded` contains `1`. The helper verifies that the job is
unloaded and no installed or staged copy runs, then holds the app's instance lock
through the restore. It never deletes the lock file. Keep the backup until the
restored service has been verified.

`install.sh` builds and verifies first, then unloads the agent and stops a copy
started by hand by its exact pid, and waits until it is gone (if it will not stop,
nothing is replaced). Then it swaps the app in, writes
`~/Library/LaunchAgents/com.claude-fleet.menubar.plist` (RunAtLoad, and KeepAlive
only after a crash, so Quit still quits), loads it, and only reports success once
launchd's own pid for the job runs this app with `--launchd` and holds the app's
single-copy lock. Needs Xcode or the Command Line Tools for `swiftc`.

## Phone

The same fleet panel works on an iPhone through Tailscale. The Mac must be awake,
signed in to Tailscale, and running Telebots Bar. The phone must use the same
Tailscale login as the Mac.

`install.sh` starts the app with `--serve`. A manual launch can use `--serve`,
or `menubar.serve: true` in fleet.json. Without either, serving defaults on only
when `$FLEET_HOME/phone-token` already exists. An explicit flag takes precedence
over the config. The token is made on first serve and is removed by uninstall.

The link looks like `http://<mac-magicdns-name>:8477/#t=<phone-token>`. The app
gets its address and DNS name from the Tailscale CLI at runtime. When MagicDNS
is off it uses the Mac's Tailscale IPv4 address instead. It binds only that
address, never localhost or the Wi-Fi address. When Tailscale is off, the legend
reads "Phone link: waiting for Tailscale" and the app checks again every 30 seconds.

1. Turn on Tailscale on the iPhone and open the full phone link in Safari.
2. Wait for the fleet to appear. Tap Share, then Add to Home Screen, then Add.
3. Open Telebots from the home screen once with Tailscale still on.

The phone saves the token locally. Safari keeps the link fragment until the app
has been added because home-screen apps have their own storage; the standalone
app removes it after saving it. Later launches use that stored token. Turn the
phone's Tailscale toggle off whenever you like. When a request cannot reach the
Mac, the page shows only "Turn on Tailscale on this phone, then pull to refresh".
Turn Tailscale on and pull down, or reopen Telebots. On a cold launch while the
VPN is off, Safari may show its own connection error before the page can load.
The page checks every five seconds while visible and after each action.

In the Mac panel, open the `?` legend and choose Rotate phone link. It replaces
the token and copies the new link to the clipboard. The old link stops working;
open the new one on the phone and replace the old home-screen shortcut. The log
records the rotation and the address with the token redacted.

Security: the server accepts only a Tailscale address whose `whois` identity
matches the Mac's own login, and lookup failures are denied. Reads (the page, its
manifest and icons, and `GET /api/state`) may reuse a check for up to ten minutes;
every action (`POST /api/action`) asks `whois` again, so a device that stops being
the owner's is refused on its very next action. The bearer token and the Mac's
identity are checked again right after that lookup, and an admitted action must
start within five seconds of it while the link is unchanged, or it never runs
(the phone is told it did not run). The static page, manifest and icons need only
that owner check and contain no fleet data or token; every API request also needs
the 32-byte bearer token. Actions use the panel's identity checks, deadlines,
per-bot single-flight guard and ten-second cooldown, and denied requests and
action results are logged in ET without tokens.

The page links directly to `/manifest.webmanifest` and `/apple-touch-icon.png`.
Safari can fetch these and both PNG icons without an Authorization header after
the owner check. The manifest keeps `display: standalone` and leaves `start_url`
unset so installation uses the current page URL, including its setup fragment,
as defined by the [manifest standard](https://www.w3.org/TR/appmanifest/#start_url-member).
The page includes Apple's standalone meta tags, and the page and API use
`Cache-Control: no-store`. This is manual Add to Home Screen over HTTP, with no
TLS, service worker, or offline fleet cache. Without a saved token or setup
fragment, the page says "No link token. Open the link you were sent once so this
phone learns it." and makes no API request.

The phone never opens a Mac permission prompt. If Terminal access has not been
granted, the action stops and the same error appears in the phone and Mac rows.
Grant Telebots Bar access in System Settings > Privacy & Security > Automation
on the Mac before trying again.

API: `GET /api/state` returns the store's rows, numeric indicators, watchdog,
Telegram, snapshot, server time, notes and action receipts. `POST /api/action`
accepts only `reconnect`, `fresh`, `reconnect_all_down`, `watchdog_on`,
`watchdog_off`, `snapshot`, and `restore`; use `bot` for a single bot and
`confirm: true` for Fresh start or a connected bot's Reconnect. An accepted
request returns `action_id` and progresses in `/api/state`; malformed requests
return 400, a missing token 401, an unverified peer 403, a busy action 409 and a
cooldown 429. Snapshot leaves auto-recovery on, just as the native button does.

Run `Telebots Bar --phone-self-test` for offline address, identity, token, HTTP
parser and action-guard tests. The tests never restart a bot or read a live token.

Only one copy runs per user: the app takes an exclusive lock on
`~/Library/Application Support/Telebots Bar/instance.lock`, which the system
releases when the process ends, crash or not. The holder then writes who it is
(`<pid> launchd` or `<pid> manual`, then its start time) and reads it back; if that
write fails it lets go and exits with an error. A second copy started by hand quits
at once. The copy launchd starts (`--launchd`) is the supervised one, so it asks a
copy started by hand to quit and takes over. It trusts the lock file's record only
when it names a live Telebots Bar with that start time: an owner it cannot confirm
(a record not written yet, or one an earlier copy left behind) gets five seconds,
then the launchd copy exits with an error so launchd starts it again, rather than
exiting cleanly and leaving a copy started by hand unsupervised.

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
- The watchdog's outage rule: a stale heartbeat is not a fault while Telegram is
  not confirmed reachable. With a watchdog that records its checks, that means its
  last pass is under 3 minutes old and `net.good_since` shows Telegram reachable
  for at least 10 minutes, and no `plugin.alert-only` latch is set; with one that
  does not, this Mac's own check of `api.telegram.org` decides. Such a bot stays
  connected, and its heartbeat reads "waiting on Telegram".
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
- Both kinds of watchdog marker in its state folder: `<key>.disabled`, the manual
  kill switch the Watchdog switch sets, and `<key>.disabled-auto`, which the
  watchdog's own crash-loop breaker writes after too many restarts in an hour. That
  bot then shows `auto-recovery paused` with when and why (the marker's own lines),
  and the menu bar shows the shield. The watchdog skips the bot and reminds you every
  6 hours until the marker is removed; the app only reads it and never removes it.
- Where each running session's Terminal tab is: its tty and the shell and login
  that host it, recorded in `$FLEET_HOME/menubar-windows.json` by the running app,
  so the window a dead session leaves behind can still be proven to be that bot's.
- A bot's token (`TELEGRAM_BOT_TOKEN` in `<state_dir>/.env`) and its chat (the
  first `allowFrom` entry in `<state_dir>/access.json`), exactly as the engines
  find them, only when an action you pressed sends that bot's chat line. The token
  is never logged or kept.

With no `menubar` block the app uses the fleet's own defaults, the same paths and
limit `daemon/watchdog.sh` uses, so a fresh install needs nothing here. An install
whose watchdog or engines live somewhere else can say so in an optional `menubar`
block in `fleet.json` (other tools ignore it):

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
  "snapshot_keep_watchdog": true,
  "time_zone": "Europe/London"
}
```

| Key | Default |
|---|---|
| `fresh_engine` | `$FLEET_HOME/bin/fleet-fresh`, else `fleet-fresh` on the PATH |
| `fresh_engine_log` | unset; optional engine log used to record its own Back and ready report, without claiming a Telegram receipt |
| `serve` | on when `$FLEET_HOME/phone-token` exists; `--serve` always enables it |
| `watchdog_state_dir` | `$FLEET_HOME/watchdog-state`, as in `daemon/watchdog.sh` |
| `watchdog_log` | `$FLEET_HOME/logs/watchdog.log`, as in `daemon/watchdog.sh` |
| `heartbeat_max_age` | `180` seconds, the limit in `daemon/watchdog.sh`; set it to match your watchdog |
| `resume_source` | `latest-transcript`: the newest transcript in the bot's project dir. `session-id`: the id in `<state_dir>/session.id` when its transcript exists |
| `fresh_start_arg` | empty: the engines' own fresh start (the `/fresh` path). Letters, digits and dashes only |
| `snapshot_engine` | `$FLEET_HOME/bin/fleet-snapshot` |
| `restore_engine` | `$FLEET_HOME/bin/fleet-restore` |
| `snapshot_file` | `$FLEET_HOME/restart-state/snapshot.tsv` |
| `snapshot_keep_watchdog` | `true`: Snapshot runs the engine with `--keep-watchdog`, so auto-recovery stays on. `false` lets the engine switch it off until Restore, as it does for a planned reboot |
| `time_zone` | The Mac's system time zone; override for the action log, uptimes and confirmations |

An option this build does not know, or a value it cannot use, is logged once as a
warning and ignored, so a config newer than the app still shows the fleet.

## What the buttons do

Reconnect works the way `/fresh` does:

1. The bot says in its own chat that the menu bar started it: "Reconnect from the
   menu bar, one moment".
2. The restart engine runs with the same three arguments the `/fresh` hook passes:

   ```
   /bin/bash <fresh engine> <session pid, or 999999 when the session is down> <workdir> <resume id>
   ```

3. The engine brings the bot back, and once its new poller is confirmed it sends
   "⚡ Back and ready" from the bot itself.
4. The old window closes. The engine closes a live session's window by that
   session's tty; for a session that was already down, the app closes the window
   it left behind. Either way a window is closed only when it is provably the
   bot's own: its recorded tty, held by nothing but the shell and login that hosted
   the bot's session, as the window's only tab, with no claude process in it.
   Never by position, never the frontmost window. Terminal cannot close one tab
   of several by script, so a tab that shares its window is left open, and every
   window that is left open gets a line in the log saying why.

Around that:

- The resume id keeps the conversation; only a real session id (a UUID) is ever
  passed. With no saved conversation it passes `fresh-keep`: a new, empty session
  on the same model.
- The engine does the kill, and only the exact pid whose working directory is
  that bot's workdir, so a sibling bot is never touched. The app never kills a bot
  itself.
- A connected bot is never restarted without asking: "Restart <Name> even though
  it is connected? It keeps the conversation." with Cancel as the default.
- The engine opens the new bot window in Terminal, so the first real Reconnect
  asks macOS once for permission to control Terminal. The app asks before
  anything runs, and it goes ahead only on a clear yes: a declined prompt, or an
  answer macOS cannot give (Terminal not running), restarts nothing.
- Right before the engine runs, after any permission prompt, the app reads the
  bot again: if it came back on its own, or its session changed, or the session no
  longer runs in the bot's workdir, nothing runs and the row says why.
- The engine gets 5 minutes; past that the app stops that exact engine process.
  Then up to 45 seconds of waiting for the new poller: a spinner in the row while
  it runs and a check mark when the bot is back.

Fresh start (in an expanded row) runs the same way with the fresh-start argument,
so the bot comes back with a new, empty conversation ("Fresh start from the menu
bar, one moment" first). It always asks first.

Reconnect all down runs Reconnect for every bot that is not connected, one at a
time.

Snapshot runs the fleet snapshot engine with `--keep-watchdog`, from the panel,
the phone and `--snapshot` alike: it records every bot's current session so
Restore can bring each one back later (after a reboot or a macOS update, say), and
auto-recovery stays on. A short confirmation such as "Snapshot saved · 6 sessions ·
12:41pm ET" follows and stays at the start of Restore's tooltip. Set
`menubar.snapshot_keep_watchdog` to `false` to let the engine switch auto-recovery
off until Restore instead, as a snapshot before a planned reboot does.

Restore runs the fleet restore engine: it reopens every bot that is not running,
resumed into its saved conversation, and leaves bots that are running alone. It
switches back on only the watchdog switches a snapshot itself switched off (one
taken with `--keep-watchdog` switches none off), and archives the snapshot once
every bot it reopened is back. Each bot that is down first says "Restore from the
menu bar, one moment"; each one that comes back within a minute says "⚡ Back and
ready" and its old window closes by the same proof as above; one that does not
come back gets a red line in its row. Restore is disabled while no snapshot is saved.

The Watchdog switch: off creates `<key>.disabled` in the watchdog state folder for
every bot, the same kill switch the snapshot engine uses without `--keep-watchdog`;
on removes them. Only regular files count, as in the watchdog's own test, and only
regular files are ever created or removed; anything else at that path is left
alone and reported. Nothing else is touched, including the crash-loop breaker's own
`<key>.disabled-auto` markers, which the switch does not clear. With no state
folder the switch reads unavailable rather than on. A change that fails leaves a
red line under the switch, at most three lines long (the tooltip has all of it),
so the switch, Snapshot and Restore always stay on the panel.

## Command line

```
Telebots Bar --dump                          # the panel as text, for the fleet right now
Telebots Bar --reconnect <bot> --dry-run     # log every step, including the engine command, run nothing
Telebots Bar --reconnect <bot>               # reconnect a bot that is not connected
Telebots Bar --reconnect <bot> --yes         # also a connected one (the panel's confirmation)
Telebots Bar --fresh-start <bot> --dry-run   # log the fresh start, run nothing
Telebots Bar --fresh-start <bot> --yes       # a fresh start (the panel's confirmation)
Telebots Bar --snapshot --dry-run            # log the snapshot command and what a real run does
Telebots Bar --restore --dry-run             # the same for restore, with its chat lines and windows
Telebots Bar --toggle-watchdog --dry-run     # log the files it would create or remove
Telebots Bar --render-preview <file.png>     # panel in light and dark, plus the menu bar states
Telebots Bar --dry-run                       # run the menu bar app with every action logged only
```

The binary is `~/Applications/Telebots Bar.app/Contents/MacOS/Telebots Bar`.
`TELEBOTS_BAR_DRY_RUN=1` works like `--dry-run`. `TELEBOTS_BAR_ENGINE_DEADLINE`
(seconds) shortens the engine deadlines, for tests. `--demo` shows a panel with
made-up states, clearly labelled DEMO, for screenshots only.

## Files

Every action the app takes goes to `$FLEET_HOME/menubar.log`, one line each,
stamped in the configured time zone, including every window it closed or left
open and why. `$FLEET_HOME/menubar-windows.json` holds each bot's last recorded
Terminal tab. The app changes nothing unless you press a button or flip the
switch, and it sends a bot's chat lines only for an action you pressed.
