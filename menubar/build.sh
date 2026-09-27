#!/bin/bash
# build.sh - compile Telebots Bar and assemble "Telebots Bar.app", ad-hoc signed.
#
#   build.sh               build, verify, then swap the new app into ~/Applications
#   build.sh --no-install  build and verify in the staging folder only (install.sh
#                          uses this, stops the running copy, then asks for --swap-only)
#   build.sh --swap-only   swap the already staged app into place
#
# The swap never deletes the installed app first: the new bundle is copied next to
# it under a temporary name and verified, the old one is moved aside, and the new one
# is renamed into place. If that rename fails, the old one goes straight back. The
# previous app is kept in menubar/.build.noindex/previous until the next build, so a
# rollback is one move. Quit a running copy first (install.sh does that for you).
#
# Env: TELEBOTS_BAR_APP_DIR overrides the destination folder (default ~/Applications).
# Needs: swiftc (Xcode or the Command Line Tools), iconutil, codesign (all stock macOS).

set -euo pipefail

MODE="all"
case "${1:-}" in
  "") ;;
  --no-install) MODE="build" ;;
  --swap-only) MODE="swap" ;;
  *) echo "usage: build.sh [--no-install | --swap-only]" >&2; exit 64 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Telebots Bar"
DEST_DIR="${TELEBOTS_BAR_APP_DIR:-$HOME/Applications}"
APP="$DEST_DIR/$APP_NAME.app"
# Intermediates live in a hidden .noindex folder so Spotlight never lists the staging
# copy next to the real app in ~/Applications.
BUILD="$HERE/.build.noindex"
STAGE="$BUILD/$APP_NAME.app"
PREVIOUS="$BUILD/previous"
MIN_MACOS="14.0"

for tool in swiftc iconutil codesign ditto plutil; do
  command -v "$tool" >/dev/null 2>&1 || { echo "build.sh: $tool not found (install Xcode or the Command Line Tools)" >&2; exit 1; }
done

build() {
  rm -rf "$BUILD"
  mkdir -p "$BUILD"
  touch "$BUILD/.metadata_never_index"

  echo "compiling with $(swiftc -version 2>&1 | head -1)"
  swiftc -O -warnings-as-errors -swift-version 5 -target "$(uname -m)-apple-macos$MIN_MACOS" \
    -o "$BUILD/$APP_NAME" "$HERE/Fleet.swift" "$HERE/PhoneActions.swift" \
    "$HERE/PhoneServer.swift" "$HERE/PhoneServerTests.swift" "$HERE/PhonePage.swift" "$HERE/main.swift"

  echo "drawing the app icon"
  "$BUILD/$APP_NAME" --render-icon "$BUILD/AppIcon.iconset"
  iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

  echo "assembling the app bundle"
  mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
  cp "$BUILD/$APP_NAME" "$STAGE/Contents/MacOS/$APP_NAME"
  cp "$HERE/Info.plist" "$STAGE/Contents/Info.plist"
  cp "$BUILD/AppIcon.icns" "$STAGE/Contents/Resources/AppIcon.icns"
  printf 'APPL????' > "$STAGE/Contents/PkgInfo"
  plutil -lint "$STAGE/Contents/Info.plist" >/dev/null

  echo "signing (ad-hoc)"
  codesign --force --sign - "$STAGE"
  codesign --verify --strict "$STAGE"
  echo "staged and verified: $STAGE"
}

swap() {
  [ -d "$STAGE" ] || { echo "build.sh: nothing staged at $STAGE (run build.sh --no-install first)" >&2; exit 1; }
  codesign --verify --strict "$STAGE"
  case "$APP" in
    */"$APP_NAME.app") ;;
    *) echo "build.sh: refusing to replace unexpected path $APP" >&2; exit 1 ;;
  esac
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
  all) build; swap ;;
esac
