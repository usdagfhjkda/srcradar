#!/usr/bin/env bash
#
# modules/public/katana/install.sh — install the katana module (optional URL scanning plugin)
#
# katana module parts:
#   - katana_enrich.sh         CLI entry point (daily-url stage / dashboard calls)
#   - lib/katana_runner.py     subprocess + JSONL parsing + insert into web_hash_urls
#   - lib/risks.py             dangerous route 5-word blacklist (same as URLFinder)
#   - lib/schema_map.py        katana JSONL → web_hash_urls field alignment
#   - config/katana.conf.example placeholder template (the operator fills in the real
#                              values and saves it as config/katana.conf, .gitignore keeps it out)
#
# Usage:
#   bash install.sh              # go install katana + write the example + self-check
#   bash install.sh --check      # only check the dependencies + self-check, do not touch config / do not install the binary
#   bash install.sh --uninstall  # do not delete the binary (managed by go env), only delete the config
#
# Exit codes:
#   0 = OK
#   2 = missing dependency (go / python3 / mmh3 / curl / git)
#   3 = katana module self-check failed (key file missing)

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EXAMPLE_CONF="${SCRIPT_DIR}/config/katana.conf.example"
TARGET_CONF="${SCRIPT_DIR}/config/katana.conf"

log()  { printf '[katana] %s\n' "$*"; }
warn() { printf '[katana][warn] %s\n' "$*" >&2; }
err()  { printf '[katana][err]  %s\n' "$*" >&2; }
usage() {
    sed -n '2,16p' "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v go >/dev/null 2>&1        || { err "缺 go (apt: golang-go;建议 >= 1.22)"; rc=2; }
    command -v python3 >/dev/null 2>&1   || { err "缺 python3"; rc=2; }
    command -v curl >/dev/null 2>&1      || { err "缺 curl"; rc=2; }
    command -v git >/dev/null 2>&1       || { err "缺 git"; rc=2; }

    # mmh3 Python module (algo_diff + response_hash algorithm dependency)
    python3 -c "import mmh3" 2>/dev/null \
        || { err "缺 python3 mmh3 (pip install mmh3)"; rc=2; }

    # key files
    [ -f "${SCRIPT_DIR}/katana_enrich.sh" ] \
        || { err "katana_enrich.sh 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/katana_runner.py" ] \
        || { err "lib/katana_runner.py 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/risks.py" ] \
        || { err "lib/risks.py 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/schema_map.py" ] \
        || { err "lib/schema_map.py 缺失"; rc=3; }

    return $rc
}

install_katana_bin() {
    if command -v katana >/dev/null 2>&1; then
        log "$(t public.katana_install.already_in_path)"
        return 0
    fi
    log "go install projectdiscovery/katana/v2/cmd/katana@latest"
    if ! go install github.com/projectdiscovery/katana/v2/cmd/katana@latest; then
        err "$(t public.katana_install.go_install_failed)"
        return 1
    fi
    # katana installs to $(go env GOPATH)/bin by default; PATH usually already includes it, echo it once explicitly
    local gobin
    gobin="$(go env GOPATH)/bin"
    if [ -x "${gobin}/katana" ] && ! command -v katana >/dev/null 2>&1; then
        warn "$(t public.katana_install.not_in_path gobin=${gobin})"
    fi
}

write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "$(t public.katana_install.example_exists EXAMPLE_CONF=$EXAMPLE_CONF)"
    else
        log "$(t public.katana_install.writing_example EXAMPLE_CONF=$EXAMPLE_CONF)"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'CONFEOF'
# katana module config placeholder template
#
# Copy this file to config/katana.conf (same directory) and edit it to fill in the real values.
# config/katana.conf is inside .gitignore, it is not committed.
#
# Fields:
#   KATANA_BIN            absolute path of the binary; leave empty to probe PATH (default)
#   KATANA_TIMEOUT        per-host timeout in seconds (default 600; user decision for large dictionary scenarios)
#   KATANA_EXTRA_RISKS    extra risk words (comma separated), default: remove,delete,insert,update,logout
#                         e.g. KATANA_EXTRA_RISKS=signout,drop,truncate
#   KATANA_OMIT_BODY      1=do not store the body (saves storage, response_hash degrades to status|length)
#                         0=store the body (default; algo_diff needs it)
#   KATANA_EXTRA_FLAGS    extra katana flags (space separated), e.g. --no-sandbox
#
# Authentication surface:
#   - katana itself has no auth; this module calls it via subprocess and does not link the Go source (MIT boundary)
#   - the 5-word risk blacklist is reused from URLFinder (URLFinder/config/config.go:19), belt and braces:
#     (1) katana -crawl-out-scope regex blocks it at the source
#     (2) lib/risks.py checks it once more before the insert
KATANA_BIN=
KATANA_TIMEOUT=600
KATANA_EXTRA_RISKS=remove,delete,insert,update,logout
KATANA_OMIT_BODY=0
KATANA_EXTRA_FLAGS=
CONFEOF
    fi
}

uninstall() {
    # do not delete the katana binary (the user may have installed it globally)
    rm -f "$TARGET_CONF"
    log "$(t public.katana_install.conf_removed TARGET_CONF=$TARGET_CONF)"
    log "$(t public.katana_install.binary_not_removed)"
}

# ---- main ----
MODE="${1:-install}"
case "$MODE" in
    --check)
        log "$(t public.katana_install.checking)"
        check_deps
        rc=$?
        [ $rc -eq 0 ] && log "自检通过"
        exit $rc
        ;;
    --uninstall)
        uninstall
        exit 0
        ;;
    install|--yes|"")
        log "$(t public.katana_install.checking_deps)"
        if ! check_deps; then
            err "$(t public.katana_install.deps_not_met)"
            exit 2
        fi
        install_katana_bin
        write_example
        log "$(t public.katana_install.self_test)"
        check_deps >/dev/null
        log "$(t public.katana_install.usage)"
        exit 0
        ;;
    *)
        err "unknown arg: $MODE"
        usage >&2
        exit 2
        ;;
esac
