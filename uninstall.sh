#!/usr/bin/env bash
#
# uninstall.sh - tear down nexera-telebots.
#
# Removes the launchd watchdog, stops running bot sessions by exact PID, removes
# FLEET_HOME (engines, logs, config), and uninstalls this plugin. By default it
# KEEPS your per-bot state directories (tokens, allowlists) and Claude Code's
# transcripts. Pass --purge to remove the state directories too.
#
# Usage:
#   ./uninstall.sh [--purge] [--yes] [--dry-run] [--help]
#     --purge     also remove per-bot state directories (tokens, allowlists)
#     --yes       non-interactive; take the sane default at every prompt
#     --dry-run   print every action without executing anything
#     --help      show this help
#
# Env:
#   FLEET_HOME   install root (default ~/.claude/telegram-fleet)
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
PLIST_LABEL="com.claude-fleet.watchdog"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
VOICE_PLIST_LABEL="com.claude-fleet.voice"
VOICE_PLIST_PATH="$HOME/Library/LaunchAgents/${VOICE_PLIST_LABEL}.plist"

PURGE=0
YES=0
DRY_RUN=0

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

run() {
  printf '    + %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

confirm() {
  local prompt="$1" default="${2:-Y}" reply hint
  if [ "$default" = "Y" ]; then hint="Y/n"; else hint="y/N"; fi
  if [ "$YES" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
    printf '    ? %s [%s] -> %s (non-interactive)\n' "$prompt" "$hint" "$default"
    [ "$default" = "Y" ]
    return
  fi
  printf '    ? %s [%s] ' "$prompt" "$hint"
  read -r reply || reply=""
  reply="${reply:-$default}"
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# ---- capture bot info before we remove anything -----------------------------

KEYS=()
if [ -x "$FLEET_HOME/bin/fleet-config" ]; then
  while IFS= read -r k; do
    [ -n "$k" ] && KEYS+=("$k")
  done < <("$FLEET_HOME/bin/fleet-config" bots 2>/dev/null || true)
fi

# Look up a field for a key via fleet-config (absolute, ~-expanded).
bot_field() {
  "$FLEET_HOME/bin/fleet-config" bot "$1" "$2" 2>/dev/null || true
}

# Stop a bot's claude session by exact PID, verified by working directory.
# Candidates are found by the channel marker, then each PID's cwd is checked
# against the bot's workdir with lsof before any kill. No pattern kill.
stop_bot_session() {
  local workdir="$1" pid cwd candidates
  [ -n "$workdir" ] || return 0
  # Find candidate sessions by the channel marker. pgrep is deliberately avoided
  # (unreliable for the caller's own ancestor on macOS); we verify each PID's cwd
  # before killing an exact PID, which is the safe technique here.
  # shellcheck disable=SC2009
  candidates="$(ps -axo pid=,command= 2>/dev/null | grep -F -- '--channels plugin:telegram@claude-plugins-official' | awk '{print $1}')"
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    if [ "$cwd" = "$workdir" ]; then
      run kill "$pid" || true
    fi
  done <<EOF
$candidates
EOF
}

# ---- watchdog ---------------------------------------------------------------

step "Stop and remove the watchdog daemon"
if [ -f "$PLIST_PATH" ]; then
  run launchctl bootout "gui/$(id -u)/$PLIST_LABEL" || true
  run rm -f "$PLIST_PATH"
else
  note "No watchdog plist found."
fi

step "Stop and remove the optional voice agent"
if [ -f "$VOICE_PLIST_PATH" ]; then
  run launchctl bootout "gui/$(id -u)/$VOICE_PLIST_LABEL" || true
  run rm -f "$VOICE_PLIST_PATH"
else
  note "No voice agent plist found."
fi

# ---- bot sessions -----------------------------------------------------------

step "Stop running bot sessions"
if [ "${#KEYS[@]}" -eq 0 ]; then
  note "No bots found via fleet-config. If bots are running, close their Terminal"
  note "windows, or stop them by exact PID after verifying the working directory."
else
  if confirm "Stop the ${#KEYS[@]} configured bot session(s) now?" Y; then
    for key in "${KEYS[@]}"; do
      note "bot: $key"
      stop_bot_session "$(bot_field "$key" workdir)"
    done
  else
    note "Leaving sessions running. Close their windows when ready."
  fi
fi

# ---- plugin -----------------------------------------------------------------

step "Restore the official Telegram plugin"
if [ -f "$REPO_DIR/poller/restore-stock.sh" ]; then
  run bash "$REPO_DIR/poller/restore-stock.sh"
else
  note "poller/restore-stock.sh is missing. Keep the plugin installed until its stock file is restored."
  if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
fi

step "Uninstall the plugin"
if command -v claude >/dev/null 2>&1; then
  run claude plugin uninstall telegram-fleet@nexera-telebots || true
  if confirm "Also remove the upstream telegram@claude-plugins-official plugin?" N; then
    run claude plugin uninstall telegram@claude-plugins-official || true
  else
    note "Keeping the upstream telegram plugin (other setups may use it)."
  fi
else
  note "No claude CLI found. Uninstall from a session with:"
  note "  /plugin uninstall telegram-fleet@nexera-telebots"
fi

# ---- state directories ------------------------------------------------------

step "Per-bot state directories"
if [ "$PURGE" -eq 1 ]; then
  note "--purge: this removes tokens and allowlists. This cannot be undone."
  if confirm "Remove all per-bot state directories?" N; then
    for key in "${KEYS[@]}"; do
      sd="$(bot_field "$key" state_dir)"
      [ -n "$sd" ] && [ -d "$sd" ] && run rm -rf "$sd"
    done
  else
    note "Skipped state removal."
  fi
else
  note "Keeping per-bot state directories (tokens, allowlists) and transcripts."
  note "Transcripts are Claude Code's own session files under ~/.claude/projects/."
  note "Re-run with --purge to remove the state directories."
fi

# ---- fleet home -------------------------------------------------------------

step "Remove FLEET_HOME"
if [ -d "$FLEET_HOME" ]; then
  run rm -rf "$FLEET_HOME"
else
  note "$FLEET_HOME not present."
fi

# ---- reminders --------------------------------------------------------------

step "Done"
note "To rotate a bot token: message @BotFather, pick the bot, Revoke current token."
note "Removing this project does not revoke any token; do that in BotFather."
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\n(dry run: nothing above was executed)\n'
fi
