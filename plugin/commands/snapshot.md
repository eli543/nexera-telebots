---
description: Save exact Telebots session ids and park watchdog recovery before a user-planned Mac restart.
allowed-tools: Bash, Read
---

From an attended local Terminal session, run `"${FLEET_HOME:-$HOME/.claude/telegram-fleet}/bin/fleet-snapshot"` when the user has requested a pre-reboot snapshot. Read the snapshot-restore skill for the transaction and result handling. Do not reboot the Mac yourself.

Use `--dry-run` for a preview; it writes a preview snapshot but parks nothing. `--keep-watchdog` makes a save point while leaving recovery enabled. Explain any incomplete result and preserve the snapshot for recovery. After login, use `/telegram-fleet:restore`.
