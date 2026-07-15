---
description: Guided setup for nexera-telebots. Runs preflight checks, writes fleet.json, creates per-bot state directories and tokens, sets the permission stance, and offers the daemon, patches, and voice. Use when the user wants to set up or reconfigure their Telegram fleet.
allowed-tools: Bash, Read, Write, Edit
---

You are guiding the user through setting up nexera-telebots. Work in
steps, confirm before any system change, and never print a bot token back to the
user or into a log. `$FLEET_HOME` defaults to `~/.claude/telegram-fleet`.

## Step 1: Preflight

Run these read-only checks and report a short pass/fail list:

- macOS: `uname -s` is `Darwin`. If not, tell the user the daemon layer will not
  work here and continue with the plugin and hooks only.
- Claude Code version: `claude --version`, needs 2.1.80 or newer for channels.
- Bun: `command -v bun`. If missing, point them at `https://bun.sh` and note the
  channel cannot connect without it.
- Apple Silicon for local voice: `uname -m` is `arm64`. If not, voice will use
  faster-whisper or stay off.
- Official plugin: check whether `telegram@claude-plugins-official` is installed.
  If not, offer to run `claude plugin marketplace add anthropics/claude-plugins-official`
  then `claude plugin install telegram@claude-plugins-official`.
- Engine installed: `test -x $FLEET_HOME/bin/fleet-config`. If this fails, the
  engine is not in place yet - installing the plugin alone does not lay it down.
  Have the user run the installer first (`./install.sh` from a clone of this
  repo), then come back. Steps 2 and 6 below call `$FLEET_HOME/bin/fleet-config`
  and `$FLEET_HOME/bin/fleet-launch`, which exist only after the installer runs.

## Step 2: Write fleet.json

Ask how many bots (default 1). For each bot, collect a display name, derive a
`key` matching `^[a-z0-9][a-z0-9-]{0,30}$`, a `workdir` (default
`~/claude-fleet/<key>`), a `state_dir` of `~/.claude/channels/fleet-<key>`, and
an emoji. Keep the `fleet-` prefix on state dirs so they never collide with the
stock `telegram` directory. Write `$FLEET_HOME/fleet.json` using the schema in
`config/fleet.example.json`. If a config already exists, read it first and edit
rather than overwrite. Validate by running `$FLEET_HOME/bin/fleet-config bots`.

## Step 3: Per-bot state directories and tokens

For each bot:

- Create the state directory: `mkdir -p <state_dir>`.
- Ask the user to paste that bot's BotFather token. Write it to
  `<state_dir>/.env` as `TELEGRAM_BOT_TOKEN=<token>` and immediately
  `chmod 600 <state_dir>/.env`. Do not echo the token, do not repeat it back, do
  not put it in any other file. Paste it with no surrounding quotes and no
  trailing space; the value is read literally, so quotes or whitespace break
  authentication.
- Seed `<state_dir>/access.json` with `dmPolicy` set to `pairing` and an empty
  `allowFrom`. The user will pair next and then switch to allowlist.

## Step 4: Permission stance

Present the three stances and write the chosen value to
`$FLEET_HOME/permission-stance`:

- safe (default): normal permission prompts. An unattended task can pause until
  the user returns to the terminal.
- scoped: a pre-allowed common read and edit set.
- full: `--dangerously-skip-permissions`. WARNING: any allowlisted user can then
  run commands on the Mac with no prompt. Recommend a dedicated macOS user or a
  scoped directory, and point the user at SECURITY.md. Do not choose this for
  them; require an explicit request.

## Step 5: Offer the daemon, patches, and voice

Offer each, one at a time, and act only on a yes:

- Watchdog daemon: run `/telegram-fleet:install-daemon` (show the exact
  launchctl commands first).
- Optional patches for mid-task `/fresh` and `/model`, plus `/esc`, `/opus`,
  `/fable`, `/context`, `/usage`, and the Telegram "/" command menu:
  `bash patches/apply-patches.sh`.
  Tell the user a plugin update wipes them and the idle-path commands work
  without them.
- Voice: on Apple Silicon, `uv tool install mlx-whisper` and `brew install ffmpeg`.
  Elsewhere, `pip install faster-whisper`.

## Step 6: Pairing walkthrough and health check

Tell the user to, for each bot:

1. DM the bot on Telegram.
2. It replies with a six-character code.
3. Run `/telegram:access pair <code>` in this terminal.
4. Lock down with `/telegram:access policy allowlist`.

Then run `$FLEET_HOME/bin/fleet-doctor` (or `/telegram-fleet:doctor`) and
summarize the green/red table. Finish by showing how to start a bot:
`$FLEET_HOME/bin/fleet-launch <key>`.
