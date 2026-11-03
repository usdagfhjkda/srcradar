#!/usr/bin/env bash
#
# modules/public/notify/install.sh — 装 notify 模块(可选推送插件)
#
# notify 模块组成:
#   - notify_send.sh            主推送脚本(gpg 加密 + curl POST 到 Resend HTTP API)
#   - lib/notify_config.py      config/notify.conf 读取器
#   - config/notify.conf.example 配置占位模板(operator 自填真值后另存为
#                              config/notify.conf,.gitignore 屏蔽)
#
# 用法:
#   bash install.sh              # 默认:写 config/notify.conf.example(若缺)
#                                 # + 自检 gpg / curl / notify_config.py
#   bash install.sh --check      # 只查依赖 + 自检,不动 config
#   bash install.sh --uninstall
#
# 退出码:
#   0 = OK
#   2 = 缺依赖 (gpg / python3 / curl)
#   3 = notify 模块自检失败
#

set -euo pipefail

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
    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; rc=2; }
    command -v gpg >/dev/null 2>&1      || { err "缺 gpg (apt: gnupg)"; rc=2; }
    command -v curl >/dev/null 2>&1     || { err "缺 curl (Resend HTTP API 调用需要)"; rc=2; }
    [ -f "${SCRIPT_DIR}/notify_send.sh" ] \
        || { err "notify_send.sh 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/notify_config.py" ] \
        || { err "lib/notify_config.py 缺失"; rc=3; }
    return $rc
}

# ---- 写 example 占位(若缺) ----
write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "example 已存在: $EXAMPLE_CONF"
    else
        log "写入 example: $EXAMPLE_CONF"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'EOF'
# notify 模块配置占位模板
#
# 真值请复制本文件为 config/notify.conf(同目录)再编辑。
# config/notify.conf 在 .gitignore 内,不入仓。
#
# 字段:
#   RESEND_API_KEY  Resend HTTP API key(在 https://resend.com/api-keys
#                   生成,以 re_ 开头,至少 20 字符;绑一个发件域)
#   FROM_ADDR       发件人地址(必须在 Resend 后台已验证的发件域下)
#   TO_ADDR         收件人邮箱(操作员)
#   SUBJECT         邮件主题前缀(默认 "[srcradar] daily diff")
#   GPG_RECIPIENT   GPG 收件人邮箱(== TO_ADDR;给 gpg -r 用)
#
# 任何字段留空 / REPLACE_WITH_* 都会让 notify_send.sh 拒绝发并打印配置缺失。
RESEND_API_KEY=REPLACE_WITH_RESEND_API_KEY
FROM_ADDR=noreply@your-domain.example
TO_ADDR=REPLACE_WITH_RECIPIENT_EMAIL
SUBJECT=[srcradar] daily diff
GPG_RECIPIENT=REPLACE_WITH_RECIPIENT_EMAIL
EOF
        chmod 0644 "$EXAMPLE_CONF"
    fi
}

# ---- 自检 ----
verify() {
    local rc=0
    if command -v gpg >/dev/null 2>&1; then
        printf "  OK gpg: %s\n" "$(command -v gpg)"
    else
        err "gpg 缺失"; rc=1
    fi
    if command -v curl >/dev/null 2>&1; then
        printf "  OK curl: %s\n" "$(command -v curl)"
    else
        err "curl 缺失"; rc=1
    fi
    if command -v python3 >/dev/null 2>&1; then
        printf "  OK python3: %s\n" "$(command -v python3)"
    else
        err "python3 缺失"; rc=1
    fi
    # 关键:在 TTY 下 example 通常已被 operator 复制成 notify.conf。
    # 非 TTY(--check)不强制要求真值存在,但要确认 example 在。
    if [ -t 0 ]; then
        if [ -f "$TARGET_CONF" ]; then
            printf "  OK 真值配置存在: %s\n" "$TARGET_CONF"
        else
            warn "config/notify.conf 不存在(--check / docker 默认忽略,真值由 operator 维护)"
        fi
    fi
    if [ -f "$EXAMPLE_CONF" ]; then
        printf "  OK example 模板存在\n"
    else
        err "example 模板缺失"; rc=1
    fi
    if python3 "${SCRIPT_DIR}/lib/notify_config.py" --help >/dev/null 2>&1; then
        printf "  OK notify_config.py --help 输出\n"
    else
        err "notify_config.py --help 失败"; rc=1
    fi
    # RESEND_API_KEY 格式提醒(只在 --check 路径打印,不强制)
    if [ -f "$TARGET_CONF" ]; then
        if grep -q '^RESEND_API_KEY=' "$TARGET_CONF" 2>/dev/null; then
            local key
            key="$(sed -n 's/^RESEND_API_KEY=//p' "$TARGET_CONF" | head -1)"
            if [[ "$key" == REPLACE_WITH_* ]] || [ -z "$key" ]; then
                warn "RESEND_API_KEY 仍是占位符,真值未填"
            elif [[ "$key" != re_* ]]; then
                warn "RESEND_API_KEY 不以 re_ 开头(Resend 标准格式)"
            elif [ "${#key}" -lt 20 ]; then
                warn "RESEND_API_KEY 长度 < 20 字符(疑似无效)"
            fi
        fi
    fi
    # payload 目录可写
    mkdir -p "$PAYLOAD_DIR" 2>/dev/null && chmod 0700 "$PAYLOAD_DIR" \
        && printf "  OK payload dir: %s\n" "$PAYLOAD_DIR" \
        || { warn "payload 目录不可写 ($PAYLOAD_DIR)"; }
    return $rc
}

uninstall() {
    # 仅移除可能由 install.sh 自动创建的文件;不动 operator 自填的 notify.conf
    log "保留 example 模板与 operator 自填的真值 config;无可清理项"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_deps || exit $?
            write_example
            verify || exit $?
            log "notify 模块依赖 OK"
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