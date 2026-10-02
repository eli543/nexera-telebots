---
name: fleet-ops
description: Manage an existing Telebots fleet on macOS, including adding and pairing a bot locally, inspecting health and logs, parking watchdog recovery, and explaining feature switches. Uses installed engines and the configured bot list.
---

# Fleet operations

Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}` and use `"$FLEET_HOME/bin/<engine>"`. Read `$FLEET_HOME/fleet.json`; never infer a state directory from a bot's name or a fixed list. This product requires macOS 14 or newer and Terminal.app. If the engines are missing, finish the documented installer first.

## Add one bot

Do this from an attended local session, never by approving a pairing from Telegram.

1. You do this: create one bot in @BotFather with /newbot. Choose its name and a username ending in bot. Keep the token out of the assistant conversation.
2. Run `"$FLEET_HOME/bin/fleet-add-bot" <key> --name "<name>" --emoji "<emoji>"`, using a unique key matching `^[a-z0-9][a-z0-9-]{0,30}$`.
3. You do this: paste the token into the hidden Mac dialog, or type it at the engine's hidden Terminal prompt. The engine validates it, checks for duplicates, writes its state at mode 600, and appends the config entry last. Never write a token with an assistant-authored shell command or paste it into /telegram:configure.
4. Run `"$FLEET_HOME/bin/fleet-start" <key>`. You do this: accept the first-launch folder trust prompt in the new Terminal window, and the permissions warning if full stance was explicitly selected.
5. You do this: DM the bot and read the six-character pairing code. Run `"$FLEET_HOME/bin/fleet-pair" <key> <code> --lock` locally. This updates that bot's allowlist and writes its .enrolled marker.
6. Send hi from the phone, run `"$FLEET_HOME/bin/fleet-checklist" <key>`, and complete its phone checks. An existing watchdog picks up the new bot only after enrollment. No changes to other bots are needed.

BotFather can allow a few new bots and then ask you to wait. Finish one before making another; waits can be long and Telegram does not publish the throttle rule. Community references report 20 bots per account, or 40 with Premium. Treat a BotFather refusal as the current limit rather than trying to bypass it.

## Inspect first

- `"$FLEET_HOME/bin/fleet-doctor"` reports health and setup gaps.
- `"$FLEET_HOME/bin/fleet-state" --json` reports the configured sessions.
- `"$FLEET_HOME/bin/watchdog.sh" --dry-run` explains the next recovery decisions without restarting bots.
- Watchdog logs live in `$FLEET_HOME/logs/`. Poller and per-bot engine logs live under the configured `<state_dir>/logs/`; look for poller-events.log, fresh.log, update.log, compact.log, esc.log, context.log and usage.log.
- `$FLEET_HOME/overlay.status` records overlay health. A different official plugin version pauses extras safely. Do not force an old overlay onto it or restart repeatedly to try to fix version drift.

The menu comes from `$FLEET_HOME/ux.json`: esc, fresh, update, key, inbox, opus, fable, model, effort, context, compact, usage, resume. It registers on poller start. For a stale menu, check the overlay and logs before requesting a restart; /fresh clears the conversation, while /update resumes it.

## Permissions and recovery

Use `"$FLEET_HOME/bin/fleet-checklist" --consent` while the user is at the Mac to request Terminal Automation and Accessibility access. A phone cannot approve those dialogs. Do not claim /esc or /compact worked without their reported evidence.

The permission stance is fleet-wide. safe uses ordinary prompts; scoped uses Claude Code `--permission-mode auto`; full uses `--dangerously-skip-permissions` and must be selected explicitly. A mixed stance can hold peer messages for approval. Never change stance as a workaround for a blocked task.

To park automatic recovery, create the configured state's `stopped` marker. Remove only that marker to resume watchdog coverage. Parking does not stop a running session. State changes require the user's direction; never signal a process based only on its name. Use the provided engine for a requested restart and verify its result.

Token rotation is attended. The user revokes the old token in @BotFather, enters the replacement through a local hidden-input procedure, and requests a history-preserving restart. Never print, copy into chat, or include a token in command arguments. Do not invent an unsupported fleet-add-bot replacement flag; inspect the installed engine's help first.

## Per-bot switches

Read the selected state directory before changing anything. Create or remove only the requested marker:

| Marker | Effect |
|---|---|
| stream.off | Disable the live box |
| ack.off | Disable the instant acknowledgement reaction |
| tldr_hook_off | Disable the structured reply check |
| tldr_judge_off | Disable only the optional API judge |
| reply_guard_off | Disable the reply delivery guard |
| autotranscribe.off | Disable automatic voice transcription |
| rich-stream | Enable rich live-box transport with plain fallback |
| guardian-thresholds | Override this bot's context thresholds as comma-separated percentages |

The optional document-delivery switch is `$FLEET_HOME/deliver-nudge.off`. Hooks do nothing outside a Telegram bot session. Optional AI judging is off unless configured; no API key is needed for the normal reply structure checks.

Never test against a live state directory. Use the project's scrubbed sandbox harness, an isolated HOME and fake credentials. A second poller with the same token can disconnect the real bot.
