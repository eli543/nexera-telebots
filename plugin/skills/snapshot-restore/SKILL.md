---
name: snapshot-restore
description: Preserve the configured Telebots conversations before a planned Mac restart and restore them after login. Use when the user asks to keep bot sessions across a reboot or return to saved sessions afterward.
---

# Snapshot and restore

Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}`. Use the installed engines from a plain attended Terminal session, not a bot that a restore will restart. A saved transcript preserves conversation history; it does not keep an interrupted action or a session-only watch running.

Before a planned restart, run `"$FLEET_HOME/bin/fleet-snapshot"`. It records current session ids in `$FLEET_HOME/restart-state/snapshot.tsv` and parks watchdog recovery for the transaction. Check the exit code and every result before saying it saved successfully. `--dry-run` writes a preview snapshot and parks nothing. For a save point without parking recovery, the installed engine supports `--keep-watchdog`.

Let the user perform the reboot. After login, run `"$FLEET_HOME/bin/fleet-restore"`, or preview with `--dry-run`. It validates the snapshot, resumes recorded sessions, preserves intentional manual stops, checks session and poller health, and archives that generation only when all required bots are back. An incomplete result keeps the snapshot for retry; do not delete it or claim a full restore.

If no usable snapshot exists, say so. An ordinary `"$FLEET_HOME/bin/fleet-start" <key>` is a separate start request and does not prove that the exact previous conversation resumed. Never substitute /fresh for a restore.

Report each bot's actual result, including failed or pending recovery. A resumed `⚡ Back and ready` notice with Continue lets the user resume unfinished work. Continue preserves all pending approval requirements; it is not fresh consent to send an email or perform another gated action.
