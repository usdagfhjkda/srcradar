#!/usr/bin/env bash
#
# modules/main/db/install.sh — install the db module (the database schema + fixture + init)
#
# The db module holds:
#   - schema.sql         14 tables + the indexes + the triggers (the authoritative schema)
#   - init_db.py         the Python initialiser (it backs up automatically when the path already exists)
#   - check_schema.py    the schema drift check
#   - _test_init.sqlite3 the test fixture (it is in the repo)
#
# Usage:
#   bash install.sh                              # the default: init_db into ../db/recon.sqlite3
#   bash install.sh --path /custom/path.db       # a custom db path
#   bash install.sh --check-schema               # only check the schema drift
#   bash install.sh --check                      # only check the dependencies
#   bash install.sh --uninstall
#
# Exit codes: 0/1/2/3/4, the same as the pdtm module

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# the default db path: db/recon.sqlite3 next to this script
DEFAULT_DB="$(cd .. && pwd)/db/recon.sqlite3"

log()  { printf "[db] %s\n" "$*"; }
warn() { printf "[db][warn] %s\n" "$*" >&2; }
err()  { printf "[db][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,17p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "$(t main.db_install.dep_python3_missing)"; rc=2; }
    command -v sqlite3 >/dev/null 2>&1 || { err "$(t main.db_install.dep_sqlite3_missing)"; rc=2; }
    [ -f "$SCRIPT_DIR/init_db.py" ] || { err "$(t main.db_install.init_db_missing)"; rc=3; }
    [ -f "$SCRIPT_DIR/schema.sql" ] || { err "$(t main.db_install.schema_missing)"; rc=3; }
    return $rc
}

# ---- init-db ----
init_db() {
    local db_path="$1"
    local init_py="$SCRIPT_DIR/init_db.py"
    local schema_sql="$SCRIPT_DIR/schema.sql"

    log "init-db: $db_path (schema=$schema_sql)"
    if python3 "$init_py" "$db_path" "$schema_sql"; then
        log "$(t main.db_install.init_ok path="$db_path")"
        return 0
    else
        err "$(t main.install.init_failed)"
        return 3
    fi
}

# ---- check-schema ----
check_schema() {
    local checker="$SCRIPT_DIR/check_schema.py"
    [ -f "$checker" ] || { err "$(t main.db_install.checker_missing)"; return 3; }
    log "$(t main.install.check_schema_label SCRIPT_DIR=$SCRIPT_DIR)"
    if python3 "$checker" "$SCRIPT_DIR/schema.sql"; then
        return 0
    else
        err "$(t main.install.drift_detected)"
        return 1
    fi
}

# ---- verify: confirm the 14 tables exist ----
verify_db() {
    local db_path="$1"
    [ -f "$db_path" ] || { err "$(t main.db_install.db_missing path="$db_path")"; return 1; }
    # wait for sqlite to flush after init_db.py, to avoid a race
    sleep 0.2
    local n
    n=$(sqlite3 "$db_path" "SELECT count(*) FROM sqlite_master WHERE type='table'" 2>/dev/null || echo 0)
    if [ "$n" -ge 14 ]; then
        printf "$(t main.install.table_count_ok)" "$n"
    else
        err "$(t main.install.table_count_low n=$n)"
        return 1
    fi
    return 0
}

verify() {
    local db_path="${DB_PATH:-$DEFAULT_DB}"
    if [ -f "$db_path" ]; then
        verify_db "$db_path" || return 1
    else
        warn "$(t main.install.db_missing db_path=$db_path)"
    fi
    return 0
}

uninstall() {
    log "$(t main.install.uninstall_note)"
    local db_path="${DB_PATH:-$DEFAULT_DB}"
    if [ -f "$db_path" ]; then
        warn "$(t main.install.uninstall_confirm db_path=$db_path)"
    fi
    return 0
}

main() {
    DB_PATH="$DEFAULT_DB"
    case "${1:-}" in
        --check)
            check_deps || exit $?
            log "$(t main.install.deps_ok)"
            exit 0
            ;;
        --check-schema)
            check_deps || exit $?
            check_schema || exit $?
            ;;
        --path)
            [ $# -ge 2 ] || { err "$(t main.db_install.path_needs_arg)"; exit 1; }
            DB_PATH="$2"
            shift 2
            check_deps || exit $?
            init_db "$DB_PATH" || exit $?
            verify_db "$DB_PATH" || exit $?
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            init_db "$DB_PATH" || exit $?
            verify_db "$DB_PATH" || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
