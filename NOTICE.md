# NOTICE

## What this repository contains

This repository contains only its own add-on code (hooks, skills, slash
commands, engine and daemon scripts, an installer, docs, and optional patch
files), all licensed under the MIT License (see `LICENSE`).

It installs and orchestrates the official Telegram channel plugin, which
Anthropic distributes separately under the Apache License 2.0. This repository
does not redistribute that plugin. The installer fetches it through the normal
Claude Code plugin path (`claude plugin install telegram@claude-plugins-official`).

## Relationship to Anthropic's Apache-2.0 plugin

The upstream plugin (`telegram@claude-plugins-official`, version 0.0.6,
Copyright 2026 Anthropic, PBC) is Apache-2.0 licensed. Two facts follow:

- We never vendor or copy the upstream plugin's source into this repository.
  You install it yourself from the official marketplace.
- The optional files in `patches/` modify your own local copy of that
  Apache-2.0 code after you have installed it. They do not carry Anthropic's
  code into this repository; they are unified diffs. The patched result on your
  machine retains the upstream plugin's license and notices. Per Apache-2.0
  section 4(b), the changes those patches make are stated in `patches/README.md`.

## Not affiliated with Anthropic

This project is an independent, community-built add-on layer. It is not
affiliated with, sponsored by, or endorsed by Anthropic. "Claude", "Claude
Code", and related marks belong to Anthropic. "Telegram" belongs to Telegram
Messenger Inc.

## No unofficial client

This project runs on Anthropic's sanctioned `--channels` path and the official
Telegram channel plugin. It ships no forked or spoofed Claude client and does
not replace the Claude Code CLI.

## Maintained by

Eli Almo at Nexera Intelligence (AI consulting, New York). Repository: https://github.com/eli543/claude-telegram-fleet
