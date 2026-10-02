#!/usr/bin/env bash
# Install Nexera Telebots on macOS. No bot or watchdog is started here.
# Usage: ./install.sh [--yes] [--dry-run] [--help]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
YES=0
DRY_RUN=0

usage() {
  printf 'Usage: ./install.sh [--yes] [--dry-run] [--help]\n'
  printf '  --yes       accept optional voice dependencies when available\n'
  printf '  --dry-run   print the complete install plan without changing files\n'
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1 ;;
    --dry-run|-n) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
run() {
  printf '    +'; printf ' %q' "$@"; printf '\n'
  if [ "$DRY_RUN" -eq 0 ]; then "$@"; fi
}
confirm() {
  local prompt="$1" answer
  if [ "$DRY_RUN" -eq 1 ]; then note "$prompt: optional, no action in dry run"; return 1; fi
  if [ "$YES" -eq 1 ]; then return 0; fi
  printf '    ? %s [y/N] ' "$prompt"
  read -r answer || answer=""
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}
version_ge() {
  local old_ifs="$IFS" current="$1" minimum="$2" a b c x y z
  IFS=. read -r a b c <<< "$current"
  IFS=. read -r x y z <<< "$minimum"
  IFS="$old_ifs"
  a="${a:-0}"; b="${b:-0}"; c="${c:-0}"
  x="${x:-0}"; y="${y:-0}"; z="${z:-0}"
  if [ "$a" -ne "$x" ]; then [ "$a" -gt "$x" ]; return; fi
  if [ "$b" -ne "$y" ]; then [ "$b" -gt "$y" ]; return; fi
  [ "$c" -ge "$z" ]
}

# A platform error takes precedence over all checks and every write.
if [ "$(uname -s)" != Darwin ]; then
  printf 'Nexera Telebots runs on macOS only.\n' >&2
  exit 1
fi
case "$FLEET_HOME" in /*) ;; *) printf 'FLEET_HOME must be an absolute path.\n' >&2; exit 2 ;; esac

say "Preflight"
note "macOS detected. Install root: $FLEET_HOME"
mac_version="$(sw_vers -productVersion 2>/dev/null || printf '0.0')"
note "macOS version: $mac_version (14 or newer required)."
if ! version_ge "$mac_version" 14.0; then
  note "Upgrade macOS before using Telebots."
  if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
fi
if command -v claude >/dev/null 2>&1; then
  claude_version="$(claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
  note "Claude Code: ${claude_version:-version unavailable}"
  if [ -n "$claude_version" ] && ! version_ge "$claude_version" 2.1.224; then
    note "Cross-bot handoff needs Claude Code 2.1.224 or newer."
  fi
else
  note "Claude Code is missing. Install it and sign in before installing Telebots."
  if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
fi
if command -v bun >/dev/null 2>&1; then
  bun_version="$(bun --version 2>/dev/null || printf '0.0')"
  note "Bun: $bun_version (1.3 or newer required)."
  if ! version_ge "$bun_version" 1.3; then
    if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
  fi
else
  note "Bun is missing. Install it before installing Telebots."
  if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
fi
if ! command -v python3 >/dev/null 2>&1; then
  note "Python 3 is missing."
  if [ "$DRY_RUN" -eq 0 ]; then exit 1; fi
fi
overlay="$REPO_DIR/poller/install-overlay.sh"
verify="$REPO_DIR/poller/verify-overlay.sh"
if [ "$DRY_RUN" -eq 0 ]; then
  for required in "$REPO_DIR/engine" "$REPO_DIR/daemon/watchdog.sh" \
    "$REPO_DIR/daemon/watchdog-loop.sh" "$overlay" "$verify" "$REPO_DIR/poller/ux.json"; do
    if [ ! -e "$required" ]; then
      printf 'Required install component is missing: %s\n' "$required" >&2
      exit 1
    fi
  done
fi

say "Install directories and scripts"
run mkdir -p "$FLEET_HOME/bin" "$FLEET_HOME/templates" "$FLEET_HOME/logs" "$FLEET_HOME/watchdog-state" "$FLEET_HOME/restart-state"
run cp -R "$REPO_DIR/engine/." "$FLEET_HOME/bin/"
run cp "$REPO_DIR/daemon/watchdog.sh" "$REPO_DIR/daemon/watchdog-loop.sh" "$FLEET_HOME/bin/"
run cp "$REPO_DIR/daemon/com.claude-fleet.watchdog.plist.template" "$FLEET_HOME/templates/"
for voice_script in transcribe-voice transcribe-voice-mlx transcribe-voice-faster transcribe-voice-daemon; do
  if [ -f "$REPO_DIR/voice/$voice_script" ]; then run cp "$REPO_DIR/voice/$voice_script" "$FLEET_HOME/bin/"; fi
done
if [ -f "$REPO_DIR/extras/macos-window-grid/grid.sh" ]; then
  run cp "$REPO_DIR/extras/macos-window-grid/grid.sh" "$FLEET_HOME/bin/fleet-grid"
fi
note "Set installed script permissions to executable."
if [ "$DRY_RUN" -eq 0 ]; then find "$FLEET_HOME/bin" -type f -exec chmod +x {} +; fi

say "Fleet configuration"
if [ -f "$FLEET_HOME/fleet.json" ]; then
  note "Keep existing fleet.json."
else
  note "Write fleet.json version 2 with an empty bots list. No bot is created."
  if [ "$DRY_RUN" -eq 0 ]; then
    (umask 077; cat > "$FLEET_HOME/fleet.json" <<'JSON'
{
  "version": 2,
  "bots": [],
  "options": {
    "watchdog": true,
    "voice": "auto",
    "voice_daemon": false,
    "tldr_judge": false,
    "grid": false,
    "notes_dir": "~/Telebots Notes",
    "rich_stream": true,
    "guardian_thresholds": "60,70,80,90",
    "email": false
  }
}
JSON
    )
  fi
fi
if [ -f "$FLEET_HOME/permission-stance" ]; then
  note "Keep existing permission stance."
else
  note "Write safe permission stance. Change it later for the entire fleet if needed."
  if [ "$DRY_RUN" -eq 0 ]; then
    (umask 077; printf 'safe\n' > "$FLEET_HOME/permission-stance")
  fi
fi

say "Install official Telegram plugin and Telebots plugin"
stock_slot="$HOME/.claude/plugins/cache/claude-plugins-official/telegram/0.0.7/.claude-plugin/plugin.json"
if [ -f "$stock_slot" ]; then
  note "Official Telegram plugin 0.0.7 cache is present. Keep it."
else
  if ! run claude plugin marketplace add anthropics/claude-plugins-official; then
    note "Official marketplace may already be registered; continuing to the required plugin install."
  fi
  run claude plugin install telegram@claude-plugins-official
fi
if ! run claude plugin marketplace add "$REPO_DIR"; then
  note "Local marketplace may already be registered; continuing to the required plugin install."
fi
run claude plugin install telegram-fleet@nexera-telebots

say "Apply the required overlay to your local official plugin"
note "The overlay accepts only the pinned stock 0.0.7 file or an already installed overlay."
note "It backs up stock server.ts and refuses version drift or tampering."
run bash "$overlay"
if [ "$DRY_RUN" -eq 0 ]; then
  status="$(bash "$verify" --status)" || {
    printf 'Overlay verification failed: %s\n' "$status" >&2
    exit 1
  }
  [ "$status" = OK ] || { printf 'Overlay verification failed: %s\n' "$status" >&2; exit 1; }
else
  note "Verify overlay status is OK after installation."
fi

say "Install menu and reaction table"
run cp "$REPO_DIR/poller/ux.json" "$FLEET_HOME/ux.json"

say "Optional voice dependencies"
if command -v uv >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
  if [ "$(uname -m)" = arm64 ]; then
    note "uv and Homebrew are available. Apple Silicon voice can use mlx-whisper and ffmpeg."
    if confirm "Install mlx-whisper and ffmpeg now?"; then
      run uv tool install mlx-whisper
      run brew install ffmpeg
    fi
  else
    note "uv and Homebrew are available. On an Intel Mac, use the faster-whisper backend."
    note "Install ffmpeg and faster-whisper separately before enabling voice transcription."
  fi
else
  note "Voice extras skipped: install both uv and Homebrew first, then mlx-whisper and ffmpeg."
  note "Text chat works without them. The voice-notes fallback remains available."
fi

if command -v uv >/dev/null 2>&1; then
  uv_tools="$(uv tool dir 2>/dev/null || true)"
  voice_python="$uv_tools/mlx-whisper/bin/python"
  if [ -n "$uv_tools" ] && [ -x "$voice_python" ]; then
    note "Render the optional voice agent template for the installed mlx-whisper interpreter."
    if [ "$DRY_RUN" -eq 0 ]; then
      python3 - "$REPO_DIR/daemon/com.claude-fleet.voice.plist.template" \
        "$FLEET_HOME/templates/com.claude-fleet.voice.plist.template" "$FLEET_HOME" "$voice_python" <<'PYPLIST'
import plistlib
from pathlib import Path
import sys

source, output, fleet_home, interpreter = map(Path, sys.argv[1:])
with source.open("rb") as handle:
    data = plistlib.load(handle)
def render(value):
    if isinstance(value, str):
        return value.replace("__FLEET_HOME__", str(fleet_home)).replace("__UV_TOOL_PYTHON__", str(interpreter))
    if isinstance(value, list):
        return [render(item) for item in value]
    if isinstance(value, dict):
        return {key: render(item) for key, item in value.items()}
    return value
with output.open("wb") as handle:
    plistlib.dump(render(data), handle)
PYPLIST
    fi
  else
    note "Warm voice agent template is pending. Install mlx-whisper with uv, then rerun this installer."
  fi
else
  note "Warm voice agent template is pending. Install uv and mlx-whisper, then rerun this installer."
fi

say "Next steps"
note "1. Create one bot in @BotFather on your phone. Keep its token out of chats."
note "2. Run $FLEET_HOME/bin/fleet-add-bot main and paste the token into its hidden prompt."
note "3. Run $FLEET_HOME/bin/fleet-start main, then $FLEET_HOME/bin/fleet-pair main <code> --lock."
note "4. At the Mac, run $FLEET_HOME/bin/fleet-checklist --consent, then install the watchdog."
note "   In Claude Code, /telegram-fleet:install-daemon loads the watchdog after pairing."
note "5. Run $FLEET_HOME/bin/fleet-checklist main and follow its phone steps."
note "No bot was started and no watchdog was loaded by this installer."
if [ "$DRY_RUN" -eq 1 ]; then note "Dry run: no files or services were changed."; fi
