---
description: Run the installed Telebots health check and explain each failed or skipped feature. Read-only by default.
allowed-tools: Bash, Read
---

Run `"${FLEET_HOME:-$HOME/.claude/telegram-fleet}/bin/fleet-doctor"` and report its actual rows. It checks the official plugin version and overlay, installed engines, config, token validity and duplicates, session and poller liveness, heartbeat, version gates and permission settings. Never print a token or .env contents.

Explain each failed row and its specific remedy. An unsupported plugin version, missing overlay, missing consent or absent evidence stays unresolved until checked. Do not restart a bot or change permissions merely because the doctor found a problem. If the user explicitly wants a live send test, inspect the installed help for its supported ping option and explain which bots will receive it first.

Use `/telegram-fleet:checklist` for the full phone feature checklist; the doctor alone does not prove taps, rendering, voice, handoff or real keystrokes.
