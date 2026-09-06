#!/usr/bin/env bash
#
# modules/main/manage/install.sh — 装 manage 模块(业务管理)
#
# manage 模块组成:
#   - add_business.sh  加业务(seed)
#   - set_config.sh    业务 config 切换
#   - seeds/           种子业务数据(预留 .gitkeep)
#
# 用法:
#   bash install.sh                  # 默认:检查 + 创建 seeds/
#   bash install.sh --check          # 只查依赖
#   bash install.sh --uninstall
#
# 退出码:0/1/2/3/4 同 pdtm 模块

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf "[manage] %s\n" "$*"; }
warn() { printf "[manage][warn] %s\n" "$*" >&2; }
err()  { printf "[manage][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,13p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

# ---- 检查 ----
check_deps() {
    local rc=0
    command -v bash >/dev/null 2>&1 || { err "缺 bash"; rc=2; }
    command -v sqlite3 >/dev/null 2>&1 || { err "缺 sqlite3"; rc=2; }
    [ -x "$SCRIPT_DIR/add_business.sh" ] || { err "add_business.sh 缺失/不可执行"; rc=3; }
    [ -x "$SCRIPT_DIR/set_config.sh" ] || { err "set_config.sh 缺失/不可执行"; rc=3; }
    return $rc
}

# ---- 准备 seeds 目录 ----
install() {
    mkdir -p "$SCRIPT_DIR/seeds"
    if [ ! -f "$SCRIPT_DIR/seeds/.gitkeep" ]; then
        : > "$SCRIPT_DIR/seeds/.gitkeep"
    fi
    log "已准备 seeds/ 目录"
    return 0
}

# ---- verify ----
verify() {
    [ -x "$SCRIPT_DIR/add_business.sh" ] && printf "  OK add_business.sh\n"
    [ -x "$SCRIPT_DIR/set_config.sh" ] && printf "  OK set_config.sh\n"
    [ -d "$SCRIPT_DIR/seeds" ] && printf "  OK seeds/\n"
    return 0
}

# ---- uninstall ----
uninstall() {
    log "manage 模块无需反安装(seeds/ 与脚本保留)"
    return 0
}

main() {
    case "${1:-}" in
        --check) check_deps || exit $?; log "manage 模块依赖 OK"; exit 0 ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_deps || exit $?
            install || exit $?
            verify || exit $?
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
