#!/bin/bash
# uninstall.sh - remove Telebots Bar: unload the LaunchAgent, delete its plist,
# stop a copy that is still running (exact pid, this user's Telebots Bar only)
# and delete ~/Applications/Telebots Bar.app. It keeps the action log
# ($FLEET_HOME/menubar.log) and never touches the bots, the watchdog or fleet.json.

set -euo pipefail

APP_NAME="Telebots Bar"
DEST_DIR="${TELEBOTS_BAR_APP_DIR:-$HOME/Applications}"
APP="$DEST_DIR/$APP_NAME.app"
EXE="$APP/Contents/MacOS/$APP_NAME"
LABEL="com.claude-fleet.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
LOG_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"

if launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null; then
  echo "unloaded $LABEL"
else
  echo "$LABEL was not loaded"
fi

if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo "removed $PLIST"
fi

for pid in $(pgrep -x -u "$UID_NUM" "$APP_NAME" 2>/dev/null || true); do
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  case "$cmd" in
    "$EXE"*)
      echo "stopping the running copy (pid $pid)"
      kill "$pid" 2>/dev/null || true
      ;;
    *)
      echo "leaving pid $pid alone (not $EXE)"
      ;;
  esac
done

case "$APP" in
  */"$APP_NAME.app")
    if [ -d "$APP" ]; then
      rm -rf "$APP"
      echo "removed $APP"
    fi
    ;;
esac

echo "kept the action log: $LOG_HOME/menubar.log (delete it by hand if you want)"
if [ -f "$LOG_HOME/phone-token" ] || [ -L "$LOG_HOME/phone-token" ]; then
  rm -f "$LOG_HOME/phone-token"
  echo "removed the phone link token"
fi
