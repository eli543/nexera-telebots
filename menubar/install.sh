#!/bin/bash
# install.sh - build Telebots Bar and keep it in the menu bar for good.
#
# 1. runs build.sh (compiles, draws the icon, assembles and ad-hoc signs
#    ~/Applications/Telebots Bar.app)
# 2. stops a copy that is already running (a test launch), by exact pid, only if
#    that pid is this user's Telebots Bar executable
# 3. writes ~/Library/LaunchAgents/com.claude-fleet.menubar.plist: RunAtLoad, so it
#    starts at every login, and KeepAlive on a crash (SuccessfulExit false), so
#    it comes back if it dies but Quit in the panel still quits it
# 4. loads the agent into this login session and checks the app is running
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

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

stop_running_copies() {
  local pid cmd
  for pid in $(pgrep -x -u "$UID_NUM" "$APP_NAME" 2>/dev/null || true); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$cmd" in
      "$EXE"*)
        echo "stopping the running copy (pid $pid)"
        kill "$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.5
        done
        ;;
      *)
        echo "leaving pid $pid alone (not $EXE)"
        ;;
    esac
  done
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

if [ "$DRY_RUN" = "1" ]; then
  echo "install.sh --dry-run: nothing is built, stopped, written or loaded."
  echo "1. would run $HERE/build.sh -> $APP"
  echo "2. would unload gui/$UID_NUM/$LABEL if loaded, and stop a running $APP_NAME whose executable is $EXE"
  echo "3. would write $PLIST:"
  render_plist
  echo "4. would run: launchctl bootstrap gui/$UID_NUM $PLIST"
  exit 0
fi

"$HERE/build.sh"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
stop_running_copies

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_HOME"
render_plist > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootstrap "gui/$UID_NUM" "$PLIST"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -x -u "$UID_NUM" "$APP_NAME" >/dev/null 2>&1 && break
  sleep 0.5
done
if pgrep -x -u "$UID_NUM" "$APP_NAME" >/dev/null 2>&1; then
  echo "Telebots Bar is running (pid $(pgrep -x -u "$UID_NUM" "$APP_NAME" | head -1)) and will start at every login."
  echo "agent: $PLIST"
  echo "log:   $LOG_HOME/menubar.log"
else
  echo "install.sh: the agent loaded but Telebots Bar is not running; see $LOG_HOME/menubar.stdout.log" >&2
  exit 1
fi
