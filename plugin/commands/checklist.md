---
description: Run setup checks for one Telebots bot, recording backend evidence and the phone steps that still need proof.
argument-hint: <bot-key>
allowed-tools: Bash, Read
---

Set `FLEET_HOME` to `${FLEET_HOME:-$HOME/.claude/telegram-fleet}`. Resolve the requested key from fleet.json; if several bots exist and no key was given, ask which one. Run `"$FLEET_HOME/bin/fleet-checklist" <key>`. To drive it from an assisting session, add `--non-interactive --wait 120`.

Explain each phone step just before it is needed, and wait for the engine's evidence. A Checklist OK tap gets only a 👍 reaction from the bot. Results are saved in `$FLEET_HOME/checklist-<key>.json`; read that result and report `Passed`, `Failed`, `Needs your action` or `Not configured` for every row. A timeout is missing evidence, never a pass. A completed handoff, full inbox reply, reboot restore and phone rescue remain untested until checked separately; the setup checklist says so in their rows.

For local permissions, you do this: be at the Mac before `"$FLEET_HOME/bin/fleet-checklist" --consent` opens Terminal Automation or Accessibility prompts. Warn first. A phone cannot approve those dialogs.

The optional `--crash-test` interrupts a bot to check recovery, and `--blank-fresh` adds a final step that clears its conversation. Run either only after explicit user approval for that action. The normal checklist does not authorize those steps. Recovery is quiet: a chat active within the last 30 minutes resumes, and an older chat starts blank. Real rendering, voice, keystrokes and cross-bot delivery remain unverified until the corresponding phone steps pass.
