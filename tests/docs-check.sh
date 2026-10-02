#!/usr/bin/env bash
# Check every fleet-* command named in public docs against engine/.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
docs=(
  "$root/README.md"
  "$root/SETUP-WITH-CLAUDE.md"
  "$root/SECURITY.md"
  "$root/NOTICE.md"
  "$root/CHANGELOG.md"
  "$root"/docs/*.md
)

for doc in "${docs[@]}"; do
  if [[ ! -f "$doc" ]]; then
    printf 'Missing docs file: %s\n' "$doc" >&2
    exit 1
  fi
done

names=$(grep -Eoh 'fleet-[a-z-]+' "${docs[@]}" | LC_ALL=C sort -u || true)
if [[ -z "$names" ]]; then
  printf 'No fleet commands found in docs\n' >&2
  exit 1
fi

missing=0
while IFS= read -r name; do
  if [[ -f "$root/engine/$name" ]]; then
    printf 'OK %s\n' "$name"
  else
    printf 'MISSING %s (engine/%s)\n' "$name" "$name" >&2
    missing=1
  fi
done <<< "$names"

exit "$missing"
