#!/usr/bin/env bash
#
# modules/public/ymicp/install.sh — 装 ymicp 模块(小程序备案反查客户端)
#
# ymicp 模块组成:
#   - icp_mapp_query.py      Python 客户端
#   - config.yml             客户端配置(写死后被覆盖)
#   - docker-compose.yml     第三方服务端 yiminger/ymicp 的本地部署
#
# 用法:
#   bash install.sh                  # 默认:自检 + 询问是否拉 ymicp 服务
#   bash install.sh --yes            # 不询问:直接 docker compose up
#   bash install.sh --check          # 只查依赖 + 自检服务可达性
#   bash install.sh --no-server       # 装客户端,不管服务端
#   bash install.sh --uninstall
#
# 退出码:0/1/2/3/4 同 pdtm 模块

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

YMICP_HOST="${YMICP_HOST:-127.0.0.1}"
YMICP_PORT="${YMICP_PORT:-16181}"

log()  { printf "[ymicp] %s\n" "$*"; }
warn() { printf "[ymicp][warn] %s\n" "$*" >&2; }
err()  { printf "[ymicp][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,17p" "$0"
}

check_deps() {
    local rc=0
    command -v python3 >/dev/null 2>&1 || { err "缺 python3"; rc=2; }
    python3 -c "import requests" 2>/dev/null || { err "缺 python-requests (apt: python3-requests)"; rc=2; }
    [ -f "$SCRIPT_DIR/icp_mapp_query.py" ] || { err "icp_mapp_query.py 缺失"; rc=3; }
    # config.yml / docker-compose.yml 在 srcradar 主仓主动不提供(用户自管,见 README)
    # 因此 install.sh 不报警告,只 hint 用户读 README 自己生成
    log "config.yml / docker-compose.yml 由用户自管(见 README §文件说明)"
    return $rc
}

# ---- 自检 ymicp 服务可达性 ----
probe_server() {
    local url="http://${YMICP_HOST}:${YMICP_PORT}/"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --max-time 5 -o /dev/null "$url" 2>/dev/null; then
            log "ymicp 服务可达: $url"
            return 0
        else
            warn "ymicp 服务不可达: $url"
            return 1
        fi
    fi
    warn "无 curl,跳过服务自检"
    return 2
}

# ---- 询问是否启服务端(default N) ----
ask_run_server() {
    if [ ! -t 0 ]; then
        log "non-TTY:不启服务端"
        return 1
    fi
    local reply
    read -r -p "要用 docker compose 启 ymicp 服务端? [y/N]: " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                  return 1 ;;
    esac
}

# ---- docker compose up ----
run_server() {
    if ! command -v docker >/dev/null 2>&1; then
        err "缺 docker,无法启服务端"
        return 2
    fi
    if [ ! -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        err "docker-compose.yml 缺失"
        return 3
    fi
    log "docker compose -f $SCRIPT_DIR/docker-compose.yml up -d"
    (
        cd "$SCRIPT_DIR"
        docker compose -f docker-compose.yml up -d
    )
    log "等 5s 让服务启动..."
    sleep 5
    probe_server || warn "服务可能还没起来,稍后重 probe"
    return 0
}

verify() {
    local rc=0
    [ -x "$SCRIPT_DIR/icp_mapp_query.py" ] || [ -f "$SCRIPT_DIR/icp_mapp_query.py" ] \
        && printf "  OK icp_mapp_query.py\n" || { err "icp_mapp_query.py 缺失"; rc=1; }
    python3 "$SCRIPT_DIR/icp_mapp_query.py" --help >/dev/null 2>&1 && printf "  OK --help 输出\n" \
        || warn "icp_mapp_query.py --help 失败(语法/import 问题?)"
    probe_server >/dev/null 2>&1 && printf "  OK ymicp 服务可达\n" \
        || warn "ymicp 服务不可达(--no-server 模式可忽略)"
    return $rc
}

uninstall() {
    if command -v docker >/dev/null 2>&1 && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
        log "docker compose down (如服务在跑)"
        (
            cd "$SCRIPT_DIR"
            docker compose -f docker-compose.yml down 2>/dev/null || true
        )
    fi
    log "已清理 ymicp docker(代码与配置保留)"
    return 0
}

main() {
    AUTO_YES=0
    case "${1:-}" in
        --check)
            check_deps || exit $?
            probe_server || true
            log "ymicp 模块依赖 OK"
            exit 0
            ;;
        --yes)
            AUTO_YES=1
            shift
            check_deps || exit $?
            verify || true
            run_server || true
            ;;
        --no-server)
            check_deps || exit $?
            verify || exit $?
            ;;
        --uninstall) uninstall ;;
        "")
            check_deps || exit $?
            if [ "$AUTO_YES" = 1 ] || ask_run_server; then
                run_server || true
            else
                log "跳过服务端(--no-server 模式)"
            fi
            verify || true
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
