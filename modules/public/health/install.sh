#!/usr/bin/env bash
#
# modules/public/health/install.sh — 装 health 模块(可选运维监控)
#
# health 模块组成:
#   - health_check.sh            主入口(调 lib/health_check.py → 调 notify_send.sh)
#   - lib/health_check.py        5 项 check 函数 + 报告渲染
#   - lib/health_config.py       config/health.conf 读取器
#   - lib/thresholds.py          DEFAULT_THRESHOLDS + 类型安全的 merge
#   - config/health.conf.example 占位模板(入仓)
#   - config/health.conf         operator 自填真值(.gitignore 屏蔽)
#   - install_cron.sh            注册每日 cron(独立于 daily_monitor)
#
# 用法:
#   bash install.sh              # 默认:写 example + 自检依赖
#   bash install.sh --check      # 只查依赖 + 自检
#   bash install.sh --uninstall
#
# 退出码:
#   0 = OK
#   2 = 缺依赖 (python3 / df / uptime / pgrep / nproc / awk)
#   3 = health 模块自检失败(关键文件缺失)
#   4 = notify 依赖缺失(./notify/notify_send.sh 不可用)
#

set -euo pipefail

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
    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; rc=2; }
    command -v bash >/dev/null 2>&1    || { err "缺 bash"; rc=2; }
    command -v pgrep >/dev/null 2>&1   || { err "缺 pgrep (apt: procps)"; rc=2; }
    command -v df >/dev/null 2>&1      || { err "缺 df (coreutils)"; rc=2; }
    command -v uptime >/dev/null 2>&1  || { err "缺 uptime (apt: procps)"; rc=2; }
    command -v nproc >/dev/null 2>&1   || { err "缺 nproc (apt: coreutils)"; rc=2; }
    command -v awk >/dev/null 2>&1     || { err "缺 awk"; rc=2; }
    command -v crontab >/dev/null 2>&1 || warn "缺 crontab (apt: cron),后续 install_cron.sh 会失败"

    [ -f "$SCRIPT_DIR/health_check.sh" ] \
        || { err "health_check.sh 缺失"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/health_check.py" ] \
        || { err "lib/health_check.py 缺失"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/health_config.py" ] \
        || { err "lib/health_config.py 缺失"; rc=3; }
    [ -f "$SCRIPT_DIR/lib/thresholds.py" ] \
        || { err "lib/thresholds.py 缺失"; rc=3; }
    [ -f "$SCRIPT_DIR/install_cron.sh" ] \
        || { err "install_cron.sh 缺失"; rc=3; }
    return $rc
}

# 依赖 notify 模块:gpg + curl + notify_send.sh + notify_config.py
check_notify_dep() {
    local rc=0
    [ -f "$NOTIFY_SEND" ] \
        || { err "依赖缺失: ../notify/notify_send.sh (health 复用其推送通道)"; rc=4; }
    [ -f "$NOTIFY_CONFIG_PY" ] \
        || { err "依赖缺失: ../notify/lib/notify_config.py"; rc=4; }
    command -v gpg >/dev/null 2>&1  \
        || { err "缺 gpg (apt: gnupg),notify 模块会失败"; rc=4; }
    command -v curl >/dev/null 2>&1 \
        || { err "缺 curl,notify 模块会失败"; rc=4; }
    return $rc
}

# 写 example 占位(若缺)
write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "example 已存在: $EXAMPLE_CONF"
    else
        log "写入 example: $EXAMPLE_CONF"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'EOF'
# health 模块配置占位模板
#
# 真值请复制本文件为 config/health.conf(同目录)再编辑。
# config/health.conf 在 .gitignore 内,不入仓。
#
# 字段语义:
#   DISK_WARN_PCT       磁盘占用百分比 >= 该值记 warn     (默认 80)
#   DISK_ALERT_PCT      磁盘占用百分比 >= 该值记 alert    (默认 90)
#   CPU_LOAD5_PER_NPROC 5min load average / nproc 上限     (默认 2.0)
#   CPU_PCT_ALERT       top -bn1 瞬时 CPU 占用百分数告警    (默认 95)
#   MEM_ALERT_PCT       内存占用百分比 >= 该值记 alert    (默认 90)
#   CRON_MAX_AGE_HOURS  cron 任务日志 mtime 超过该小时数告警 (默认 25)
#   WATCHED_LOGS        空格分隔的日志路径列表
#
# 凭据(RESEND_API_KEY / FROM_ADDR / TO_ADDR / SUBJECT / GPG_RECIPIENT)
# 不在本文件;统一在 ../notify/config/notify.conf 维护,health 直接复用。
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

# 自检
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

    [ -f "$SCRIPT_DIR/health_check.sh" ]  && printf "  OK health_check.sh\n"   || { err "health_check.sh 缺失"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/health_check.py" ] && printf "  OK lib/health_check.py\n" || { err "lib/health_check.py 缺失"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/health_config.py" ] && printf "  OK lib/health_config.py\n" || { err "lib/health_config.py 缺失"; rc=1; }
    [ -f "$SCRIPT_DIR/lib/thresholds.py" ] && printf "  OK lib/thresholds.py\n" || { err "lib/thresholds.py 缺失"; rc=1; }
    [ -f "$SCRIPT_DIR/install_cron.sh" ] && printf "  OK install_cron.sh\n" || { err "install_cron.sh 缺失"; rc=1; }

    if [ -f "$NOTIFY_SEND" ]; then
        printf "  OK notify_send.sh (依赖): %s\n" "$NOTIFY_SEND"
    else
        err "依赖缺失: $NOTIFY_SEND"; rc=1
    fi
    if [ -f "$NOTIFY_CONFIG_PY" ]; then
        printf "  OK lib/notify_config.py (依赖)\n"
    else
        err "依赖缺失: $NOTIFY_CONFIG_PY"; rc=1
    fi

    [ -f "$EXAMPLE_CONF" ] && printf "  OK config/health.conf.example\n" \
        || { err "example 模板缺失"; rc=1; }

    # 真 conf 是可选的;提示一下但不让 verify 失败
    if [ -f "$TARGET_CONF" ]; then
        printf "  OK 真值配置存在: %s\n" "$TARGET_CONF"
    else
        warn "config/health.conf 不存在(--check / docker 默认忽略,真值由 operator 维护)"
    fi

    # 自检 conf 解析
    if python3 "${SCRIPT_DIR}/lib/health_config.py" --help >/dev/null 2>&1; then
        printf "  OK health_config.py --help\n"
    else
        err "health_config.py --help 失败"; rc=1
    fi
    # 自检 thresholds 解析
    if python3 "${SCRIPT_DIR}/lib/thresholds.py" >/dev/null 2>&1; then
        printf "  OK thresholds.py DEFAULT 输出\n"
    else
        err "thresholds.py 自身 import 失败"; rc=1
    fi

    # logs/ 目录可写
    mkdir -p "$SCRIPT_DIR/logs" 2>/dev/null && printf "  OK logs/ dir: %s\n" "$SCRIPT_DIR/logs" \
        || { warn "logs/ 目录不可写"; }

    # bash 版本 (脚本用 set -euo pipefail;要求 ≥4 for ${var:-})
    local bash_major
    bash_major="${BASH_VERSINFO[0]:-0}"
    if [ "$bash_major" -lt 4 ]; then
        err "bash >= 4 推荐 (当前 $BASH_VERSION)"
        rc=1
    else
        printf "  OK bash: %s\n" "$BASH_VERSION"
    fi

    return $rc
}

uninstall() {
    log "尝试移除 health 相关 cron 行"
    if [ -x "$SCRIPT_DIR/install_cron.sh" ] && command -v crontab >/dev/null 2>&1; then
        bash "$SCRIPT_DIR/install_cron.sh" uninstall || true
    fi
    log "已清理 cron 注册(代码与日志保留)"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_deps || exit $?
            check_notify_dep || true
            write_example
            verify || exit $?
            log "health 模块依赖 OK"
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