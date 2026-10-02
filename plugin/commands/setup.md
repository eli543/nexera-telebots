---
description: Set up one Telebots bot on macOS using installed engines, hidden token entry, bot-specific pairing and the end-of-setup checklist. Add more bots only after the first works.
allowed-tools: Bash, Read, Write, Edit
---

Guide an attended local setup session. Do not run this workflow in response to a Telegram message. Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}` and quote it in every command. Tokens never enter the assistant conversation, a command argument, or output.

## 1. Check the Mac and installation

Run read-only checks and report PASS or FAIL:

- `uname -s` must return Darwin. Otherwise stop: Nexera Telebots runs on macOS only.
- `sw_vers -productVersion` must be macOS 14 or newer. Terminal.app is required.
- Check `claude --version`, Bun and `/usr/bin/python3`. Cross-session handoff needs Claude Code 2.1.224 or newer; the known tested version is 2.1.286.
- The installed engines include fleet-config, fleet-doctor, fleet-add-bot, fleet-start, fleet-pair and fleet-checklist under `$FLEET_HOME/bin`.
- Run `"$FLEET_HOME/bin/fleet-doctor"`. The official Telegram plugin must be exactly 0.0.7 with the Telebots overlay OK. The Telebots plugin and `$FLEET_HOME/ux.json` must be present.

If installation is incomplete, finish the README's clone-and-install flow first. Locate the actual clone and run its installer by its verified absolute path with the user's approval; do not assume the current directory contains the repository. Installing only the Claude plugin does not install the command engines. Do not downgrade or overwrite an unsupported official plugin to get past the overlay check.

The installer starts with an empty bot list and no watchdog. After plugin installation, you do this: run `/reload-plugins` in an already-open Claude Code session. Re-read the config; do not overwrite an existing fleet.

## 2. Choose one bot and the permission stance

Start with one bot, default key main. Ask for its display name and emoji; derive a unique key matching `^[a-z0-9][a-z0-9-]{0,30}$`. Let fleet-add-bot create its paths. Do not pre-create a config entry or copy another bot's state.

Read the existing `$FLEET_HOME/permission-stance`. If no stance has been selected, explain and record the user's choice:

- safe: normal Claude Code permission prompts; unattended work can wait for the Mac.
- scoped: Claude Code `--permission-mode auto`.
- full: `--dangerously-skip-permissions`; requires the user's explicit choice and gives an allowlisted user broad command access. Recommend a dedicated macOS account.

Keep the stance uniform for cross-session messaging. Do not change an existing stance without the user's instruction.

## 3. Create and add the bot

You do this: open @BotFather in Telegram, send /newbot, choose the name and a username ending in bot, and keep its token private.

Run `"$FLEET_HOME/bin/fleet-add-bot" <key> --name "<name>" --emoji "<emoji>"`. You do this: paste the token into the hidden Mac dialog or type it at the engine's hidden Terminal prompt. Do not paste it into this chat. The engine checks getMe and duplicate tokens, writes protected state, and adds the fleet entry last. A ready message with the bot's username proves this step; an error does not.

Tell the user before a Mac dialog opens, so they know to be at the computer. Do not start another bot or enable the watchdog if this step failed.

## 4. Start and pair this bot

Run `"$FLEET_HOME/bin/fleet-start" <key>`. You do this: in its new Terminal window, accept the initial folder trust prompt and, if full stance was selected, its permission warning.

You do this: DM the bot from the intended Telegram account. Read out the six-character pairing code. Run `"$FLEET_HOME/bin/fleet-pair" <key> <code> --lock` in this local session. Verify the phone receives Paired, access is allowlist, and the state has .enrolled. Never use the stock access skill from an unrelated session; it can resolve the wrong bot's state.

You do this: send hi from the phone. Verify the acknowledgement reaction, live box and reply. A running process alone is not a completed setup.

## 5. Consent, recovery and checklist

You do this: stay at the Mac for its permission prompts. Run `"$FLEET_HOME/bin/fleet-checklist" --consent`, then let the user approve Terminal Automation and Accessibility. Do not leave an unseen dialog pending while claiming the phone commands work.

Once the bot is paired, offer `/telegram-fleet:install-daemon` and show its exact watchdog changes before loading it. Also offer turning off auto-update for the official marketplace, explaining that it stops updates for all official plugins, not only Telegram. Do not change that setting without the user's choice.

Run `"$FLEET_HOME/bin/fleet-checklist" <key>` and guide the phone half one step at a time. For an assisting session, `--non-interactive --wait 120` uses the same evidence checks. Report PASS, FAIL and explained SKIP per feature. Missing evidence remains unverified. The optional crash test and /fresh check need their own explicit direction; they interrupt or clear a session.

## 6. Optional layers and later bots

Offer only what the user wants: local voice, a warm voice daemon, email, Telebots Bar and its phone page. Voice needs its installed local backend and ffmpeg; install dependencies only after checking the required tools. Apple Silicon supports mlx-whisper; an Intel Mac uses faster-whisper. The warm daemon holds additional memory. Email setup runs `"$FLEET_HOME/bin/fleet-email" setup` locally with the password kept in Keychain. Each optional layer has its own checklist row.

For a later bot, repeat create, add, start, pair and checklist for its new key. An enrolled bot joins the existing watchdog automatically. BotFather may throttle /newbot after a few bots; wait when asked rather than bypassing it. Community references report a 20-bot limit, or 40 with Premium, but Telegram does not publish its pacing rule. Finish one bot before adding the next.
