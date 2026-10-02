#!/bin/bash
# Offer a handoff or compaction when the configured context thresholds are crossed.
# Exit 0 on every path. CC_GUARDIAN_DRYRUN=1 prints the payload without sending.
[ -n "${TELEGRAM_STATE_DIR:-}" ] || exit 0
exec /usr/bin/python3 "$(cd "$(dirname "$0")" && pwd)/context-guardian.py" "$@"
