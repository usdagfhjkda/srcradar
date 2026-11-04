#!/usr/bin/env bash
# check.sh — only checks the go / python3 / git versions srcradar needs.
#
# Behaviour:
#   - installs nothing.
#   - checks the three tools one by one, each reported separately.
#   - all of them pass: exit 0 + a summary OK.
#   - any of them fails: exit 1 + prints how to fix it (no auto-fix).
#
# Design rationale:
#   the check / check-deps paths of init.sh are coupled to 6+ items, installing
#   what can be installed and reporting what cannot.
#   check.sh has a single responsibility — it only answers "is the environment
#   good enough to run srcradar", and leaves once the answer is given.
#   installing is install.sh's job.
#
# Usage:
#   ./check.sh             # check everything
#   ./check.sh -h|--help   # usage
#
# Thresholds:
#   GO_MIN  = 1.25    (db_align needs Go 1.21+; cdnmatch / ENScan_GO need the latest tag)
#   PY_MIN  = 3.12    (db/init_db.py / daily/lib/*.py / ymicp/*.py really only need >= 3.7,
#                       tightened to 3.12 is for modern pathlib syntax + tomllib safety margin)
#   GIT_MIN = 2.0     (init clones the upstream fork; any >= 2.x works)
#
# Exit codes:
#   0   all three pass
#   1   any of them fails (missing or version too low)
#   2   bad arguments
#
# Related script: install.sh (runs the check.sh checks again at its entry point)

set -euo pipefail

# shellcheck disable=SC2034 # SCRIPT_DIR is referenced by other scripts that source this
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GO_MIN="1.25"
PY_MIN="3.12"
GIT_MIN="2.0"

log() { printf '[check] %s\n' "$*"; }
err() { printf '[check][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,30p' "$0"
}

# cross-platform: command -v + fallback paths for the known false negatives
detect_cmd_path() {
    local cmd="$1"
    command -v "$cmd" 2>/dev/null && return 0
    case "$cmd" in
        go)      [ -x /usr/local/go/bin/go ]      && echo /usr/local/go/bin/go      && return 0
                 [ -x /opt/homebrew/bin/go ]       && echo /opt/homebrew/bin/go       && return 0
                 [ -x /usr/lib/go/bin/go ]         && echo /usr/lib/go/bin/go         && return 0 ;;
        python3) [ -x /opt/homebrew/bin/python3 ] && echo /opt/homebrew/bin/python3 && return 0 ;;
        git)     [ -x /usr/local/bin/git ]        && echo /usr/local/bin/git        && return 0 ;;
    esac
    return 1
}

parse_version() {
    local cmd="$1" binp out
    if ! binp="$(detect_cmd_path "$cmd")"; then
        return 1
    fi
    if [ "$cmd" = "go" ]; then
        out="$("$binp" version 2>/dev/null || true)"
    else
        out="$("$binp" --version 2>/dev/null | head -1 || true)"
        if [ -z "$out" ]; then
            out="$("$binp" -version 2>/dev/null | head -1 || true)"
        fi
    fi
    printf '%s\n' "$out" | grep -oE '[0-9]+(\.[0-9]+){1,3}' | head -1
}

ver_ge() {
    [ "$1" = "$2" ] && return 0
    local highest
    highest="$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)"
    [ "$highest" = "$1" ]
}

check_one() {
    local cmd="$1" min="$2" label="$3"
    local actual
    if ! actual="$(parse_version "$cmd" 2>/dev/null)" || [ -z "$actual" ]; then
        err "  X ${label} ${cmd}: NOT FOUND"
        case "$cmd" in
            go)      err "      fix: install Go >= ${min} (https://go.dev/dl/)" ;;
            python3) err "      fix: install Python >= ${min} (uv or official)" ;;
            git)     err "      fix: install Git >= ${min} (apt: git / brew: git)" ;;
        esac
        FAILED=1
        return 1
    fi
    if ver_ge "$actual" "$min"; then
        printf '  OK %-7s %-9s actual=%-9s required>=%s\n' "$cmd" "$label" "$actual" "$min"
        return 0
    fi
    err "  X ${label} ${cmd}: ${actual} (< ${min})"
    err "      fix: upgrade to >= ${min}"
    FAILED=1
    return 1
}

main() {
    case "${1:-}" in
        -h|--help|"") : ;;
        *) err "unknown arg: $1"; usage; exit 2 ;;
    esac
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi

    log "srcradar env check (no-install)"
    log "thresholds: go>=${GO_MIN}  python3>=${PY_MIN}  git>=${GIT_MIN}"
    printf '\n'

    FAILED=0

    check_one "go"      "$GO_MIN"  "Go"         || true
    check_one "python3" "$PY_MIN"  "Python3"    || true
    check_one "git"     "$GIT_MIN" "Git"        || true

    printf '\n'
    if [ "$FAILED" = 1 ]; then
        err "env NOT met; fix the X items above, then re-run ./check.sh"
        return 1
    fi
    log "env OK: all 3 checks passed; safe to run ./install.sh"
    return 0
}

main "$@"
