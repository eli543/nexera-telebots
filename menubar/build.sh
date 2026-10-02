#!/bin/bash
# build.sh - compile Telebots Bar and stage "Telebots Bar.app", ad-hoc signed.
#
#   build.sh           build and verify into menubar/.build.noindex/staged only. Nothing
#                      installed or running is touched. This is the default.
#   build.sh --swap    swap the staged app into ~/Applications. It refuses while the app
#                      runs, under launchd or started by hand: install.sh stops it first,
#                      swaps, and starts the LaunchAgent again, so use install.sh to update
#                      an installed copy.
#
# (--no-install and --swap-only still work, as older names for the two modes.)
#
# The swap never deletes the installed app first: the staged bundle is copied next to
# it under a temporary name and verified, the old one is moved aside, and the new one
# is renamed into place. If that rename fails, the old one goes straight back. The
# app a swap replaced is kept in menubar/.build.noindex/previous until the next swap
# (a plain build leaves it alone), so a rollback is one move; README.md has the steps.
#
# Env: TELEBOTS_BAR_APP_DIR overrides the destination folder (default ~/Applications).
# Needs: swiftc (Xcode or the Command Line Tools), iconutil, codesign (all stock macOS).

set -euo pipefail

MODE="build"
case "${1:-}" in
  ""|--no-install) MODE="build" ;;
  --swap|--swap-only) MODE="swap" ;;
  *) echo "usage: build.sh [--swap]" >&2; exit 64 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Telebots Bar"
DEST_DIR="${TELEBOTS_BAR_APP_DIR:-$HOME/Applications}"
APP="$DEST_DIR/$APP_NAME.app"
EXE="$APP/Contents/MacOS/$APP_NAME"
LABEL="com.claude-fleet.menubar"
UID_NUM="$(id -u)"
# Everything lives in a hidden .noindex folder so Spotlight never lists a staged copy
# next to the real app in ~/Applications.
BUILD="$HERE/.build.noindex"
WORK="$BUILD/work"
STAGED_DIR="$BUILD/staged"
STAGE="$STAGED_DIR/$APP_NAME.app"
PREVIOUS="$BUILD/previous"
MIN_MACOS="14.0"

# glassEffect is guarded for macOS 26 at runtime, but compiling its symbol
# still requires the macOS 26 SDK or newer. The app itself runs on macOS 14+.
SDK_VERSION="$(xcrun --show-sdk-version 2>/dev/null || true)"
SDK_MAJOR="${SDK_VERSION%%.*}"
if [[ ! "$SDK_MAJOR" =~ ^[0-9]+$ ]] || [ "$SDK_MAJOR" -lt 26 ]; then
  echo "build.sh: Xcode or Command Line Tools with the macOS 26 SDK is required (found ${SDK_VERSION:-none}); the built app runs on macOS 14+." >&2
  exit 1
fi

for tool in swiftc iconutil codesign ditto plutil python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "build.sh: $tool not found (install Xcode or the Command Line Tools)" >&2; exit 1; }
done

build() {
  mkdir -p "$BUILD"
  touch "$BUILD/.metadata_never_index"
  # Only this build's own folders are replaced; previous/ (the rollback copy) stays.
  rm -rf "${WORK:?}" "${STAGED_DIR:?}"
  # The layout before staged/ kept these at the top level; they are rebuilt below.
  rm -rf "${BUILD:?}/$APP_NAME.app" "${BUILD:?}/${APP_NAME:?}" "${BUILD:?}/AppIcon.iconset" "${BUILD:?}/AppIcon.icns"
  mkdir -p "$WORK" "$STAGED_DIR"

  echo "compiling with $(swiftc -version 2>&1 | head -1)"
  swiftc -O -warnings-as-errors -swift-version 5 -target "$(uname -m)-apple-macos$MIN_MACOS" \
    -o "$WORK/$APP_NAME" "$HERE/Fleet.swift" "$HERE/PhoneActions.swift" \
    "$HERE/PhoneServer.swift" "$HERE/PhoneServerTests.swift" "$HERE/PhonePage.swift" "$HERE/main.swift"

  echo "drawing the app icon"
  "$WORK/$APP_NAME" --render-icon "$WORK/AppIcon.iconset"
  iconutil -c icns "$WORK/AppIcon.iconset" -o "$WORK/AppIcon.icns"

  echo "assembling the app bundle"
  mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
  cp "$WORK/$APP_NAME" "$STAGE/Contents/MacOS/$APP_NAME"
  cp "$HERE/Info.plist" "$STAGE/Contents/Info.plist"
  cp "$WORK/AppIcon.icns" "$STAGE/Contents/Resources/AppIcon.icns"
  printf 'APPL????' > "$STAGE/Contents/PkgInfo"
  plutil -lint "$STAGE/Contents/Info.plist" >/dev/null

  echo "signing (ad-hoc)"
  codesign --force --sign - "$STAGE"
  codesign --verify --strict "$STAGE"
  echo "staged and verified: $STAGE"
  echo "nothing installed was touched; install.sh installs it (or build.sh --swap once the app is stopped)"
}

# launchd's own pid for the job, or nothing when it is not loaded or not running
# (launchctl fails for a job that is not loaded, which must not end the script).
launchd_pid() {
  { launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null || true; } | awk '$1 == "pid" && $2 == "=" {print $3; exit}'
}

# Every running copy of the installed app, by exact pid and executable path.
running_copies() {
  local pid cmd
  for pid in $(pgrep -x -u "$UID_NUM" "$APP_NAME" 2>/dev/null || true); do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$cmd" in
      "$EXE"|"$EXE "*|"$STAGE/Contents/MacOS/$APP_NAME"|"$STAGE/Contents/MacOS/$APP_NAME "*) echo "$pid" ;;
    esac
  done
}

swap() {
  [ -d "$STAGE" ] || { echo "build.sh: nothing staged at $STAGE (run build.sh first)" >&2; exit 1; }
  codesign --verify --strict "$STAGE"
  case "$APP" in
    */"$APP_NAME.app") ;;
    *) echo "build.sh: refusing to replace unexpected path $APP" >&2; exit 1 ;;
  esac
  # The shared flock covers copy, validation and both renames. Normal staged
  # copies hold it too. Re-enter only with the descriptor inherited from its holder.
  if [ -z "${TELEBOTS_SWAP_LOCK_FD:-}" ]; then
    exec python3 "$HERE/app-lock.py" /bin/bash "$0" --swap
  fi
  python3 "$HERE/app-lock.py" --verify
  local copies job_state
  job_state="$(python3 "$HERE/app-lock.py" --job-state)"
  if [ "$job_state" = loaded ]; then
    echo "build.sh: the LaunchAgent is loaded; unload it before replacement (even if it has no running pid). Nothing swapped." >&2
    exit 1
  fi
  python3 "$HERE/app-lock.py" --no-copies
  copies="$(running_copies | tr '\n' ' ')"
  if [ -n "$copies" ]; then
    echo "build.sh: Telebots Bar is running (pid ${copies% }), so nothing was swapped." >&2
    echo "build.sh: quit it from its panel (? then Quit Telebots Bar), or run install.sh, which stops it for you." >&2
    exit 1
  fi
  mkdir -p "$DEST_DIR"
  local incoming="$DEST_DIR/.$APP_NAME.app.incoming-$$"
  rm -rf "$incoming"
  ditto "$STAGE" "$incoming"
  if ! codesign --verify --strict "$incoming"; then
    rm -rf "$incoming"
    echo "build.sh: the copied app did not verify; the installed app was not touched" >&2
    exit 1
  fi
  rm -rf "$PREVIOUS"
  mkdir -p "$PREVIOUS"
  local moved_old=0
  if [ -e "$APP" ]; then
    if mv "$APP" "$PREVIOUS/$APP_NAME.app" 2>/dev/null; then
      moved_old=1
    else
      # Another volume: copy it aside first, then remove it.
      ditto "$APP" "$PREVIOUS/$APP_NAME.app" && rm -rf "$APP" && moved_old=1
    fi
  fi
  if ! mv "$incoming" "$APP"; then
    echo "build.sh: could not move the new app into place; putting the old one back" >&2
    if [ "$moved_old" = "1" ]; then
      mv "$PREVIOUS/$APP_NAME.app" "$APP" 2>/dev/null || ditto "$PREVIOUS/$APP_NAME.app" "$APP"
    fi
    rm -rf "$incoming"
    exit 1
  fi
  codesign --verify --strict "$APP"
  echo "installed: $APP"
  if [ "$moved_old" = "1" ]; then
    echo "previous app kept for a rollback: $PREVIOUS/$APP_NAME.app"
  fi
}

case "$MODE" in
  build) build ;;
  swap) swap ;;
esac
