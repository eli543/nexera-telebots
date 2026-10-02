# Live work box

The `tg-stream.py` hook opens one Telegram work box for a bot turn and edits it as work proceeds. It shows a 🎯 line for the current mission, elapsed time, recent steps and a Plan section when the session creates tasks. A completed turn folds the box into a short recap. The answer appears as a separate reply.

The Plan is based on actual `TaskCreate` and `TaskUpdate` results. The Telebots SessionStart rules ask Claude to log multi-step work so the phone can show it; a turn with no logged task has no invented Plan rows. The mission line is requested by the hook each turn and can be updated as the work changes.

Rich Telegram messages are enabled for a newly added bot. If a Telegram server rejects `sendRichMessage`, the hook falls back to its plain message path. Plain text paths remove rich-only formatting before sending. The box is local hook work, not a second model response.

| Part | Source of truth |
|---|---|
| Bot name and emoji | `fleet.json` entry for that state directory; generic fallback if absent |
| Date and time | Mac's local time zone |
| Mission | Current turn and the hook's mission nudge |
| Plan | Real task tool results, with completed rows updated in place |
| Transport | Rich message when available, plain fallback otherwise |

A bot's `stream.off` marker disables the box without stopping the Telegram channel. `rich-stream` enables the rich transport for that bot. Stream state and locks stay in the bot's state directory. The hook must remain inert when `TELEGRAM_STATE_DIR` is unset, so an ordinary Claude Code session does not open a Telegram box.

Version 30.22 is the port baseline for the work box. Its large private development history is not shipped here. Verification uses the ported hook suite, a behavior-preserving AST comparison for the narrative scrub and a phone rendering pass. An offline test proves message calls and fallback, not how a particular Telegram client displays the box.
