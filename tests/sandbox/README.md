# Offline sandbox checks

Run from a checkout on macOS:

```sh
bash tests/sandbox/run.sh \
  --base /tmp/telebots-sandbox-runs \
  --real-home "$HOME" \
  --main-checkout "$PWD" \
  --stock-server /path/to/verified/stock-0.0.7-server.ts
```

The stock file must match the pinned SHA-256 in `runner.py`. The runner copies it into a disposable home. It never runs a poller against the user's plugin cache or uses an inherited Telegram token.

For the full suite, set `TELEBOTS_SANDBOX_BASE` to a disposable directory outside your home and run `bash tests/run.sh --all`.
The full runner requires the pinned stock 0.0.7 `server.ts` and auto-discovers it in the official marketplace clone or newest plugin cache; set `TELEBOTS_STOCK_SERVER_SOURCE` if you keep a verified copy elsewhere.

During a parallel build, add `--phase1`. This runs installer and uninstaller checks against sandbox-only overlay stubs and records STUB for package scenarios that cannot yet run. STUB is not a pass. Once the other packages are merged, omit `--phase1`. Integration mode fails if a suite or a required package is missing.

Each pass uses one disposable root for scenarios 1-20 and a second root for the non-Mac installer check, scenario 21. The poller, engine, hook and ops suites run once per pass; their assertions support several scenario entries. The 42-result summary counts 21 grouped scenario decisions on each of two passes. It does not mean 42 isolated installs or 42 independent suite runs. `results.json` and shim logs stay under the selected base.

The untouched check compares SHA-256 hashes for official Telegram plugin files, real hooks and settings; name, size and modification time for LaunchAgents and the fleet directory; the main checkout's HEAD and status; and the PIDs returned by `launchctl list` for three named daemon labels. It ignores transient child processes. Runtime channels, sessions, projects, logs, `.in_use`, synced markers and the fleet window-state file are excluded. The check reports the exact protected path that changed. If macOS denies the read-only label lookup, phase 1 reports PARTIAL and integration mode fails.

The shim commands never execute Claude Code, launchctl, AppleScript, browser opening, Keychain reads, process kills or code signing. The fake transcriber is for offline voice fixtures only.
