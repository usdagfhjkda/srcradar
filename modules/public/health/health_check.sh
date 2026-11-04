#!/usr/bin/env bash
#
# modules/public/health/health_check.sh — the main entry point
#
# How to call it:
#   bash health_check.sh [--dryrun] [--config <conf>]
#
# What it does:
#   1) calls lib/health_check.py to run the 5 checks
#      - writes logs/health_report.md (the full version, with the numbers and
#        the paths, used as the plaintext attachment)
#      - writes logs/health_report.body.md (the redacted version, summary and
#        counts only)
#   2) if anything is off (warn/alert) -> calls ../notify/notify_send.sh to
#      send the mail
#      - reuses the credentials from modules/public/notify/config/notify.conf
#      - encrypts the attachment with gpg + the mail body (the text field) ->
#        the Resend HTTP API
#   3) all OK -> writes only an INFO line to logs/health.log, no mail
#
# Safety:
#   - the real configs (config/health.conf / notify.conf) are filled in by the
#     operator and are both in .gitignore, so they never enter the repo
#   - any push failure is absorbed with || true, so it cannot change the
#     health verdict itself
#   - no token is persisted, ~/.bashrc is not written and no cloud API is
#     called (only notify is reused)
#
# Exit codes:
#   0 = all OK, or the anomaly was notified (regardless of the push result)
#   1 = bad arguments
#   2 = a dependency is missing (python3 / lib/health_check.py)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${SCRIPT_DIR}/config/health.conf"
EXAMPLE_CONF="${SCRIPT_DIR}/config/health.conf.example"
LIB_DIR="${SCRIPT_DIR}/lib"
LOG_DIR="${SCRIPT_DIR}/logs"
HEALTH_CHECK_PY="${LIB_DIR}/health_check.py"
HEALTH_LOG="${LOG_DIR}/health.log"

DRYRUN=0

log()  { printf "[health] %s\n" "$*"; }
warn() { printf "[health][warn] %s\n" "$*" >&2; }
err()  { printf "[health][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,33p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dryrun) DRYRUN=1; shift ;;
            --config) CONFIG_FILE="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            --) shift; break ;;
            -*) err "unknown arg: $1"; usage; exit 1 ;;
            *) break ;;
        esac
    done
}

main() {
    parse_args "$@"

    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; exit 2; }
    [ -f "$HEALTH_CHECK_PY" ] || { err "lib/health_check.py 缺失: $HEALTH_CHECK_PY"; exit 2; }
    mkdir -p "$LOG_DIR"
    touch "$HEALTH_LOG" 2>/dev/null || true

    # the INFO line is written by health_check.sh after the run (with the timestamp
    # and a summary of the findings); the stderr anomalies are collected by
    # health_check.py with 2>> on the health log

    # falls back to the example when the conf is missing (only so that --dryrun can run too; the real credentials are in notify.conf)
    EFFECTIVE_CONFIG="$CONFIG_FILE"
    if [ ! -f "$CONFIG_FILE" ]; then
        if [ -f "$EXAMPLE_CONF" ]; then
            warn "config/health.conf 不存在,用 example 作占位(实际阈值用 DEFAULT)"
            EFFECTIVE_CONFIG="$EXAMPLE_CONF"
        else
            warn "config 全部缺失,health_check.py 会回退到 DEFAULT_THRESHOLDS"
            EFFECTIVE_CONFIG="${SCRIPT_DIR}/lib/thresholds.py"  # 任意存在的路径,trigger DEFAULT 分支
        fi
    fi

    ARGS=(--config "$EFFECTIVE_CONFIG" --out "${LOG_DIR}/health_report.md" --date-ph "$(date '+%Y-%m-%d')")
    [ "$DRYRUN" = "1" ] && ARGS+=(--dryrun)

    log "run health_check.py ..."
    if ! python3 "$HEALTH_CHECK_PY" "${ARGS[@]}" 2>>"$HEALTH_LOG"; then
        err "health_check.py 失败,详见 $HEALTH_LOG"
        exit 3
    fi
    log "report: ${LOG_DIR}/health_report.md"
    [ "$DRYRUN" = "1" ] && log "--dryrun: 已生成报告,未发邮件"

    # write the INFO line to health.log (a trace of every run; a failure here
    # does not affect the main flow)
    if [ -f "${LOG_DIR}/health_report.md" ]; then
        # the overall line of report.md is actually "- overall: **ALERT**", so the
        # grep anchor needs the "- "; when grep finds nothing rc=1, which would
        # abort main under set -euo pipefail, so it must be absorbed with || true
        OVERALL="$(grep -m1 "^- overall:" "${LOG_DIR}/health_report.md" 2>/dev/null \
            | sed -e 's/^- overall:\s*//' -e 's/\s*$//' || true)"
        # grep -c returns rc=1 when there is no match, which makes the command
        # substitution fail under set -euo pipefail; absorb it with || true (do
        # not use || echo 0, that would print the count twice).
COUNT="$(grep -c "^| " "${LOG_DIR}/health_report.md" 2>/dev/null || true)"
        printf "[%s] overall=%s findings_rows=%s dryrun=%d\n" \
            "$(date '+%Y-%m-%d %H:%M:%S %z')" "${OVERALL:-UNKNOWN}" "${COUNT}" "$DRYRUN" \
            >> "$HEALTH_LOG" 2>/dev/null || true
    fi
}

main "$@"