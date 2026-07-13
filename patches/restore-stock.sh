#!/usr/bin/env bash
#
# restore-stock.sh - restore the pristine upstream server.ts from the backup that
# apply-patches.sh created (server.ts.stock-backup), undoing all fleet patches.
#
# Use this before a `claude plugin update`, when reporting an upstream bug, or to
# get back to a clean plugin. The backup is left in place (restore is idempotent).
#
# Usage: ./restore-stock.sh [--help]
#
set -euo pipefail

HOME_DIR="${HOME:?HOME must be set}"
CACHE_ROOT="$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram"

case "${1:-}" in
  -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") : ;;
  *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
esac

mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

[ -d "$CACHE_ROOT" ] || { echo "error: plugin cache not found: $CACHE_ROOT" >&2; exit 4; }
SERVER=""; NEWEST=0
for d in "$CACHE_ROOT"/*/ ; do
  [ -f "${d}server.ts" ] || continue
  m="$(mtime_of "${d}server.ts")"
  if [ "$m" -ge "$NEWEST" ]; then NEWEST="$m"; SERVER="${d}server.ts"; fi
done
[ -n "$SERVER" ] || { echo "error: no server.ts found under $CACHE_ROOT/*/" >&2; exit 4; }

BACKUP="$SERVER.stock-backup"
if [ ! -e "$BACKUP" ]; then
  echo "error: no backup found at $BACKUP" >&2
  echo "Nothing to restore. If the plugin was updated, the cache dir may have changed;" >&2
  echo "reinstall the stock plugin: claude plugin install telegram@claude-plugins-official" >&2
  exit 5
fi

cp "$BACKUP" "$SERVER"
echo "Restored pristine stock server.ts from backup:"
echo "  $BACKUP -> $SERVER"
echo "Backup left in place. Restart your fleet bots (or /fresh) to load stock behavior."
