#!/bin/bash
# grid.sh - tile the fleet's bot Terminal windows into a 3x2 grid (optional,
# macOS + Terminal.app only). Only moves/resizes windows; it NEVER touches the bot
# processes (no kill, no restart).
#
#   grid.sh laptop    the locked MacBook 16" 3x2 grid
#   grid.sh monitor   the same grid scaled to fill the active external display
#
# Cells are assigned by bot order in fleet.json:
#   bot 1 -> TL   bot 2 -> TM   bot 3 -> TR
#   bot 4 -> BL   bot 5 -> BM   bot 6 -> BR
# (top row left->right, then bottom row). Bots beyond the first six are ignored.
#
# Installed as $FLEET_HOME/bin/fleet-grid; fleet-launch calls it when the fleet's
# `grid` option is true. Env: FLEET_HOME (default ~/.claude/telegram-fleet).

set -u

case "${1:-}" in -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac

MODE="${1:-laptop}"

FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
resolve_bin() {
  if [ -x "$FLEET_HOME/bin/$1" ]; then printf '%s\n' "$FLEET_HOME/bin/$1"; return; fi
  if [ -x "$SELF_DIR/$1" ]; then printf '%s\n' "$SELF_DIR/$1"; return; fi
  printf '%s\n' "$1"
}
FC_BIN="$(resolve_bin fleet-config)"
fc() { "$FC_BIN" "$@"; }

# Bot workdirs in fleet.json order -> the tiling order.
BOT_WD=()
for key in $(fc bots); do
  BOT_WD+=("$(fc bot "$key" workdir)")
done
if [ "${#BOT_WD[@]}" -eq 0 ]; then
  echo "grid: no bots configured." >&2
  exit 1
fi

CELLS=(TL TM TR BL BM BR)

# --- compute the grid geometry ---------------------------------------------
if [ "$MODE" = "monitor" ]; then
  # Build the grid from the ACTIVE display's VISIBLE frame (excludes menu bar and
  # Dock) and convert Cocoa bottom-left coords to Terminal's top-left window coords.
  read -r fh vx vy vw vh <<EOF
$(/usr/bin/osascript -l JavaScript -e 'ObjC.import("AppKit"); var s=$.NSScreen.mainScreen; var f=s.frame; var v=s.visibleFrame; [Math.round(f.size.height), Math.round(v.origin.x), Math.round(v.origin.y), Math.round(v.size.width), Math.round(v.size.height)].join(" ")' 2>/dev/null)
EOF
  : "${fh:=1620}"; : "${vx:=0}"; : "${vy:=141}"; : "${vw:=3840}"; : "${vh:=1449}"
  top=$(( fh - vy - vh ))
  bottom=$(( fh - vy ))
  colw=$(( vw / 3 ))
  rowh=$(( (bottom - top) / 2 ))
  x0=$vx; x1=$(( vx + colw )); x2=$(( vx + 2 * colw )); x3=$(( vx + vw ))
  y0=$top; y1=$(( top + rowh )); y2=$bottom
else
  # MacBook 16" locked laptop grid (three 576-wide columns).
  x0=0; x1=576; x2=1152; x3=1728
  y0=34; y1=535; y2=1037
fi

bounds_for_cell() {  # $1 = cell name -> "left top right bottom"
  case "$1" in
    TL) echo "$x0 $y0 $x1 $y1" ;;
    TM) echo "$x1 $y0 $x2 $y1" ;;
    TR) echo "$x2 $y0 $x3 $y1" ;;
    BL) echo "$x0 $y1 $x1 $y2" ;;
    BM) echo "$x1 $y1 $x2 $y2" ;;
    BR) echo "$x2 $y1 $x3 $y2" ;;
  esac
}

# workdir -> cell name via its index in the bot list (or empty if not a bot / >6)
cell_for_cwd() {
  local want="$1" i
  i=0
  while [ "$i" -lt "${#BOT_WD[@]}" ] && [ "$i" -lt 6 ]; do
    if [ "${BOT_WD[$i]}" = "$want" ]; then echo "${CELLS[$i]}"; return; fi
    i=$((i+1))
  done
}

# --- apply to each running bot window --------------------------------------
applied=0
# ps -Awwo + grep, deliberately not pgrep (macOS pgrep omits the caller's ancestor).
# shellcheck disable=SC2009
while read -r pid ttyv _rest; do
  case "$ttyv" in "??"|"") continue ;; esac
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | sed 's/^n//')
  cell=$(cell_for_cwd "$cwd")
  [ -z "$cell" ] && continue
  # four integer bounds, intentional split:
  # shellcheck disable=SC2046
  set -- $(bounds_for_cell "$cell")
  /usr/bin/osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
  repeat with w in windows
    try
      if (tty of selected tab of w) is "/dev/$ttyv" then
        set bounds of w to {$1, $2, $3, $4}
      end if
    end try
  end repeat
end tell
OSA
  applied=$((applied+1))
done < <(ps -Awwo pid=,tty=,command= 2>/dev/null | grep -F -- "--channels plugin:telegram@claude-plugins-official" | grep -v grep)

if [ "$applied" -eq 0 ]; then
  echo "No bot windows found (are the channels running?)."
  exit 1
fi
echo "Tiled $applied bot window(s) into the $MODE grid."
