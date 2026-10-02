---
description: Preview and perform an approved Telebots uninstall using the installed uninstaller, preserving bot state unless purge is explicitly requested.
allowed-tools: Bash, Read
---

Use `"${FLEET_HOME:-$HOME/.claude/telegram-fleet}/uninstall.sh" --dry-run` to show the concrete teardown first. Require that installed file; if it is missing, locate the real clone and use its verified absolute uninstall.sh path. Never assume the current directory is the clone.

Explain what the preview will remove, including the watchdog, optional voice agent, plugins and installed engines, and that stock official-plugin code is restored. Confirm the user's requested scope before the real run. Keep per-bot tokens, allowlists and transcripts unless the user explicitly approves the purge option. Read the script's actual output and help; do not claim a resource was removed unless checked.

The optional purge removes state and tokens. It is a separate destructive choice, not a default. Never remove unrelated official-plugin setups. Removing files does not revoke a Telegram token; the user can revoke it in @BotFather if needed. Never kill processes by name pattern.
