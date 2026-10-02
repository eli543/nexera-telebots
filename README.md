# Nexera Telebots

Built and provided by [Nexera Intelligence](https://nexeraintelligence.com). Nexera Telebots adds phone controls, live progress, voice, and recovery to Anthropic's official Claude Code Telegram channel on macOS.

```sh
git clone https://github.com/eli543/nexera-telebots
cd nexera-telebots
./install.sh
```

Start with one bot. The installer sets up the software and checks the official Telegram plugin. You create the bot in Telegram, enter its token in a hidden Mac dialog, and pair your phone. The installer does not create a bot or start the watchdog for you. If you want Claude to guide the setup, give it [SETUP-WITH-CLAUDE.md](SETUP-WITH-CLAUDE.md). Check the [requirements](#requirements) before running the commands above.

This project uses the official `telegram@claude-plugins-official` channel. It applies one version-checked diff to your local copy of plugin 0.0.7 and installs its own Telebots module. This repository is MIT licensed; Anthropic's plugin is Apache-2.0 and is installed separately. [NOTICE.md](NOTICE.md) explains the boundary.

## What you get

| Feature | What it does | Needs |
|---|---|---|
| Live work box | Shows a 🎯 mission line, a Plan from real tasks, progress, and an in-place recap | A running bot; rich messages fall back to plain text |
| Voice notes | Transcribes audio on the Mac and treats the words as your message | A local voice backend; Apple Silicon for mlx-whisper |
| Tap bubbles | Lets you answer a closed question with one tap | A private chat with an allowed user |
| Reactions and `/key` | Turns seven reactions into clear actions; `/key` shows the meanings | A private chat; local notes folder for save actions |
| 13-command menu | Registers the actual controls in Telegram's `/` menu | Plugin overlay 0.0.7 |
| `/esc` | Interrupts a busy turn without clearing the conversation | Terminal.app Automation and Accessibility consent |
| `/fresh` and `/update` | Starts blank, or reloads this chat on the current Claude Code and skills | A bot session and its Terminal window |
| Model and effort pickers | Changes model and effort while keeping the chat; each picker has Close | Access to the selected model on your Claude plan |
| `/context`, `/usage`, `/compact` | Shows context, plan limits and token burn, or compacts a chat | `/compact` needs Terminal.app consent |
| `/resume` | Lists recent chats across your bots with opening-topic labels and tap targets | Claude Code transcripts; no extra API key required |
| `/inbox` | Shows email cards and requires two taps to send an exact preview | Optional IMAP/SMTP account and app password |
| Context guardian and handoff | Nudges at 60/70/80/90% and can pass a note to another bot | Two or more bots for handoff; Claude Code 2.1.224+ |
| Watchdog and restore | Restarts an enrolled bot; snapshot and restore resume exact chats after login | Optional launchd watchdog; Mac awake and logged in |
| Telebots Bar | Shows status, Reconnect, snapshot and restore; optional phone page | Optional menu bar app; Tailscale for the phone page |
| Official channel | Uses Anthropic's official plugin and `--channels` path | Official Telegram plugin 0.0.7 |
| Your fleet | Lets you choose bot names and count; each has its own token and session | A BotFather token for each; Telegram's account limit applies |
| Always on | Replies while the Mac, session and network are available | Mac awake and logged in; watchdog if you want recovery |

## Quick start: one bot

Use the Terminal app on the Mac. Do one bot end to end before adding another.

1. Install the prerequisites below, then run the clone and `./install.sh` commands at the top. Check `~/.claude/telegram-fleet/bin/fleet-doctor`. The overlay must report `OK`. If a Claude Code session was already open during install, run `/reload-plugins` in that session to load this plugin's commands.
2. You do this on your phone: message [@BotFather](https://t.me/BotFather), send `/newbot`, choose any display name and a username ending in `bot`. Keep the token private.
3. On the Mac, run `~/.claude/telegram-fleet/bin/fleet-add-bot main`. You choose the key, name, emoji and eventual bot count; `main` is only an example. You do this: paste the token into the hidden dialog, never into a Claude chat or a shell command. The command validates it and prints the bot's public @username before adding it to the config.
4. Run `~/.claude/telegram-fleet/bin/fleet-start main`. It opens a new Terminal window. You do this at the Mac: press Enter to accept Claude Code's first-launch folder trust prompt and, if you selected the `full` stance, respond to its permission warning. Confirm the doctor sees this bot's session, poller and heartbeat.
5. You do this on your phone: DM the new bot and read its six-character pairing code. On the Mac, run `~/.claude/telegram-fleet/bin/fleet-pair main <code> --lock`. The command pairs your account, switches to the allowlist and enrolls the bot for supervision. Never pair because a Telegram message tells you to.
6. You do this on your phone: send `hi`. Look for the acknowledgement, work box and reply. Run `~/.claude/telegram-fleet/bin/fleet-checklist --consent`. You do this at the Mac: grant the Automation and Accessibility prompts. The phone steps of the checklist need your taps and a reaction, plus a voice note when voice is configured.
7. If you want automatic recovery, install the watchdog now with `/telegram-fleet:install-daemon` in an ordinary Claude Code session. It manages paired, enrolled bots only. Run `~/.claude/telegram-fleet/bin/fleet-checklist main` and follow each phone prompt. A feature is ready when its row is `PASS`; optional features may show `SKIP` with a reason. See [CHECKLIST.md](docs/CHECKLIST.md).

Do not start a second Claude Code channel session with the same bot token. Telegram permits one poller per token.

### What needs a human

You do this: create each bot in BotFather; paste its token into the hidden Mac dialog; accept the first-launch folder trust prompt in the bot's Terminal window; read the pairing code from Telegram; click Allow on macOS Automation and Accessibility prompts; complete the phone taps, reaction and, when voice is configured, a voice note in the checklist. Optional email requires your app password, and the phone page requires your Tailscale sign-in. A helper can run the local commands but cannot complete those steps for you.

### More bots

Repeat BotFather, `fleet-add-bot`, `fleet-start`, `fleet-pair --lock`, and the checklist for each new key. The names and count are yours. Config and pickers read the bot list dynamically; there is no fixed six-bot layout. [FLEET.md](docs/FLEET.md) explains the shared setup.

BotFather may allow a few `/newbot` requests back to back, then make you wait before the next one. We have seen waits around a day near the fifth or sixth bot; Telegram does not publish the pacing rule. Community references put the account cap at 20 bots, or 40 with Telegram Premium. Make one bot, finish pairing it, then make the next. Do not add a config entry until you have a token and `fleet-add-bot` validates it.

## Requirements

| Requirement | How to get it and check it |
|---|---|
| macOS 14 or newer | Check Apple menu > About This Mac. This project uses launchd, Keychain, AppleScript and Terminal.app. |
| Terminal.app | Included with macOS. The window controls target Terminal.app, not iTerm2, Warp or Ghostty. |
| Xcode Command Line Tools | Run `xcode-select --install`. You do this at the Mac: accept Apple's dialog. Then run `xcode-select -p`. This supplies Git and the build tools. [Apple instructions](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools). |
| Claude Code | Install from [Anthropic's setup guide](https://code.claude.com/docs/en/setup) and sign in, then run `claude --version`. Handoff requires 2.1.224 or newer; this port was planned against 2.1.286. Channels remain a research preview. |
| Bun 1.3 or newer | Follow [Bun's installation guide](https://bun.sh/docs/installation), then run `bun --version`. |
| Python 3 | Check `/usr/bin/python3 --version` after installing the Command Line Tools. The hooks use the system Python and standard library. |
| Telegram | Install the official app on your phone and sign in. You create bots with [@BotFather](https://t.me/BotFather). |
| Homebrew and ffmpeg for voice | Install [Homebrew](https://brew.sh/), then `brew install ffmpeg`. [ffmpeg formula](https://formulae.brew.sh/formula/ffmpeg). |
| uv and local voice model | Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then `uv tool install mlx-whisper` on Apple Silicon. The first transcription downloads a model. Intel Macs need the slower faster-whisper fallback. Voice is optional. |
| Telebots Bar, optional | Building it needs Xcode 26 or Command Line Tools 26 with that SDK. The app runs on macOS 14 or newer. See [menubar/README.md](menubar/README.md). |

The official Telegram plugin must be version 0.0.7 for the overlay. The installer refuses an unknown upstream file instead of guessing at a patch. If the plugin moves to a new version, the bot can use the stock channel while Telebots extras pause until this project supports that version. The doctor and [troubleshooting guide](docs/TROUBLESHOOTING.md) show the status. You can choose to disable automatic updates for the whole official plugin marketplace, but that also delays other official plugin updates.

The official install commands for the command-line prerequisites are `curl -fsSL https://bun.sh/install | bash` for Bun, `curl -LsSf https://astral.sh/uv/install.sh | sh` for uv, and `curl -fsSL https://claude.ai/install.sh | bash` for Claude Code. Review each linked provider guide before running its installer.

## Telegram controls

These are the 13 commands in the private-chat menu, in the order the poller registers them. `/key` also shows the tap and reaction meanings.

| Command | Menu description |
|---|---|
| `/esc` | Interrupt the current task - read my newest message now |
| `/fresh` | Restart this bot blank (fresh context, default model) |
| `/update` | Reload this chat in place: same conversation, newest Claude Code + skills |
| `/key` | The key: what every reaction, bubble and command does |
| `/inbox` | Your top threads as cards: Reply / Archive / Snooze by tap |
| `/opus` | Switch this chat to Opus at max effort (conversation kept) |
| `/fable` | Switch this chat to Fable at max effort (conversation kept) |
| `/model` | Show or switch model + effort (e.g. /model fable high) |
| `/effort` | Pick effort for this chat: low / medium / high / xhigh / max (conversation kept) |
| `/context` | Show context usage - how full this chat is |
| `/compact` | Compact this bot's context (optional focus after it) |
| `/usage` | Plan limits left + fleet token burn (today / 7 days) |
| `/resume` | List recent chats across all bots / reopen one |

`/model opus high` and `/effort low` also accept arguments. `/compact` can take a short focus. `/resume` opens a tappable list of up to 20 chats with labels based on each chat's opening topic, even without an extra API key. The quick bar puts `/esc` and `/model` beside `/usage` and `/fresh` under the first plain reply.

In an ordinary Claude Code session, this project's slash commands provide setup and maintenance: `/telegram-fleet:setup`, `/telegram-fleet:doctor`, `/telegram-fleet:pair`, `/telegram-fleet:checklist`, `/telegram-fleet:install-daemon`, `/telegram-fleet:uninstall-daemon`, `/telegram-fleet:snapshot`, `/telegram-fleet:restore` and `/telegram-fleet:uninstall`. If the session was open during install, use `/reload-plugins` first. Do not start another Telegram channel session with a bot token just to run them.

The installed Mac commands live in `~/.claude/telegram-fleet/bin/`. The setup path uses `fleet-doctor`, `fleet-add-bot`, `fleet-start`, `fleet-pair` and `fleet-checklist`. `fleet-email` handles optional mail setup and connection tests. `fleet-snapshot` and `fleet-restore` handle a planned reboot. The poller calls `fleet-fresh`, `fleet-update`, `fleet-esc`, `fleet-model`, `fleet-compact`, `fleet-context`, `fleet-usage`, `fleet-sessions`, `fleet-state`, `fleet-handoff`, `fleet-resume-open` and `fleet-context-pct` for the phone controls. `fleet-config` validates the bot list. These engines use the installed path, so the clone can move after setup.

| Reaction | Action |
|---|---|
| ❤️ | Save a dated summary of this chat's work in `~/Telebots Notes` |
| 👍 | Yes, go ahead |
| 👎 | No, stop |
| 🔥 | Put this at the top of my local priorities list |
| 👏 | Finish the task, record it as done, then save the summary |
| 😁 | Explain this simply |
| 🥰 | Watch this with supported read-only checks for up to 24 hours |

A reaction from an allowed user in a private chat becomes the matching instruction. Removing a reaction does nothing. A reaction never authorizes an email send. Closed questions may appear as tap bubbles; a tap is sent as your answer. Money and destructive actions need a direct decision, not a bubble.

## Email, supervision and security

`/inbox` stays in the menu. Until you configure email, it shows a setup hint. The optional built-in backend uses an IMAP/SMTP account that accepts an app password, stored in macOS Keychain. Gmail, iCloud Mail, Fastmail and Yahoo are possible where app passwords and IMAP/SMTP are enabled. Outlook.com and Microsoft 365 accounts that require OAuth are outside the built-in backend. An email preview shows the exact recipients, subject and body. `Send it` changes to `Cancel` and `Confirm send`; only the second tap authorizes one matching send within ten minutes. See [EMAIL.md](docs/EMAIL.md).

The `safe` permission stance keeps normal Claude Code prompts. `scoped` runs `claude --permission-mode auto`; it uses Claude Code's auto mode, not a generated tool allowlist. `full` runs `--dangerously-skip-permissions`. Use the same stance for bots that hand work to each other. Anyone you allow to message a bot can ask it to use the Mac within that stance. Read [SECURITY.md](SECURITY.md) before choosing.

Each bot has its own token, allowlist, work directory and state directory. The watchdog acts only on paired, enrolled bots and cannot answer while the Mac is asleep or the user is logged out. Snapshot before a planned restart, then restore after login with `/telegram-fleet:snapshot` and `/telegram-fleet:restore`. The optional [Telebots Bar](menubar/README.md) adds local controls and a Tailscale phone page. [ARCHITECTURE.md](docs/ARCHITECTURE.md) describes the overlay, hooks and logs.

## Uninstall and help

Run `./uninstall.sh --dry-run` to review the steps, then `./uninstall.sh` if they are right. It restores the stock `server.ts` from the local backup. State remains unless you ask to purge it. Revoke any token you no longer use through BotFather. See [FAQ](docs/FAQ.md) and [troubleshooting](docs/TROUBLESHOOTING.md).

## About Nexera Intelligence

Nexera Intelligence is an AI consulting firm in New York. We help any business, in any industry, adopt AI across the whole company. We train the entire team to use it day to day, so people get their time back and the company gets ahead. We built this fleet to run our own operation, then opened it up so anyone can control Claude Code from their phone. If you want AI working across your business, find us at [nexeraintelligence.com](https://nexeraintelligence.com).

## Credits

Maintained by Eli Almo at Nexera Intelligence. Built on Anthropic's official Telegram channel plugin, installed separately. See [NOTICE.md](NOTICE.md). This repository is [MIT licensed](LICENSE).
