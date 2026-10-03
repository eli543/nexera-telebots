# End-of-setup feature checklist

Run `~/.claude/telegram-fleet/bin/fleet-checklist <key>` after the bot is paired and has answered `hi`. The checker runs automated rows first, then gives one phone action at a time. It waits for poller, engine and hook evidence, rather than accepting a verbal claim that a feature worked. Results appear in Terminal, in `$FLEET_HOME/checklist-<key>.json`, and as one Telegram summary. An assisting Claude can use `--non-interactive --wait 120` and guide the person through each phone action.

`Passed` means the named evidence was observed. `Failed` means an automated check found a problem. `Needs your action` means a person must complete or repair a step. `Not configured` names an optional feature that is off or unavailable. A Mac process being alive is not proof that the phone received a reply. The person must complete the phone steps; an offline sandbox cannot replace them.

Before using the keyboard controls, run `fleet-checklist --consent` while the person is at the Mac. You do this: grant macOS Automation and Accessibility prompts. A prompt can block a bot while remaining invisible on the phone.

## Automated rows

| Row | Check | Evidence |
|---|---|---|
| A1 | Official Telegram plugin 0.0.7 and overlay | `verify-overlay.sh --status` returns `OK` |
| A2 | Telebots plugin, UX table and engines | Plugin enabled, `ux.json` present, engine files executable |
| A3 | Token valid and unique | Telegram `getMe` succeeds; no other configured bot has the token; only @username printed |
| A4 | Session, poller and heartbeat | Exact PIDs are live, heartbeat younger than 60 seconds |
| A5 | Telegram update subscriptions | `getWebhookInfo` lists message, callback query and message reaction |
| A6 | Menu | `getMyCommands` lists all 13 private-chat commands in order |
| A7 | Access locked | Allowlist policy and `.enrolled` marker present |
| A8 | Local voice | A Mac-generated sample transcribes to expected words; Not configured if no voice backend is installed |
| A9 | Watchdog | Loaded and dry-run pass lists this bot as healthy; Not configured if not installed |
| A10 | Claude Code and handoff settings | Version gates, inbound messaging and a uniform permission stance |
| A11 | Optional layers | `fleet-email test` and Telebots Bar status when configured; otherwise Not configured |

## Phone rows

| Row | You do this | Evidence |
|---|---|---|
| P1 | Send `hi` | Acknowledgement, 🎯 work box and reply; inbound and streamer records |
| P2 | Tap `Checklist OK` | Tap event and a 👍 reaction only |
| P3 | Add ❤️ to a bot message | New reaction event and a dated file in your notes folder |
| P4 | Send `/key` | Legend arrives and KEY event is logged |
| P5 | Open `/model`, tap Close; open `/effort`, tap Close | Picker and Close events for both |
| P6 | Open `/resume`, check a description, tap Close | Render log includes at least one non-empty topic label and Close |
| P7 | Send a voice note | Transcript status `ok`; Not configured if voice is off or no local backend is installed |
| P8 | Send `/update`, inspect the restart message | `⚡ Back and ready` and a Continue bubble; update and fresh logs |
| P9 | Start a slow task, then send `/esc` | Verified Escape reaches this bot's Terminal window; esc log |
| P10 | Send `/context` and `/usage` | Both reports arrive; separate engine logs |
| P11 | Send `/compact` | Compaction starts or says `Not enough messages to compact`; compact exit 0 or 14 |
| P12 | Follow the context nudge, tap Keep going | Temporary 1 percent threshold, guardian and tap events; original threshold restored |
| P13 | With two bots, open Hand off and tap Cancel | Picker lists the other bot only; no note delivered; Not configured with one bot |
| P14 | With email configured, open `/inbox`, tap Snooze 1d | Card and snooze evidence; otherwise Not configured |
| P15 | If you choose a crash test, approve `--crash-test` | Exact session PID stopped, watchdog recovers, `⚡ Back and ready` arrives; otherwise Not configured |
| P16 | If you choose a blank restart, rerun with `--blank-fresh` and send `/fresh` last | New blank session and back-ready notice; otherwise Not configured |

The checker lowers the guardian threshold for P12 only long enough to trigger a nudge and restores it afterward. P15 is deliberately optional because it stops this bot's current session. It must identify the exact bot and PID and ask before acting. P16 is last because it clears context.

If Telebots Bar is installed for acceptance, also test Reconnect on a deliberately stopped bot, Fresh start, Snapshot and Restore from the panel, and the phone page over Tailscale if enabled. These are real Mac and phone checks, separate from the automated row that only finds the app running.

## When a row fails

Keep the JSON and the bot's `<state_dir>/logs/` directory. `poller-events.log` records timestamps and event names without message text or tokens. Engine logs give the restart, compact, voice and session-label results. Start with `fleet-doctor`, then use [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the matching failure. Repeat the failed row after fixing it; do not mark it passed from a log that predates the fix.
