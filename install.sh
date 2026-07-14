#!/usr/bin/env bash
#
# install.sh - bootstrap claude-telegram-fleet from a bare shell.
#
# Installs the daemon layer (watchdog + snapshot/restore engines) into FLEET_HOME
# and, with your consent, the official plugin, this repo's plugin, the launchd
# watchdog, the optional server.ts patches, and the voice dependencies.
#
# Idempotent: safe to run again. Nothing here inlines a token or a chat id.
#
# Usage:
#   ./install.sh [--yes] [--dry-run] [--help]
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
MIN_CLAUDE="2.1.80"

YES=0
DRY_RUN=0

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---- output helpers ---------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# Print an action, then run it unless --dry-run.
run() {
  printf '    + %s\n' "$*"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$@"
  fi
}

# Write a single-line value to a file, respecting --dry-run.
write_value() {
  local path="$1" value="$2"
  printf '    + write %s <- %s\n' "$path" "$value"
  if [ "$DRY_RUN" -eq 0 ]; then
    printf '%s\n' "$value" > "$path"
  fi
}

# Ask a yes/no question. Non-interactive (--yes or --dry-run) takes the default.
# confirm "Question?" Y|N
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

# Compare dotted numeric versions: version_ge A B is true when A >= B.
version_ge() {
  local a="$1" b="$2" i x y
  local av bv
  IFS=. read -r -a av <<< "$a"
  IFS=. read -r -a bv <<< "$b"
  for i in 0 1 2; do
    x="${av[$i]:-0}"; y="${bv[$i]:-0}"
    x="${x//[!0-9]/}"; y="${y//[!0-9]/}"
    x="${x:-0}"; y="${y:-0}"
    if [ "$x" -gt "$y" ]; then return 0; fi
    if [ "$x" -lt "$y" ]; then return 1; fi
  done
  return 0
}

# ---- preflight (read-only; always runs) -------------------------------------

step "Preflight checks"

IS_MAC=0
if [ "$(uname -s)" = "Darwin" ]; then
  IS_MAC=1
  note "macOS detected."
else
  note "Not macOS. The daemon layer (launchd watchdog, snapshot/restore) is"
  note "macOS-only and will be skipped. The plugin and hooks may still work."
fi

IS_ARM=0
if [ "$(uname -m)" = "arm64" ]; then
  IS_ARM=1
  note "Apple Silicon detected (local voice via mlx-whisper is available)."
else
  note "Not Apple Silicon. Voice will use faster-whisper if you enable it."
fi

if command -v claude >/dev/null 2>&1; then
  CLAUDE_VER="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  CLAUDE_VER="${CLAUDE_VER:-0.0.0}"
  if version_ge "$CLAUDE_VER" "$MIN_CLAUDE"; then
    note "Claude Code $CLAUDE_VER (>= $MIN_CLAUDE required for channels). OK."
  else
    note "Claude Code $CLAUDE_VER is older than $MIN_CLAUDE. Channels need $MIN_CLAUDE or newer."
    note "Upgrade Claude Code before using the channel."
  fi
else
  note "Claude Code CLI not found on PATH. Install it and authenticate first:"
  note "  https://code.claude.com/docs"
  if [ "$DRY_RUN" -eq 0 ]; then
    exit 1
  fi
fi

if command -v bun >/dev/null 2>&1; then
  note "Bun $(bun --version 2>/dev/null || echo '?') present. OK."
else
  note "Bun not found. The official plugin's MCP server requires it."
  if confirm "Install Bun now (curl -fsSL https://bun.sh/install | bash)?" Y; then
    run bash -c 'curl -fsSL https://bun.sh/install | bash'
  else
    note "Skipping Bun. Install it before starting a bot: https://bun.sh"
  fi
fi

# ---- FLEET_HOME layout ------------------------------------------------------

step "Create FLEET_HOME at $FLEET_HOME"
run mkdir -p "$FLEET_HOME/bin" "$FLEET_HOME/logs" "$FLEET_HOME/watchdog-state" "$FLEET_HOME/restart-state"

step "Install engine, daemon, and voice scripts into $FLEET_HOME/bin"
if [ -d "$REPO_DIR/engine" ]; then
  run cp -R "$REPO_DIR/engine/." "$FLEET_HOME/bin/"
else
  note "engine/ not found in the repo; skipping (expected in a partial checkout)."
fi
if [ -f "$REPO_DIR/daemon/watchdog.sh" ]; then
  run cp "$REPO_DIR/daemon/watchdog.sh" "$REPO_DIR/daemon/watchdog-loop.sh" "$FLEET_HOME/bin/"
else
  note "daemon/ scripts not found; skipping the watchdog binaries."
fi
if [ -f "$REPO_DIR/voice/transcribe-voice-mlx" ]; then
  run cp "$REPO_DIR/voice/transcribe-voice-mlx" "$REPO_DIR/voice/transcribe-voice-faster" "$FLEET_HOME/bin/"
fi
# Optional window-grid extra: installed under the name fleet-launch calls.
if [ -f "$REPO_DIR/extras/macos-window-grid/grid.sh" ]; then
  run cp "$REPO_DIR/extras/macos-window-grid/grid.sh" "$FLEET_HOME/bin/fleet-grid"
fi
run find "$FLEET_HOME/bin" -type f -exec chmod +x {} +

# ---- config -----------------------------------------------------------------

step "Fleet config"
if [ -f "$FLEET_HOME/fleet.json" ]; then
  note "fleet.json already exists; keeping it."
else
  run cp "$REPO_DIR/config/fleet.example.json" "$FLEET_HOME/fleet.json"
  note "Wrote a single-bot fleet.json. Edit it, or run /telegram-fleet:setup"
  note "inside a Claude Code session for the guided version."
fi

# ---- permission stance ------------------------------------------------------

step "Permission stance"
STANCE="safe"
if [ -f "$FLEET_HOME/permission-stance" ]; then
  STANCE="$(cat "$FLEET_HOME/permission-stance" 2>/dev/null || echo safe)"
  note "Existing stance: $STANCE (leaving as is; edit $FLEET_HOME/permission-stance to change)."
else
  note "safe    normal permission prompts (recommended default)"
  note "scoped  pre-allow a common read/edit set"
  note "full    --dangerously-skip-permissions (near-shell access; see SECURITY.md)"
  if [ "$YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    printf '    ? stance [safe/scoped/full] (safe) '
    read -r reply || reply=""
    case "$reply" in scoped) STANCE="scoped" ;; full) STANCE="full" ;; *) STANCE="safe" ;; esac
  fi
  write_value "$FLEET_HOME/permission-stance" "$STANCE"
fi
if [ "$STANCE" = "full" ]; then
  note "WARNING: 'full' lets any allowlisted user run commands with no prompt."
  note "Run the bots under a dedicated macOS user or a scoped directory."
fi

# ---- plugins ----------------------------------------------------------------

step "Install the official plugin and this plugin"
if command -v claude >/dev/null 2>&1; then
  if confirm "Add the official marketplace and install telegram@claude-plugins-official?" Y; then
    run claude plugin marketplace add anthropics/claude-plugins-official || true
    run claude plugin install telegram@claude-plugins-official || true
  fi
  if confirm "Add this repo as a marketplace and install telegram-fleet?" Y; then
    run claude plugin marketplace add "$REPO_DIR" || true
    run claude plugin install telegram-fleet@claude-telegram-fleet || true
  fi
  note "After installing, run /reload-plugins inside a session if it is open."
else
  note "Skipping plugin install (no claude CLI). Install the plugin from a session:"
  note "  /plugin marketplace add eli543/claude-telegram-fleet"
  note "  /plugin install telegram-fleet@claude-telegram-fleet"
fi

# ---- daemon -----------------------------------------------------------------

step "Watchdog daemon (launchd)"
if [ "$IS_MAC" -eq 1 ] && [ -f "$REPO_DIR/daemon/com.claude-fleet.watchdog.plist.template" ]; then
  note "This installs a launchd agent that keeps your bots alive."
  note "Label: $PLIST_LABEL"
  note "Plist: $PLIST_PATH"
  note "It will run: launchctl bootout gui/$(id -u)/$PLIST_LABEL (if loaded), then bootstrap."
  if confirm "Install and load the watchdog now?" Y; then
    run mkdir -p "$HOME/Library/LaunchAgents"
    if [ "$DRY_RUN" -eq 0 ]; then
      sed "s|__FLEET_HOME__|$FLEET_HOME|g" \
        "$REPO_DIR/daemon/com.claude-fleet.watchdog.plist.template" > "$PLIST_PATH"
    fi
    printf '    + render %s (substitute __FLEET_HOME__)\n' "$PLIST_PATH"
    run launchctl bootout "gui/$(id -u)/$PLIST_LABEL" || true
    run launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    note "Loaded. Check $FLEET_HOME/logs/watchdog.log for a heartbeat."
  else
    note "Skipped. Install later with /telegram-fleet:install-daemon."
  fi
else
  note "Skipping the daemon (not macOS, or the plist template is not in this checkout)."
fi

# ---- optional patches -------------------------------------------------------

step "Optional server.ts patches (mid-task /fresh + /model, /esc, /opus /fable, /context /usage, command menu)"
if [ -f "$REPO_DIR/patches/apply-patches.sh" ]; then
  note "These modify your local copy of the official plugin and are wiped by a"
  note "plugin update. Idle-path /fresh /resume /model work without them; /esc,"
  note "/opus, /fable, /context, /usage, and the '/' menu exist only with them."
  if confirm "Apply the optional patches now?" N; then
    run bash "$REPO_DIR/patches/apply-patches.sh"
  else
    note "Skipped. Apply later with: bash patches/apply-patches.sh"
  fi
else
  note "No patches/apply-patches.sh in this checkout; skipping."
fi

# ---- voice ------------------------------------------------------------------

step "Voice transcription dependencies"
if [ "$IS_ARM" -eq 1 ]; then
  if confirm "Install mlx-whisper and ffmpeg for local voice notes?" Y; then
    if command -v uv >/dev/null 2>&1; then
      run uv tool install mlx-whisper || true
    else
      note "uv not found; install it first: https://docs.astral.sh/uv/"
    fi
    if command -v brew >/dev/null 2>&1; then
      run brew install ffmpeg || true
    else
      note "Homebrew not found; install ffmpeg by hand for audio decoding."
    fi
  fi
else
  if confirm "Install faster-whisper for voice notes on this hardware?" N; then
    run bash -c 'pip install faster-whisper' || true
  fi
fi

# ---- doctor -----------------------------------------------------------------

step "Health check"
if [ -x "$FLEET_HOME/bin/fleet-doctor" ]; then
  run "$FLEET_HOME/bin/fleet-doctor" || true
else
  note "fleet-doctor not installed yet; run /telegram-fleet:doctor later."
fi

# ---- next steps -------------------------------------------------------------

step "Next steps"
note "1. Create a bot with @BotFather (/newbot) and copy its token."
note "2. Write the token into each bot's .env, or run /telegram:configure <token>."
note "3. DM your bot, then run /telegram:access pair <code> in your terminal."
note "4. Lock down: /telegram:access policy allowlist."
note "5. Start a bot: $FLEET_HOME/bin/fleet-launch main"
note ""
note "Read SECURITY.md before you widen the permission stance."
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\n(dry run: nothing above was executed)\n'
fi
