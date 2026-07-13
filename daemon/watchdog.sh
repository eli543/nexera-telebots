#!/bin/bash
# watchdog.sh - one health pass over every bot in fleet.json. Driven every 60s by
# watchdog-loop.sh (under launchd KeepAlive). For each bot it checks: is the
# Claude session alive AND is its Telegram poller (server.ts) alive AND is the
# poller heartbeat fresh? A session whose poller died reads messages but can never
# reply - the exact failure this recovers from.
#
# Safety (preserves the live guard rails):
#   - processes are matched by their exact TELEGRAM_STATE_DIR in the env, so one
#     bot's PIDs can never be confused with another's. ps -axo, never pgrep/pkill.
#   - grace: a bot must look unhealthy for >= GRACE seconds before any action.
#   - cooldown: never restart the same bot more than once per COOLDOWN.
#   - crash-loop breaker: after MAX_RESTARTS_PER_HOUR, disable that bot + alert.
#   - per-bot kill switch: $FLEET_HOME/watchdog-state/<key>.disabled.
#   - single-instance lockfile with a 10-min stale breaker.
#   - restart is delegated to fleet-fresh (PID+cwd guarded kill, never a sibling).
#
# Usage: watchdog.sh [--dry-run]   (--dry-run reports only; never restarts/alerts)
# macOS (ps eww / stat -f / lsof via fleet-fresh). Config driven; no bot list here.

set -u

case "${1:-}" in -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
resolve_bin() {
  if [ -x "$FLEET_HOME/bin/$1" ]; then printf '%s\n' "$FLEET_HOME/bin/$1"; return; fi
  if [ -x "$SELF_DIR/$1" ]; then printf '%s\n' "$SELF_DIR/$1"; return; fi
  if [ -x "$SELF_DIR/../engine/$1" ]; then printf '%s\n' "$SELF_DIR/../engine/$1"; return; fi
  printf '%s\n' "$1"
}
FC_BIN="$(resolve_bin fleet-config)"
FRESH="$(resolve_bin fleet-fresh)"
fc() { "$FC_BIN" "$@"; }

# ---- tunables (identical to the live watchdog) -----------------------------
GRACE=45
COOLDOWN=180
MAX_RESTARTS_PER_HOUR=4
HEARTBEAT_MAX_AGE=180
STALL_MIN=600

STATE="$FLEET_HOME/watchdog-state"
LOG_DIR="$FLEET_HOME/logs"
LOG="$LOG_DIR/watchdog.log"
mkdir -p "$STATE" "$LOG_DIR"

# Alert channel: the alert_bot option key (fleet.json options.alert_bot), else
# the first bot. Alerts about a specific bot are sent FROM that bot's own token
# to its own chat when possible.
ALERT_KEY="$(fc option alert_bot 2>/dev/null || true)"
[ -z "$ALERT_KEY" ] && ALERT_KEY="$(fc bots 2>/dev/null | head -1)"
ALERT_STATE="$(fc bot "$ALERT_KEY" state_dir 2>/dev/null || true)"

# Fleet-specific lock so parallel fleets (or a test run) never collide.
LOCK="/tmp/claude-telegram-fleet-watchdog-$(printf '%s' "$FLEET_HOME" | cksum | awk '{print $1}').lock"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true; }
say() { log "$*"; [ $DRY_RUN -eq 1 ] && printf '%s\n' "$*"; }

token_of_state() {
  [ -f "$1/.env" ] || return 0
  grep -m1 '^TELEGRAM_BOT_TOKEN=' "$1/.env" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]'
}
chat_id_of_state() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1] + "/access.json"))
    a = d.get("allowFrom") or []
    print(str(a[0]) if a else "")
except Exception:
    print("")
PY
}
state_of_pid() {
  ps eww -p "$1" 2>/dev/null | tr ' ' '\n' | grep -m1 '^TELEGRAM_STATE_DIR=' | cut -d= -f2-
}
# first pid whose command matches $1 and whose state dir env matches $2
first_pid_matching() {
  local sig="$1" want="$2" pid
  # ps -axo + grep, deliberately not pgrep (macOS pgrep omits the caller's ancestor).
  # shellcheck disable=SC2009
  for pid in $(ps -axo pid=,command= | grep -F -- "$sig" | grep -v grep | awk '{print $1}'); do
    [ -z "$pid" ] && continue
    [ "$(state_of_pid "$pid")" = "$want" ] && { echo "$pid"; return; }
  done
}

# alert <text> [token] [chat] - default to the alert bot; NEVER sends in dry-run.
alert() {
  [ $DRY_RUN -eq 1 ] && { log "[DRY-RUN] alert suppressed: $1"; return 0; }
  local tok="${2:-}" cid="${3:-}"
  [ -z "$tok" ] && tok="$(token_of_state "$ALERT_STATE")"
  [ -z "$cid" ] && cid="$(chat_id_of_state "$ALERT_STATE")"
  { [ -z "$tok" ] || [ -z "$cid" ]; } && { log "alert skipped (no alert token/chat)"; return 0; }
  curl -s --max-time 10 "https://api.telegram.org/bot${tok}/sendMessage" \
    --data-urlencode "chat_id=${cid}" --data-urlencode "text=$1" >/dev/null 2>&1
}

# Is Telegram reachable with this bot's token? Read-only (getWebhookInfo does not
# consume the update queue). A stale heartbeat with an unreachable API = network
# outage, not a dead poller - don't restart.
telegram_reachable() {
  local tok resp
  tok=$(token_of_state "$1"); [ -z "$tok" ] && return 0
  resp=$(curl -s --max-time 10 "https://api.telegram.org/bot${tok}/getWebhookInfo" 2>/dev/null)
  case "$resp" in *'"ok"'*) return 0 ;; *) return 1 ;; esac
}

restart_count_1h() {
  local f="$STATE/$1.restart_log" cutoff n=0 ts
  cutoff=$(( now - 3600 ))
  [ -f "$f" ] || { echo 0; return; }
  while read -r ts; do [ -n "$ts" ] && [ "$ts" -ge "$cutoff" ] 2>/dev/null && n=$((n+1)); done < "$f"
  echo "$n"
}

RESTART_RESULT=""
restart_channel() {
  local key="$1" sdir proj cwd sid latest b sage spid up=""
  sdir=$(fc bot "$key" state_dir)
  proj=$(fc project-dir "$key")
  cwd=$(fc bot "$key" workdir)

  # Resume the dead session's conversation only if its transcript was touched in
  # the last 30 min (else it comes back fresh - avoids resuming a stale chat).
  sid=""
  if [ -d "$proj" ]; then
    # transcript files are named by uuid (.jsonl); -t needs ls.
    # shellcheck disable=SC2012
    latest=$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1)
    if [ -n "$latest" ]; then
      sage=$(( now - $(stat -f %m "$latest" 2>/dev/null || echo 0) ))
      [ "$sage" -lt 1800 ] && { b=$(basename "$latest"); sid="${b%.jsonl}"; }
    fi
  fi

  spid=$(first_pid_matching "--channels plugin:telegram@claude-plugins-official" "$sdir")
  [ -z "$spid" ] && spid=999999
  log "RESTART $key via fleet-fresh (pid=$spid resume=${sid:-none})"
  /bin/bash "$FRESH" "$spid" "$cwd" "$sid" quiet >/dev/null 2>&1

  for _ in $(seq 1 40); do
    sleep 1
    [ -n "$(first_pid_matching "server.ts" "$sdir")" ] && { up=1; break; }
  done
  if [ -n "$up" ]; then
    RESTART_RESULT=$([ -n "$sid" ] && echo "resumed" || echo "fresh")
  else
    log "  recovery did not come up; FRESH fallback"
    spid=$(first_pid_matching "--channels plugin:telegram@claude-plugins-official" "$sdir")
    [ -z "$spid" ] && spid=999999
    /bin/bash "$FRESH" "$spid" "$cwd" "" quiet >/dev/null 2>&1
    for _ in $(seq 1 40); do sleep 1; [ -n "$(first_pid_matching "server.ts" "$sdir")" ] && { up=1; break; }; done
    RESTART_RESULT=$([ -n "$up" ] && echo "fresh-fallback" || echo "failed")
  fi
  log "  result: $RESTART_RESULT"
}

# Mode (b): a bot got a message but has written nothing since, for a long time.
# ALERT-ONLY, never restarts (also matches a session legitimately inside one long
# tool call). The ping itself is OFF by default; detection still logs.
check_stall() {
  local key="$1" sdir="$2" li li_ts proj latest tm flag
  li="$sdir/last-inbound"
  [ -f "$li" ] || return 0
  li_ts=$(head -1 "$li" 2>/dev/null | tr -cd '0-9'); [ -z "$li_ts" ] && return 0
  proj=$(fc project-dir "$key"); tm=0
  # shellcheck disable=SC2012
  latest=$(ls -t "$proj"/*.jsonl 2>/dev/null | head -1)
  [ -n "$latest" ] && tm=$(stat -f %m "$latest" 2>/dev/null || echo 0)
  flag="$STATE/$key.stall_alerted"
  if [ "$li_ts" -gt "$tm" ] && [ $(( now - li_ts )) -ge "$STALL_MIN" ]; then
    if [ ! -f "$flag" ] || [ "$(cat "$flag" 2>/dev/null)" != "$li_ts" ]; then
      log "$key: STALL suspected - inbound $(( now - li_ts ))s ago, no transcript activity since. ALERT-ONLY, no restart. (ping off by default)"
      [ $DRY_RUN -eq 0 ] && printf '%s\n' "$li_ts" > "$flag"
    fi
  else
    [ $DRY_RUN -eq 0 ] && rm -f "$flag" 2>/dev/null
  fi
  return 0
}

# ---- lock (with 10-min stale breaker) --------------------------------------
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -d "$LOCK" ]; then
    lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
    if [ "$lock_age" -gt 600 ]; then
      rmdir "$LOCK" 2>/dev/null && mkdir "$LOCK" 2>/dev/null || exit 0
    else
      exit 0
    fi
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# ---- log rotation (keep last 800 lines past ~1MB) --------------------------
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 800 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

now=$(date +%s)
healthy_n=0; acted_n=0; total=0
for key in $(fc bots); do
  total=$((total+1))
  sdir=$(fc bot "$key" state_dir)
  us="$STATE/$key.unhealthy_since"
  lr="$STATE/$key.last_restart"
  rl="$STATE/$key.restart_log"
  disabled="$STATE/$key.disabled"

  if [ -f "$disabled" ]; then
    say "$key: DISABLED (manual override), skipping"
    continue
  fi

  spid=$(first_pid_matching "--channels plugin:telegram@claude-plugins-official" "$sdir")
  wpid=$(first_pid_matching "server.ts" "$sdir")

  # Mode (a): both PIDs exist but the heartbeat says polling stopped. No heartbeat
  # file = poller predates the liveness patch: fall back to PID-only (rollout-safe).
  hb_stale=0; hb_reason=""
  if [ -n "$spid" ] && [ -n "$wpid" ] && [ -f "$sdir/poller.heartbeat" ]; then
    hbts=$(head -1 "$sdir/poller.heartbeat" 2>/dev/null | tr -cd '0-9')
    if [ -n "$hbts" ] && [ $(( now - hbts )) -gt "$HEARTBEAT_MAX_AGE" ]; then
      if [ $DRY_RUN -eq 1 ]; then
        hb_stale=1; hb_reason="poller alive (pid=$wpid) but heartbeat stale $(( now - hbts ))s"
      elif telegram_reachable "$sdir"; then
        hb_stale=1; hb_reason="poller alive (pid=$wpid) but heartbeat stale $(( now - hbts ))s (>${HEARTBEAT_MAX_AGE}s), API reachable"
      else
        log "$key: heartbeat stale $(( now - hbts ))s but Telegram API unreachable - assuming network outage, no action"
      fi
    fi
  fi

  if [ -n "$spid" ] && [ -n "$wpid" ] && [ $hb_stale -eq 0 ]; then
    healthy_n=$((healthy_n+1))
    [ -f "$us" ] && { [ $DRY_RUN -eq 0 ] && rm -f "$us"; log "$key: recovered, healthy again"; }
    say "$key: healthy (session=$spid poller=$wpid)"
    [ $DRY_RUN -eq 0 ] && check_stall "$key" "$sdir"
    continue
  fi

  # unhealthy
  reason="session=${spid:-DOWN} poller=${wpid:-DOWN}"
  [ $hb_stale -eq 1 ] && reason="$hb_reason"

  if [ $DRY_RUN -eq 1 ]; then
    say "$key: UNHEALTHY ($reason) - [DRY-RUN] would restart, no action taken"
    continue
  fi

  [ -f "$us" ] || printf '%s\n' "$now" > "$us"
  since=$(cat "$us"); age=$(( now - since ))
  last=0; [ -f "$lr" ] && last=$(cat "$lr"); cdage=$(( now - last ))
  botok=$(token_of_state "$sdir"); botcid=$(chat_id_of_state "$sdir")

  if [ "$age" -lt "$GRACE" ]; then
    log "$key: unhealthy ${age}s ($reason) - within grace ${GRACE}s, waiting"
    continue
  fi
  if [ "$cdage" -lt "$COOLDOWN" ]; then
    log "$key: unhealthy ($reason) but restarted ${cdage}s ago - cooldown ${COOLDOWN}s, waiting"
    continue
  fi

  rc=$(restart_count_1h "$key")
  if [ "$rc" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    touch "$disabled"
    log "$key: crash-looping (${rc} restarts/hr) - DISABLED, alerting"
    alert "I keep dropping my Telegram connection (${rc}+ times this hour), so the watchdog stopped auto-restarting me - I need a manual look. (Remove ${disabled} to re-enable auto-recovery.)" "$botok" "$botcid"
    continue
  fi

  restart_channel "$key"
  printf '%s\n' "$now" >> "$rl"
  printf '%s\n' "$now" > "$lr"
  rm -f "$us"
  acted_n=$((acted_n+1))
  # Reconnect chatter is intentionally silent (a blip that auto-recovers needs no
  # ping). Only a genuine "could not recover" alert is sent.
  case "$RESTART_RESULT" in
    resumed|fresh|fresh-fallback) : ;;
    failed)
      alert "My Telegram link dropped and the watchdog could not bring me back automatically. I need a manual hand: /mcp reconnect (keeps my conversation) or /fresh." "$botok" "$botcid" ;;
  esac
done

say "heartbeat: ${healthy_n}/${total} healthy, ${acted_n} recovered this pass"
