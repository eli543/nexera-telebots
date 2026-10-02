#!/usr/bin/env python3
"""
tg-reply-guard: guarantees a channel turn never ends without a Telegram reply.

The bug it fixes: in a channel session the user reads ONLY Telegram - a reply reaches
the reader only if the model calls mcp__plugin_telegram_telegram__reply. Late in long
sessions the model sometimes answers in plain terminal text instead, so the user sees
silence. Instructions decay; this hook is the structural backstop.

Three modes, one script:
  prompt (UserPromptSubmit)                 -> start a new turn, clear markers
  reply  (PostToolUse on the reply tool)    -> mark "reply sent this turn"
  stop   (Stop)                             -> if a substantive answer was produced
                                               but no reply was sent this turn,
                                               block ONCE and tell the model to send.
                                               Stays quiet while the chat is under a
                                               live Telegram flood-ban cooldown.

Safe by design: fail-open (any error -> allow), loop-safe (blocks at most once per
turn, plus honors stop_hook_active), env-gated on TELEGRAM_STATE_DIR, kill-switch
file $TELEGRAM_STATE_DIR/reply_guard_off. Coexists with the tg-stream Stop hook.
"""
import sys, os, json, uuid, time, stat

REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
MIN_SUBSTANTIVE_CHARS = 40
BLOCK_REASON = (
    "DELIVERY CHECK: you produced an answer this turn but never called "
    + REPLY_TOOL + ". The user reads only Telegram - terminal text is invisible to the reader "
    "until you use the reply tool. Send your answer via the "
    + REPLY_TOOL + " tool now (the full answer, or a summary with a TLDR), then stop. "
    "Do not redo any work - just deliver what you already wrote. "
    "If you truly already sent a reply this turn, stop normally."
)


def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return None


def _write(p, s):
    try:
        with open(p, "w") as f:
            f.write(s)
    except Exception:
        pass


def _rm(p):
    try:
        os.remove(p)
    except Exception:
        pass


RATE_STATE_FILE = "ratelimit.json"
RATE_COOLDOWN_MAX = 86400.0
RATE_MAX_BYTES = 1000000


COOLDOWN_SUPPRESS_MIN = 60.0


def _small_json(path):
    """Parse a small regular JSON file, else None. Stats before it opens, so a FIFO, a
    directory or an oversized file is skipped instead of read."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > RATE_MAX_BYTES:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cooldown_until(state):
    """Epoch seconds when this chat's flood-ban cooldown expires, 0.0 if none is live."""
    try:
        sidecar = _small_json(os.path.join(state, "stream", RATE_STATE_FILE)) or {}
        cooldown = sidecar.get("cooldown")
        if not isinstance(cooldown, dict) or not cooldown:
            return 0.0
        access = _small_json(os.path.join(state, "access.json")) or {}
        allow = access.get("allowFrom") or []
        row = cooldown.get(str(allow[0])) if allow else None
        if not isinstance(row, dict):
            return 0.0
        until = float(row.get("until") or 0)
        remaining = until - time.time()
        if remaining < COOLDOWN_SUPPRESS_MIN or remaining > RATE_COOLDOWN_MAX:
            return 0.0
        return until
    except Exception:
        return 0.0


def _substantive(transcript_path):
    """True ONLY if we can read the transcript AND the assistant emitted real text
    since the last genuine user prompt. Conservative: on any unreadable/parse failure
    return False (do NOT block). This keeps the guard from ever over-blocking the reply
    path when the transcript isn't available - worst case it stays inert. It only blocks
    on positive confirmation."""
    try:
        with open(transcript_path) as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        last_user = -1
        for i, e in enumerate(entries):
            if e.get("type") != "user":
                continue
            c = (e.get("message") or {}).get("content")
            if isinstance(c, str) and c.strip():
                last_user = i
            elif isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "text" for b in c
            ):
                last_user = i
        text = ""
        for e in entries[last_user + 1:]:
            if e.get("type") != "assistant":
                continue
            c = (e.get("message") or {}).get("content") or []
            if isinstance(c, str):
                text += c
            else:
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text += b.get("text", "")
        return len(text.strip()) >= MIN_SUBSTANTIVE_CHARS
    except Exception:
        return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    state = os.environ.get("TELEGRAM_STATE_DIR")
    if not state or not os.path.isdir(state):
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}


    if os.path.exists(os.path.join(state, "reply_guard_off")):
        return

    turn_f = os.path.join(state, "reply_guard_turn")
    sent_f = os.path.join(state, "reply_guard_sent")
    blocked_f = os.path.join(state, "reply_guard_blocked")

    if mode == "prompt":
        _write(turn_f, uuid.uuid4().hex)
        _rm(sent_f)
        _rm(blocked_f)

    elif mode == "reply":
        tid = _read(turn_f)
        if tid:
            _write(sent_f, tid)

    elif mode == "stop":
        if data.get("stop_hook_active"):
            return
        tid = _read(turn_f)
        if not tid:
            return
        if _read(sent_f) == tid:
            return
        if _read(blocked_f) == tid:
            return
        if not _substantive(data.get("transcript_path") or ""):
            return
        if _cooldown_until(state) > 0:
            return
        _write(blocked_f, tid)
        print(json.dumps({"decision": "block", "reason": BLOCK_REASON}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
