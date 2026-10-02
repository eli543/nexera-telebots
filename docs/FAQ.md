# FAQ

## Do I need more than one bot?

No. Start with one. Each additional bot needs a separate BotFather token, Terminal session and pairing. You choose the names and count. See [FLEET.md](FLEET.md).

## Does this run on Linux or Windows?

No. Nexera Telebots is macOS only, with macOS 14 or newer required. It uses launchd, Keychain, AppleScript and Terminal.app. The experimental Windows folder was removed from this release.

## Why use this instead of only the official Telegram plugin?

The official plugin handles the Telegram channel and pairing. Telebots adds the local overlay for mid-task controls, tap callbacks, reactions, the menu and voice, plus hooks for the live work box and reply checks. Optional launchd supervision and snapshot/restore add recovery. The official plugin remains the channel in use.

## Is the project an unofficial Claude Code client?

No. It runs Claude Code with Anthropic's official `telegram@claude-plugins-official` plugin through `--channels`. It modifies the user's local copy of plugin 0.0.7 with a version-checked diff and ships its own module. [NOTICE.md](../NOTICE.md) explains the licenses. Claude Code channels remain a research preview and can change.

## Can BotFather make as many bots as I want?

Bot names and the number you configure are your choice, but Telegram controls creation. BotFather may throttle `/newbot` after the first few; waits can reach about a day. Telegram does not publish that pacing rule. Community references put the account limit at 20 bots, or 40 with Telegram Premium. Finish one bot before creating the next.

## Does voice require Apple Silicon?

Apple Silicon is needed for the fast mlx-whisper path. Voice is optional. An Intel Mac can use the slower faster-whisper fallback when installed. ffmpeg and a backend must be present, and the first model run may download roughly 1.6 to 1.9 GB. Without a local backend the voice checklist row is skipped rather than pretending transcription worked.

## What does the optional AI key send?

The local reply checks and the no-key `/resume` labeler need no extra key. If you enable AI extras, an outbound draft may be sent to Anthropic for the TLDR judge, the opening of a chat may be sent for a topic label, and the model-list refresher may call Anthropic. The key stays in macOS Keychain. See [SECURITY.md](../SECURITY.md).

## Does `/inbox` work with my mail provider?

The built-in backend needs IMAP and SMTP access with an app password. Gmail, iCloud Mail, Fastmail and Yahoo can work when their account settings permit that access. Outlook.com and Microsoft 365 accounts that require OAuth are outside the built-in backend in v0.3.0. `/inbox` remains visible and shows a setup hint until the connection passes `fleet-email test`. See [EMAIL.md](EMAIL.md).

## What happens when the official Telegram plugin updates?

The overlay is pinned to the stock 0.0.7 file. It will not patch an unknown version. On an unsupported update, Telebots extras pause; the stock bot can still answer and the watchdog uses PID-only checks instead of restarting in a heartbeat loop. Check `fleet-doctor` and the overlay status, then update Telebots when a supported seam is available. Turning off auto-update for the whole official marketplace also delays updates to its other plugins.

## Can I switch features off?

Yes. The [architecture guide](ARCHITECTURE.md#kill-switches-and-testing) lists the hook switches. Voice, email, the warm voice daemon, the watchdog, Telebots Bar and the phone page are separately optional. A switch must be checked against a real bot reply to confirm its effect.

## How do I handle a leaked token?

Revoke the token in [@BotFather](https://t.me/BotFather). Enter the replacement through a hidden local prompt, never through a Claude chat, then restart that bot and run its checklist. The old token stops working when revoked. Check shared logs and recordings for any exposure.
