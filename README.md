# Nexera Telebots

_Built and provided by [Nexera Intelligence](https://nexeraintelligence.com) - AI consulting, New York._

> Setting this up with Claude? If you dropped this repo's link into Claude and
> said "set this up for me," point Claude at
> [SETUP-WITH-CLAUDE.md](SETUP-WITH-CLAUDE.md). That file is written as direct
> instructions to an assisting Claude: it stands up the first bot end to end,
> verifies each step before the next, and explains every component as it goes,
> then repeats compactly for any further bots. Setting it up by hand? The
> [Setup](#setup) section below is the same walkthrough for a person.

The always-on upgrade for Claude Code's official Telegram channel: live progress
streaming, voice notes transcribed locally, replies that arrive structured
instead of as walls of text, `/fresh` `/resume` `/model` plus an `/esc`
interrupt, one-tap `/opus` and `/fable` switches, live `/context` and `/usage`
meters (your plan limits included), a real Telegram "/" command menu, a
self-healing watchdog, snapshot and
restore that survive a Mac reboot, and a fleet of identical bots sharing one
brain.

![platform: macOS](https://img.shields.io/badge/platform-macOS-black)
![license: MIT](https://img.shields.io/badge/license-MIT-black)

The stock plugin gives you a chat bridge. This gives you an operation. It is
built on Anthropic's official plugin and the sanctioned `--channels` path: no
forked client, no spoofed CLI. Read [SECURITY.md](SECURITY.md) first, because a
Telegram-reachable agent that runs on your Mac is remote access to your Mac by
design, and the access-control story is the part you should understand before
you install anything.

<!-- HERO GIF: send a Telegram message; an emoji reaction lands instantly; the live "Working" box appears with a sliding bar, a climbing timer, and steps checking off one by one; it folds in place into a "What I did - tap to expand" recap; the numbered answer with its TLDR lands underneath. Record at 1280x720. Rotate tokens before recording any terminal. -->

## Setup

Everything from zero to a bot that answers, in order. Deeper detail lives in the
linked docs; this section alone is enough to stand a bot up.

### The shape of the thing, first

One design idea explains every moving part here: each bot is an individual
Telegram bot - its own BotFather identity, its own token, its own allowlist -
driving its own Claude Code session in its own Terminal window, in its own
working directory. That is deliberate, not an accident of implementation:

- One window per bot means one PID and one tty per bot, so the watchdog can
  kill and relaunch a wedged bot by exact PID, `/esc` can press Escape in
  exactly the right window, and no restart can ever take a sibling down.
- One Telegram bot per session means Telegram's one-poller-per-token rule maps
  cleanly onto one poller process per bot, instead of a router process
  multiplexing chats (the failure mode of most multi-bot designs).
- One working directory per bot gives each session its own transcript history,
  which is what makes `/resume`, snapshot/restore, and the cross-bot picker
  possible.

The bots stay identical on purpose - same hooks, same commands, same knowledge
base - so a fleet is N interchangeable phone lines into one operation, not N
different personalities. Full topology: [docs/FLEET.md](docs/FLEET.md).

### 1. Install

Three lines inside a Claude Code session:

```
/plugin marketplace add eli543/nexera-telebots
/plugin install telegram-fleet@nexera-telebots
/telegram-fleet:setup
```

`/telegram-fleet:setup` walks you through the rest: preflight checks, one or
more bots, tokens (written to a per-bot `.env` with `chmod 600`, never echoed),
your permission stance, voice, the watchdog, and the pairing walkthrough.

Prefer a bare shell, or not yet in a session? The same underlying steps run
from the installer:

```
git clone https://github.com/eli543/nexera-telebots
cd nexera-telebots
./install.sh
```

Only the installer copies the engine scripts into
`~/.claude/telegram-fleet/bin/` and writes a starter `fleet.json` (the one config
file). The three-line in-session path installs the Claude-facing surface - hooks,
skills, and slash commands - but not the engine, so run `./install.sh` as well
before you launch a bot; `/telegram-fleet:setup` and every `fleet-*` command
depend on that `bin/` directory. The installer is also the one-command path: it
lays down both plugins, the engine, and the watchdog in a single pass.
Requirements are firm and listed [below](#requirements): macOS, Bun, Claude
Code 2.1.80+ with channels.

### 2. Create a bot and pair it

Once per bot:

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`,
   and copy the token it gives you.
2. Give the bot a home. Each bot needs a `fleet.json` entry (a `name`, a short
   `key`, a `workdir` directory the session runs in, and a `state_dir` of
   `~/.claude/channels/fleet-<key>`) plus its token:
   ```
   mkdir -p ~/.claude/channels/fleet-main
   printf 'TELEGRAM_BOT_TOKEN=%s\n' "<token>" > ~/.claude/channels/fleet-main/.env
   chmod 600 ~/.claude/channels/fleet-main/.env
   ```
   `/telegram-fleet:setup` does this for you, including the `fleet.json` entry.
3. Launch it: `~/.claude/telegram-fleet/bin/fleet-launch main`. A Terminal
   window opens running that bot's Claude Code session.
4. Pair: DM your new bot; it replies with a six-character code; run
   `/telegram:access pair <code>` in the terminal. Then lock the door:
   `/telegram:access policy allowlist`.

Send the bot a message. You should get an instant emoji reaction, a live
"Working" box, and a structured reply. To add a second bot later, repeat this
section with a new key - one BotFather bot, one `fleet.json` entry, one state
dir, one launch. [docs/FLEET.md](docs/FLEET.md) covers the shared-brain pattern.

### 3. Know the restart family

Six ways a session starts, stops, or switches, all from your phone, all safe to
mix. The first three restart the session process; `/esc` does not.

| You send | What happens | Context |
|---|---|---|
| `/fresh` | Kill this bot's session, relaunch blank on your default model | Wiped, on purpose |
| `/model opus` (or `sonnet`, `fable`, `haiku`, plus optional effort) | Restart on the new model, resumed into this same conversation | Kept |
| `/opus`, `/fable` | One-tap shortcuts for the two switches above - a menu tap cannot carry arguments, so they exist as commands | Kept |
| `/esc` | Press Escape in this bot's Terminal window, interrupting the current turn so your queued message is read NOW | Kept - nothing restarts |
| `/resume` | Numbered picker of recent sessions across every bot; `/resume 3` reopens session 3 here | Kept - reopened |
| (nothing - a bot dies) | The launchd watchdog notices within ~60s and relaunches it resumed into the same conversation | Kept |

Rebooting the Mac: `/telegram-fleet:snapshot` before, `/telegram-fleet:restore`
after login, and every bot comes back in its exact prior conversation. The
watchdog also parks itself during a snapshot so it cannot fight the restore;
`fleet-snapshot --keep-watchdog` takes the same snapshot without parking it, for
a save point that is not a reboot. The restore archives the snapshot only once
every bot is confirmed back (its session running, its own Telegram poller alive,
and a fresh heartbeat from patch 03). Otherwise it keeps the snapshot and says
which bots are still pending. Restore validates the entire snapshot before acting;
invalid or empty snapshots stay available for correction. Both snapshot flags can
be combined in either order.

Honest wiring note: `/fresh`, `/resume`, and `/model` work out of the box via
idle-path hooks, and gain mid-task handling with the optional patches. `/esc`,
`/opus`, `/fable`, `/context`, and `/usage` are patch-only - `/esc` exists to
interrupt a BUSY session, which an idle-path hook can never do. Apply the
patches with `bash patches/apply-patches.sh` (verified, reversible, wiped by
plugin updates and safe to re-apply; see [patches/README.md](patches/README.md)).

### 4. Register the "/" command menu

The Telegram "/" menu button is registered by the poller itself on every start
(optional patch 09), listing the real commands in a sensible order: `esc`,
`fresh`, `opus`, `fable`, `model`, `context`, `usage`, `resume`. The poller-start
registration is the source of truth - it re-asserts on every restart, so a
plugin update that reverts it heals on the next re-patch and restart. To refresh
the menu instantly without a restart, there is a one-off `setMyCommands` curl in
[patches/README.md](patches/README.md). Telegram clients cache the menu for a
minute or so.

That is the whole setup. The rest of this README is what you get, the fleet
story, requirements, and the security model.

## What you get over the stock plugin

Every claim below is measured against Anthropic's stock Telegram channel plugin,
which does message round-trip, pairing and allowlist access control, and the
`reply` / `react` / `edit_message` tools.

| Capability | Stock telegram plugin | nexera-telebots |
|---|---|---|
| Message round-trip, pairing and allowlist access control | Yes | Yes, unchanged. We build on it |
| Live progress: animated status box, edit-in-place, folds into a recap | Manual `edit_message` only | Automatic, zero model tokens |
| Guaranteed instant reaction ack | Static `ackReaction` option | Content-aware, guaranteed by a hook |
| Reply reliability: a turn cannot end silently in the terminal | No | Yes, a Stop-hook guard |
| Structured replies: numbered key plus TLDR, verified | No | Deterministic check plus optional Haiku judge |
| Documents handed to you as tappable files | No | Yes, a delivery-nudge hook |
| Voice notes as prompts | No | Yes, local, about 1-2s on Apple Silicon |
| `/fresh` `/resume` `/model` from Telegram | No | Yes, including a cross-bot resume picker |
| Interrupt a busy session from your phone | No | Yes, `/esc` sends a real Escape to the right window |
| One-tap model switches | No | Yes, `/opus` and `/fable`, conversation kept |
| Live meters: plan limits left, context fill, token burn | No | Yes, `/usage` and `/context`, computed outside the model |
| A real "/" command menu | Dead `/start` `/help` `/status` boilerplate | The actual command list, re-registered on every poller start |
| Keeps bots alive with a watchdog | No. "Events only arrive while the session is open" | Yes, launchd plus auto-resume into the same conversation |
| See and fix the fleet from the Mac menu bar or your phone | No | Yes, the optional [Telebots Bar](menubar/README.md): live status, Reconnect, watchdog, snapshot and restore, plus an iPhone home-screen page over Tailscale |
| Survives a reboot with exact conversations | No | Yes, snapshot and restore |
| Multi-bot | Manual multi-instance, separate allowlists | Managed fleet, one shared brain |

Honest positioning. Streaming, voice, watchdogs, and Telegram-side slash
commands each exist in other community projects, so this repo does them deeper,
not first. The parts that appear to have no shipped public counterpart are the
verified-reply contract, exact-conversation restore across a reboot, the
identical-fleet-one-brain topology, the cross-bot resume picker, and the
reply-delivery guarantee. See [docs/FAQ.md](docs/FAQ.md) for the landscape.

<!-- VOICE GIF: record a Telegram voice note; about a second later the bot is already working on the transcribed request, with the live box open. -->

## The fleet

One bot is the default. A fleet is the growth path: N identical bots, each in
its own working directory with its own token and allowlist, all sharing one
knowledge base (your `CLAUDE.md` and any notes you point them at). Because the
bots are identical, `/resume` in any one of them shows a numbered picker of
sessions across every bot, and `/resume 3` reopens another bot's conversation in
the bot you are chatting with. The config is one file:

```json
{
  "version": 1,
  "bots": [
    { "name": "Main", "key": "main", "workdir": "~/claude-fleet/main", "state_dir": "~/.claude/channels/fleet-main", "emoji": "🤖" },
    { "name": "Research", "key": "research", "workdir": "~/claude-fleet/research", "state_dir": "~/.claude/channels/fleet-research", "emoji": "🔬" }
  ],
  "options": { "watchdog": true, "voice": "auto", "tldr_judge": false, "grid": false }
}
```

Adding a bot later is an edit to this file plus a state directory. See
[docs/FLEET.md](docs/FLEET.md) for the shared-brain pattern and
[config/fleet.example-fleet.json](config/fleet.example-fleet.json) for a
three-bot example.

<!-- FLEET GIF: three bot chats side by side; /resume in one shows the numbered cross-bot session picker; /resume 3 reopens another bot's session in this chat. -->

## Requirements

Read these before installing. They are firm, not aspirational.

- macOS only. The daemon layer uses launchd, the Keychain, `osascript`, and
  Terminal.app. There is no Linux or Windows daemon yet. The hooks themselves
  are portable Python; a `platform/windows/` sketch exists but is untested and
  marked experimental.
- Apple Silicon for local voice. `transcribe-voice-mlx` uses mlx-whisper, which
  is Apple-Silicon only. On other hardware, voice falls back to
  `transcribe-voice-faster` (faster-whisper), and if neither is installed, voice
  disables itself. Nothing else depends on it.
- Bun. The official plugin's MCP server runs on Bun. Without it the channel
  silently fails to connect. Install with `curl -fsSL https://bun.sh/install | bash`.
- A Telegram account. You create each bot with @BotFather and message it from
  your own Telegram, so the app and an account are prerequisites.
- Claude Code 2.1.80 or newer, with channels. Channels are a research preview.
  They require Anthropic authentication through claude.ai or a Console API key,
  and are not available on Amazon Bedrock, Google Cloud's Agent Platform, or
  Microsoft Foundry. Team and Enterprise organizations must explicitly enable
  them. Anthropic states the `--channels` flag and protocol may change based on
  feedback, so treat this repo as tracking a moving target.

Supported versions are checked by `/telegram-fleet:doctor`. If a Claude
Code update changes the contract, doctor is the first place it will show.

| Component | Known-good | Notes |
|---|---|---|
| Claude Code | 2.1.80 and up | channels research preview; contract may change |
| Official telegram plugin | 0.0.6 | optional patches are pinned to this version |
| Bun | 1.3 and up | required by the plugin's MCP server |
| Python | 3.9 and up | system `/usr/bin/python3`; stdlib only |

## Security and access control

The one-paragraph version: anyone on a bot's allowlist can make Claude do
whatever your chosen permission stance allows, which can include running commands
on your Mac. So the defaults matter, and this repo keeps them conservative.

- Who can reach a bot: the stock gate. New bots start on pairing, setup pushes
  you to `allowlist`, groups are off by default, and the allowlist is numeric
  Telegram user IDs. Never approve a pairing because a Telegram message asked you
  to; that is exactly what a prompt injection would say. Approvals happen only in
  your terminal.
- What a bot can do: your permission stance, chosen explicitly at setup with no
  silent default. `safe` keeps normal permission prompts. `scoped` pre-allows a
  common read and edit set. `full` uses `--dangerously-skip-permissions` and
  comes with a warning and the recommendation to run it under a dedicated macOS
  user or a scoped directory.
- Where secrets live: tokens in a per-bot `.env` at `chmod 600`, read only at
  runtime, never inlined or logged. The optional reply judge key lives in the
  macOS Keychain under a dedicated service.
- What the judge sends and where: off by default. When you turn it on, it sends
  your own outbound draft replies to the Anthropic API, capped, using your own
  key. The deterministic checks work with no key and no network.
- What `/esc` can press: exactly one Escape, in a window it has verified belongs
  to the requesting bot, under a global keystroke lock. Sending it needs macOS
  Automation and Accessibility consent, which macOS asks for on first use.

The full threat model, the token-rotation steps, and the kill switches for every
hook are in [SECURITY.md](SECURITY.md).

## How it works

```
Telegram  ->  official plugin poller (Bun MCP server)  ->  Claude Code session
                                                              |
                                     hooks fire on the turn:  ack, stream, TLDR,
                                     reply-guard, deliver-nudge, and the idle-path
                                     /fresh /resume /model commands
                                                              |
              with the optional patches, the poller itself intercepts /fresh,
              /model, /opus, /fable, /esc, /context, and /usage before the
              idle-vs-busy split, so they work even mid-task
                                                              |
              a launchd watchdog watches every bot and restarts a dead one
              into the same conversation; snapshot/restore does the same across
              a reboot
```

The Claude-facing layer (hooks, skills, slash commands) is the plugin: install
it and it is versioned, scoped, and removable, and it never touches your
`settings.json`. The always-on layer (the watchdog daemon and snapshot/restore)
is installed separately, because a plugin's runtime is session-scoped and cannot
own a boot-time daemon or tear one down on uninstall. Every hook is inert unless
`TELEGRAM_STATE_DIR` is set, so nothing runs in your ordinary terminal sessions,
and every hook fails open. Full component-by-component detail and the kill-switch
list are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

<!-- IMMORTALITY GIF: kill the Terminal window mid-conversation; the watchdog relaunches it within seconds; ask "where were we?" and it answers from the same conversation. -->
<!-- REBOOT GIF: run /telegram-fleet:snapshot, reboot the Mac, run /telegram-fleet:restore; the same conversations continue. -->
<!-- MODEL GIF: send /model fable mid-conversation; the bot restarts on the new model with history intact. -->
<!-- ESC GIF: the bot is mid-task in the live box; send /esc; the box stops and the bot answers the newest message instead. -->

## Command reference

From your Telegram chat with a bot:

| Command | Effect | Needs patches? |
|---|---|---|
| `/fresh` | Restart this bot with a blank context and default model | No (mid-task handling: patch 04) |
| `/resume` | Show a numbered picker of recent sessions across all bots | No |
| `/resume N` | Reopen session N here, in this bot | No |
| `/model` | Report the current model and effort | No (mid-task handling: patch 05) |
| `/model <class> [effort]` | Switch model class (`opus`, `sonnet`, `fable`, `haiku`) and effort (`low`, `medium`, `high`, `xhigh`, `max`), history intact | No (mid-task handling: patch 05) |
| `/opus`, `/fable` | One-tap switch to that model class, history intact | Yes (07) |
| `/esc` | Interrupt the current turn so your newest message is read now | Yes (06) |
| `/context` | This chat's context-window usage: used vs cap, with a bar | Yes (08) |
| `/usage` | Your plan limits left (session and weekly, with reset times) plus fleet-wide token burn: today and the trailing 7 days, per bot | Yes (08) |

Inside a Claude Code session:

| Command | Effect |
|---|---|
| `/telegram-fleet:setup` | Guided setup: bots, tokens, permission stance, voice, watchdog |
| `/telegram-fleet:doctor` | Health and version checks; a green or red table |
| `/telegram-fleet:install-daemon` | Install the launchd watchdog, after showing you the commands |
| `/telegram-fleet:uninstall-daemon` | Remove the watchdog |
| `/telegram-fleet:snapshot` | Capture running sessions before a reboot |
| `/telegram-fleet:restore` | Resume the exact conversations after login |
| `/telegram-fleet:uninstall` | Full teardown story |

Setup and access still use the stock skills `/telegram:configure` and
`/telegram:access` for token and allowlist changes.

## Uninstall

```
./uninstall.sh          # or /telegram-fleet:uninstall inside a session
```

This boots out the watchdog and removes its plist, stops bot sessions by exact
PID, and uninstalls the plugin. By default it keeps your tokens, allowlists, and
transcripts; `./uninstall.sh --purge` removes the state directories after
confirmation. It prints what remains either way, reminds you the upstream
`telegram` plugin is still installed, and points you at BotFather `/revoke` to
rotate a token. Full story: [docs/FAQ.md](docs/FAQ.md).

## FAQ

Short answers here; the long ones are in [docs/FAQ.md](docs/FAQ.md).

- Why not just the stock plugin or Remote Control? This is the stock plugin, plus
  the always-on layer Anthropic does not ship.
- Linux or Windows? Not yet for the daemon. The hooks are portable Python; a
  systemd port is a welcome pull request.
- What does the judge cost? Haiku, a fraction of a cent per long reply, and off
  by default.
- Is this within Anthropic's terms? Yes. Official plugin, official `--channels`
  flag, no spoofed client.
- Does a plugin update overwrite the optional patches? Yes. That is why they are
  optional and quarantined, with re-apply tooling.

## About Nexera Intelligence

Nexera Intelligence is an AI consulting firm in New York. We get small and
mid-sized businesses using AI inside the tools they already work in, from the
first setup to the automations that run in the background. We built this fleet
to run our own operation, then opened it up so anyone can have the same
always-on control of Claude Code from their phone. If you want AI working in
your business, find us at [nexeraintelligence.com](https://nexeraintelligence.com).

## Credits

Maintained by Eli Almo at Nexera Intelligence. Built on Anthropic's official Telegram channel plugin
(`telegram@claude-plugins-official`), which is installed, not redistributed. See
[NOTICE.md](NOTICE.md) for the licensing relationship and the not-affiliated
statement.

MIT licensed. See [LICENSE](LICENSE).
