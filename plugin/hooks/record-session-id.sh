#!/bin/bash
# Record the exact channel session for resume, update, and recovery. Always exits 0.
[ -n "${TELEGRAM_STATE_DIR:-}" ] && [ -d "$TELEGRAM_STATE_DIR" ] || exit 0
/usr/bin/python3 -c '
import json, os, re, sys, tempfile
try:
    data = json.load(sys.stdin)
    value = data.get("session_id") if isinstance(data, dict) else None
    if data.get("agent_id") or data.get("agentId"):
        sys.exit(0)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
        state = os.environ["TELEGRAM_STATE_DIR"]
        fd, path = tempfile.mkstemp(prefix=".session-id-", dir=state)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.replace(path, os.path.join(state, "session.id"))
except BaseException:
    pass
' >/dev/null 2>&1 || true
exit 0
