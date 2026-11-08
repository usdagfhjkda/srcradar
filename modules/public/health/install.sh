#!/usr/bin/env bash
#
# modules/public/health/install.sh — install the health module (optional ops monitoring)
#
# health module components:
#   - health_check.sh            main entry point (calls lib/health_check.py → calls notify_send.sh)
#   - lib/health_check.py        5 check functions + report rendering
#   - lib/health_config.py       config/health.conf reader
#   - lib/thresholds.py          DEFAULT_THRESHOLDS + type-safe merge
#   - config/health.conf.example placeholder template (committed to the repo)
#   - config/health.conf         real values filled in by the operator (.gitignore blocked)
#   - install_cron.sh            register the daily cron (independent of daily_monitor)
#
# Usage:
#   bash install.sh              # default: write the example + self-check the deps
#   bash install.sh --check      # only check the deps + self-check
#   bash install.sh --uninstall
#
# Exit codes:
#   0 = OK
#   2 = missing dependency (python3 / df / uptime / pgrep / nproc / awk)
#   3 = health module self-check failed (a key file is missing)
#   4 = notify dependency missing (./notify/notify_send.sh is not usable)
#

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EXAMPLE_CONF="${SCRIPT_DIR}/config/health.conf.example"
TARGET_CONF="${SCRIPT_DIR}/config/health.conf"
NOTIFY_SEND="${SCRIPT_DIR}/../notify/notify_send.sh"
NOTIFY_CONFIG_PY="${SCRIPT_DIR}/../notify/lib/notify_config.py"

log()  { printf "[health] %s\n" "$*"; }
warn() { printf "[health][warn] %s\n" "$*" >&2; }
err()  { printf "[health][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,17p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "$(t public.health.install.python3_missing)"; rc=2; }
    command -v bash >/dev/null 2>&1    || { err "$(t public.health.install.bash_missing)"; rc=2; }
    command -v pgrep >/dev/null 2>&1   || { err "$(t public.health.install.pgrep_missing)"; rc=2; }
    command -v df >/dev/null 2>&1      || { err "$(t public.health.install.df_missing)"; rc=2; }
    command -v uptime >/dev/null 2>&1  || { err "$(t public.health.install.uptime_missing)"; rc=2; }
    command -v nproc >/dev/null 2>&1   || { err "$(t public.health.install.nproc_missing)"; rc=2; }
    command -v awk >/dev/null 2>&1     || { err "$(t public.health.install.awk_missing)"; rc=2; }
    command -v crontab >/dev/null 2>&1 || warn "$(t public.health.install.crontab_missing)"

    [ -f "$SCRIPT_DIR/health_check.sh" ] \
        || { err "$(t public.health.install.health_check_script_missing)"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/health_check.py" ] \
        || { err "$(t public.health.install.health_check_py_missing)"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/health_config.py" ] \
        || { err "$(t public.health.install.health_config_py_missing)"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/thresholds.py" ] \
        || { err "$(t public.health.install.thresholds_py_missing)"; rc=3; }
    [ -f "$SCRIPT_DIR/install_cron.sh" ] \
        || { err "$(t public.health.install.install_cron_script_missing)"; rc=3; }
    return $rc
}

# depends on the notify module: gpg + curl + notify_send.sh + notify_config.py
check_notify_dep() {
    local rc=0
    [ -f "$NOTIFY_SEND" ] \
        || { err "$(t public.health.install.notify_send_dependency_missing)"; rc=4; }
    [ -f "$NOTIFY_CONFIG_PY" ] \
        || { err "$(t public.health.install.notify_config_dependency_missing)"; rc=4; }
    command -v gpg >/dev/null 2>&1  \
        || { err "$(t public.health.install.gpg_missing)"; rc=4; }
    command -v curl >/dev/null 2>&1 \
        || { err "$(t public.health.install.curl_missing)"; rc=4; }
    return $rc
}

# write the example placeholder (if missing)
write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "$(t public.install.health_example_exists EXAMPLE_CONF=$EXAMPLE_CONF)"
    else
        log "$(t public.install.health_example_written EXAMPLE_CONF=$EXAMPLE_CONF)"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'EOF'
# health module config placeholder template
#
# Copy this file to config/health.conf (in the same directory) and edit it
# with the real values.
# config/health.conf is in .gitignore, so it is not committed to the repo.
#
# Field semantics:
#   DISK_WARN_PCT       disk usage percentage >= this value logs a warn     (default 80)
#   DISK_ALERT_PCT      disk usage percentage >= this value logs an alert   (default 90)
#   CPU_LOAD5_PER_NPROC 5min load average / nproc ceiling                    (default 2.0)
#   CPU_PCT_ALERT       alert on the instantaneous CPU usage percent from top -bn1  (default 95)
#   MEM_ALERT_PCT       memory usage percentage >= this value logs an alert (default 90)
#   CRON_MAX_AGE_HOURS  alert when a cron job log mtime exceeds this many hours (default 25)
#   WATCHED_LOGS        space-separated list of log paths
#
# Credentials (RESEND_API_KEY / FROM_ADDR / TO_ADDR / SUBJECT / GPG_RECIPIENT)
# are not in this file; they are maintained in ../notify/config/notify.conf and health reuses them as they are.
DISK_WARN_PCT=80
DISK_ALERT_PCT=90
CPU_LOAD5_PER_NPROC=2.0
CPU_PCT_ALERT=95
MEM_ALERT_PCT=90
CRON_MAX_AGE_HOURS=25
WATCHED_LOGS=modules/main/daily/logs/dashboard_watchdog.log
EOF
        chmod 0644 "$EXAMPLE_CONF"
    fi
}

# self-check
verify() {
    local rc=0

    printf "  OK python3:   %s\n" "$(command -v python3 2>/dev/null || echo MISSING)"
    printf "  OK pgrep:      %s\n" "$(command -v pgrep 2>/dev/null || echo MISSING)"
    printf "  OK df:         %s\n" "$(command -v df 2>/dev/null || echo MISSING)"
    printf "  OK uptime:     %s\n" "$(command -v uptime 2>/dev/null || echo MISSING)"
    printf "  OK nproc:      %s\n" "$(command -v nproc 2>/dev/null || echo MISSING)"
    printf "  OK awk:        %s\n" "$(command -v awk 2>/dev/null || echo MISSING)"
    printf "  OK gpg:        %s\n" "$(command -v gpg 2>/dev/null || echo MISSING)"
    printf "  OK curl:       %s\n" "$(command -v curl 2>/dev/null || echo MISSING)"
    printf "  OK crontab:    %s\n" "$(command -v crontab 2>/dev/null || echo MISSING)"

    [ -f "$SCRIPT_DIR/health_check.sh" ]  && printf "  OK health_check.sh\n"   || { err "$(t public.health.install.health_check_script_missing)"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/health_check.py" ] && printf "  OK lib/health_check.py\n" || { err "$(t public.health.install.health_check_py_missing)"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/health_config.py" ] && printf "  OK lib/health_config.py\n" || { err "$(t public.health.install.health_config_py_missing)"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/thresholds.py" ] && printf "  OK lib/thresholds.py\n" || { err "$(t public.health.install.thresholds_py_missing)"; rc=1; }
    [ -f "$SCRIPT_DIR/install_cron.sh" ] && printf "  OK install_cron.sh\n" || { err "$(t public.health.install.install_cron_script_missing)"; rc=1; }

    if [ -f "$NOTIFY_SEND" ]; then
        printf "%s\n" "$(t public.install.health_notify_send_ok NOTIFY_SEND="$NOTIFY_SEND")"
    else
        err "$(t public.install.health_notify_send_missing NOTIFY_SEND=$NOTIFY_SEND)"; rc=1
    fi
    if [ -f "$NOTIFY_CONFIG_PY" ]; then
        printf "$(t public.install.health_notify_config_ok)"
    else
        err "$(t public.install.health_notify_config_missing NOTIFY_CONFIG_PY=$NOTIFY_CONFIG_PY)"; rc=1
    fi

    [ -f "$EXAMPLE_CONF" ] && printf "  OK config/health.conf.example\n" \
        || { err "$(t public.health.install.example_template_missing)"; rc=1; }

    # the real conf is optional; hint about it but do not fail verify
    if [ -f "$TARGET_CONF" ]; then
        printf "%s\n" "$(t public.install.health_real_config_ok TARGET_CONF="$TARGET_CONF")"
    else
        warn "$(t public.install.health_real_config_missing)"
    fi

    # self-check the conf parsing
    if python3 "${SCRIPT_DIR}/lib/health_config.py" --help >/dev/null 2>&1; then
        printf "  OK health_config.py --help\n"
    else
        err "$(t public.install.health_config_help_failed)"; rc=1
    fi
    # self-check the thresholds parsing
    if python3 "${SCRIPT_DIR}/lib/thresholds.py" >/dev/null 2>&1; then
        printf "$(t public.install.health_thresholds_default_ok)"
    else
        err "$(t public.install.health_thresholds_import_failed)"; rc=1
    fi

    # logs/ directory is writable
    mkdir -p "$SCRIPT_DIR/logs" 2>/dev/null && printf "  OK logs/ dir: %s\n" "$SCRIPT_DIR/logs" \
        || { warn "$(t public.health.install.logs_dir_not_writable)"; }

    # bash version (the script uses set -euo pipefail; requires >=4 for ${var:-})
    local bash_major
    bash_major="${BASH_VERSINFO[0]:-0}"
    if [ "$bash_major" -lt 4 ]; then
        err "$(t public.install.bash_version_recommended BASH_VERSION=$BASH_VERSION)"
        rc=1
    else
        printf "  OK bash: %s\n" "$BASH_VERSION"
    fi

    return $rc
}

uninstall() {
    log "$(t public.install.health_cron_removal_attempt)"
    if [ -x "$SCRIPT_DIR/install_cron.sh" ] && command -v crontab >/dev/null 2>&1; then
        bash "$SCRIPT_DIR/install_cron.sh" uninstall || true
    fi
    log "$(t public.install.health_cron_registration_cleaned)"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_deps || exit $?
            check_notify_dep || true
            write_example
            verify || exit $?
            log "$(t public.install.health_module_deps_ok)"
            exit 0
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            check_notify_dep || true
            write_example
            verify || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"