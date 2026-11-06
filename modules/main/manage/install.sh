#!/usr/bin/env bash
#
# modules/main/manage/install.sh — install the manage module (business management)
#
# manage module contents:
#   - add_business.sh  add a business (seed)
#   - set_config.sh    switch the business config
#   - seeds/           seed business data (reserved .gitkeep)
#
# Usage:
#   bash install.sh                  # default: check + create seeds/
#   bash install.sh --check          # only check the dependencies
#   bash install.sh --uninstall
#
# Exit codes: 0/1/2/3/4 same as the pdtm module

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf "[manage] %s\n" "$*"; }
warn() { printf "[manage][warn] %s\n" "$*" >&2; }
err()  { printf "[manage][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,13p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

# ---- check ----
check_deps() {
    local rc=0
    command -v bash >/dev/null 2>&1 || { err "$(t main.manage_install.dep_bash_missing)"; rc=2; }
    command -v sqlite3 >/dev/null 2>&1 || { err "$(t main.manage_install.dep_sqlite3_missing)"; rc=2; }
    [ -x "$SCRIPT_DIR/add_business.sh" ] || { err "$(t main.manage_install.add_business_missing)"; rc=3; }
    [ -x "$SCRIPT_DIR/set_config.sh" ] || { err "$(t main.manage_install.set_config_missing)"; rc=3; }
    return $rc
}

# ---- prepare the seeds directory ----
install() {
    mkdir -p "$SCRIPT_DIR/seeds"
    if [ ! -f "$SCRIPT_DIR/seeds/.gitkeep" ]; then
        : > "$SCRIPT_DIR/seeds/.gitkeep"
    fi
    log "$(t main.manage_install.seeds_ready)"
    return 0
}

# ---- verify ----
verify() {
    [ -x "$SCRIPT_DIR/add_business.sh" ] && printf "  OK add_business.sh\n"
    [ -x "$SCRIPT_DIR/set_config.sh" ] && printf "  OK set_config.sh\n"
    [ -d "$SCRIPT_DIR/seeds" ] && printf "  OK seeds/\n"
    return 0
}

# ---- uninstall ----
uninstall() {
    log "$(t main.manage_install.nothing_to_uninstall)"
    return 0
}

main() {
    case "${1:-}" in
        --check) check_deps || exit $?; log "$(t main.manage_install.deps_ok)"; exit 0 ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            install || exit $?
            verify || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
