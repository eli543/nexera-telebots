#!/usr/bin/env bash
#
# verify-patches.sh - report which fleet patches are currently present in the
# installed server.ts, by grepping for each patch's unique marker. Read-only.
#
# Prints a per-patch present/absent table plus the current sha256 and how it
# compares to the known stock and fully-patched reference hashes. Exit 0 always.
#
# Usage: ./verify-patches.sh [--help]
#
set -euo pipefail

HOME_DIR="${HOME:?HOME must be set}"
CACHE_ROOT="$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram"
STOCK_SHA="20087f27c81be642cbb0f0254626092330052a5c31b5abde8dec2569889a11e4"
PATCHED_SHA="9d641451b927a635052f59ece69c86d07a0b34776e3cf2240509945289281440"

case "${1:-}" in
  -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") : ;;
  *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
esac

mtime_of()  { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }
sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else echo "unknown"; fi
}

[ -d "$CACHE_ROOT" ] || { echo "error: plugin cache not found: $CACHE_ROOT" >&2; exit 4; }
SERVER=""; NEWEST=0
for d in "$CACHE_ROOT"/*/ ; do
  [ -f "${d}server.ts" ] || continue
  m="$(mtime_of "${d}server.ts")"
  if [ "$m" -ge "$NEWEST" ]; then NEWEST="$m"; SERVER="${d}server.ts"; fi
done
[ -n "$SERVER" ] || { echo "error: no server.ts found under $CACHE_ROOT/*/" >&2; exit 4; }

echo "server.ts: $SERVER"
CUR_SHA="$(sha256_of "$SERVER")"
echo "sha256:    $CUR_SHA"
if   [ "$CUR_SHA" = "$STOCK_SHA" ];   then echo "           == pristine stock (no patches applied)"
elif [ "$CUR_SHA" = "$PATCHED_SHA" ]; then echo "           == fully patched (all 5, genericized)"
else echo "           (custom / partially patched / different version)"; fi
echo ""

check() { # name  marker
  if grep -Fq -e "$2" "$SERVER"; then printf "  [x] %s\n" "$1"; else printf "  [ ] %s\n" "$1"; fi
}
echo "Patch presence:"
check "01 state-dir-guard   (hijack guard)"       "TELEGRAM_STATE_DIR not set - not a configured bot"
check "02 attachments       (voice notes marker)" "[telegram-fleet] Voice notes and file attachments"
check "03 liveness          (watchdog heartbeat)" "--- watchdog liveness signals"
check "04 fresh-midtask     (/fresh + fleet.json)" "async function handleFresh("
check "05 model-midtask     (/model)"             "async function handleModel("
echo ""
echo "[x] present  [ ] absent. Run ./apply-patches.sh to (re)apply, ./restore-stock.sh to reset."
