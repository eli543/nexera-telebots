# server.ts patches

This directory holds five local patches for the upstream Telegram plugin's
`server.ts`, plus scripts to apply, verify, and revert them. The patches turn the
stock poller into the fleet-aware poller: a hijack guard, watchdog liveness
signals, and mid-task `/fresh` and `/model` handling that read the fleet from
`fleet.json` instead of any hardcoded machine.

Everything here is optional. The fleet's idle-path `/fresh`, `/resume`, and
`/model` (the UserPromptSubmit hooks) work with ZERO patches. You only need these
to get the mid-task behavior and the watchdog's liveness-based recovery. See the
feature map below.

## License and attribution (Apache-2.0 section 4(b))

The file these patches modify, `server.ts`, is part of Anthropic's
`telegram@claude-plugins-official` plugin, licensed under Apache-2.0
(Copyright 2026 Anthropic, PBC). This project does not copy or redistribute that
file. The patches modify a copy that already lives on your machine, inside the
plugin cache, after you install the plugin yourself.

Per Apache-2.0 section 4(b), the significant changes each patch makes are stated
below. The upstream LICENSE remains in place in the plugin directory and is not
altered. Nothing here uses Anthropic's name or marks to suggest endorsement.

## Version and integrity

The patches were generated against `telegram@claude-plugins-official` version
`0.0.6`, upstream `server.ts` sha256:

```
20087f27c81be642cbb0f0254626092330052a5c31b5abde8dec2569889a11e4
```

`apply-patches.sh` verifies both the version and this hash before touching
anything, and refuses a mismatch unless you pass `--force`. The fully-patched
(genericized) result has sha256
`9d641451b927a635052f59ece69c86d07a0b34776e3cf2240509945289281440`.

## Fragility: a plugin update wipes these

Important. The plugin cache is version-pinned
(`~/.claude/plugins/cache/claude-plugins-official/telegram/<version>/`). Any
`claude plugin update` that changes the version creates a new cache directory and
silently discards these patches. After any update, run `./apply-patches.sh` again
(and re-verify the target version first). Run `./restore-stock.sh` before you
intend to update, so you update from a clean base.

## Scripts

```
./apply-patches.sh [--force]   verify version + hash, back up, apply 01..05 in order
./verify-patches.sh            report which patches are present (read-only)
./restore-stock.sh             restore server.ts from server.ts.stock-backup
```

`apply-patches.sh` finds the newest installed `server.ts`, checks the version and
stock hash, applies each patch to a working copy with `patch -p1 --dry-run` then
for real, and only then backs up the pristine file to `server.ts.stock-backup` and
installs the result. If any patch fails its dry-run, nothing is changed.

## The five patches

Each is a standalone unified diff that applies with `patch -p1` on top of the
previous one. The supported configurations are the sequential prefixes: 01, then
01+02, then 01+02+03, and so on. Applying a later patch without its predecessors
is not supported.

### 01-state-dir-guard.patch  (feature: fleet hijack-guard)

Change: adds an early exit at the top of `server.ts`. If `TELEGRAM_STATE_DIR` is
not set, the process writes one line to stderr and exits 0 instead of falling back
to the default `telegram` state directory.

Why: a session that merely has the plugin enabled (for example a desktop or
"agent mode" session) would otherwise load the poller with no state dir, adopt
whichever bot owns the default directory, start a second poller on that bot's
token, and stale-kill the real bot. The guard stops that at the source.
Configured fleet bots always set the env, so they are unaffected.

### 02-attachments.patch  (feature: voice notes / file attachments)

Change: inserts a marker comment above the existing `download_attachment` tool.
No behavior changes.

Why this is a marker only: attachment and voice support (the `download_attachment`
tool, the document / voice / audio / video / video_note / sticker handlers, and
the `attachment_*` inbound meta) is already shipped by upstream `server.ts` at
version 0.0.6. When these patches were written that support was verified present
and byte-identical to the fleet's own copy, so there is no code to add. The marker
documents that the fleet's voice-notes skill depends on this upstream tool, and
keeps the five-patch numbering aligned with the feature map (voice notes -> 02).
On this plugin version, voice notes therefore need no code patch. If a future
upstream version ever drops attachment support, this patch would need to become a
real re-add.

### 03-liveness.patch  (feature: watchdog liveness detection + diagnostics)

Change: three additions, all guarded by try/catch so they can never break polling.
- Writes `poller.heartbeat` on every successful `getUpdates` cycle and
  `last-inbound` when a batch carries a message, via a grammy API transformer.
  Zeroes the heartbeat at the two "polling is dead but process is alive" exits
  (clean `bot.stop()` and persistent 409). This is what the watchdog reads to tell
  a live poller from a wedged one.
- Adds a `reason` argument to `shutdown()` and names each trigger (stdin end/close,
  each signal, each orphan condition) so an unexpected death can be traced.
- Writes per-bot start and death diagnostic logs. The upstream fleet wrote these to
  `/tmp`; this patch writes them under `$TELEGRAM_STATE_DIR/logs/`
  (`poller-starts.log`, `poller-deaths.log`) so they are per-bot and cleaned up with
  the state dir.

### 04-fresh-midtask.patch  (feature: mid-task /fresh)

Change: adds handling for a bare `/fresh` inside the poller, before Claude Code's
idle-vs-busy split, so it works mid-task (the idle-only hook cannot). It resolves
this bot's working directory, finds the owning `claude` process by walking the
parent chain, and spawns a detached restart.

Genericization: the upstream fleet resolved the workdir from a hardcoded map of
personal worktree paths and spawned a personal `~/.local/bin` script. This patch
replaces that map with `resolveWorkdir()`, which reads `fleet.json` from
`$FLEET_CONFIG` (default `$FLEET_HOME/fleet.json`, `$FLEET_HOME` default
`~/.claude/telegram-fleet`) and matches this bot's `state_dir` to find its
`workdir`. Missing or unreadable config produces a graceful reply, never a crash.
The restart is spawned as `$FLEET_HOME/bin/fleet-fresh`. The process-discovery,
POSIX safe-quoting, and detached (`nohup ... & disown`) techniques are unchanged.
No usernames, machine paths, persona names, or chat ids appear anywhere.

### 05-model-midtask.patch  (feature: mid-task /model)

Change: adds handling for `/model` and `/model <args>` inside the poller
(case-insensitive, since phones auto-capitalize), before the idle-vs-busy split.
It resolves the workdir with the same `resolveWorkdir()` from patch 04 and spawns
`$FLEET_HOME/bin/fleet-model`, which owns validation, the reply, and the restart.

Genericization: same as patch 04. The personal model-engine path is replaced with
`$FLEET_HOME/bin/fleet-model`, args are passed through the shared `shq()`
single-quoter, and the workdir comes from `fleet.json`.

## Feature-to-patch map

```
fleet hijack-guard (default-dir hijack) ...... 01
voice notes / file attachments ............... 02  (marker; upstream already ships it)
watchdog liveness detection .................. 03
mid-task /fresh .............................. 04
mid-task /model .............................. 05
idle-path /fresh, /resume, /model ............ none (UserPromptSubmit hooks; zero patches)
```

## Notes on the genericized fleet.json read (patches 04 and 05)

The resolver added by patch 04 and reused by patch 05:

- reads `$FLEET_CONFIG`, else `$FLEET_HOME/fleet.json`, else
  `~/.claude/telegram-fleet/fleet.json`;
- parses it with `JSON.parse` over `fs.readFileSync`, wrapped so any error returns
  `null` rather than throwing;
- expands a leading `~` in configured paths;
- matches the bot whose `state_dir` equals this poller's `STATE_DIR` and returns
  its `workdir`;
- on any miss (no config, bad JSON, no matching bot) the handlers reply that they
  cannot act automatically and return, leaving the poller running.

This keeps the poller safe on a machine with no fleet config: `/fresh` and `/model`
degrade to a clear message instead of crashing.
