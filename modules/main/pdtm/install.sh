#!/usr/bin/env bash
#
# modules/main/pdtm/install.sh — 装 pdtm 模块(扫描流水线)
#
# pdtm 模块组成:
#   - pdtm/         PD 工具链编排器
#   - cdnmatch/     Go module (projectdiscovery/cdncheck fork + 本地改)
#   - *.sh / *.py   扫描流水线 (scan.sh / scanner.sh / pipeline.sh ...)
#
# 用法:
#   bash install.sh                  # 默认:全装
#   bash install.sh --check          # 只查依赖
#   bash install.sh --cdnmatch-only  # 只装 cdnmatch
#   bash install.sh --pdtm-only      # 只装 pdtm 编排器 + PD 工具链
#   bash install.sh --uninstall      # 反向回滚
#
# 退出码:
#   0   成功
#   1   参数错误
#   2   缺依赖
#   3   build 失败
#   4   缺产物

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf '[pdtm] %s\n' "$*"; }
warn() { printf '[pdtm][warn] %s\n' "$*" >&2; }
err()  { printf '[pdtm][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,15p' "$0"
}

# ---- 检查 go 在 PATH(并尝试常见漏报路径) ----
check_go() {
    local go_bin=""
    if command -v go >/dev/null 2>&1; then
        go_bin="$(command -v go)"
    elif [ -x /usr/local/go/bin/go ]; then
        go_bin=/usr/local/go/bin/go
    elif [ -x /opt/homebrew/bin/go ]; then
        go_bin=/opt/homebrew/bin/go
    fi
    if [ -z "$go_bin" ]; then
        err "缺 go:装 Go >= 1.25 (https://go.dev/dl/)"
        return 2
    fi
    log "go: $($go_bin version)"
    return 0
}

# ---- 装 pdtm 编排器(go install 到 ~/go/bin/) ----
install_pdtm_bin() {
    if command -v pdtm >/dev/null 2>&1 || [ -x "$HOME/go/bin/pdtm" ]; then
        log "pdtm 已存在,跳过 ($HOME/go/bin/pdtm)"
        return 0
    fi
    log "go install pdtm -> $HOME/go/bin/pdtm"
    (cd /tmp && go install -v github.com/projectdiscovery/pdtm/cmd/pdtm@latest)
    [ -x "$HOME/go/bin/pdtm" ] || { err "go install 后 $HOME/go/bin/pdtm 缺失"; return 3; }
    log "pdtm 已装"
    return 0
}

# ---- 跑 pdtm -ia 装 PD 工具链 ----
install_pdtm_tools() {
    local pdtm_bin="$HOME/go/bin/pdtm"
    [ -x "$pdtm_bin" ] || pdtm_bin="$(command -v pdtm || true)"
    [ -n "$pdtm_bin" ] || { err "pdtm 未装;run install_pdtm_bin first"; return 3; }
    log "pdtm -ia (dnsx httpx subfinder alterx naabu cdncheck...)"
    "$pdtm_bin" -ia
    local miss=0
    for t in dnsx httpx subfinder alterx naabu; do
        if [ ! -x "$HOME/.pdtm/go/bin/$t" ]; then
            warn "$t 未在 $HOME/.pdtm/go/bin/(pdtm -ia 可能跳过)"
        else
            printf "  OK %s\n" "$t"
        fi
    done
    return 0
}

# ---- 装 cdnmatch(本地 Go module,需 cdncheck vendor) ----
install_cdnmatch() {
    local cdn_dir="$SCRIPT_DIR/cdnmatch"
    local vendor_dir="$SCRIPT_DIR/cdncheck"
    local bin="$SCRIPT_DIR/bin/cdnmatch"

    if [ ! -d "$cdn_dir" ]; then
        err "pdtm/cdnmatch 目录缺失: $cdn_dir"
        return 3
    fi
    if [ -x "$bin" ]; then
        log "cdnmatch 已存在: $bin (跳过 rebuild)"
        return 0
    fi
    if [ ! -d "$vendor_dir" ]; then
        log "git clone cdncheck (vendor, 不入仓) -> $vendor_dir"
        git clone --depth 1 https://github.com/projectdiscovery/cdncheck.git "$vendor_dir"
    fi
    (
        cd "$cdn_dir"
        go mod tidy
        mkdir -p "$SCRIPT_DIR/bin"
        go build -o "$bin" .
    )
    [ -x "$bin" ] || { err "cdnmatch build 完但 $bin 缺失"; return 4; }
    log "cdnmatch 已装: $bin"
    return 0
}

# ---- verify:关键产物存在 ----
verify() {
    local rc=0
    if [ -x "$SCRIPT_DIR/bin/cdnmatch" ]; then
        printf "  OK cdnmatch\n"
    else
        warn "cdnmatch 未装;用 --cdnmatch-only 装"
        rc=1
    fi
    if command -v pdtm >/dev/null 2>&1 || [ -x "$HOME/go/bin/pdtm" ]; then
        printf "  OK pdtm\n"
    else
        warn "pdtm 未装;用 --pdtm-only 装"
        rc=1
    fi
    return $rc
}

# ---- uninstall ----
uninstall() {
    rm -rf "$SCRIPT_DIR/bin" "$SCRIPT_DIR/cdncheck"
    log "已清理 pdtm/bin/ 与 pdtm/cdncheck/(vendor)"
    log "pdtm 编排器仍需 go install 卸载:$HOME/go/bin/pdtm"
    return 0
}

main() {
    case "${1:-}" in
        --check)
            check_go || exit 2
            log "pdtm 模块依赖 OK"
            exit 0
            ;;
        --cdnmatch-only) install_cdnmatch || exit $? ;;
        --pdtm-only)
            check_go || exit 2
            install_pdtm_bin || exit $?
            install_pdtm_tools || exit $?
            ;;
        --uninstall) uninstall ;;
        --yes|"")
            check_go || exit 2
            install_pdtm_bin || exit $?
            install_pdtm_tools || exit $?
            install_cdnmatch || exit $?
            verify
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
