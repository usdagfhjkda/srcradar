#!/usr/bin/env bash
#
# modules/public/notify/notify_send.sh — 主推送脚本
#
# 调用方式:
#   notify_send.sh <summary.md> <business_name> <run_id> <date_placeholder>
#
# 行为:
#   1) 读 config/notify.conf(.gitignore 屏蔽,operator 自填)
#   2) gpg --batch --yes --trust-model always \
#         -r "$GPG_RECIPIENT" \
#         -o summary.gpg -e summary.md
#      注意: GPG_RECIPIENT 是占位符(REPLACE_WITH_RECIPIENT_EMAIL),
#            真收件人邮箱不入任何文件;operator 复制 example → notify.conf 后自填。
#   3) 写一份纯文本 body(只摘要,不含子域 / IP / 状态码 / 真实业务名细节)
#      作为 Resend 邮件正文(text 字段)
#   4) build_resend_payload() 写 ~/.cache/notify/payload.json:
#        {
#          "from": "$FROM_ADDR",
#          "to": ["$TO_ADDR"],
#          "subject": "$SUBJECT $BUSINESS $DATE_PH daily diff",
#          "text": "<summary.body.md 内容>",
#          "attachments": [{"filename": "summary.gpg",
#                           "content": "<base64(summary.gpg)>"}]
#        }
#      attachments[].content 必须是 base64 字符串(Resend API 强制)。
#   5) push_resend():
#        curl -fsS -X POST https://api.resend.com/emails \
#          -H "Authorization: Bearer $RESEND_API_KEY" \
#          -H "Content-Type: application/json" \
#          --data-binary "@$PAYLOAD_JSON"
#      --dryrun 跳过 curl 调用,只生成 summary.gpg / body / payload.json。
#
# 安全:
#   - 真 Resend API key / 真实邮箱地址只能从 config/notify.conf 读(.gitignore)
#   - 所有失败都用 set -euo pipefail 显式返回 rc;caller daily_monitor.sh 用
#     `|| true` 兜底决定 rc
#   - 不改 ~/.bashrc / 不写 cookie / 不持久化 token
#
# 退出码:
#   0 = 推送成功(或被 --dryrun 短路)
#   1 = 业务侧参数错(只缺 summary.md / business_name 等)
#   2 = notify.conf 缺失或必填字段是 REPLACE_WITH_*
#   3 = gpg 失败
#   4 = Resend HTTP 失败(curl 退出码非 0 或 HTTP 状态 ≥ 400)
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
            # --text-file <path>:用 caller 提供的纯文本文件作邮件正文(text 字段),
            # 跳过 daily diff 模板。文本必须是 plain text,不含 HTML 标签(<...> 形式)。
            # 不传 → 走原 daily diff 模板(向后兼容 daily_monitor)。
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

# 读 config/notify.conf 走 lib/notify_config.py
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

    # 必填字段:任何一项是 REPLACE_WITH_* 或空都拒发
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

# gpg 加密 summary.md → summary.gpg(同目录)
gpg_encrypt() {
    local out="${SUMMARY%.md}.gpg"
    # -r 用 GPG_RECIPIENT(真值在 notify.conf 里,不入仓)
    # shellcheck disable=SC2154  # set by load_conf via printf -v
    gpg --batch --yes --trust-model always \
        -r "$GPG_RECIPIENT" \
        -o "$out" -e "$SUMMARY" || { err "gpg 加密失败(rc=$?)"; return 3; }
    GPG_OUT="$out"
    log "gpg ok: $out ($(wc -c <"$out") bytes)"
}

# 写纯文本 body(只摘要,严格不包含资产细节)
write_body() {
    local body_file="${SUMMARY%.md}.body.md"
    if [ -n "${TEXT_FILE:-}" ]; then
        # caller 提供自定义正文(plain text,不含 HTML 标签)
        # source 与 target 同路径时,`>` 会先 truncate 再读 = 空文件(health 模块设计);
        # 用 cat 到 tmp + mv 避免(也可直接 if 判同 skip,但写权限还要 chmod)
        local tmp
        tmp="$(mktemp "${body_file}.XXXXXX")"
        cat -- "$TEXT_FILE" > "$tmp"
        mv -f -- "$tmp" "$body_file"
        chmod 0600 "$body_file"
        BODY_OUT="$body_file"
        log "body ok (caller-provided text-file): $body_file ($(wc -c <"$body_file") bytes)"
        return 0
    fi
    # 默认:daily diff 模板(BUSINESS / DATE_PH / 四个占位符由 caller 解析后注入)。
    # 真业务名 / 子域 / IP / 状态码 / 风险等级一律不入仓。
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

# build_resend_payload: 把 {from, to, subject, text, attachments[]} 写到 payload.json
# attachments[0].content 必须是 base64(Resend API 强制)。
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

# push_resend: curl POST 到 Resend HTTP API
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
    # 占位符默认值(让 body 仍可写;真实数字由 caller 从 summary.md 解析后注入)
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