#!/usr/bin/env bash
# Local checks. Set TELEBOTS_SANDBOX_BASE to a disposable directory first.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---all}"

if [ -z "${TELEBOTS_SANDBOX_ROOT:-}" ]; then
  real_home="$HOME"
  main_checkout="${TELEBOTS_MAIN_CHECKOUT:-$ROOT}"
  base="${TELEBOTS_SANDBOX_BASE:?Set TELEBOTS_SANDBOX_BASE to a disposable test directory}"
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
  if [ -n "${TELEBOTS_STOCK_SERVER_SOURCE:-}" ]; then
    cp "$TELEBOTS_STOCK_SERVER_SOURCE" "$unit_root/inputs/stock-server.ts"
  fi
  rm -f "$unit_root/shims/bun"
  for tool in bun shellcheck; do
    found="$(command -v "$tool" || true)"
    if [ -n "$found" ]; then ln -s "$found" "$unit_root/tools/$tool"; fi
  done
  exec /bin/bash "$ROOT/tests/sandbox/sbenv" "$unit_root" /bin/bash "$unit_root/repo/tests/run.sh" "$MODE" "$real_home" "$main_checkout"
fi

cd "$ROOT"
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

# One path variable changes when WP3 moves these two tests to tests/hooks/.
HOOK_TEST_DIR="$ROOT/tests"
echo "== Python unit tests =="
for test in "$HOOK_TEST_DIR"/*.test.py "$ROOT/tests/engine"/*.test.py "$ROOT/tests/engine"/test_*.py "$ROOT/tests/ops"/*.test.py "$ROOT/tests/ops"/test_*.py; do
  [ -f "$test" ] || continue
  python3 "$test"
done
if [ -f "$ROOT/tests/hooks/ast_equiv.py" ]; then
  python3 "$ROOT/tests/hooks/ast_equiv.py" --self-test
fi

if [ -f poller/telebots.ts ]; then
  echo "== TypeScript and Bun tests =="
  if [ -f poller/package.json ]; then
    (cd poller && bun install --frozen-lockfile)
  fi
  if [ -f poller/tsconfig.json ]; then
    bun x tsc --noEmit --project poller/tsconfig.json
  fi
  if [ "$MODE" = "--unit-only" ]; then
    bun test tests/poller/module.test.ts tests/poller/email.test.ts
  else
    bun test tests/poller
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
  else
    /bin/bash tests/sandbox/run.sh --base "$(dirname "$TELEBOTS_SANDBOX_ROOT")/scenario-runs" --real-home "${2:?}" --main-checkout "${3:?}"
  fi
fi
if [ "$privacy_status" -ne 0 ]; then exit "$privacy_status"; fi
echo "LOCAL CHECKS PASSED"
