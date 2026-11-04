#!/usr/bin/env bash
#
# modules/main/daily/install.sh — install the daily module (the daily monitoring)
#
# The daily module holds:
#   - daily_monitor.sh / run_one_business.sh / install_cron.sh / truncate_recon_db.py
#   - bin/    backup_file.sh / dashboard_watchdog.sh
#   - lib/    dashboard.py / diff.py / log.py / score.py + migrate_*.py + *.sql
#
# Usage:
#   bash install.sh                  # the default: install everything (register cron + write the log dir)
#   bash install.sh --check          # only check the dependencies
#   bash install.sh --no-cron        # install the code but skip the cron registration
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

log()  { printf "[daily] %s\n" "$*"; }
warn() { printf "[daily][warn] %s\n" "$*" >&2; }
err()  { printf "[daily][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,15p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

# ---- dependency check ----
check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; rc=2; }
    command -v flock >/dev/null 2>&1 || { err "缺 flock (apt: util-linux)"; rc=2; }
    command -v crontab >/dev/null 2>&1 || { err "缺 crontab (apt: cron)"; rc=2; }
    command -v sqlite3 >/dev/null 2>&1 || { err "缺 sqlite3"; rc=2; }
    [ -d "$SCRIPT_DIR/lib" ] || { err "daily/lib 缺失"; rc=3; }
    [ -x "$SCRIPT_DIR/daily_monitor.sh" ] || { err "daily_monitor.sh 缺失/不可执行"; rc=3; }
    return $rc
}

# ---- prepare the runtime directories ----
install() {
    mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/reports" "$SCRIPT_DIR/snapshots"
    log "$(t main.daily_install.dirs_ready)"
    return 0
}

# ---- register the cron ----
register_cron() {
    if ! command -v crontab >/dev/null 2>&1; then
        warn "$(t main.daily_install.no_crontab)"
        return 0
    fi
    log "$(t main.daily_install.registering_cron)"
    if [ -x "$SCRIPT_DIR/install_cron.sh" ]; then
        bash "$SCRIPT_DIR/install_cron.sh"
    else
        warn "$(t main.daily_install.cron_script_missing)"
    fi
    return 0
}

# ---- verify ----
verify() {
    [ -x "$SCRIPT_DIR/daily_monitor.sh" ] && printf "  OK daily_monitor.sh\n" || { err "daily_monitor.sh 缺失"; return 1; }
    [ -x "$SCRIPT_DIR/run_one_business.sh" ] && printf "  OK run_one_business.sh\n" || err "run_one_business.sh 缺失"
    [ -x "$SCRIPT_DIR/install_cron.sh" ] && printf "  OK install_cron.sh\n" || err "install_cron.sh 缺失"
    command -v crontab >/dev/null 2>&1 && crontab -l >/dev/null 2>&1 && printf "  OK cron 表已注册\n" || warn "cron 表未注册(可能正常)"
    return 0
}

# ---- uninstall ----
uninstall() {
    if command -v crontab >/dev/null 2>&1; then
        log "$(t main.daily_install.removing_cron)"
        crontab -l 2>/dev/null | grep -v "daily/daily_monitor.sh" | crontab - 2>/dev/null || true
    fi
    log "$(t main.daily_install.cron_cleaned)"
    return 0
}

main() {
    case "${1:-}" in
        --check) check_deps || exit $?; log "daily 模块依赖 OK"; exit 0 ;;
        --no-cron)
            check_deps || exit $?
            install || exit $?
            verify || exit $?
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            install || exit $?
            register_cron || exit $?
            verify || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
