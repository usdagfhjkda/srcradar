#!/usr/bin/env bash
#
# modules/public/notify/install.sh — install the notify module (optional push plugin)
#
# notify module parts:
#   - notify_send.sh            main push script (gpg encryption + curl POST to the Resend HTTP API)
#   - lib/notify_config.py      reader for config/notify.conf
#   - config/notify.conf.example config placeholder template (the operator fills in the real
#                              values and saves it as config/notify.conf, .gitignore keeps it out)
#
# Usage:
#   bash install.sh              # default: write config/notify.conf.example (if missing)
#                                 # + self-check gpg / curl / notify_config.py
#   bash install.sh --check      # only check the dependencies + self-check, do not touch config
#   bash install.sh --uninstall
#
# Exit codes:
#   0 = OK
#   2 = missing dependency (gpg / python3 / curl)
#   3 = notify module self-check failed
#

set -euo pipefail
# The runtime messages below go through t(), so the shell-side lookup has
# to be available before anything is printed.
# shellcheck source=modules/main/lib/i18n.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/modules/main/lib/i18n.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EXAMPLE_CONF="${SCRIPT_DIR}/config/notify.conf.example"
TARGET_CONF="${SCRIPT_DIR}/config/notify.conf"
PAYLOAD_DIR="${HOME}/.cache/notify"

log()  { printf "[notify] %s\n" "$*"; }
warn() { printf "[notify][warn] %s\n" "$*" >&2; }
err()  { printf "[notify][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,17p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "$(t public.notify.install.python3_missing)"; rc=2; }
    command -v gpg >/dev/null 2>&1      || { err "$(t public.notify.install.gpg_missing)"; rc=2; }
    command -v curl >/dev/null 2>&1     || { err "$(t public.notify.install.curl_missing)"; rc=2; }
    [ -f "${SCRIPT_DIR}/notify_send.sh" ] \
        || { err "$(t public.notify.install.notify_send_script_missing)"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/notify_config.py" ] \
        || { err "$(t public.notify.install.notify_config_py_missing)"; rc=3; }
    return $rc
}

# ---- write the example placeholder (if missing) ----
write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "$(t public.install.notify_example_exists EXAMPLE_CONF=$EXAMPLE_CONF)"
    else
        log "$(t public.install.notify_example_written EXAMPLE_CONF=$EXAMPLE_CONF)"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'EOF'
# notify module config placeholder template
#
# Copy this file to config/notify.conf (same directory) and edit it to fill in the real values.
# config/notify.conf is inside .gitignore, it is not committed.
#
# Fields:
#   RESEND_API_KEY  Resend HTTP API key (generated at https://resend.com/api-keys
#                   starts with re_, at least 20 characters; binds one sending domain)
#   FROM_ADDR       sender address (must be under a sending domain already verified in the Resend backend)
#   TO_ADDR         recipient email (the operator)
#   SUBJECT         email subject prefix (default "[srcradar] daily diff")
#   GPG_RECIPIENT   GPG recipient email (== TO_ADDR; used for gpg -r)
#
# Any field left empty / REPLACE_WITH_* makes notify_send.sh refuse to send and print what is missing.
RESEND_API_KEY=REPLACE_WITH_RESEND_API_KEY
FROM_ADDR=noreply@your-domain.example
TO_ADDR=REPLACE_WITH_RECIPIENT_EMAIL
SUBJECT=[srcradar] daily diff
GPG_RECIPIENT=REPLACE_WITH_RECIPIENT_EMAIL
EOF
        chmod 0644 "$EXAMPLE_CONF"
    fi
}

# ---- self-check ----
verify() {
    local rc=0
    if command -v gpg >/dev/null 2>&1; then
        printf "  OK gpg: %s\n" "$(command -v gpg)"
    else
        err "$(t public.install.gpg_missing)"; rc=1
    fi
    if command -v curl >/dev/null 2>&1; then
        printf "  OK curl: %s\n" "$(command -v curl)"
    else
        err "$(t public.install.curl_missing)"; rc=1
    fi
    if command -v python3 >/dev/null 2>&1; then
        printf "  OK python3: %s\n" "$(command -v python3)"
    else
        err "$(t public.install.python3_missing)"; rc=1
    fi
    # key point: under a TTY the example has normally already been copied to notify.conf by the operator.
    # non-TTY (--check) does not require the real values to exist, but the example must be there.
    if [ -t 0 ]; then
        if [ -f "$TARGET_CONF" ]; then
            printf "$(t public.install.notify_real_config_ok)" "$TARGET_CONF"
        else
            warn "$(t public.install.notify_real_config_missing)"
        fi
    fi
    if [ -f "$EXAMPLE_CONF" ]; then
        printf "$(t public.install.notify_example_template_ok)"
    else
        err "$(t public.install.notify_example_template_missing)"; rc=1
    fi
    if python3 "${SCRIPT_DIR}/lib/notify_config.py" --help >/dev/null 2>&1; then
        printf "$(t public.install.notify_config_help_ok)"
    else
        err "$(t public.install.notify_config_help_failed)"; rc=1
    fi
    # RESEND_API_KEY format reminder (printed only on the --check path, not enforced)
    if [ -f "$TARGET_CONF" ]; then
        if grep -q '^RESEND_API_KEY=' "$TARGET_CONF" 2>/dev/null; then
            local key
            key="$(sed -n 's/^RESEND_API_KEY=//p' "$TARGET_CONF" | head -1)"
            if [[ "$key" == REPLACE_WITH_* ]] || [ -z "$key" ]; then
                warn "$(t public.install.resend_api_key_placeholder)"
            elif [[ "$key" != re_* ]]; then
                warn "$(t public.install.resend_api_key_prefix)"
            elif [ "${#key}" -lt 20 ]; then
                warn "$(t public.install.resend_api_key_too_short)"
            fi
        fi
    fi
    # payload directory is writable
    mkdir -p "$PAYLOAD_DIR" 2>/dev/null && chmod 0700 "$PAYLOAD_DIR" \
        && printf "  OK payload dir: %s\n" "$PAYLOAD_DIR" \
        || { warn "$(t public.notify.install.payload_dir_not_writable PAYLOAD_DIR="$PAYLOAD_DIR")"; }
    return $rc
}

uninstall() {
    # only remove the files that install.sh may have created; leave the operator's notify.conf alone
    log "$(t public.install.notify_uninstall_nothing_to_clean)"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_deps || exit $?
            write_example
            verify || exit $?
            log "$(t public.install.notify_module_deps_ok)"
            exit 0
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            write_example
            verify || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"