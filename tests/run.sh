#!/usr/bin/env bash
# Local checks. Set TELEBOTS_SANDBOX_BASE to a disposable directory first.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---all}"

if [ -z "${TELEBOTS_SANDBOX_ROOT:-}" ]; then
  MODE=--all
  allow_missing_stock=0
  for arg in "$@"; do
    case "$arg" in
      --all|--static-only|--unit-only|--phase1) MODE="$arg" ;;
      --allow-missing-stock) allow_missing_stock=1 ;;
      *) printf 'Unknown test option: %s\n' "$arg" >&2; exit 2 ;;
    esac
  done
  real_home="$HOME"
  main_checkout="${TELEBOTS_MAIN_CHECKOUT:-$ROOT}"
  base="${TELEBOTS_SANDBOX_BASE:?Set TELEBOTS_SANDBOX_BASE to a disposable test directory}"
  # Discover before sbenv replaces HOME with the disposable sandbox home.
  source "$ROOT/tests/sandbox/stock-source.sh"
  stock_source=''
  if ! stock_source="$(/usr/bin/python3 - "$real_home" "${TELEBOTS_STOCK_SERVER_SOURCE:-}" "$base/inputs/stock-server.ts" "$STOCK_SHA256" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

home, explicit, staged, expected = sys.argv[1:]
marketplace = Path(home) / '.claude/plugins/marketplaces/claude-plugins-official/external_plugins/telegram/server.ts'
cache = Path(home) / '.claude/plugins/cache/claude-plugins-official/telegram'
cached = sorted((p for p in cache.glob('*/server.ts') if p.is_file()),
                key=lambda p: p.stat().st_mtime, reverse=True)
candidates = [Path(explicit)] if explicit else [marketplace, *cached, Path(staged)]
for candidate in candidates:
    if not candidate.is_file():
        if explicit:
            print(f'TELEBOTS_STOCK_SERVER_SOURCE file does not exist: {candidate}', file=sys.stderr)
        continue
    actual = sha256(candidate.read_bytes()).hexdigest()
    if actual == expected:
        print(candidate)
        break
    print(f'Rejected {candidate}: not stock (SHA-256 {actual}; pin {expected}).', file=sys.stderr)
else:
    print(f'Stock server unavailable: set TELEBOTS_STOCK_SERVER_SOURCE to a stock server.ts with SHA-256 {expected}.', file=sys.stderr)
    sys.exit(1)
PY
)"; then
    if [ "$allow_missing_stock" -ne 1 ]; then exit 1; fi
  fi
  mkdir -p "$base"
  unit_root="$(mktemp -d "$base/unit-XXXXXX")"
  touch "$unit_root/.telebots-sandbox"
  mkdir -p "$unit_root/home" "$unit_root/tmp" "$unit_root/cache" "$unit_root/tools" "$unit_root/logs" "$unit_root/inputs"
  cp -R "$ROOT/tests/sandbox/shims" "$unit_root/shims"
  /usr/bin/python3 - "$ROOT" "$unit_root/repo" <<'PY'
import shutil
import sys
shutil.copytree(sys.argv[1], sys.argv[2], symlinks=True,
                ignore=shutil.ignore_patterns('.git', 'node_modules', '__pycache__', '*.pyc'))
PY
  if [ -n "$stock_source" ]; then
    cp "$stock_source" "$unit_root/inputs/stock-server.ts"
  fi
  if [ -n "${TELEBOTS_LIVE_STREAMER_SOURCE:-}" ]; then
    cp "$TELEBOTS_LIVE_STREAMER_SOURCE" "$unit_root/inputs/live-tg-stream.py"
  fi
  rm -f "$unit_root/shims/bun"
  for tool in bun shellcheck; do
    found="$(command -v "$tool" || true)"
    if [ -n "$found" ]; then ln -s "$found" "$unit_root/tools/$tool"; fi
  done
  /bin/bash "$ROOT/tests/sandbox/sbenv" "$unit_root" /usr/bin/git -C "$ROOT" diff --check
  exec /bin/bash "$ROOT/tests/sandbox/sbenv" "$unit_root" /bin/bash "$unit_root/repo/tests/run.sh" "$MODE" "$real_home" "$main_checkout" "$allow_missing_stock"
fi

cd "$ROOT"
allow_missing_stock="${4:-0}"
echo "== Shell syntax and shellcheck =="
while IFS= read -r file; do
  first="$(head -n 1 "$file")"
  is_shell=0
  case "$file" in *.sh) is_shell=1 ;; esac
  case "$first" in '#!'*bash*|'#!'*'/sh'*) is_shell=1 ;; esac
  if [ "$is_shell" -eq 1 ]; then
    /bin/bash -n "$file"
    shellcheck -S error "$file"
  fi
done < <(find . -name .git -prune -o -name node_modules -prune -o -type f -print | sort -u)

echo "== Python compile =="
while IFS= read -r file; do
  python3 -m py_compile "$file"
done < <(find . -name .git -prune -o -name node_modules -prune -o -name '*.py' -type f -print | sort -u)

echo "== JSON validity =="
while IFS= read -r file; do
  python3 -m json.tool "$file" >/dev/null
done < <(find . -name .git -prune -o -name node_modules -prune -o -name '*.json' -type f -print | sort -u)

echo "== Workflow syntax =="
if command -v actionlint >/dev/null 2>&1; then
  actionlint .github/workflows/*.yml
else
  ruby -e 'require "yaml"; ARGV.each { |file| YAML.load_file(file) }' .github/workflows/*.yml
  echo "actionlint unavailable; Ruby YAML parser passed."
fi

HOOK_TEST_DIR="$ROOT/tests/hooks"
echo "== Python unit tests =="
for test in "$HOOK_TEST_DIR"/*.test.py "$ROOT/tests/engine"/*.test.py "$ROOT/tests/engine"/test_*.py "$ROOT/tests/ops"/*.test.py "$ROOT/tests/ops"/test_*.py; do
  [ -f "$test" ] || continue
  case "$test" in
    */streamer-smoke.test.py) python3 "$test" --require-404-fallback ;;
    *) python3 "$test" ;;
  esac
done
if [ -f "$ROOT/tests/hooks/ast_equiv.py" ]; then
  if [ -f "$TELEBOTS_SANDBOX_ROOT/inputs/live-tg-stream.py" ]; then
    python3 "$ROOT/tests/hooks/ast_equiv.py" --live "$TELEBOTS_SANDBOX_ROOT/inputs/live-tg-stream.py" --self-test
  else
    python3 "$ROOT/tests/hooks/ast_equiv.py" --self-test
  fi
fi

echo "== Documentation commands =="
/bin/bash "$ROOT/tests/docs-check.sh"

if [ -f poller/telebots.ts ]; then
  if [ -f "$TELEBOTS_STOCK_SERVER" ]; then
    echo "== TypeScript and Bun tests =="
    if [ -f poller/package.json ]; then
      (cd poller && bun install --frozen-lockfile)
    fi
    if [ -f poller/tsconfig.json ]; then
      bun x tsc --noEmit --project poller/tsconfig.json
    fi
    /bin/bash tests/sandbox/typecheck-overlay.sh
    bun test tests/poller
  elif [ "$allow_missing_stock" -eq 1 ]; then
    echo "SKIPPED poller typecheck: pinned stock server.ts is unavailable."
    echo "SKIPPED poller tests: pinned stock server.ts is unavailable."
  else
    printf 'Pinned stock server.ts is missing: %s\n' "$TELEBOTS_STOCK_SERVER" >&2
    exit 1
  fi
else
  echo "SKIP TypeScript and Bun tests: WP1 poller is not present in this worktree."
fi

echo "== Privacy and token gate =="
privacy_status=0
python3 tests/sandbox/privacy-check.py || privacy_status=$?

if [ "$MODE" != "--static-only" ] && [ "$MODE" != "--unit-only" ]; then
  echo "== Sandbox scenarios =="
  if [ "$MODE" = "--phase1" ]; then
    /bin/bash tests/sandbox/run.sh --base "$(dirname "$TELEBOTS_SANDBOX_ROOT")/scenario-runs" --real-home "${2:?}" --main-checkout "${3:?}" --phase1
  elif [ "$allow_missing_stock" -eq 1 ] && [ ! -f "$TELEBOTS_STOCK_SERVER" ]; then
    echo "SKIPPED sandbox integration scenarios: pinned stock server.ts is unavailable."
  else
    scenario_args=(--base "$(dirname "$TELEBOTS_SANDBOX_ROOT")/scenario-runs" --real-home "${2:?}" --main-checkout "${3:?}" --stock-server "$TELEBOTS_STOCK_SERVER")
    if [ -f "$TELEBOTS_SANDBOX_ROOT/inputs/live-tg-stream.py" ]; then
      scenario_args+=(--live-streamer "$TELEBOTS_SANDBOX_ROOT/inputs/live-tg-stream.py")
    fi
    /bin/bash tests/sandbox/run.sh "${scenario_args[@]}"
  fi
fi
if [ "$privacy_status" -ne 0 ]; then exit "$privacy_status"; fi
echo "LOCAL CHECKS PASSED"
