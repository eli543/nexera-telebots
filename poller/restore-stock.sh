#!/usr/bin/env bash
# Restore a verified stock backup without overwriting unknown server modifications.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECT_VERSION='0.0.7'
STOCK_SHA='d902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458'
PATCHED_SHA='96de02805e65730bfaecb95117206b9bc652d14818daa363d07c534d50933973'
if [[ $# -gt 1 ]]; then printf '%s\n' 'Usage: restore-stock.sh [--help]' >&2; exit 20; fi
case "${1:-}" in
  -h|--help) printf '%s\n' 'Restore server.ts.stock-backup after verifying its stock hash.' 'Usage: restore-stock.sh [--help]'; exit 0 ;;
  '') ;;
  *) printf '%s\n' 'Usage: restore-stock.sh [--help]' >&2; exit 20 ;;
esac
# shellcheck source=overlay-common.sh
source "$SCRIPT_DIR/overlay-common.sh"
overlay_run restore
