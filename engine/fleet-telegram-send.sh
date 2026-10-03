#!/bin/bash
# Source this file and call telegram_send TOKEN CHAT TEXT [MARKUP] [TIMEOUT].
# stdout: accepted (Telegram ok:true), attempted (no acceptance proof), failed
# (transport failed or request could not be made). Only accepted returns zero.

telegram_send() {
  local token="${1:-}" chat="${2:-}" message="${3:-}" markup="${4:-}" timeout="${5:-10}"
  local base="https://api.telegram.org" response rc
  if [[ ! "$token" =~ ^[A-Za-z0-9:_-]+$ || ! "$chat" =~ ^-?[0-9]+$ || ! "$timeout" =~ ^[1-9][0-9]?$ ]]; then
    printf 'failed\n'
    return 2
  fi
  if [[ "${TELEBOTS_TEST:-0}" == 1 && "${TELEBOTS_API_BASE:-}" =~ ^http://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?/?$ ]]; then
    base="${TELEBOTS_API_BASE%/}"
  fi
  local args=(--data-urlencode "chat_id=$chat" --data-urlencode "text=$message")
  [[ -n "$markup" ]] && args+=(--data-urlencode "reply_markup=$markup")
  response=$(printf 'url = "%s/bot%s/sendMessage"\n' "$base" "$token" | \
    curl --config - -s --max-time "$timeout" -X POST "${args[@]}" 2>/dev/null)
  rc=$?
  if [[ "$rc" != 0 ]]; then
    printf 'failed\n'
    return 2
  fi
  if printf '%s' "$response" | python3 -c 'import json,sys
try: sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)
except (ValueError,TypeError): sys.exit(1)'; then
    printf 'accepted\n'
    return 0
  fi
  printf 'attempted\n'
  return 1
}
