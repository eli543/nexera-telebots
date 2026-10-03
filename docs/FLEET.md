# One bot or a fleet

One bot is a complete installation. More bots give you separate Telegram chats and Claude Code sessions with the same Telebots commands, hooks and rules. Choose your names and count, within Telegram's bot limits and the Mac's capacity.

Each bot has its own BotFather token, Terminal.app window, working directory, state directory, access list and transcript history. Do not point two sessions at one token. You can give bots the same project context by linking their workdirs to a shared `CLAUDE.md` or common notes, but that shared knowledge is your choice and is separate from their tokens and state.

## Add another bot

Finish the first bot and its checklist before starting the next. Then:

1. You do this on your phone: create the next bot in [@BotFather](https://t.me/BotFather) and keep the token private.
2. Run `~/.claude/telegram-fleet/bin/fleet-add-bot <key> --name <name> --emoji <emoji>` on the Mac. You do this: paste the token into the hidden dialog. The command validates it before adding the entry to `fleet.json`.
3. Run `~/.claude/telegram-fleet/bin/fleet-start <key>`. You do this at the Mac: accept the new folder trust prompt in that bot's Terminal window.
4. You do this on your phone: DM that bot, match its code to the Pair/Ignore dialog on the Mac and click Pair. If the dialog is unavailable, run `~/.claude/telegram-fleet/bin/fleet-pair <key> <code> --lock` locally.
5. Send `hi` and run `~/.claude/telegram-fleet/bin/fleet-checklist <key>`. Complete the requested phone steps. The existing watchdog starts supervising the new bot only after enrollment.

Do not hand-edit `.env` or create a config entry while waiting for a BotFather token. BotFather may delay further `/newbot` requests. Add one bot at a time.

## Config and paths

`~/.claude/telegram-fleet/fleet.json` is the one bot list. Version 2 has a `bots` array and options for watchdog, voice, notes, rich messages, context thresholds and optional email. Version 1 remains readable with defaults. The installer starts version 2 with an empty `bots` array.

A key must match `^[a-z0-9][a-z0-9-]{0,30}$` and be unique. The default state directory pattern is `~/.claude/channels/fleet-<key>/`. The engine validates resolved workdirs and state dirs to avoid collisions and ambiguous PID checks. Use `fleet-config validate` after a manual config change. Normal setup uses `fleet-add-bot` to make those entries safely.

## Resume and handoff

`/resume` lists up to 20 recent chats across configured bots, newest first. Each row identifies its source bot and gives a short label based on the conversation's opening topic. Without an optional AI key, a local heuristic builds the label from the first substantive request, including a transcribed voice note. It does not label every row with a raw session ID. Tap a row to reopen it in the bot you are chatting with; the source bot's session is left in place. The list is refreshed when you ask for it, and stale selections are refused.

With two or more bots, a context nudge can offer Hand off. The picker excludes the current bot and resolves the other bots from `fleet.json` when tapped. The sending bot prepares a self-contained note, shows it for review, and sends it through Claude Code's same-user cross-session messaging only after confirmation. The recipient session name is `telebots-<key>`. If it is down, the note stays local for retry. Handoff needs Claude Code 2.1.224 or later and a uniform permission stance so a message is not held for a terminal approval. A handoff never counts as approval for access, money or email.

## Availability

The optional watchdog can restart an enrolled bot whose session or poller dies, subject to its cooldown and breaker. It skips parked bots. It resumes the previous conversation only when that transcript was active within 30 minutes; otherwise the bot starts blank. Recovery is quiet. Suspected stalls are logged locally. Phone stall alerts are off by default; set `options.stall_phone_alerts` to `true` in `fleet.json` for one alert per suspected stall. A stall alert never restarts the bot. A Mac that is asleep, logged out or offline cannot receive or answer Telegram updates. For a planned reboot, snapshot first and restore after login. Prove recovery with a real message round trip, not only a process badge.
