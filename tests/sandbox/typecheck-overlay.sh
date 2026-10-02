#!/usr/bin/env bash
# Typecheck the patched official server and the Telebots modules together.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
sandbox="${TELEBOTS_SANDBOX_ROOT:?Run through tests/sandbox/sbenv}"
stock="${TELEBOTS_STOCK_SERVER:?Set the pinned stock server input}"
test -f "$stock"
test -f "$root/poller/node_modules/typescript/bin/tsc"
stage="$sandbox/typecheck-overlay"
mkdir -p "$stage/slot"
cp "$stock" "$stage/slot/server.ts"
for file in telebots.ts email.ts contracts.ts ux.json; do
  cp "$root/poller/$file" "$stage/slot/$file"
done
patch --batch --forward -p1 -d "$stage/slot" < "$root/poller/seam-0.0.7.patch" >/dev/null
ln -s "$root/poller/node_modules" "$stage/slot/node_modules"
python3 - "$stage/tsconfig.json" "$root/poller/tsconfig.json" "$stage/slot" <<'PY'
import json
from pathlib import Path
import sys

output, inherited, slot = map(Path, sys.argv[1:])
value = {"extends": str(inherited), "files": [str(slot / name) for name in
         ("server.ts", "telebots.ts", "email.ts", "contracts.ts")], "include": []}
output.write_text(json.dumps(value))
PY
bun "$root/poller/node_modules/typescript/bin/tsc" --noEmit --project "$stage/tsconfig.json"
echo "Patched server and Telebots modules passed strict TypeScript checking."
