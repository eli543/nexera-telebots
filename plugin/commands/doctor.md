---
description: Run the fleet health check and interpret it. Checks Claude Code version, Bun, the official plugin, fleet.json, and per-bot liveness. Use when the user asks whether their Telegram fleet is healthy, why a bot is down, or after a Claude Code update.
allowed-tools: Bash, Read
---

Run `$FLEET_HOME/bin/fleet-doctor` (`$FLEET_HOME` defaults to
`~/.claude/telegram-fleet`) and interpret the result for the user.

The doctor prints a green/red table covering:

- Claude Code version against the minimum (2.1.80) for channels.
- Bun present.
- The official `telegram@claude-plugins-official` plugin installed.
- `fleet.json` valid and parseable.
- Per bot: state directory present, `.env` at mode 600, `access.json` policy,
  and whether the session and poller look alive with a fresh heartbeat.

It makes no changes and sends no messages by default. If the user wants a live
send test, run `$FLEET_HOME/bin/fleet-doctor --ping`, which sends one test
message per bot.

After running it:

- Summarize what is green and what is red, in plain language.
- For any red row, give the specific fix. Common ones: Bun missing (install from
  bun.sh), a stale heartbeat (the poll loop is dead even though the process is
  up, so restart the bot), version drift after a Claude Code update (check the
  supported-versions table in the README), or a 409 conflict (two pollers on one
  token, see docs/TROUBLESHOOTING.md).
- If the doctor exits non-zero, at least one check is red; say which.
