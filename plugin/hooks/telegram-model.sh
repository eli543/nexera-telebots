#!/bin/bash
# UserPromptSubmit hook (idle path): the /model command.
#
#   /model                        -> report the model + effort this chat is on.
#   /model opus|sonnet|fable|haiku [low|medium|high|xhigh|max]
#                                 -> switch this bot to that model (and effort),
#                                    restarting resumed so the conversation is kept.
#   /model low|medium|high|xhigh|max
#                                 -> change only the effort, keep the current model.
#
# All the real logic (parse, validate, write state, restart-resume) lives in
# $FLEET_HOME/bin/fleet-model, which the poller intercept
# ALSO calls - so the mid-task path and this idle path can never drift. This hook only
# detects the command and hands off, passing the AUTHORITATIVE session_id so the restart
# resumes THIS exact conversation.
#
# Gating (fleet - no hardcoded worktree list): inert unless TELEGRAM_STATE_DIR is set
# AND `fleet-config bot-by-state "$TELEGRAM_STATE_DIR"` resolves to a bot key.
#
# Exit codes: 0 = proceed (non-matching / soft error); 2 = block (a /model command was
# handled). No `set -e`: a scripting error must NEVER block a normal prompt.

set -u
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0

FLEET_HOME="${FLEET_HOME:-${HOME:-}/.claude/telegram-fleet}"
FLEET_BIN="$FLEET_HOME/bin"
CONFIG="$FLEET_BIN/fleet-config"
MODEL_ENGINE="$FLEET_BIN/fleet-model"
LOG="$TELEGRAM_STATE_DIR/logs/model-hook.log"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true
}

input="$(cat)"
[ -z "$input" ] && exit 0
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0

# Cheap substring gate before the heavier python extraction.
case "$input" in
  *'/model'*) ;;
  *) exit 0 ;;
esac

[ -f "$CONFIG" ] || exit 0
key="$(python3 "$CONFIG" bot-by-state "$TELEGRAM_STATE_DIR" 2>/dev/null)" || exit 0
[ -z "$key" ] && exit 0
workdir="$(python3 "$CONFIG" bot "$key" workdir 2>/dev/null)"
[ -z "$workdir" ] && exit 0

# Pull channel-body, chat_id and the AUTHORITATIVE session_id in one python pass.
# session_id is this session's uuid, passed to the engine so the restart resumes THIS
# conversation. Body whitespace collapsed (commands are single-line).
body=""; chat_id=""; session_id=""
{
  IFS= read -r body
  IFS= read -r chat_id
  IFS= read -r session_id
} < <(printf '%s' "$input" | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
except Exception:
    print(); print(); print(); sys.exit(0)
prompt = d.get('prompt','') or ''
m = re.search(r'<channel\b[^>]*>(.*?)</channel>', prompt, re.DOTALL)
print(re.sub(r'\s+', ' ', m.group(1) if m else '').strip())
mc = re.search(r'chat_id=\"([^\"]+)\"', prompt)
print(mc.group(1) if mc else '')
print(d.get('session_id','') or '')
" 2>/dev/null)

# Classify: the body must be exactly '/model' or '/model <args>' (case-insensitive on
# the command word). Anything else is a normal message - pass through.
bl="$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')"
args=""
case "$bl" in
  /model) args="" ;;
  /model' '*) args="$(printf '%s' "$body" | sed -E 's/^[[:space:]]*[/][Mm][Oo][Dd][Ee][Ll][[:space:]]+//')" ;;
  *) exit 0 ;;
esac

mkdir -p "$TELEGRAM_STATE_DIR/logs" 2>/dev/null || true

# chat_id fallback from access.json allowFrom[0].
if [ -z "$chat_id" ] && [ -f "$TELEGRAM_STATE_DIR/access.json" ]; then
  chat_id="$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1])); a = d.get('allowFrom') or []
    print(str(a[0]) if a else '')
except Exception:
    print('')
" "$TELEGRAM_STATE_DIR/access.json" 2>/dev/null)"
fi

log "TRIGGER key=$key workdir=$workdir chat_id=$chat_id session_id=$session_id"

# Run detached: the engine may kill and relaunch this very session, so it must outlive
# the hook (same detach /fresh and /resume use). Args: workdir chat_id raw_args session.
if [ -x "$MODEL_ENGINE" ]; then
  nohup "$MODEL_ENGINE" "$workdir" "$chat_id" "$args" "$session_id" </dev/null >>"$LOG" 2>&1 &
  disown 2>/dev/null || true
  log "handoff spawned (bg pid=$!)"
else
  log "ERROR model engine missing: $MODEL_ENGINE"
fi

echo "model-hook: /model handled" >&2
exit 2
