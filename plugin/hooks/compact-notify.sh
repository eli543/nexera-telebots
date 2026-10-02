#!/bin/bash
# Notify the Telegram chat before automatic compaction and after any compaction.
# Modes: pre, post, followup. Exit 0 on every path. Requires TELEGRAM_STATE_DIR.
# CC_COMPACT_DRYRUN=1 prints the payload, updates local flags, and never sends or spawns.
# Post can start a detached followup that edits the same message once its boundary lands.
set -uo pipefail

MODE="${1:-}"
[ -n "${TELEGRAM_STATE_DIR:-}" ] && [ -d "$TELEGRAM_STATE_DIR" ] || exit 0
STATE="${TELEGRAM_STATE_DIR%/}"
FLEET_HOME="${FLEET_HOME:-${HOME:-}/.claude/telegram-fleet}"
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$HOOK_DIR/telebots_hooklib.py"
LOG="$STATE/logs/compact.log"
mkdir -p "$STATE/logs" 2>/dev/null || exit 0
BOT="$(/usr/bin/python3 "$LIB" metadata | /usr/bin/sed -n '2p')"
[ -n "$BOT" ] || BOT="Claude"
DRYRUN="${CC_COMPACT_DRYRUN:-0}"
PY=/usr/bin/python3
PCT_HELPER="$FLEET_HOME/bin/fleet-context-pct"
TEST="${CC_COMPACT_TEST:-0}"
HOOK_START=$(date +%s 2>/dev/null)
case "$HOOK_START" in ''|*[!0-9]*) HOOK_START=0 ;; esac

log() { { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "${MODE:-?}" "$*" >> "$LOG"; } 2>/dev/null || true; }

case "$MODE" in
  -h|--help) /usr/bin/sed -n '2,/^$/p' "$0" | /usr/bin/sed 's/^# \{0,1\}//'; exit 0 ;;
  pre|post|followup) ;;
  *) log "reason=2 skip: first argument must be pre, post or followup (got '${MODE:-}')"; exit 0 ;;
esac

if [ "$MODE" = "followup" ]; then
  BOT="${CC_FU_BOT:-unknown bot}"
  FU_CHAT="${CC_FU_CHAT:-}"
  FU_MSG="${CC_FU_MSG:-}"
  FU_WHEN="${CC_FU_WHEN:-just now}"
  FU_BEFORE="${CC_FU_BEFORE:-unknown}"
  FU_WAIT="${CC_COMPACT_FOLLOWUP_WAIT:-120}"
  FU_STATE="${TELEGRAM_STATE_DIR:-}"
  FU_STATE="${FU_STATE%/}"

  FU_TOKEN=""
  if [ -n "$FU_STATE" ] && [ -f "$FU_STATE/.env" ]; then
    FU_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$FU_STATE/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
  fi
  if [ -z "$FU_CHAT" ] || [ -z "$FU_MSG" ] || [ -z "$FU_TOKEN" ]; then
    log "$BOT reason=14 nothing to edit (chat id present: $([ -n "$FU_CHAT" ] && echo yes || echo no), message id present: $([ -n "$FU_MSG" ] && echo yes || echo no), token present: $([ -n "$FU_TOKEN" ] && echo yes || echo no))"
    exit 0
  fi

  FU_RESULT=$(MODEL_STATE_FILE="$FU_STATE/model-state" HELPER="$PCT_HELPER" "$PY" - <<'PY' 2>/dev/null
import datetime, importlib.util, importlib.machinery, json, os, sys, time

tr = os.environ.get("CC_FU_TRANSCRIPT") or ""
helper = os.environ.get("HELPER") or ""
try:
    with open(os.environ.get("MODEL_STATE_FILE", ""), "r", encoding="utf-8", errors="replace") as fh:
        model_state = fh.read()
except Exception:
    model_state = ""

TAIL = 262144          # 256 KB. The boundary is appended, so this reaches it on a huge file.
POLL = 0.5

def env_float(name, fallback):
    try:
        return float(os.environ.get(name) or fallback)
    except Exception:
        return float(fallback)

budget = env_float("CC_COMPACT_FOLLOWUP_WAIT", 120.0)
hook_start = env_float("CC_FU_START", 0.0)
if hook_start <= 0:
    hook_start = time.time()
floor = hook_start - 5.0

def cap_for(served):
    """The pct helper's own cap rule, imported so the two can never drift."""
    try:
        spec = importlib.util.spec_from_loader("cc_pct_helper", importlib.machinery.SourceFileLoader("cc_pct_helper", helper))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.context_cap(served, model_state)
    except Exception:
        ms = (model_state or "").lower()
        return 1_000_000 if ("[1m]" in ms or "fable" in ms or "fable" in (served or "").lower()) else 200_000

def parse_ts(value):
    """Read a transcript timestamp as epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    elif text.endswith("+00:00"):
        text = text[:-6]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            stamp = datetime.datetime.strptime(text, fmt)
        except Exception:
            continue
        return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()
    return None

def baseline_from_head():
    """The session's fixed overhead - system prompt, tools, skills - measured as the FIRST
    non-sidechain assistant usage record. It sits near the top of the file, so this reads the
    head and stops; it is never called from inside the poll loop."""
    try:
        with open(tr, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("subtype") == "compact_boundary" or rec.get("isCompactSummary") is True:
                    continue
                if rec.get("type") != "assistant" or rec.get("isSidechain") is True:
                    continue
                msg = rec.get("message")
                msg = msg if isinstance(msg, dict) else {}
                use = msg.get("usage")
                use = use if isinstance(use, dict) else {}
                if "input_tokens" not in use:
                    continue

                def n(k):
                    v = use.get(k, 0)
                    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

                total = n("input_tokens") + n("cache_read_input_tokens") + n("cache_creation_input_tokens")
                return total, (msg.get("model") or "")
    except Exception:
        pass
    return None, ""

def fresh_boundary():
    """postTokens off the newest boundary in the tail that was written for THIS compaction."""
    try:
        size = os.path.getsize(tr)
    except Exception:
        return None
    start = max(0, size - TAIL)
    try:
        with open(tr, "rb") as fh:
            if start:
                fh.seek(start)
            blob = fh.read()
    except Exception:
        return None
    if start:
        cut = blob.find(b"\n")
        blob = blob[cut + 1:] if cut >= 0 else b""
    best = None
    best_ts = None
    for raw in blob.splitlines():
        if b"compact_boundary" not in raw:      # cheap reject; the words also occur in chat
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(rec, dict) or rec.get("subtype") != "compact_boundary":
            continue
        meta = rec.get("compactMetadata")
        if not isinstance(meta, dict):
            continue
        post = meta.get("postTokens")
        if isinstance(post, bool) or not isinstance(post, (int, float)):
            continue
        stamp = parse_ts(rec.get("timestamp"))
        if stamp is None or stamp < floor:
            continue
        if best_ts is None or stamp >= best_ts:
            best_ts, best = stamp, int(post)
    return best

if not tr or not os.path.isfile(tr):
    print("error - - - - 0.0")
    sys.exit(0)

baseline, served = baseline_from_head()
started = time.time()
post = None
while True:
    post = fresh_boundary()
    if post is not None:
        break
    if (time.time() - started) >= budget:
        break
    time.sleep(POLL)
elapsed = time.time() - started

if post is None:
    print("timeout - - - - %.1f" % elapsed)
    sys.exit(0)
if baseline is None:
    baseline, served = baseline_from_head()
cap = cap_for(served)
if not cap:
    print("error - - - - %.1f" % elapsed)
    sys.exit(0)
pct = max(0, min(100, int(round((post + (baseline or 0)) / float(cap) * 100))))
print("ok %d %d %d %d %.1f" % (pct, post, baseline or 0, cap, elapsed))
PY
)
  read -r FU_STATUS FU_PCT FU_POST FU_BASE FU_CAP FU_ELAPSED <<EOF
$FU_RESULT
EOF

  if [ "${FU_STATUS:-}" != "ok" ]; then
    log "$BOT reason=13 no boundary record for this compaction inside ${FU_WAIT}s (${FU_STATUS:-no answer}, waited ${FU_ELAPSED:-?}s) - the ping stays exactly as it was sent"
    exit 0
  fi
  log "$BOT reason=10 boundary found after ${FU_ELAPSED}s: postTokens=${FU_POST} baseline=${FU_BASE} cap=${FU_CAP} after=${FU_PCT}%"

  if [ "$FU_BEFORE" = "unknown" ]; then
    FU_TEXT="Compaction done at ${FU_WHEN}. ${BOT} context is now about ${FU_PCT}%. Next message picks up where we left off."
  else
    FU_TEXT="Compaction done at ${FU_WHEN}. ${BOT} context went from ${FU_BEFORE}% to about ${FU_PCT}%. Next message picks up where we left off."
  fi
  if [ "$TEST" = "1" ]; then
    FU_TEXT="[test] $FU_TEXT"
  fi

  if [ "$DRYRUN" = "1" ]; then
    MSG="$FU_TEXT" CHAT="$FU_CHAT" MID="$FU_MSG" "$PY" -c 'import json,os; print(json.dumps({"dry_run": True, "method": "editMessageText", "chat_id": os.environ["CHAT"], "message_id": os.environ["MID"], "text": os.environ["MSG"]}))'
    exit 0
  fi
  FU_RESP=$(MSG="$FU_TEXT" CHAT="$FU_CHAT" MID="$FU_MSG" "$PY" -c 'import json,os; print(json.dumps({"method": "editMessageText", "_timeout": 10, "chat_id": os.environ["CHAT"], "message_id": os.environ["MID"], "text": os.environ["MSG"]}))' | "$PY" "$LIB" send)
  if printf '%s' "$FU_RESP" | grep -q '"ok":true'; then
    log "reason=11 message=${FU_MSG} ok=true"
  else
    log "reason=12 message=${FU_MSG} ok=false"
  fi
  exit 0
fi

INPUT="$(cat 2>/dev/null)"
if [ -z "$INPUT" ]; then log "reason=2 skip: empty event on stdin"; exit 0; fi

FIELDS=$(printf '%s' "$INPUT" | "$PY" -c '
import json, re, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
def clean_id(v):
    return re.sub(r"[^A-Za-z0-9._-]", "", str(v or ""))[:100]
def clean_word(v):
    return re.sub(r"[^a-z_]", "", str(v or "").lower())[:20]
tp = d.get("transcript_path") or d.get("transcriptPath") or ""
print(clean_id(d.get("session_id") or d.get("sessionId")))
print(clean_word(d.get("trigger")))
print(clean_word(d.get("source")))
print(tp if isinstance(tp, str) else "")
print(clean_id(d.get("agent_id") or d.get("agentId")))
print(clean_id(d.get("agent_type") or d.get("agentType")))
' 2>/dev/null)

SESSION_ID=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '1p')
TRIGGER=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '2p')
SOURCE=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '3p')
TRANSCRIPT=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '4p')
AGENT_ID=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '5p')
AGENT_TYPE=$(printf '%s' "$FIELDS" | /usr/bin/sed -n '6p')

if [ "$MODE" = "post" ] && [ "$SOURCE" != "compact" ]; then
  exit 0   # reason 3, silent by design
fi

if [ -z "${TELEGRAM_STATE_DIR:-}" ]; then
  log "reason=1 no-op: not a channel session (TELEGRAM_STATE_DIR unset) trigger=${TRIGGER:-} source=${SOURCE:-} session=${SESSION_ID:-}"
  exit 0
fi
STATE="${TELEGRAM_STATE_DIR%/}"

if [ ! -d "$STATE" ]; then
  log "$BOT reason=1 no-op: state dir '$STATE' is not a directory"
  exit 0
fi

if [ -n "$AGENT_ID" ]; then
  SUB_PCT="unknown"
  if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] && [ -x "$PCT_HELPER" ]; then
    P=$("$PY" "$PCT_HELPER" "$TRANSCRIPT" "$STATE/model-state" 2>/dev/null | head -1 | tr -dc '0-9')
    [ -n "$P" ] && SUB_PCT="${P}%"
  fi
  log "$BOT reason=15 skip: a background agent of $BOT compacted its own context (agent_id=$AGENT_ID agent_type=${AGENT_TYPE:-unknown} trigger=${TRIGGER:-none} source=${SOURCE:-none} session=${SESSION_ID:-}); $BOT itself is at $SUB_PCT - nothing sent, no flag file touched"
  exit 0
fi

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
  PROJ=$("$PY" "$LIB" project 2>/dev/null)
  SID="$SESSION_ID"
  [ -n "$SID" ] || SID=$(cat "$STATE/session.id" 2>/dev/null | tr -dc 'A-Za-z0-9._-' | head -c 100)
  if [ -n "$PROJ" ] && [ -n "$SID" ] && [ -f "$PROJ/$SID.jsonl" ]; then
    TRANSCRIPT="$PROJ/$SID.jsonl"
    log "$BOT note: event carried no usable transcript_path, fell back to $TRANSCRIPT"
  else
    TRANSCRIPT=""
  fi
fi

CURL_MAX=6
[ "$MODE" = "pre" ] && CURL_MAX=4

SENT_CHAT_ID=""
SENT_MSG_ID=""

tg_send() {
  local msg="$1"
  [ "$TEST" = "1" ] && msg="[test] $msg"
  local chat token resp
  chat=$("$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    lst = d.get("allowFrom")
    if isinstance(lst, list) and lst and isinstance(lst[0], str):
        print(lst[0])
except Exception:
    pass
' "$STATE/access.json" 2>/dev/null | head -1 | tr -dc '0-9-')
  if [ "$DRYRUN" = "1" ]; then
    MSG="$msg" CHAT="$chat" "$PY" -c 'import json,os; print(json.dumps({"dry_run": True, "method": "sendMessage", "chat_id": os.environ["CHAT"], "text": os.environ["MSG"]}))'
    log "reason=8 dry_run=true"
    return 0
  fi
  token=""
  if [ -f "$STATE/.env" ]; then
    token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$STATE/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
  fi
  if [ -z "$chat" ] || [ -z "$token" ]; then
    log "$BOT reason=7 NOT sent (chat id present: $([ -n "$chat" ] && echo yes || echo no), token present: $([ -n "$token" ] && echo yes || echo no))"
    return 1
  fi
  resp=$(MSG="$msg" CHAT="$chat" MAX="$CURL_MAX" "$PY" -c 'import json,os; print(json.dumps({"_timeout": int(os.environ["MAX"]), "chat_id": os.environ["CHAT"], "text": os.environ["MSG"]}))' | "$PY" "$LIB" send)
  if ! printf '%s' "$resp" | grep -q '"ok":true'; then
    log "reason=7 ok=false"
    return 1
  fi
  log "reason=8 ok=true"
  if [ "$MODE" = "post" ]; then
    SENT_CHAT_ID="$chat"
    SENT_MSG_ID=$(printf '%s' "$resp" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    r = d.get("result") if isinstance(d, dict) else None
    v = (r or {}).get("message_id")
    print(int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else "")
except Exception:
    pass
' 2>/dev/null | head -1 | tr -dc '0-9')
  fi
  return 0
}

NOW_LOCAL=$("$PY" -c 'import datetime; d=datetime.datetime.now().astimezone(); print(d.strftime("%-I:%M%p").lower() + " " + d.tzname())' 2>/dev/null)
[ -n "$NOW_LOCAL" ] || NOW_LOCAL="just now"

if [ "$MODE" = "pre" ]; then
  PCT_BEFORE="unknown"
  if [ -n "$TRANSCRIPT" ] && [ -x "$PCT_HELPER" ]; then
    P=$("$PY" "$PCT_HELPER" "$TRANSCRIPT" "$STATE/model-state" 2>/dev/null | head -1 | tr -dc '0-9')
    [ -n "$P" ] && PCT_BEFORE="$P"
  fi
  [ "$PCT_BEFORE" = "unknown" ] && log "$BOT reason=5 before-percent unknown (transcript='${TRANSCRIPT:-none}')"

  SESSION_ID="$SESSION_ID" TRIGGER="$TRIGGER" PCT_BEFORE="$PCT_BEFORE" TRANSCRIPT="$TRANSCRIPT" OUT="$STATE/compact-started" "$PY" -c '
import json, os, time
try:
    transcript = os.environ.get("TRANSCRIPT") or ""
    size = os.path.getsize(transcript) if transcript and os.path.isfile(transcript) else None
    with open(os.environ["OUT"], "w", encoding="utf-8") as fh:
        json.dump({
            "ts": time.time(),
            "trigger": os.environ.get("TRIGGER") or "unknown",
            "pct_before": os.environ.get("PCT_BEFORE") or "unknown",
            "session_id": os.environ.get("SESSION_ID") or "",
            "transcript_path": transcript,
            "transcript_bytes": size,
        }, fh)
        fh.write("\n")
except Exception:
    pass
' 2>/dev/null

  log "$BOT trigger=${TRIGGER:-unknown} pct_before=$PCT_BEFORE session=${SESSION_ID:-} - wrote $STATE/compact-started"

  if [ "$TRIGGER" = "auto" ]; then
    if [ "$PCT_BEFORE" = "unknown" ]; then
      tg_send "Auto-compacting $BOT now. I'll ping when it's done."
    else
      tg_send "Auto-compacting $BOT at ${PCT_BEFORE}%. I'll ping when it's done."
    fi
  else
    log "$BOT reason=6 trigger=${TRIGGER:-unknown} - no message (a manual compaction was already acked)"
  fi
  exit 0
fi

PCT_BEFORE=$("$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get("pct_before")
    print(str(v) if v is not None else "unknown")
except Exception:
    print("unknown")
' "$STATE/compact-started" 2>/dev/null | head -1 | tr -dc 'A-Za-z0-9')
[ -n "$PCT_BEFORE" ] || PCT_BEFORE="unknown"

AFTER_RESULT=$(TRANSCRIPT="$TRANSCRIPT" MODEL_STATE_FILE="$STATE/model-state" HELPER="$PCT_HELPER" COMPACT_STARTED_FILE="$STATE/compact-started" SESSION_ID="$SESSION_ID" HOOK_START="$HOOK_START" "$PY" - <<'PY' 2>/dev/null
import datetime, importlib.util, importlib.machinery, json, math, os, sys, time

tr = os.environ.get("TRANSCRIPT") or ""
helper = os.environ.get("HELPER") or ""
try:
    with open(os.environ.get("MODEL_STATE_FILE", ""), "r", encoding="utf-8", errors="replace") as fh:
        model_state = fh.read()
except Exception:
    model_state = ""

try:
    started = float(os.environ.get("HOOK_START") or time.time())
except ValueError:
    started = time.time()
snapshot_bytes = None
try:
    with open(os.environ.get("COMPACT_STARTED_FILE", "")) as handle:
        marker = json.load(handle)
    stamp = marker.get("ts")
    if (marker.get("session_id") == os.environ.get("SESSION_ID") and
            isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and
            math.isfinite(stamp) and 0 <= time.time() - stamp <= 86400):
        started = float(stamp)
        size = marker.get("transcript_bytes")
        if (marker.get("transcript_path") == tr and isinstance(size, int) and
                not isinstance(size, bool) and size >= 0):
            snapshot_bytes = size
except Exception:
    pass


def fresh_timestamp(value):
    try:
        if not isinstance(value, str):
            return False
        stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return False
        return started <= stamp.timestamp() <= time.time() + 5
    except (ValueError, OverflowError):
        return False

def cap_for(served):
    """The pct helper's own cap rule, imported so the two can never drift."""
    try:
        spec = importlib.util.spec_from_loader("cc_pct_helper", importlib.machinery.SourceFileLoader("cc_pct_helper", helper))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.context_cap(served, model_state)
    except Exception:
        ms = (model_state or "").lower()
        return 1_000_000 if ("[1m]" in ms or "fable" in ms or "fable" in (served or "").lower()) else 200_000

def scan():
    """(post_tokens, baseline_tokens, served_model) from the transcript, or (None, ..)."""
    post = None
    summary_chars = None
    baseline = None
    served = ""
    try:
        with open(tr, "rb") as fh:
            offset = 0
            for raw in fh:
                appended = snapshot_bytes is not None and offset >= snapshot_bytes
                offset += len(raw)
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("subtype") == "compact_boundary":
                    meta = rec.get("compactMetadata")
                    value = meta.get("postTokens") if isinstance(meta, dict) else None
                    if (fresh_timestamp(rec.get("timestamp")) and isinstance(value, (int, float)) and
                            not isinstance(value, bool) and math.isfinite(value) and value >= 0):
                        post = int(value)
                elif (rec.get("isCompactSummary") is True and rec.get("isSidechain") is not True and
                      (appended or fresh_timestamp(rec.get("timestamp")))):
                    msg = rec.get("message")
                    body = (msg or {}).get("content") if isinstance(msg, dict) else None
                    if isinstance(body, str):
                        summary_chars = len(body)
                elif baseline is None and rec.get("type") == "assistant" and rec.get("isSidechain") is not True:
                    msg = rec.get("message")
                    msg = msg if isinstance(msg, dict) else {}
                    use = msg.get("usage")
                    use = use if isinstance(use, dict) else {}
                    if "input_tokens" in use:
                        def n(k):
                            v = use.get(k, 0)
                            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
                        baseline = n("input_tokens") + n("cache_read_input_tokens") + n("cache_creation_input_tokens")
                        served = msg.get("model") or ""
    except Exception:
        return None, None, None, ""
    return post, summary_chars, baseline, served

if not tr or not os.path.isfile(tr):
    print("unknown unknown")
    sys.exit(0)

post, summary_chars, baseline, served = scan()

if post is None and summary_chars is None:
    print("unknown unknown")
    sys.exit(0)

payload = post if post is not None else max(0, summary_chars // 4)
total = payload + (baseline or 0)
cap = cap_for(served)
if not cap:
    print("unknown unknown")
    sys.exit(0)
print(max(0, min(100, int(round(total / cap * 100)))), "boundary" if post is not None else "summary")
PY
)
read -r PCT_AFTER PCT_AFTER_SOURCE <<EOF
$AFTER_RESULT
EOF
PCT_AFTER=$(printf '%s' "$PCT_AFTER" | head -1 | tr -dc 'A-Za-z0-9')
[ -n "$PCT_AFTER" ] || PCT_AFTER="unknown"
[ -n "${PCT_AFTER_SOURCE:-}" ] || PCT_AFTER_SOURCE="unknown"
[ "$PCT_AFTER" = "unknown" ] && log "$BOT reason=5 after-percent unknown (transcript='${TRANSCRIPT:-none}')"

if [ "$PCT_BEFORE" = "unknown" ] && [ "$PCT_AFTER" = "unknown" ]; then
  MSG="Compaction done at ${NOW_LOCAL}. ${BOT} is compacted. Next message picks up where we left off."
elif [ "$PCT_BEFORE" = "unknown" ]; then
  MSG="Compaction done at ${NOW_LOCAL}. ${BOT} context is now about ${PCT_AFTER}%. Next message picks up where we left off."
elif [ "$PCT_AFTER" = "unknown" ]; then
  MSG="Compaction done at ${NOW_LOCAL}. ${BOT} context was ${PCT_BEFORE}% before it. Next message picks up where we left off."
else
  MSG="Compaction done at ${NOW_LOCAL}. ${BOT} context went from ${PCT_BEFORE}% to about ${PCT_AFTER}%. Next message picks up where we left off."
fi
SENT=0
tg_send "$MSG" && SENT=1

if [ "$SENT" = "1" ] && [ "$PCT_AFTER_SOURCE" != "boundary" ] && [ -n "$SENT_MSG_ID" ]; then
  SELF="$HOOK_DIR/compact-notify.sh"
  SPAWN=$(CC_FU_SELF="$SELF" CC_FU_LOG="$LOG" CC_FU_BOT="$BOT" CC_FU_CHAT="$SENT_CHAT_ID" \
          CC_FU_MSG="$SENT_MSG_ID" CC_FU_TRANSCRIPT="$TRANSCRIPT" CC_FU_WHEN="$NOW_LOCAL" \
          CC_FU_BEFORE="$PCT_BEFORE" CC_FU_START="$HOOK_START" "$PY" - <<'PY' 2>&1
import os, subprocess

log_path = os.environ.get("CC_FU_LOG") or os.devnull
self_path = os.environ.get("CC_FU_SELF") or ""
try:
    sink = open(log_path, "a")
except Exception:
    sink = subprocess.DEVNULL
try:
    child = subprocess.Popen(
        ["/bin/bash", self_path, "followup"],
        stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
        start_new_session=True)
    print("pid=%d" % child.pid)
except Exception:
    print("spawn_failed=true")
PY
)
  log "$BOT reason=9 after_source=$PCT_AFTER_SOURCE - detached follow-up spawned to edit message ${SENT_MSG_ID} when the boundary record lands ($SPAWN)"
elif [ "$PCT_AFTER_SOURCE" != "boundary" ]; then
  log "$BOT reason=9 after_source=$PCT_AFTER_SOURCE and NO follow-up spawned (sent: $SENT, message id: ${SENT_MSG_ID:-none})"
fi

rm -f "$STATE/compact-started" "$STATE/compact-inflight" 2>/dev/null || true
if [ "$TEST" = "1" ]; then
  log "$BOT test mode: guardian nudge flags left alone"
elif [ -n "$SESSION_ID" ]; then
  n=0
  for f in "$STATE/guardian-nudged-${SESSION_ID}-"*; do
    [ -e "$f" ] || continue
    rm -f "$f" 2>/dev/null && n=$((n + 1))
  done
  log "$BOT cleared compact-started, compact-inflight and $n guardian nudge flag(s) for session $SESSION_ID"
else
  log "$BOT cleared compact-started and compact-inflight; no session id in the event, so no guardian flags were touched"
fi

if [ "$SENT" = "1" ]; then
  echo "[compact-notify] Compaction finished at ${NOW_LOCAL}; the hook sent the notification. Do not repeat it. Re-read any file before editing it."
else
  echo "[compact-notify] Compaction finished at ${NOW_LOCAL}; its Telegram notification could not be sent. Re-read any file before editing it."
fi
exit 0
