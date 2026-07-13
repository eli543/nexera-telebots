# claude-telegram-fleet

The always-on upgrade for Claude Code's official Telegram channel: live progress
streaming, voice notes transcribed locally, replies that arrive structured
instead of as walls of text, `/fresh` `/resume` `/model` from your phone, a
self-healing watchdog, snapshot and restore that survive a Mac reboot, and a
fleet of identical bots sharing one brain.

![platform: macOS](https://img.shields.io/badge/platform-macOS-black)
![license: MIT](https://img.shields.io/badge/license-MIT-black)
![Claude Code](https://img.shields.io/badge/Claude%20Code-%E2%89%A5%202.1.80-black)
![channels: research preview](https://img.shields.io/badge/channels-research%20preview-orange)

The stock plugin gives you a chat bridge. This gives you an operation. It is
built on Anthropic's official plugin and the sanctioned `--channels` path: no
forked client, no spoofed CLI. Read [SECURITY.md](SECURITY.md) first, because a
Telegram-reachable agent that runs on your Mac is remote access to your Mac by
design, and the access-control story is the part you should understand before
you install anything.

<!-- HERO GIF: send a Telegram message; an emoji reaction lands instantly; the live "Working" box appears with a sliding bar, a climbing timer, and steps checking off one by one; it folds in place into a "What I did - tap to expand" recap; the numbered answer with its TLDR lands underneath. Record at 1280x720. Rotate tokens before recording any terminal. -->

## What you get over the stock plugin

Every claim below is measured against Anthropic's stock Telegram channel plugin,
which does message round-trip, pairing and allowlist access control, and the
`reply` / `react` / `edit_message` tools.

| Capability | Stock telegram plugin | claude-telegram-fleet |
|---|---|---|
| Message round-trip, pairing and allowlist access control | Yes | Yes, unchanged. We build on it |
| Live progress: animated status box, edit-in-place, folds into a recap | Manual `edit_message` only | Automatic, zero model tokens |
| Guaranteed instant reaction ack | Static `ackReaction` option | Content-aware, guaranteed by a hook |
| Reply reliability: a turn cannot end silently in the terminal | No | Yes, a Stop-hook guard |
| Structured replies: numbered key plus TLDR, verified | No | Deterministic check plus optional Haiku judge |
| Documents handed to you as tappable files | No | Yes, a delivery-nudge hook |
| Voice notes as prompts | No | Yes, local, about 1-2s on Apple Silicon |
| `/fresh` `/resume` `/model` from Telegram | No | Yes, including a cross-bot resume picker |
| Keeps bots alive with a watchdog | No. "Events only arrive while the session is open" | Yes, launchd plus auto-resume into the same conversation |
| Survives a reboot with exact conversations | No | Yes, snapshot and restore |
| Multi-bot | Manual multi-instance, separate allowlists | Managed fleet, one shared brain |

Honest positioning. Streaming, voice, watchdogs, and Telegram-side slash
commands each exist in other community projects, so this repo does them deeper,
not first. The parts that appear to have no shipped public counterpart are the
verified-reply contract, exact-conversation restore across a reboot, the
identical-fleet-one-brain topology, the cross-bot resume picker, and the
reply-delivery guarantee. See [docs/FAQ.md](docs/FAQ.md) for the landscape.

## Quickstart

Three lines inside a Claude Code session:

```
/plugin marketplace add eli543/claude-telegram-fleet
/plugin install telegram-fleet@claude-telegram-fleet
/telegram-fleet:setup
```

`/telegram-fleet:setup` walks you through the rest: preflight checks, one
or more bots, tokens (written to a per-bot `.env` with `chmod 600`, never
echoed), your permission stance, voice, the watchdog, and the pairing walkthrough.

Prefer a bare shell, or not yet in a session? The same underlying steps run from
the installer:

```
git clone https://github.com/eli543/claude-telegram-fleet
cd claude-telegram-fleet
./install.sh
```

Then, once per bot, the stock pairing flow: create a bot with
[@BotFather](https://t.me/BotFather) (`/newbot`), DM your new bot, it replies
with a six-character code, and you run `/telegram:access pair <code>` in the
terminal. Setup then offers to lock the policy to `allowlist`.

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

## Command reference

From your Telegram chat with a bot:

| Command | Effect |
|---|---|
| `/fresh` | Restart this bot with a blank context and default model |
| `/resume` | Show a numbered picker of recent sessions across all bots |
| `/resume N` | Reopen session N here, in this bot |
| `/model` | Report the current model and effort |
| `/model <class> [effort]` | Switch model class (`opus`, `sonnet`, `fable`, `haiku`) and effort (`low`, `medium`, `high`, `xhigh`, `max`), history intact |

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

## Credits

Maintained by Eli Almo. Built on Anthropic's official Telegram channel plugin
(`telegram@claude-plugins-official`), which is installed, not redistributed. See
[NOTICE.md](NOTICE.md) for the licensing relationship and the not-affiliated
statement.

MIT licensed. See [LICENSE](LICENSE).
