#!/bin/bash
# UserPromptSubmit hook (idle path): the /resume counterpart to telegram-fresh.sh.
#
#   /resume  (also /sessions, /history, /past)  -> reply with a numbered list of the
#            most recent chats across ALL configured fleet bots.
#   /resume N  (also /sessions N, /open N, /reopen N)  -> reopen chat N HERE: copy its
#            transcript into THIS bot's project dir and relaunch THIS bot resumed into
#            it (full history). The origin bot is never touched.
#
# Listing/resolving are done by $FLEET_HOME/bin/fleet-sessions. Reopening reuses the
# same restart engine as /fresh ($FLEET_HOME/bin/fleet-fresh) with the chat UUID as the
# 3rd arg so the launcher does `claude --resume <uuid>`.
#
# Gating (fleet - no hardcoded worktree list): inert unless TELEGRAM_STATE_DIR is set
# AND `fleet-config bot-by-state "$TELEGRAM_STATE_DIR"` resolves to a bot key.
#
# fleet-sessions resolve <N> is expected to print one TAB-separated line:
#   <uuid>\t<origin_workdir>\t<origin_key>\t<src_transcript_path>
# (rc 3 = "send /resume first", rc 5 = "list stale", other non-zero = "no #N").
#
# Exit codes: 0 = proceed (non-matching / soft error); 2 = block (list sent or reopen
# kicked off). No `set -e`: a scripting error must never block a normal prompt.

set -u

FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
FLEET_BIN="$FLEET_HOME/bin"
CONFIG="$FLEET_BIN/fleet-config"
SESSIONS="$FLEET_BIN/fleet-sessions"
RESTART_ENGINE="$FLEET_BIN/fleet-fresh"
LOG="$FLEET_HOME/logs/resume-hook.log"

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG" 2>/dev/null || true
}

tg_send() {  # $1=token $2=chat_id $3=text
  [ -z "$1" ] || [ -z "$2" ] && return 0
  curl -s -m 8 -X POST "https://api.telegram.org/bot$1/sendMessage" \
    --data-urlencode "chat_id=$2" \
    --data-urlencode "text=$3" >> "$LOG" 2>&1 || log "WARN telegram send failed"
}

input="$(cat)"
[ -z "$input" ] && exit 0
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0

# Cheap command-word substring gate before any python/config work.
case "$input" in
  */resume*|*/sessions*|*/history*|*/past*|*/open*|*/reopen*) ;;
  *) exit 0 ;;
esac

[ -f "$CONFIG" ] || exit 0
key="$(python3 "$CONFIG" bot-by-state "$TELEGRAM_STATE_DIR" 2>/dev/null)" || exit 0
[ -z "$key" ] && exit 0
workdir="$(python3 "$CONFIG" bot "$key" workdir 2>/dev/null)"
[ -z "$workdir" ] && exit 0

# Pull channel-body, session_id and chat_id in one python pass (body whitespace
# collapsed - commands are single-line, so this only affects matching).
body=""; session_id=""; chat_id=""
{
  IFS= read -r body
  IFS= read -r session_id
  IFS= read -r chat_id
} < <(printf '%s' "$input" | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
except Exception:
    print(); print(); print(); sys.exit(0)
prompt = d.get('prompt','') or ''
m = re.search(r'<channel\b[^>]*>(.*?)</channel>', prompt, re.DOTALL)
print(re.sub(r'\s+', ' ', m.group(1) if m else '').strip())
print(d.get('session_id','') or '')
mc = re.search(r'chat_id=\"([^\"]+)\"', prompt)
print(mc.group(1) if mc else '')
" 2>/dev/null)

# Classify the command. Empty body / anything else -> no-op.
bl="$(printf '%s' "$body" | tr '[:upper:]' '[:lower:]')"
mode=""; pick=""
case "$bl" in
  /resume|/sessions|/history|/past) mode="list" ;;
  *)
    if [[ "$bl" =~ ^/(resume|sessions|open|reopen)[[:space:]]+([0-9]+)$ ]]; then
      mode="resume"; pick="${BASH_REMATCH[2]}"
    fi
    ;;
esac
[ -z "$mode" ] && exit 0

mkdir -p "$FLEET_HOME/logs" 2>/dev/null || true

# THIS bot's token for replies to the current chat; chat_id fallback from access.json.
bot_token=""
if [ -f "$TELEGRAM_STATE_DIR/.env" ]; then
  bot_token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$TELEGRAM_STATE_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')"
fi
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

log "TRIGGER mode=$mode pick=$pick key=$key chat_id=$chat_id"

# ---------------------------------------------------------------------------
# LIST: build the cross-bot list and send it to the current chat.
# ---------------------------------------------------------------------------
if [ "$mode" = "list" ]; then
  if [ ! -x "$SESSIONS" ]; then
    log "ERROR sessions engine missing: $SESSIONS"
    exit 0
  fi
  list_text="$(python3 "$SESSIONS" list --exclude-uuid "$session_id" 2>>"$LOG")"
  [ -z "$list_text" ] && list_text="Couldn't read your sessions. Try again in a moment."
  # Send as HTML (bold bot names + emoji). If Telegram rejects the markup, fall back to
  # plain text so the list can never silently vanish.
  if [ -n "$bot_token" ] && [ -n "$chat_id" ]; then
    resp="$(curl -s -m 8 -X POST "https://api.telegram.org/bot$bot_token/sendMessage" \
      --data-urlencode "chat_id=$chat_id" \
      --data-urlencode "text=$list_text" \
      --data-urlencode "parse_mode=HTML" 2>>"$LOG")"
    printf '%s\n' "$resp" >> "$LOG" 2>/dev/null || true
    case "$resp" in
      *'"ok":true'*) : ;;
      *)
        plain="$(printf '%s' "$list_text" | sed -e 's/<[^>]*>//g' -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&amp;/\&/g')"
        tg_send "$bot_token" "$chat_id" "$plain"
        log "list HTML send rejected; sent plain fallback"
        ;;
    esac
  fi
  echo "resume-hook: sent session list; prompt blocked" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# RESUME N (open-here): resolve the pick -> (uuid, origin workdir, origin key, source
# transcript), copy that transcript into THIS bot's project dir, and relaunch THIS bot
# resumed into it. The origin bot is NOT touched.
# ---------------------------------------------------------------------------
if [ ! -x "$SESSIONS" ]; then
  log "ERROR sessions engine missing: $SESSIONS"
  exit 0
fi
resolved="$(python3 "$SESSIONS" resolve "$pick" 2>>"$LOG")"
rc=$?
if [ $rc -ne 0 ] || [ -z "$resolved" ]; then
  if [ $rc -eq 3 ]; then
    tg_send "$bot_token" "$chat_id" "Send /resume first to pull up the numbered list, then /resume $pick."
  elif [ $rc -eq 5 ]; then
    tg_send "$bot_token" "$chat_id" "That list is a few hours old and your chats have moved since. Send /resume to refresh it, then /resume $pick."
  else
    tg_send "$bot_token" "$chat_id" "No session #$pick in the current list. Send /resume to refresh it."
  fi
  echo "resume-hook: resolve failed (rc=$rc)" >&2
  exit 2
fi

uuid="$(printf '%s' "$resolved" | cut -f1)"
origin_workdir="$(printf '%s' "$resolved" | cut -f2)"
origin_key="$(printf '%s' "$resolved" | cut -f3)"
src="$(printf '%s' "$resolved" | cut -f4)"

# THIS bot's (destination) Claude Code project dir - fleet-config returns the
# FULL absolute path (~/.claude/projects/<derived-name>), use it as-is.
dst_dir="$(python3 "$CONFIG" project-dir "$key" 2>/dev/null)"
if [ -z "$dst_dir" ]; then
  log "ERROR no project-dir for key=$key"
  exit 0
fi
dst="$dst_dir/$uuid.jsonl"

# Fallback if resolve did not include a source path: derive it from the origin bot's
# own project dir, the same layout the lister uses.
if [ -z "$src" ] && [ -n "$origin_workdir" ]; then
  okey="$(python3 "$CONFIG" bot-by-workdir "$origin_workdir" 2>/dev/null)"
  if [ -n "$okey" ]; then
    oproj_dir="$(python3 "$CONFIG" project-dir "$okey" 2>/dev/null)"
    [ -n "$oproj_dir" ] && src="$oproj_dir/$uuid.jsonl"
  fi
fi

# The picked chat's transcript must exist before we touch anything.
if [ ! -f "$src" ]; then
  log "ERROR open-here source missing: src=$src uuid=$uuid origin=$origin_workdir"
  tg_send "$bot_token" "$chat_id" "Couldn't find that chat's history on disk. Send /resume to refresh the list, then /resume $pick."
  echo "resume-hook: source transcript missing" >&2
  exit 2
fi

# Find THIS bot's live claude PID (the one to replace). ps + lsof cwd match, NOT pgrep
# (pgrep is unreliable on some macOS builds). Dead pid 999999 if none is found.
cur_pid=""
# shellcheck disable=SC2009  # pgrep is unreliable on some macOS builds; ps+lsof cwd match is the intended technique
while read -r p _rest; do
  [ -z "$p" ] && continue
  pcwd="$(lsof -a -p "$p" -d cwd 2>/dev/null | awk 'NR>1 {print $NF}')"
  if [ "$pcwd" = "$workdir" ]; then cur_pid="$p"; break; fi
done < <(ps -axo pid=,command= 2>/dev/null | grep -F -- "--channels plugin:telegram@claude-plugins-official" | grep -v grep)
[ -z "$cur_pid" ] && cur_pid="999999"

log "OPEN-HERE uuid=$uuid origin=$origin_key here=$key workdir=$workdir cur_pid=$cur_pid src=$src dst=$dst"

# Dry-run hook for safe testing: print the intended action, touch nothing.
if [ "${CC_RESUME_DRYRUN:-}" = "1" ]; then
  echo "DRYRUN would: cp '$src' '$dst' (skip if same) then $RESTART_ENGINE $cur_pid $workdir $uuid  (open #$pick from ${origin_key:-?} here in $key)" >&2
  exit 2
fi

# Copy the picked chat's history into THIS bot's project dir so `claude --resume <uuid>`
# (run from this workdir) finds it. COPY, never move: the origin bot's own copy stays
# intact. Skip when it is already here (the user picked one of this bot's own chats).
if [ "$src" != "$dst" ]; then
  mkdir -p "$dst_dir" 2>/dev/null || true
  if ! cp "$src" "$dst" 2>>"$LOG"; then
    log "ERROR open-here copy failed src=$src dst=$dst"
    tg_send "$bot_token" "$chat_id" "Couldn't stage that chat to reopen here - nothing was changed. Try /resume $pick again."
    echo "resume-hook: copy failed" >&2
    exit 2
  fi
fi

# Ack here, then relaunch THIS bot resumed into the picked chat.
tg_send "$bot_token" "$chat_id" "Reopening #$pick right here with the full history... back in ~10s. Whatever this chat was on is saved and stays resumable too."

if [ -x "$RESTART_ENGINE" ]; then
  nohup "$RESTART_ENGINE" "$cur_pid" "$workdir" "$uuid" </dev/null >>"$LOG" 2>&1 &
  disown 2>/dev/null || true
  log "open-here restart spawned for $workdir (bg pid=$!)"
else
  log "ERROR restart engine missing: $RESTART_ENGINE"
  tg_send "$bot_token" "$chat_id" "Reopen failed: restart engine missing. Flag this to the user."
fi

echo "resume-hook: open-here triggered (${origin_key:-?} -> $key, $uuid)" >&2
exit 2
