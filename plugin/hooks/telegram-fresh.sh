#!/bin/bash
# UserPromptSubmit hook (idle path): detect a Telegram channel message whose body is
# exactly "/fresh" in a configured fleet bot session, and trigger a detached restart of
# THIS bot's claude session (blank / fresh context), acking via THIS bot's own token.
#
# Gating (rewritten for the fleet - no hardcoded worktree list): this hook is inert
# unless the session is a configured fleet bot. It requires TELEGRAM_STATE_DIR to be set
# AND `fleet-config bot-by-state "$TELEGRAM_STATE_DIR"` to resolve to a known bot key.
# All bot facts (key, workdir, token, chat id) come from fleet.json + the per-bot state
# dir at runtime.
#
# Companion mid-task path: the stock plugin's server.ts intercept (patch 04) handles a
# /fresh that arrives mid-turn, when this idle hook cannot fire. Both call the same
# engine ($FLEET_HOME/bin/fleet-fresh), so they can never drift.
#
# Exit codes:
#   0 - prompt proceeds normally (any non-matching input or soft error - fail open)
#   2 - block the prompt (only after a confirmed /fresh kicked off a restart)
#
# IMPORTANT: no `set -e`. A scripting error must NEVER block a normal prompt.

set -u

FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
FLEET_BIN="$FLEET_HOME/bin"
CONFIG="$FLEET_BIN/fleet-config"
RESTART_ENGINE="$FLEET_BIN/fleet-fresh"
LOG="$FLEET_HOME/logs/fresh-hook.log"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true
}

input="$(cat)"
[ -z "$input" ] && exit 0

# Gate 1 (cheap): only a channel bot session sets TELEGRAM_STATE_DIR.
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0

# Gate 2 (cheap substring): skip the python + config work unless "/fresh" appears.
case "$input" in
  *'/fresh'*) ;;
  *) exit 0 ;;
esac

# Gate 3: config must exist and resolve this state dir to a bot key ("is this a fleet
# session?"). fleet-config bot-by-state exits non-zero when the state dir is unknown.
[ -f "$CONFIG" ] || exit 0
key="$(python3 "$CONFIG" bot-by-state "$TELEGRAM_STATE_DIR" 2>/dev/null)" || exit 0
[ -z "$key" ] && exit 0
workdir="$(python3 "$CONFIG" bot "$key" workdir 2>/dev/null)"
[ -z "$workdir" ] && exit 0

# Extract the channel body + chat_id from the prompt in one python pass.
body=""; chat_id=""
{
  IFS= read -r body
  IFS= read -r chat_id
} < <(printf '%s' "$input" | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
except Exception:
    print(); print(); sys.exit(0)
prompt = d.get('prompt','') or ''
m = re.search(r'<channel\b[^>]*>(.*?)</channel>', prompt, re.DOTALL)
print((m.group(1) if m else '').strip())
mc = re.search(r'chat_id=\"([^\"]+)\"', prompt)
print(mc.group(1) if mc else '')
" 2>/dev/null)

# Body must be exactly /fresh.
[ "$body" = "/fresh" ] || exit 0

mkdir -p "$FLEET_HOME/logs" 2>/dev/null || true

# Token from THIS bot's state .env; chat_id from the tag, else access.json allowFrom[0].
bot_token=""
if [ -f "$TELEGRAM_STATE_DIR/.env" ]; then
  bot_token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$TELEGRAM_STATE_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')"
fi
if [ -z "$chat_id" ] && [ -f "$TELEGRAM_STATE_DIR/access.json" ]; then
  chat_id="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    a = d.get('allowFrom') or []
    print(str(a[0]) if a else '')
except Exception:
    print('')
" "$TELEGRAM_STATE_DIR/access.json" 2>/dev/null)"
fi

log "TRIGGER key=$key workdir=$workdir chat_id=$chat_id"

# Walk the parent PID chain (<=5 hops) to find the owning claude PID.
target_pid="$PPID"
pid="$target_pid"
for _ in 1 2 3 4 5; do
  cmd="$(ps -p "$pid" -o comm= 2>/dev/null | tr -d '[:space:]')"
  if [[ "$cmd" == *"claude"* ]]; then
    target_pid="$pid"
    break
  fi
  next="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  [ -z "$next" ] && break
  pid="$next"
done
log "target_pid=$target_pid"

# Ack "starting fresh" via the CORRECT bot for this session (neutral wording).
if [ -n "$bot_token" ] && [ -n "$chat_id" ]; then
  curl -s -m 5 -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
    --data-urlencode "chat_id=${chat_id}" \
    --data-urlencode "text=Starting a fresh session... back in ~5s." \
    >> "$LOG" 2>&1 || log "WARN telegram ack failed"
fi

# Spawn the detached restart engine. macOS ships no setsid, so:
#   nohup     - ignore SIGHUP when the parent claude dies
#   </dev/null >>LOG 2>&1  - detach stdio from the pipe to claude
#   &         - background
#   disown    - drop from the shell job table so the parent's exit does not wait
# The child reparents to the init/launchd process once claude dies.
if [ -x "$RESTART_ENGINE" ]; then
  nohup "$RESTART_ENGINE" "$target_pid" "$workdir" fresh-reset </dev/null >>"$LOG" 2>&1 &
  disown 2>/dev/null || true
  log "restart spawned for $workdir (bg pid=$!)"
else
  log "ERROR restart engine missing: $RESTART_ENGINE"
  echo "fresh trigger received but restart engine $RESTART_ENGINE is missing; aborting" >&2
  exit 0
fi

echo "fresh restart triggered (pid: $target_pid, workdir: $workdir); the new session will pick up your next Telegram message." >&2
exit 2
