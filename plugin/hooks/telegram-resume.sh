#!/bin/bash
# Show the session picker or reopen a numbered item. Exit 2 only for handled prompts.
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0
exec /usr/bin/python3 "$(cd "$(dirname "$0")" && pwd)/telegram-resume.py" "$@"
