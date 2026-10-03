#!/usr/bin/env bash
# Fetch and verify the stock server for CI, or compare today's upstream for the watch.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$here/stock-source.sh"

if [ "$#" -ne 2 ]; then
  printf 'Usage: fetch-stock.sh --pinned|--watch OUTPUT\n' >&2
  exit 2
fi
case "$1" in
  --pinned) ref="$STOCK_COMMIT" ;;
  --watch) ref=main ;;
  *) printf 'Unknown fetch mode: %s\n' "$1" >&2; exit 2 ;;
esac
output="$2"
url="$STOCK_RAW_ROOT/$ref/$STOCK_SERVER_PATH"
curl --fail --location --silent --show-error "$url" --output "$output"
actual="$(/usr/bin/python3 - "$output" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys
print(sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
if [ "$actual" != "$STOCK_SHA256" ]; then
  printf 'Upstream Telegram server.ts is not stock for this port. Pinned: %s Current: %s\n' "$STOCK_SHA256" "$actual" >&2
  rm -f "$output"
  exit 1
fi
printf 'Upstream Telegram server.ts matches the supported pin: %s\n' "$STOCK_SHA256"
