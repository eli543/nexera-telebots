#!/usr/bin/env bash
# Run all 21 offline scenarios twice. See runner.py for phase 1 stub status.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$here/runner.py" "$@"
