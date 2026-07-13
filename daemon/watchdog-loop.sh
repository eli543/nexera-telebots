#!/bin/bash
# watchdog-loop.sh - keep-alive wrapper that runs one watchdog pass every 60s
# inside a single long-lived process.
#
# Why a loop instead of a launchd StartInterval timer: interval timers can
# silently stop firing after a system sleep (timer coalescing), leaving the fleet
# with no auto-recovery for hours. This wrapper keeps the cadence in a normal
# process; the launchd job runs it with KeepAlive=true, so if the wrapper ever
# dies launchd brings it right back. sleep is suspended during system sleep and
# resumes on wake, so the first pass after a long sleep fires promptly.
#
# Env: FLEET_HOME (default ~/.claude/telegram-fleet).

set -u

FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

# watchdog.sh sits beside this file (both installed into $FLEET_HOME/bin).
if [ -x "$FLEET_HOME/bin/watchdog.sh" ]; then
  WATCHDOG="$FLEET_HOME/bin/watchdog.sh"
else
  WATCHDOG="$SELF_DIR/watchdog.sh"
fi

while true; do
  /bin/bash "$WATCHDOG"
  sleep 60
done
