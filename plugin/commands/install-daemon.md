---
description: Install the Telebots launchd watchdog after at least one bot has been paired and locked. Uses the installed plist template and shows every system change before it runs.
allowed-tools: Bash, Read
---

This is an attended macOS setup operation. Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}`. Require Darwin, the installed `$FLEET_HOME/bin/watchdog.sh` and watchdog-loop.sh, and `$FLEET_HOME/templates/com.claude-fleet.watchdog.plist.template`. If any is absent, finish the installer first; never resolve a template relative to the current working directory.

Read fleet.json and require at least one correctly paired bot: its state has .enrolled and its access policy is allowlist. The watchdog manages only enrolled bots and skips a state with a stopped marker. Do not create enrollment markers to bypass pairing.

Show the rendered plist and exact commands. Explain that it will start at login and recover enrolled bots while the Mac is awake and logged in. Get the user's approval if they have not already approved these changes. Warn before any Mac permission prompt.

Use a plist parser when replacing __FLEET_HOME__; raw sed replacement breaks paths containing an ampersand. A safe rendering sequence is:

```bash
export FLEET_HOME="${FLEET_HOME:-$HOME/.claude/telegram-fleet}"
python3 - "$FLEET_HOME" <<'PYPLIST'
import os
from pathlib import Path
import plistlib
import sys

fleet = str(Path(sys.argv[1]).expanduser().resolve())
source = Path(fleet) / "templates/com.claude-fleet.watchdog.plist.template"
with source.open("rb") as handle:
    data = plistlib.load(handle)
def replace(value):
    if isinstance(value, str):
        return value.replace("__FLEET_HOME__", fleet)
    if isinstance(value, list):
        return [replace(item) for item in value]
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    return value
out = Path.home() / "Library/LaunchAgents/com.claude-fleet.watchdog.plist"
out.parent.mkdir(parents=True, exist_ok=True)
os.umask(0o077)
with out.open("wb") as handle:
    plistlib.dump(replace(data), handle)
PYPLIST
launchctl bootout "gui/$(id -u)/com.claude-fleet.watchdog" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.claude-fleet.watchdog.plist"
```

Afterward, check `launchctl print "gui/$(id -u)/com.claude-fleet.watchdog"`, run the watchdog's `--dry-run`, and inspect `$FLEET_HOME/logs/watchdog.log`. Confirm only what those results prove. A loaded agent without a healthy enrolled bot is not proof of recovery. Never use a broad process kill.
