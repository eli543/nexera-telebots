# NOTICE

## What this repository contains

This repository contains its own hooks, skills, commands, engine and daemon scripts, installer, documentation, `poller/telebots.ts` and one unified diff. Its original code is licensed under the MIT License in [LICENSE](LICENSE).

The installer obtains Anthropic's official `telegram@claude-plugins-official` plugin through the normal Claude Code plugin path. Anthropic distributes that plugin under Apache License 2.0. This repository does not redistribute its source.

## Change to the locally installed Anthropic plugin

The installer modifies your own local copy of `telegram@claude-plugins-official` version 0.0.7 with one unified diff, `poller/seam-0.0.7.patch`, and adds one file of our own, `telebots.ts` (MIT). Anthropic's code is not copied into this repository. The patched copy on your Mac retains Anthropic's license and notices. The change statement required by Apache-2.0 section 4(b) is in `poller/README.md`.

The overlay checks the exact upstream version and file hash before applying the diff. It keeps a local stock backup so uninstall can restore that file.

## No affiliation or unofficial client

Nexera Telebots is an independent project. Anthropic does not sponsor or endorse it. "Claude" and "Claude Code" are Anthropic marks; "Telegram" belongs to Telegram Messenger Inc.

The project runs through the official Telegram channel and Claude Code's `--channels` path. It ships no replacement Claude Code client.

## Maintained by

Eli Almo at Nexera Intelligence, New York. Repository: https://github.com/eli543/nexera-telebots
