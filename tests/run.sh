#!/bin/bash
# Offline test + static-check harness for the plugin hooks layer. Runs from anywhere.
#   - python3 -m py_compile on every hook .py
#   - bash -n on every hook .sh
#   - the two offline unit-test suites (tldr-nudge, tg-deliver-nudge)
# No network, no bot token, no live session touched. Exit 0 only if everything passes.

set -u

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
hooks="$root/plugin/hooks"
fails=0

echo "== py_compile (hook .py) =="
for f in "$hooks"/*.py; do
  [ -e "$f" ] || continue
  if python3 -m py_compile "$f" 2>/tmp/fleet-pycompile-err; then
    echo "  ok    $(basename "$f")"
  else
    echo "  FAIL  $(basename "$f")"; sed 's/^/        /' /tmp/fleet-pycompile-err; fails=$((fails + 1))
  fi
done

echo "== bash -n (hook .sh) =="
for f in "$hooks"/*.sh; do
  [ -e "$f" ] || continue
  if bash -n "$f" 2>/tmp/fleet-bashn-err; then
    echo "  ok    $(basename "$f")"
  else
    echo "  FAIL  $(basename "$f")"; sed 's/^/        /' /tmp/fleet-bashn-err; fails=$((fails + 1))
  fi
done

echo "== unit tests =="
for t in tldr-nudge.test.py tg-deliver-nudge.test.py; do
  echo "-- $t --"
  if python3 "$here/$t"; then
    echo "  ok    $t"
  else
    echo "  FAIL  $t"; fails=$((fails + 1))
  fi
done

# Tidy the bytecode py_compile leaves behind so it never lands in the repo.
rm -rf "$hooks/__pycache__" "$here/__pycache__" 2>/dev/null || true

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
  exit 0
else
  echo "$fails CHECK(S) FAILED"
  exit 1
fi
