#!/bin/bash
# install.sh - build Telebots Bar and keep it in the menu bar for good.
#
# 1. builds and verifies the new app in the staging folder (build.sh --no-install);
#    nothing installed is touched yet
# 2. stops the running copy: unloads the LaunchAgent, then stops a copy started by
#    hand by its exact pid, only if that pid is this user's Telebots Bar executable,
#    and waits until it is really gone (if it will not stop, nothing is replaced)
# 3. swaps the new app into ~/Applications (build.sh --swap-only): the old one is
#    moved aside and kept for a rollback, the new one renamed into place
# 4. writes ~/Library/LaunchAgents/com.claude-fleet.menubar.plist: RunAtLoad, so it
#    starts at every login, and KeepAlive on a crash (SuccessfulExit false), so it
#    comes back if it dies but Quit in the panel still quits it. The agent passes
#    --launchd, so the supervised copy takes over from any copy started by hand
# 5. loads the agent, then checks launchd's own pid for the job, that the pid runs
#    this app's executable, and that it holds the app's single-copy lock, before
#    it says the install worked
#
# FLEET_HOME and FLEET_CONFIG, when set in this shell, are passed to the app the
# same way the engines read them. Undo everything with uninstall.sh.
#
# Usage: install.sh [--dry-run]   (--dry-run prints the agent plist and the steps,
#                                  and changes nothing)

set -euo pipefail

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  *) echo "usage: install.sh [--dry-run]" >&2; exit 64 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Telebots Bar"
DEST_DIR="${TELEBOTS_BAR_APP_DIR:-$HOME/Applications}"
APP="$DEST_DIR/$APP_NAME.app"
EXE="$APP/Contents/MacOS/$APP_NAME"
LABEL="com.claude-fleet.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
LOG_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
LOCK="$HOME/Library/Application Support/$APP_NAME/instance.lock"

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# Every running copy of this user's Telebots Bar, by exact pid and executable path.
running_copies() {
  local pid cmd
  for pid in $(pgrep -x -u "$UID_NUM" "$APP_NAME" 2>/dev/null || true); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$cmd" in
      "$EXE"*) echo "$pid" ;;
    esac
  done
}

stop_running_copies() {
  local pid left
  for pid in $(running_copies); do
    echo "stopping the running copy (pid $pid)"
    kill "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    left="$(running_copies)"
    [ -z "$left" ] && return 0
    sleep 0.5
  done
  echo "install.sh: Telebots Bar did not stop (pid $(running_copies | tr '\n' ' ')), so nothing was replaced." >&2
  echo "install.sh: quit it from its panel (? then Quit Telebots Bar) and run install.sh again." >&2
  exit 1
}

ENV_BLOCK=""
for var in FLEET_HOME FLEET_CONFIG; do
  if [ -n "${!var:-}" ]; then
    ENV_BLOCK="$ENV_BLOCK
      <key>$var</key>
      <string>$(xml_escape "${!var}")</string>"
  fi
done
if [ -n "$ENV_BLOCK" ]; then
  ENV_BLOCK="
    <key>EnvironmentVariables</key>
    <dict>$ENV_BLOCK
    </dict>"
fi

render_plist() {
  cat <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
      <string>$(xml_escape "$EXE")</string>
      <string>--launchd</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$LOG_HOME/menubar.stdout.log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$LOG_HOME/menubar.stdout.log")</string>$ENV_BLOCK
  </dict>
</plist>
PLIST_END
}

# launchd's own pid for the job, or nothing.
launchd_pid() {
  launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}'
}

if [ "$DRY_RUN" = "1" ]; then
  echo "install.sh --dry-run: nothing is built, stopped, written or loaded."
  echo "1. would run $HERE/build.sh --no-install (build and verify in $HERE/.build.noindex)"
  echo "2. would unload gui/$UID_NUM/$LABEL if loaded, stop a running $APP_NAME whose executable is $EXE, and wait until it is gone"
  if [ -n "$(running_copies)" ]; then
    echo "   running now: pid $(running_copies | tr '\n' ' ')"
  fi
  echo "3. would run $HERE/build.sh --swap-only (the old app kept in $HERE/.build.noindex/previous)"
  echo "4. would write $PLIST:"
  render_plist
  echo "5. would run: launchctl bootstrap gui/$UID_NUM $PLIST"
  echo "   then check launchd's pid for $LABEL runs $EXE --launchd and holds $LOCK"
  exit 0
fi

"$HERE/build.sh" --no-install

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
stop_running_copies

"$HERE/build.sh" --swap-only

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_HOME"
render_plist > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"

launchctl bootstrap "gui/$UID_NUM" "$PLIST"

pid=""
for _ in $(seq 1 20); do
  pid="$(launchd_pid)"
  [ -n "$pid" ] && break
  sleep 0.5
done
if [ -z "$pid" ]; then
  echo "install.sh: the agent loaded but launchd reports no running pid for $LABEL; see $LOG_HOME/menubar.stdout.log" >&2
  exit 1
fi
cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
case "$cmd" in
  "$EXE --launchd"*) ;;
  *) echo "install.sh: launchd's pid $pid runs '$cmd', not $EXE --launchd" >&2; exit 1 ;;
esac
holder=""
for _ in $(seq 1 20); do
  holder="$(head -1 "$LOCK" 2>/dev/null || true)"
  [ "$holder" = "$pid launchd" ] && break
  sleep 0.5
done
if [ "$holder" != "$pid launchd" ]; then
  echo "install.sh: pid $pid is running but does not hold the single-copy lock ($LOCK says '${holder:-nothing}')" >&2
  exit 1
fi
echo "Telebots Bar is running under launchd (pid $pid, $EXE) and will start at every login."
echo "agent: $PLIST"
echo "log:   $LOG_HOME/menubar.log"
