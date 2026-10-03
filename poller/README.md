# Official Telegram plugin overlay

Telebots adds one small seam to your installed `telegram@claude-plugins-official`
0.0.7 and puts its own feature code beside it. The official plugin remains the
channel entry point. No complete upstream `server.ts` is shipped in this repo.

## License and change notice

The modified file belongs to Anthropic's official Telegram plugin, copyright
2026 Anthropic, PBC, licensed under Apache-2.0. The upstream license is left
untouched. This directory distributes a unified diff against the user's local
copy and Telebots' own MIT code. There is no implication of Anthropic endorsement.

For Apache-2.0 section 4(b), the table below records every seam change. The
modified file also carries a Telebots change comment. The prior nine-patch
0.0.6 set is replaced by this single 0.0.7 seam.

| Seam | Change to the upstream file |
| --- | --- |
| 1 | Exit before reading credentials when `TELEGRAM_STATE_DIR` is absent. |
| 2 | Inspect the nearest `claude` ancestor. A non-channel ancestor still serves stock MCP tools, but cannot stale-kill, write `bot.pid` or heartbeat, run approval checks, import or initialize Telebots, mutate feature state, or poll. Tests skip all PID probes and signals. |
| 3 | Write the module's `caps` file only after this process writes `bot.pid` and callback registration succeeds. |
| 4 | Dynamically import and initialize `telebots.ts` in a guarded block only for polling-enabled loads. Missing imports and initialization failures leave stock tools and polling available. Callback setup failure disposes the module and uses the same fallback. |
| 5 | Keep `poller.heartbeat`, `last-inbound`, and successful poll logging in the seam. Zero heartbeat on shutdown, stopped polling, aborted setup, and persistent conflict. The module is not needed for these signals. |
| 6 | Add reply-button schema and delegate validation, chunk placement, and one-time quick-bar bookkeeping to the module. |
| 7 | Append module tools and route `email_preview` and `email_send`. Invalidate preview consent before any `edit_message` API attempt, including one that fails. |
| 8 | Accept `attachment_file_id` as an alias for `download_attachment.file_id`; require one nonempty ID. |
| 9 | Register the module's callback families before stock's catch-all permission handler. Unknown permission callbacks continue into stock handling. |
| 10 | Include voice and audio duration and caption in attachment metadata. |
| 11 | After stock access control, run the module intercept before the permission-reply intercept. Queue ordinary forwarding through the module; retain stock forwarding as the fallback. |
| 12 | Use the module's explicit update list and poll-start menu registration, with stock update defaults and menu when unavailable. |
| 13 | Record starts and named shutdown reasons in `<state>/logs/`. Remove the PID file only when this process wrote it and still owns it. |
| 14 | Keep stock MCP instructions and add the already-transcribed voice rule. Tests cap the resulting instructions and each tool description at 2,000 characters. |

The seam also accepts an API origin for isolated tests. `TELEBOTS_API_ROOT` is
honored only when `TELEBOTS_TEST=1` and the URL is a literal HTTP(S) origin at
`127.0.0.1`, `localhost`, or `[::1]`, with an optional port. Userinfo, paths,
query strings, fragments, alternate numeric host spellings, and remote hosts
are rejected. The same origin is used for stock attachment and photo downloads.
Invalid overrides are ignored with a fixed diagnostic that contains no URL or
credential. Test mode disables the entire stale-PID block, including signal 0.

## Install, verify, restore

```sh
./poller/install-overlay.sh
./poller/verify-overlay.sh --status
./poller/restore-stock.sh
```

The scripts need Bash, Python 3, and `patch`. They select the highest installed
semantic-version cache slot, including a newest slot whose server is missing.
They check its `.claude-plugin/plugin.json` before reading its server hash. An
unsupported version never causes a fallback to modifying an older slot.

Stock SHA-256:

```text
d902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458
```

Patched SHA-256:

```text
96de02805e65730bfaecb95117206b9bc652d14818daa363d07c534d50933973
```

Install accepts only those two server hashes and plugin 0.0.7. It dry-runs the
seam on verified stock, applies it in a temporary directory in the same cache
slot, and verifies the result before publishing any file. There is no force
flag. `server.ts.stock-backup` is created once from verified stock; an existing
unverified backup is refused. A patched server without its stock backup is
also refused.

The installed bundle is `telebots.ts`, `email.ts`, `contracts.ts`, and `ux.json`.
The marker `.telebots-overlay.json` records their hashes and both server pins.
Verification checks actual support-file bytes against the marker and this
package's source files, and checks the backup. Reinstall repairs absent or
modified support files when the server and backup remain trusted. A fully
verified second installation changes no file or timestamp.

A per-slot lock prevents overlapping overlay operations. All replacements use
same-filesystem atomic renames. Dependencies and the marker arrive before
`server.ts`; a handled write failure restores the previous bytes. No multi-file
filesystem transaction can guarantee rollback after power loss. The verifier
will detect an interrupted or incomplete bundle on the next check.

Restore preflight reports RESTORE, STOCK, ABSENT, or OTHER_VERSION:<version>.
Only an exact patched 0.0.7 server hash with a verified stock backup is restored.
The restore command can find that slot beneath a newer official plugin version;
it leaves the newer slot untouched. Stock and absent slots need no restoration.
Unknown server modifications are refused before uninstall removes anything.
Restore replaces the server atomically, removes unchanged support files named
by the installed marker, leaves locally modified support files inert, and
retains the stock backup. These scripts never start, stop, or signal a bot.
A running process loads changes on its next restart.

## Status and exit codes

`verify-overlay.sh --status` writes exactly one token to stdout. Only `OK`
returns zero. The human-readable form without `--status` uses the same code.

| Token | Exit | Meaning |
| --- | --- | --- |
| `OK` | 0 | Pinned server, complete support bundle, marker, and stock backup verified. |
| `STOCK` | 10 | The server is the pinned, unchanged 0.0.7 stock file. |
| `MISSING_MODULE` | 11 | The patched server is present but at least one support file is absent. |
| `TAMPERED` | 12 | Unknown server bytes, a changed support file, invalid marker or backup, or an unsafe file type. |
| `VERSION_MISMATCH:<ver>` | 13 | The newest slot has a different version. Invalid metadata uses `unknown`. |
| `NOT_INSTALLED` | 14 | No cache slot or no server in the selected supported slot. |

Install and restore return zero on success. Their refusal codes are 12 for an
unknown server hash or unsafe destination, 13 for a version mismatch, 14 for a
missing installation, 20 for invalid arguments, 21 for an incomplete package,
22 for a missing or corrupt required stock backup, 23 for a failed patch or
patched-hash mismatch, 24 for a lock or concurrent modification, and 25 for an
I/O failure. No gate has an override.

An official plugin update can remove the overlay. For a supported stock slot,
reinstall this overlay. For a different version, keep stock behavior and wait
for a reviewed seam for that version; do not apply this patch to it. Watchdog
and restart-engine integration belongs to the operations packages and uses
these exact C5 tokens to choose PID-only health checks when extras are absent.

## Live-function port map

Ranges refer to the 3,091-line source used for the port. Function names are the
stable review keys; small differences between source snapshots can move lines.
All feature methods listed below are in `TelebotsModule` in `telebots.ts`
unless another home is named.

| Live source or functions | New home |
| --- | --- |
| State-dir guard, 27-37 | Seam 1. |
| Startup diagnostics, 76-86; `nearestClaudeAncestor`, 87-140; stale-PID and PID write, 141-173 | Seam `pollerLog`, `nearestClaudeAncestor`, and guarded PID block. Logs contain process IDs and fixed state, never parent argv. |
| Caps block, 175-192 | `afterPidWrite`. |
| `defaultAccess`, `assertSendable`, `readAccessFile`, `loadAccess`, `assertAllowedChat`, `saveAccess`, `pruneExpired`, `gate`, `dmCommandGate`, `isMentioned`, `checkApprovals`, `chunk`, `safeName` | Upstream implementations stay in `server.ts`. The seam additionally disables the approval interval for non-channel loads. |
| `QUICK_BAR` and lifetime flag, 505-523 | `DEFAULT_UX.quick_bar`, `replyMarkup`, and `replySent`. |
| MCP instructions, 540-550 | Seam 14. |
| Reply buttons, validation and layout, 613-617 and 701-777 | Seam 6 plus exported `replyButtons`, `replyMarkup`, and `replySent`. |
| `downloadToInbox`, 667-687 | `downloadToInbox` in the module for transcription; stock attachment download stays in the seam-patched tool. |
| Attachment ID alias, 817-825 | Seam 8. |
| `shutdown` and orphan timer, 856-902 | Seam 13; upstream stdin-based orphan detection is retained. |
| `resumeAllowlistHas`, 953-974 | `diskAllows` and `allowedCallback`, reading disk on each press. |
| Callback registration for `eff:`, `qr:`, `md:`, `me:`, `ho:`, `rs:`, `rsx:`, 976-1153 | `registerHandlers` builds a local Composer, then attaches it once; `handleCallback` dispatches the families. |
| Voice and audio metadata handlers, 1257-1283 | Seam 10. |
| `normalizeReactionEmoji`, `addedReactionEmoji`, reaction handler, 1316-1373 | The local `normalize` in exported `addedReactionEmoji`, and `registerHandlers`. Phrases come from `DEFAULT_UX` and `ux.json`. |
| Engine constants, regexes, model tables and `isEffortLevel`, 1395-1521 | `engine`, command regexes, `EFFORTS`, `MODELS`, `MODEL_LABELS`, and anchored validation in `intercept` / `handleCallback`. |
| `shq`, 1523-1528 | Exported `shq` and `spawnEngine`; detached engine argv stays POSIX-quoted. |
| Fixed worktree/handoff tables, 1530-1558 | `fleet`, `self`, `expand`, `samePath`, and `otherBot`, reading `fleet.json`. |
| `freshLog`, 1560-1570 | `logEvent` plus per-engine logs under `<state>/logs`. |
| `findOwningClaudePid`, 1572-1591 | Exported `findOwningClaudePid` and `ownerPid`. The seam passes its already-resolved nearest owner. |
| `handleFresh`, `handleModel`, `handleEsc`, `handleCompact`, `handleUpdate`, `handleContext`, `handleUsage` | `intercept` and `handleEngine`, preserving the C6 argv contracts. |
| `handleEffort`, `readModelState`, `handleEffortPick`, `handleEffortClose`, `handleModelPicker`, `handleModelPick`, `handleModelEffortPick`, `handleModelEffortClose`, 1661-1820 | `handleEffort`, `readModelState`, `effortKeyboard`, `handleModelPicker`, `handleModelPick`, and the matching pick/close branches in `handleCallback`. |
| `handleQuickReply`, `injectTap`, `injectReaction`, 1822-1956 | `handleQuickReply`, `inject`, reaction middleware, `enqueue`, and `forwardInbound`. Generic send-confirm bubbles cannot authorize email. |
| `execFileAsync`, `readFleetState`, `handoffSelfWord`, `handoffLabel`, `handoffRowFor`, `handleHandoffPicker`, `handleHandoffPick`, `handleHandoffCancel`, 1958-2137 | `execOutput`, `readFleetState`, `self`, `otherBot`, `handoffLabel`, `handoffRowFor`, `handleHandoffPicker`, `handleHandoffPick`, and the cancel branch of `handleCallback`. |
| `KEY_TEXT`, `handleKey`, 2254-2280 | Exported `keyText`, `DEFAULT_UX`, and the `/key` branch of `intercept`. |
| `isExecutableFile`, 2341-2354 | `executable`. |
| `renderResumePicker`, `handleResumeOpen`, `handleResumeRefresh`, `handleResumeClose`, 2356-2522 | Same-named module methods, with the in-flight latch and 60-second release watchdog. Typed `/resume` stays with its existing hook. |
| `formatVoiceDuration`, `voiceHeader`, `voiceBody`, `transcribeErrorStatus`, `firstErrorLine`, 2524-2612 | `formatVoiceDuration`, `voiceBody`, and status handling in `transcribeInbound`. Logs retain status/timing/count evidence without arbitrary error text. |
| `enqueueInbound`, 2614-2627 | `enqueue` and `drain`, keeping per-chat arrival order. |
| `transcribeInbound`, 2629-2700 | Same-named module method, calling `$FLEET_HOME/bin/transcribe-voice`. |
| `forwardInbound`, 2702-2778 | Same-named module method and `ForwardParams` in `contracts.ts`. |
| `handleInbound`, 2780-2968 | Stock gate and permission path in the seam; `intercept` and `enqueueForward` perform Telebots work. |
| `wdWrite`, heartbeat transformer and poll loop, 2982-3091 | Seam 5 and 12; `onPollStart` supplies the 13-command menu from `ux.json`. |
| New exact-email consent and tools | `email.ts`: `canonicalEmail`, `emailDigest`, `emailText`, `ConsentStore`, `EmailService`, and `emailTools`; exposed by `callTool`, `beforeEdit`, and the `mail:` callback branch. |

## Email adapter contract

`email_preview` displays every recipient, the subject, and the complete plain
text body in one message. The limit is 3,900 characters including headers;
oversized emails are refused. Send it replaces the buttons with Cancel first
and Confirm send second. Only the second tap authorizes `email_send` for the
identical email, chat, and consent ID. Editing the preview or restarting the
poller cancels unfinished consent and requires a fresh preview and both taps.
Nonchannel diagnostic loads expose stock tools and do not initialize consent.

The poller alone holds active consent in process memory. It records a Confirm
send tap only after receiving the matching Telegram callback for the exact
private chat, preview message and email digest. `email_send` checks that memory,
then spends the consent before one backend attempt. A poller restart loses all
active consent. `consents.json` is a mode 600 audit record keyed by the
24-character hex ID. Its `phase` is never accepted as send authorization.
Records contain `id`, `chat`, `message`, `digest`, `expires`, `phase`, and the
canonical `email`. Expiry is Unix milliseconds, ten minutes after preview. Digest input
is compact UTF-8 JSON with keys in this order: `bcc`, `body`, `cc`, `subject`,
`to`. Missing Cc and Bcc become empty arrays; recipient order, case, and body
whitespace are preserved. Lone surrogates and hidden header characters are
refused. Phases are `preview`, `confirm`, `authorized`, `consumed`, `cancelled`.

The poller calls `fleet-email send` with only `send` in argv. JSON stdin carries
the exact email, chat, message ID, digest and a random one-time nonce. The
backend checks the current allowlist and reads the endpoint and per-session
secret from a mode-0600 bot state file. It requires an HMAC over the nonce and
digest from the local 127.0.0.1 authority. It never trusts an `authorized` or
`consumed` phase read from disk. This check also wraps custom adapters. Only
then does the adapter claim `<state>/mail-attempts/<id>` with exclusive creation
before contacting SMTP. Return `{"ok":true}` only for confirmed success.
Failed or uncertain attempts remain spent and are never retried automatically.
The poller's 30-second backend deadline does not send a signal; the adapter
must set its own bounded network deadlines.

The normal poller workflow enforces two taps. An assistant with shell permission
in any stance could reach the mail credentials or authority file directly, so
the boundary is as strong as your permission stance makes shell access.

## Verification boundaries

Tests require a scrubbed environment with `TELEBOTS_TEST=1`,
`TELEBOTS_SANDBOX_ROOT`, and sandbox-local `HOME`, `TMPDIR`, `FLEET_HOME`, and
`TELEGRAM_STATE_DIR`. They reject an inherited token environment variable. Put
a verified upstream stock file at `<sandbox>/inputs/stock-server.ts` or set
`TELEBOTS_STOCK_SERVER` to a fixture inside that sandbox. No upstream source is
vendored for tests.

```sh
<sandbox-wrapper> bash -n poller/install-overlay.sh poller/verify-overlay.sh poller/restore-stock.sh poller/overlay-common.sh
<sandbox-wrapper> shellcheck -S error poller/install-overlay.sh poller/verify-overlay.sh poller/restore-stock.sh poller/overlay-common.sh
<sandbox-wrapper> bun test --timeout 0 tests/poller
<sandbox-wrapper> bun poller/node_modules/typescript/bin/tsc -p poller/tsconfig.json
```

The overlay suite proves version/hash gates, every status token, full dependency
installation, idempotence, refusal without changes, failed-write rollback,
restore, callback ordering, and the instruction cap. The module and mock-MCP
suites cover the feature behavior. These offline checks do not establish
phone rendering, real Telegram delivery, macOS permissions, reboot recovery,
or acceptance on a separate user's machine.

The shared fixtures are importable by other sandbox suites. `MockTelegram`
accepts a sandbox directory, binds a random loopback port, exposes `apiRoot`,
and records calls in `telegram-calls.jsonl` without token URLs. Its `message`,
`voice`, `callback`, and `reaction` helpers enqueue updates. `richSupported`,
`pollingError`, and `failNext` script 404, 401, 409, and 429 responses.
`FakeClaudeClient` accepts sandbox paths for `server`, `state`, `fleetHome`,
and `root`, plus that `apiRoot`. It provides `initialize`, `listTools`,
`callTool`, captured `notifications`, and `close`. It launches Bun through a
fixture executable named `claude`; it never starts Claude Code. Closing stdin
ends both fixture processes normally. Test-runner timeouts are disabled because
Bun otherwise signals dangling subprocesses. Individual waits reject normally
and leave EOF cleanup to the test fixture.
