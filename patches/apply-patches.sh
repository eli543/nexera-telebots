#!/usr/bin/env bash
#
# apply-patches.sh - apply the telegram-fleet server.ts patch set (01..09) onto the
# installed upstream Telegram plugin, in order, with version + hash verification and
# a pristine backup. Idempotent-safe: it refuses to run against anything that is not
# the exact known-stock server.ts unless you pass --force.
#
# What it does:
#   1. Finds the newest installed plugin cache: ~/.claude/plugins/cache/
#      claude-plugins-official/telegram/*/server.ts
#   2. Verifies the plugin version is 0.0.6 (the version these patches target).
#   3. Verifies server.ts sha256 == the known stock hash (skipped with --force).
#   4. Applies 01..09 to a temp copy, each with `patch -p1 --dry-run` then for real.
#   5. Backs up the pristine stock to server.ts.stock-backup (once), installs result.
#
# These patches modify a LOCAL copy of Anthropic's Apache-2.0 licensed plugin. See
# README.md in this directory for the per-patch change statements (Apache-2.0 4(b)).
#
# WARNING: a plugin update replaces the cache directory and silently discards these
# patches. Re-run this script after any `claude plugin update`.
#
# Usage: ./apply-patches.sh [--force] [--help]
#   --force   skip the version and stock-hash gates (try anyway; dry-run still guards)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${HOME:?HOME must be set}"
CACHE_ROOT="$HOME_DIR/.claude/plugins/cache/claude-plugins-official/telegram"

# These patches were generated against this exact upstream file.
EXPECT_VERSION="0.0.6"
STOCK_SHA="20087f27c81be642cbb0f0254626092330052a5c31b5abde8dec2569889a11e4"
PATCHED_SHA="1f677f91bb2e528d7942731b7e9151d9a2119f063a3172b969d0bb9f4c39f0e4"
PATCHES="01-state-dir-guard 02-attachments 03-liveness 04-fresh-midtask 05-model-midtask 06-esc-midtask 07-model-shortcuts 08-context-usage 09-command-menu"

FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else err "no shasum or sha256sum available"; exit 3; fi
}

mtime_of() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# --- 1. locate the newest installed server.ts -------------------------------------
[ -d "$CACHE_ROOT" ] || { err "plugin cache not found: $CACHE_ROOT"; err "install it first: claude plugin install telegram@claude-plugins-official"; exit 4; }
SERVER=""; NEWEST=0
for d in "$CACHE_ROOT"/*/ ; do
  [ -f "${d}server.ts" ] || continue
  m="$(mtime_of "${d}server.ts")"
  if [ "$m" -ge "$NEWEST" ]; then NEWEST="$m"; SERVER="${d}server.ts"; fi
done
[ -n "$SERVER" ] || { err "no server.ts found under $CACHE_ROOT/*/"; exit 4; }
CACHE_DIR="$(dirname "$SERVER")"
say "Found plugin server.ts: $SERVER"

# --- 2. verify plugin version -----------------------------------------------------
PLUGIN_JSON="$CACHE_DIR/.claude-plugin/plugin.json"
VER="unknown"
if [ -f "$PLUGIN_JSON" ]; then
  VER="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN_JSON" | head -1)"
fi
say "Plugin version: ${VER}"
if [ "$VER" != "$EXPECT_VERSION" ]; then
  if [ "$FORCE" -eq 1 ]; then
    say "  --force: proceeding despite version != $EXPECT_VERSION"
  else
    err "expected plugin version $EXPECT_VERSION, found '$VER'."
    err "These patches were generated for $EXPECT_VERSION. Re-run with --force to try anyway,"
    err "or regenerate the patches against the new upstream. Aborting."
    exit 5
  fi
fi

# --- 3. verify stock hash ---------------------------------------------------------
CUR_SHA="$(sha256_of "$SERVER")"
if [ "$CUR_SHA" = "$PATCHED_SHA" ]; then
  say "server.ts already matches the fully-patched hash - nothing to do."
  exit 0
fi
if [ "$CUR_SHA" != "$STOCK_SHA" ]; then
  if [ "$FORCE" -eq 1 ]; then
    say "  --force: proceeding despite sha256 mismatch (got $CUR_SHA)"
  else
    err "server.ts sha256 does not match the known stock hash."
    err "  expected: $STOCK_SHA"
    err "  got:      $CUR_SHA"
    err "It may already be patched, a different version, or locally modified."
    err "Run ./verify-patches.sh to inspect, ./restore-stock.sh to reset, or --force to override."
    exit 6
  fi
fi

# --- 4. apply to a temp copy (dry-run then real) ----------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/telegram-fleet-apply.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cp "$SERVER" "$WORK/server.ts"
say ""
say "Applying patches to a working copy:"
for p in $PATCHES; do
  pf="$SCRIPT_DIR/$p.patch"
  [ -f "$pf" ] || { err "missing patch file: $pf"; exit 7; }
  if ! patch -p1 --dry-run -d "$WORK" < "$pf" >/dev/null 2>&1; then
    err "$p.patch FAILED dry-run - aborting, original untouched."
    patch -p1 --dry-run -d "$WORK" < "$pf" || true
    exit 8
  fi
  patch -p1 -d "$WORK" < "$pf" >/dev/null 2>&1
  say "  applied  $p.patch"
done

# --- 4b. sanity: patched hash ------------------------------------------------------
NEW_SHA="$(sha256_of "$WORK/server.ts")"
if [ "$NEW_SHA" = "$PATCHED_SHA" ]; then
  say "Patched result matches expected hash ($PATCHED_SHA)."
else
  say "Note: patched hash $NEW_SHA differs from the reference $PATCHED_SHA"
  say "      (expected only if --force was used against a non-stock base)."
fi

# --- 5. back up pristine stock, then install --------------------------------------
BACKUP="$SERVER.stock-backup"
if [ -e "$BACKUP" ]; then
  say "Backup already exists, keeping it: $BACKUP"
else
  cp "$SERVER" "$BACKUP"
  say "Backed up stock server.ts -> $BACKUP"
fi
cp "$WORK/server.ts" "$SERVER"
say ""
say "Done. All 9 patches applied to $SERVER"
say "Restart your fleet bots (or run /fresh) for the changes to take effect."
say "The Telegram '/' command menu re-registers when each poller restarts; for"
say "instant effect see 'Command menu registration' in patches/README.md."
