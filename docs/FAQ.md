# FAQ

## Why not just use the stock plugin, or Remote Control?

This is the stock plugin, plus the layer Anthropic does not ship. The official
Telegram channel gives you a chat bridge: messages in, replies out, access
control. It explicitly does not keep a session alive ("events only arrive while
the session is open"), stream progress, transcribe voice, survive a reboot, or
add Telegram-side commands. This repo adds those on top of the official plugin,
without replacing it. If the stock bridge is all you need, you do not need this.

## What makes this different from the many other Claude-plus-Telegram projects?

The community space is large, and the common features are genuinely common.
Streaming, voice, keepalive daemons, and slash commands each exist in several
projects, so this repo does those deeper rather than claiming them first. The
parts that appear to have no shipped public counterpart, based on reading the
most relevant projects, are:

- The verified reply contract: a deterministic numbered-TLDR check plus an
  optional model judge that bounces a badly structured reply.
- Restore across a macOS reboot that resumes your exact conversations, not a
  summary and not a plain `--continue`.
- The identical-fleet-one-brain topology. Other multi-bot projects run different
  specialist agents; this one runs identical bots on one knowledge base.
- The cross-bot resume picker.
- The reply-delivery guarantee as an enforced Stop hook, rather than a prompt.

That is the honest positioning. Everything else in the feature table is an
improvement on things others also do.

## Does this run on Linux or Windows?

The daemon layer is macOS only. It uses launchd, the Keychain, `osascript`, and
Terminal.app. There is no supported Linux or Windows daemon yet.

The hooks themselves are portable Python with no third-party dependencies, so the
Claude-facing behavior would port. A `platform/windows/` sketch exists, built to
mirror the macOS logic through Task Scheduler and Windows Terminal, but it is
untested and marked experimental in every file. A systemd port for Linux is a
welcome pull request and a good first issue.

## What does the reply judge cost, and is my data safe?

The judge uses a fast, inexpensive model (Haiku class), which costs a fraction of
a cent per long reply. It runs only on long replies, and it is off by default.

When you turn it on, it sends your own outbound draft reply text, capped, to the
Anthropic API using your own key, which lives in the macOS Keychain. The
recipient is Anthropic, the same company running your Claude Code session. The
deterministic structural checks run locally with no key and no network, so you
can leave the judge off and still get most of the value. See
[SECURITY.md](../SECURITY.md) for the full data-flow description.

## Is this within Anthropic's terms of service?

Yes, as far as the mechanism goes. It runs the official Telegram channel plugin
through the sanctioned `--channels` flag. It ships no forked or spoofed Claude
client and does not replace the Claude Code CLI. That is the opposite posture
from the unofficial clients Anthropic has moved against. Your own use still has to
follow Anthropic's terms; the tooling does not change what those require of you.

## What happens when Claude Code updates?

Channels are an Anthropic research preview, and Anthropic states the `--channels`
flag and protocol may change based on feedback. So a Claude Code update can change
behavior.

- `/telegram-fleet:doctor` checks your versions against the known-good set
  and is the first place a mismatch will show.
- The README carries a supported-versions table.
- CI runs the tests against the current Claude Code so regressions surface early.

If an update breaks something, the doctor output and the version table are the
starting point for an issue.

## Does a plugin update overwrite the optional patches?

Yes. The mid-task `/fresh` and `/model` interceptors are patches on the official
plugin's `server.ts`, which sits in a version-pinned cache directory. When the
plugin updates, Claude Code writes a new cache directory and the patches do not
carry over. This is expected, and it is why the patches are optional and
quarantined rather than part of the core.

- The idle-path `/fresh`, `/resume`, and `/model` work with no patches at all.
- To restore mid-task interception after an update, rerun
  `patches/apply-patches.sh`.

## Do I have to run a fleet?

No. One bot is the default and the common case. A fleet is two or more bots in the
same config file, sharing one knowledge base. Start with one; add bots when you
want more independent chat surfaces into the same operation. See
[FLEET.md](FLEET.md).

## Can I turn a feature off?

Every hook has a kill switch, a single file you create to disable it with no
restart. The list is in [ARCHITECTURE.md](ARCHITECTURE.md). Turning a hook off
leaves the bot running without that behavior.

## How do I rotate a token if it leaks?

Message @BotFather, select the bot, choose `Revoke current token`, and configure
the new one with `/telegram:configure <new-token>`. The old token stops working
immediately. Rotate if a token ever appeared in a screen recording or a shared
log.
