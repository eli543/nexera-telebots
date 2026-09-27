#!/bin/bash
# build.sh - compile Telebots Bar and assemble "Telebots Bar.app" in ~/Applications,
# ad-hoc signed. Safe to re-run: it replaces that one app bundle and nothing else.
# Quit a running copy first (install.sh does that for you).
#
# Env: TELEBOTS_BAR_APP_DIR overrides the destination folder (default ~/Applications).
# Needs: swiftc (Xcode or the Command Line Tools), iconutil, codesign (all stock macOS).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Telebots Bar"
DEST_DIR="${TELEBOTS_BAR_APP_DIR:-$HOME/Applications}"
APP="$DEST_DIR/$APP_NAME.app"
# Intermediates live in a hidden .noindex folder so Spotlight never lists the staging
# copy next to the real app in ~/Applications.
BUILD="$HERE/.build.noindex"
MIN_MACOS="14.0"

for tool in swiftc iconutil codesign ditto plutil; do
  command -v "$tool" >/dev/null 2>&1 || { echo "build.sh: $tool not found (install Xcode or the Command Line Tools)" >&2; exit 1; }
done

rm -rf "$BUILD"
mkdir -p "$BUILD"
touch "$BUILD/.metadata_never_index"

echo "compiling with $(swiftc -version 2>&1 | head -1)"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos$MIN_MACOS" \
  -o "$BUILD/$APP_NAME" "$HERE/Fleet.swift" "$HERE/main.swift"

echo "drawing the app icon"
"$BUILD/$APP_NAME" --render-icon "$BUILD/AppIcon.iconset"
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$BUILD/AppIcon.icns"

echo "assembling the app bundle"
STAGE="$BUILD/$APP_NAME.app"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources"
cp "$BUILD/$APP_NAME" "$STAGE/Contents/MacOS/$APP_NAME"
cp "$HERE/Info.plist" "$STAGE/Contents/Info.plist"
cp "$BUILD/AppIcon.icns" "$STAGE/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$STAGE/Contents/PkgInfo"
plutil -lint "$STAGE/Contents/Info.plist" >/dev/null

echo "signing (ad-hoc)"
codesign --force --sign - "$STAGE"
codesign --verify --strict "$STAGE"

mkdir -p "$DEST_DIR"
case "$APP" in
  */"$APP_NAME.app") rm -rf "$APP" ;;
  *) echo "build.sh: refusing to replace unexpected path $APP" >&2; exit 1 ;;
esac
ditto "$STAGE" "$APP"
codesign --verify --strict "$APP"
echo "built: $APP"
