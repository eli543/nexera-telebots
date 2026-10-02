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

During a parallel build, add `--phase1`. This runs installer and uninstaller checks against sandbox-only overlay stubs and records STUB for package scenarios that cannot yet run. STUB is not a pass. Once the other packages are merged, omit `--phase1`. Integration mode fails if a suite or a required package is missing.

Each of the 21 scenarios gets a new root on each of two passes. `results.json` and shim logs stay under the selected base. The untouched check compares static files in the real hooks, official plugin cache, local bin and LaunchAgents trees, the home root listing, the main checkout's branch, HEAD and status, and named live daemon PIDs before and after both passes. If macOS denies read-only process inspection, phase 1 reports PARTIAL and integration mode fails. Live channel logs are excluded because running bots write them independently.

The shim commands never execute Claude Code, launchctl, AppleScript, browser opening, Keychain reads, process kills or code signing. The fake transcriber is for offline voice fixtures only.
