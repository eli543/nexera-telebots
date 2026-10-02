---
description: Restore saved Telebots conversations after Mac login and report verified recovery per bot.
allowed-tools: Bash, Read
---

From a plain attended Terminal session after login, run `"${FLEET_HOME:-$HOME/.claude/telegram-fleet}/bin/fleet-restore"` for the user's requested restore. Never run it from a bot that it will restart. Read the snapshot-restore skill for result handling. `--dry-run` previews without starting bots.

Report each actual result. The engine archives a snapshot generation only when its recovery checks pass. Keep an incomplete snapshot for retry. Without a valid snapshot, say exact restoration is unavailable; `fleet-start <key>` is a separate start operation, not proof of a resumed conversation.
