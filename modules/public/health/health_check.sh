#!/usr/bin/env bash
#
# modules/public/health/health_check.sh — 主入口
#
# 调用方式:
#   bash health_check.sh [--dryrun] [--config <conf>]
#
# 行为:
#   1) 调 lib/health_check.py 跑 5 项 check
#      - 生成 logs/health_report.md (完整版,含数值 / 路径,作为附件明文)
#      - 生成 logs/health_report.body.md (脱敏版,只摘要 + 计数)
#   2) 若任一异常(warn/alert) → 调 ../notify/notify_send.sh 发邮件
#      - 复用 modules/public/notify/config/notify.conf 的凭据
#      - gpg 加密附件 + 邮件正文(text 字段) → Resend HTTP API
#   3) 全 OK → 只写 INFO 到 logs/health.log,不发邮件
#
# 安全:
#   - 真配置(config/health.conf / notify.conf)由 operator 自填,
#     都在 .gitignore 内,不入仓
#   - 任何 push 失败都用 || true 兜底,不影响健康结论本身
#   - 不持久化 token / 不写 ~/.bashrc / 不调任何云 API(只复用 notify)
#
# 退出码:
#   0 = 全 OK 或 异常已通知(不论 push 成败)
#   1 = 参数错
#   2 = 依赖缺失(python3 / lib/health_check.py)
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

    # INFO 行由 health_check.sh 在跑完后写(含时间戳 + findings 摘要);
    # stderr 异常信息由 health_check.py 用 2>>"$HEALTH_LOG" 收集

    # conf 缺时回退 example(只是为了让 --dryrun 也能跑通;真凭据在 notify.conf)
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

    # 写 INFO 行到 health.log(每次跑留痕;失败也不影响主流程)
    # 提取 overall + findings 计数,无需 re-parse 整份 report
    if [ -f "${LOG_DIR}/health_report.md" ]; then
        # report.md 的 overall 行实际格式是 "- overall: **ALERT**",grep 锚点要带 "- ";
        # grep 无匹配时 rc=1,在 set -euo pipefail 下会 abort main,必须 || true 兜底
        OVERALL="$(grep -m1 "^- overall:" "${LOG_DIR}/health_report.md" 2>/dev/null \
            | sed -e 's/^- overall:\s*//' -e 's/\s*$//' || true)"
        # grep -c 无匹配时 rc=1,会让 cmd sub 在 set -euo pipefail 下失败;
# 加 || true 兑底(不要 || echo 0,那会双输出 "0\n0")。
COUNT="$(grep -c "^| " "${LOG_DIR}/health_report.md" 2>/dev/null || true)"
        printf "[%s] overall=%s findings_rows=%s dryrun=%d\n" \
            "$(date '+%Y-%m-%d %H:%M:%S %z')" "${OVERALL:-UNKNOWN}" "${COUNT}" "$DRYRUN" \
            >> "$HEALTH_LOG" 2>/dev/null || true
    fi
}

main "$@"