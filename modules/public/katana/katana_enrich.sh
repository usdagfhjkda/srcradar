#!/usr/bin/env bash
# katana_enrich.sh — CLI entry point (called manually by the daily-url stage + the dashboard)
#
# Usage:
#   katana_enrich.sh --db <path> --business <name> --hosts-file <file>
#   katana_enrich.sh --help
#
# Exit codes:
#   0  = ok
#   3  = katana binary missing
#   4  = db / hosts-file does not exist
#   5  = hosts file is empty
#   6  = business not in the businesses table
#   non-zero = katana_runner.py raised on its own
#
# Environment variables:
#   KATANA_BIN            binary path (env > PATH > error)
#   KATANA_TIMEOUT        per-host timeout (default 600s, same as scan_urls)
#   KATANA_EXTRA_RISKS    extra risk words (comma separated, 5 words by default)
#   KATANA_OMIT_BODY=1    turn off storing the body (response_hash degrades to status|length)
#   KATANA_EXTRA_FLAGS    extra katana flags (space separated)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
RUNNER="${LIB_DIR}/katana_runner.py"

log()  { printf '[katana] %s\n' "$*"; }
err()  { printf '[katana][err] %s\n' "$*" >&2; }
usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}"
}

DB=""
BUSINESS=""
HOSTS_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --db)         DB="$2"; shift 2 ;;
        --business)   BUSINESS="$2"; shift 2 ;;
        --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) err "unknown arg: $1"; usage >&2; exit 2 ;;
    esac
done

if [ -z "$DB" ] || [ -z "$BUSINESS" ] || [ -z "$HOSTS_FILE" ]; then
    err "missing required: --db / --business / --hosts-file"
    usage >&2
    exit 2
fi

if [ ! -f "$RUNNER" ]; then
    err "runner missing: $RUNNER"
    exit 3
fi

# exec is intentional: it lets the python process take over directly, so signals (Ctrl+C) reach the runner more cleanly;
# there is no logic after it, SC2093 is a false positive.
# shellcheck disable=SC2093
exec python3 "$RUNNER" enrich \
    --db "$DB" \
    --business "$BUSINESS" \
    --hosts-file "$HOSTS_FILE"
