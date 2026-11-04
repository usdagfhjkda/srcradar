#!/usr/bin/env bash
#
# modules/public/notify/notify_send.sh — main push script
#
# Invocation:
#   notify_send.sh <summary.md> <business_name> <run_id> <date_placeholder>
#
# Behaviour:
#   1) read config/notify.conf (.gitignore keeps it out, the operator fills it in)
#   2) gpg --batch --yes --trust-model always \
#         -r "$GPG_RECIPIENT" \
#         -o summary.gpg -e summary.md
#      note: GPG_RECIPIENT is a placeholder (REPLACE_WITH_RECIPIENT_EMAIL), the real recipient
#            email goes into no file; the operator copies example → notify.conf and fills it in.
#   3) write a plain text body (summary only, no subdomains / IP / status codes / real business name details)
#      used as the Resend email body (the text field)
#   4) build_resend_payload() writes ~/.cache/notify/payload.json:
#        {
#          "from": "$FROM_ADDR",
#          "to": ["$TO_ADDR"],
#          "subject": "$SUBJECT $BUSINESS $DATE_PH daily diff",
#          "text": "<summary.body.md content>",
#          "attachments": [{"filename": "summary.gpg",
#                           "content": "<base64(summary.gpg)>"}]
#        }
#      attachments[].content must be a base64 string (the Resend API enforces it).
#   5) push_resend():
#        curl -fsS -X POST https://api.resend.com/emails \
#          -H "Authorization: Bearer ***" \
#          -H "Content-Type: application/json" \
#          --data-binary "@$PAYLOAD_JSON"
#      --dryrun skips the curl call, it only generates summary.gpg / body / payload.json.
#
# Safety:
#   - the real Resend API key / real email addresses are only read from config/notify.conf (.gitignore)
#   - every failure returns an explicit rc through set -euo pipefail; the caller daily_monitor.sh
#     decides the rc with `|| true` as a fallback
#   - does not touch ~/.bashrc / does not write cookies / does not persist tokens
#
# Exit codes:
#   0 = push succeeded (or was short-circuited by --dryrun)
#   1 = caller side argument error (only a missing summary.md / business_name etc.)
#   2 = notify.conf is missing or a required field is REPLACE_WITH_*
#   3 = gpg failed
#   4 = Resend HTTP failure (curl exit code non-zero or HTTP status >= 400)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_FILE="${SCRIPT_DIR}/config/notify.conf"
EXAMPLE_CONF="${SCRIPT_DIR}/config/notify.conf.example"
PAYLOAD_DIR="${HOME}/.cache/notify"
PAYLOAD_JSON="${PAYLOAD_DIR}/payload.json"
DRYRUN=0
TEXT_FILE=""

log()  { printf "[notify] %s\n" "$*"; }
warn() { printf "[notify][warn] %s\n" "$*" >&2; }
err()  { printf "[notify][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,33p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dryrun) DRYRUN=1; shift ;;
            --config) CONFIG_FILE="$2"; shift 2 ;;
            # --text-file <path>:use the plain text file supplied by the caller as the email body (the text field),
            # skipping the daily diff template. The text must be plain text, no HTML tags (the <...> form).
            # not passed -> fall back to the original daily diff template (backwards compatible with daily_monitor).
            --text-file) TEXT_FILE="$2"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            --) shift; break ;;
            -*) err "unknown arg: $1"; usage; exit 1 ;;
            *) break ;;
        esac
    done
    SUMMARY="$1"; BUSINESS="$2"; RUN_ID="$3"; DATE_PH="$4"
    if [ -z "${SUMMARY:-}" ] || [ -z "${BUSINESS:-}" ] || [ -z "${RUN_ID:-}" ] || [ -z "${DATE_PH:-}" ]; then
        err "用法: notify_send.sh [--dryrun] [--config <conf>] [--text-file <path>] <summary.md> <business_name> <run_id> <date_placeholder>"
        exit 1
    fi
    if [ ! -f "$SUMMARY" ]; then
        err "summary.md 不存在: $SUMMARY"
        exit 1
    fi
    if [ -n "${TEXT_FILE:-}" ] && [ ! -f "$TEXT_FILE" ]; then
        err "--text-file 指向的文件不存在: $TEXT_FILE"
        exit 1
    fi
}

# read config/notify.conf through lib/notify_config.py
load_conf() {
    if [ ! -f "$CONFIG_FILE" ]; then
        err "config/notify.conf 不存在,请复制 example 并填真值:"
        err "  cp $EXAMPLE_CONF $CONFIG_FILE && chmod 600 $CONFIG_FILE"
        exit 2
    fi
    # shellcheck disable=SC1090
    parsed="$(python3 "${SCRIPT_DIR}/lib/notify_config.py" "$CONFIG_FILE" 2>/dev/null)" || {
        err "notify_config.py parse 失败,config 跳过"; exit 2;
    }
    if [ -z "$parsed" ]; then
        err "notify.conf 为空或解析失败"; exit 2
    fi
    while IFS= read -r line; do
        key="${line%%=*}"
        val="${line#*=}"
        val="${val#\'}"; val="${val%\'}"
        if [ -z "${!key:-}" ]; then
            printf -v "$key" '%s' "$val"
        fi
    done <<< "$parsed"

    # required fields: any one of them being REPLACE_WITH_* or empty refuses to send
    local missing=0
    for k in RESEND_API_KEY FROM_ADDR TO_ADDR SUBJECT GPG_RECIPIENT; do
        v="${!k:-}"
        if [ -z "$v" ] || [[ "$v" == REPLACE_WITH_* ]]; then
            err "notify.conf 字段未填真值: $k"
            missing=1
        fi
    done
    if [ "$missing" = "1" ]; then
        err "请编辑 $CONFIG_FILE(参考 $EXAMPLE_CONF)"
        exit 2
    fi
}

# gpg encrypts summary.md → summary.gpg (same directory)
gpg_encrypt() {
    local out="${SUMMARY%.md}.gpg"
    # -r uses GPG_RECIPIENT (the real value lives in notify.conf, it is not committed)
    # shellcheck disable=SC2154  # set by load_conf via printf -v
    gpg --batch --yes --trust-model always \
        -r "$GPG_RECIPIENT" \
        -o "$out" -e "$SUMMARY" || { err "gpg 加密失败(rc=$?)"; return 3; }
    GPG_OUT="$out"
    log "gpg ok: $out ($(wc -c <"$out") bytes)"
}

# write the plain text body (summary only, strictly no asset details)
write_body() {
    local body_file="${SUMMARY%.md}.body.md"
    if [ -n "${TEXT_FILE:-}" ]; then
        # the caller supplies a custom body (plain text, no HTML tags)
        # when source and target are the same path, `>` truncates before it reads = empty file (health module design);
        # use cat into a tmp file + mv to avoid that (or just skip when they are the same, but the write permission still needs chmod)
        local tmp
        tmp="$(mktemp "${body_file}.XXXXXX")"
        cat -- "$TEXT_FILE" > "$tmp"
        mv -f -- "$tmp" "$body_file"
        chmod 0600 "$body_file"
        BODY_OUT="$body_file"
        log "body ok (caller-provided text-file): $body_file ($(wc -c <"$body_file") bytes)"
        return 0
    fi
    # default: the daily diff template (BUSINESS / DATE_PH / the four placeholders are injected by the caller after parsing).
    # the real business name / subdomains / IP / status codes / risk level are never committed.
    # shellcheck disable=SC2154  # SUBJECT set by load_conf via printf -v
    cat > "$body_file" <<EOF
$SUBJECT $BUSINESS $DATE_PH daily diff

+ $ADDED_PLACEHOLDER added
~ $CHANGED_PLACEHOLDER changed
! $REACTIVATED_PLACEHOLDER reactivated
deactivated: $DEACTIVATED_PLACEHOLDER

Dashboard: http://127.0.0.1:8765/$BUSINESS/diff (需 SSH 隧道)
Encrypted attachment: summary.gpg (OpenPGP,需 OpenKeychain 解密)
EOF
    chmod 0600 "$body_file"
    BODY_OUT="$body_file"
    log "body ok: $body_file"
}

# build_resend_payload: write {from, to, subject, text, attachments[]} to payload.json
# attachments[0].content must be base64 (the Resend API enforces it).
build_resend_payload() {
    mkdir -p "$PAYLOAD_DIR"
    chmod 0700 "$PAYLOAD_DIR"
    local gpg_b64
    gpg_b64="$(base64 -w 0 "$GPG_OUT")"
    local body_text
    body_text="$(cat "$BODY_OUT")"
    # shellcheck disable=SC2154  # SUBJECT/FROM_ADDR/TO_ADDR set by load_conf via printf -v
    {
        printf '{\n'
        printf '  "from": "%s",\n' "$FROM_ADDR"
        printf '  "to": ["%s"],\n' "$TO_ADDR"
        printf '  "subject": "%s %s %s daily diff",\n' "$SUBJECT" "$BUSINESS" "$DATE_PH"
        printf '  "text": %s,\n' "$(printf '%s' "$body_text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
        printf '  "attachments": [{"filename": "summary.gpg", "content": "%s"}]\n' "$gpg_b64"
        printf '}\n'
    } > "$PAYLOAD_JSON"
    chmod 0600 "$PAYLOAD_JSON"
    log "payload ok: $PAYLOAD_JSON ($(wc -c <"$PAYLOAD_JSON") bytes, attachment=$(wc -c <"$GPG_OUT") B gpg → $(echo -n "$gpg_b64" | wc -c) B b64)"
}

# push_resend: curl POST to the Resend HTTP API
push_resend() {
    if [ "$DRYRUN" = "1" ]; then
        log "--dryrun: 跳过 curl 调用(payload.json 已生成)"
        return 0
    fi
    # shellcheck disable=SC2154  # RESEND_API_KEY set by load_conf via printf -v
    local resp
    resp="$(curl -sS -X POST https://api.resend.com/emails \
        -H "Authorization: Bearer $RESEND_API_KEY" \
        -H "Content-Type: application/json" \
        -H "User-Agent: srcradar-notify/1.0 (resend)" \
        -w '\nHTTP_STATUS:%{http_code}' \
        --data-binary "@$PAYLOAD_JSON")" \
        || { err "curl exit rc=$?"; return 4; }
    local status="${resp##*HTTP_STATUS:}"
    local body="${resp%HTTP_STATUS:*}"
    if [ "${status:-0}" -ge 400 ] 2>/dev/null; then
        err "Resend HTTP $status: $body"
        return 4
    fi
    log "Resend ok → $TO_ADDR (HTTP $status)"
}

main() {
    parse_args "$@"
    # placeholder defaults (so the body is still writable; the real numbers are parsed from summary.md by the caller and injected)
    : "${ADDED_PLACEHOLDER:=N}" "${CHANGED_PLACEHOLDER:=N}" \
      "${REACTIVATED_PLACEHOLDER:=N}" "${DEACTIVATED_PLACEHOLDER:=N}"
    load_conf
    gpg_encrypt
    write_body
    build_resend_payload
    push_resend
    log "done. run_id=$RUN_ID rc=0"
}

main "$@"