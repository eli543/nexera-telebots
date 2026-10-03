# Setup checks and acceptance

Run `~/.claude/telegram-fleet/bin/fleet-checklist <key>` after pairing and a real `hi` reply. The checker runs local checks, then gives one phone action at a time. Results appear in Terminal, `$FLEET_HOME/checklist-<key>.json` and one Telegram summary. An assisting Claude can use `--non-interactive --wait 120`.

This is a setup check, not full acceptance of every feature shown in a demo. `Passed` means the row's named evidence was recorded. `Failed` means an automated check found a problem. `Needs your action` means the result is missing or requires a direct person or phone check. `Not configured` names an optional feature that is off. An engine log proves the engine action, not that a phone received it. Complete the listed phone step before relying on the feature.

First run `fleet-checklist --consent` while the person is at the Mac. Grant Terminal Automation and Accessibility when macOS asks. These prompts may be invisible from the phone.

## Automated rows

| Row | Check | Evidence |
|---|---|---|
| A1 | Official Telegram plugin 0.0.7 and overlay | `verify-overlay.sh --status` returns `OK` |
| A2 | Plugin, menu table and engines | Plugin enabled, `ux.json` present, required engines executable |
| A3 | Token valid and unique | Telegram `getMe` succeeds; no duplicate configured token |
| A4 | Session and poller | Session and poller up; heartbeat younger than 60 seconds |
| A5 | Telegram update subscriptions | `getWebhookInfo` lists messages, callbacks and reactions |
| A6 | Menu | `getMyCommands` lists the 13 commands in order |
| A7 | Access locked | Allowlist policy and `.enrolled` marker |
| A8 | Local voice | Generated sample transcribes; `Not configured` without a local backend |
| A9 | Watchdog | Loaded and its dry run succeeds; `Not configured` if absent |
| A10 | Claude Code handoff gates | Version, inbound setting and permission stance |
| A11 | Optional layers | `fleet-email test` and Telebots Bar status when present |

## Phone rows

| Row | You do this | Recorded evidence and limit |
|---|---|---|
| P1 | Send `hi` | Inbound event, acknowledgement, live box and reply guard. Read the reply on your phone. |
| P2 | Tap `Checklist OK` | Tap event. Confirm the bot adds only a 👍 reaction. |
| P3 | Add ❤️ to a bot message | Reaction event and a new dated notes file. |
| P4 | Send `/key` | Key event. Read the legend on your phone. |
| P5 | Open and close `/model` and `/effort` | Picker and Close events for both. |
| P6 | Open `/resume` and tap Close | Recent chats with topic labels and Close event. |
| P7 | Send a voice note | Voice and transcription records; `Not configured` if voice is off or missing. |
| P8 | Send `/update` | Installed build reload and restart proof. Confirm `Back and ready` on your phone. `/update` downloads nothing. |
| P9 | Send `/esc` during a slow task | The verified Terminal window receives Escape; `$FLEET_HOME/logs/esc.log` records `sent Esc`. |
| P10 | Send `/context` and `/usage` | Both intercept events and engine logs. Read the reports on your phone. |
| P11 | Send `/compact` | `$FLEET_HOME/logs/compact.log` records delivery or `Not enough messages to compact`. |
| P12 | Follow a context nudge and tap Keep going | Guardian and tap events. The temporary threshold is restored. |
| P13 | With two bots, complete a handoff | The second bot must receive and acknowledge the note. The picker alone does not prove delivery; without a linked destination receipt this row says `Needs your action`. With one bot it says `Not configured: needs two bots`. |
| P14 | With email configured, open a complete `/inbox` message, reply to the right recipients and confirm the exact preview twice | Confirm the card, full message and SMTP result. The current logs do not link the read to the send, so this row says `Needs your action`. Without email it says `Not configured`. |
| P15 | If you choose `--crash-test`, approve the exact PID at the Mac | Watchdog and quiet restart receipts. Send a new phone message to verify delivery. A chat active within 30 minutes resumes; an older chat starts blank. Without the test this row says `Needs your action`. A reboot is separate. |
| P17 | Switch model and effort | Model-state change, restart receipt and the same conversation ID before and after. Check the phone reply. |
| P18 | Choose a different chat in `/resume` | Resume pick, reopen receipt and selected conversation ID. Check that its history is present on the phone. |
| P16 | Last, optionally use `--blank-fresh` and send `/fresh` | A new conversation ID, reset model and restart proof. Without the option the row says `Needs your action`. Save current work first. |
| P19 | Snapshot, reboot, log in, restore and send a phone message | Always `Needs your action` until a real reboot and round trip. Local status cannot prove it. |
| P20 | When Telebots Bar and Tailscale are installed, reconnect from the phone with the bots down | Always `Needs your action` until a real phone rescue. Otherwise `Not configured`. |

P15 can stop the selected bot. It requires a person at the Mac, identifies the exact session PID and asks before signaling it. P16 is last because it clears context. Do not run either as a casual setup probe. The normal checklist leaves them untested.

## When a row needs work

Keep the JSON result and the matching logs. Poller events and voice records live under the bot's `<state_dir>/logs/`. Engine restart, Escape, compact, context, usage, session, model and watchdog logs live under `$FLEET_HOME/logs/`; update records live under `<state_dir>/logs/update.log`. Start with `fleet-doctor`, then use [TROUBLESHOOTING.md](TROUBLESHOOTING.md). A prior log line cannot make a new test pass.
