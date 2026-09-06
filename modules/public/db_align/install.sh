#!/usr/bin/env bash
#
# modules/public/db_align/install.sh — 装 db_align 模块(法律实体反查)
#
# db_align 模块组成:
#   - cmd/run/main.go + flags.go        CLI 入口
#   - cmd/inspect/main.go               状态检查
#   - internal/{crawler,enscan,mapper,permute,resolver,scope,store} 子包
#   - ENScan_GO/                        vendor(本 install.sh 负责 clone 上游到这)
#
# 用法:
#   bash install.sh                  # 默认:全装 + 询问是否构建
#   bash install.sh --yes            # 不询问,直接装
#   bash install.sh --check          # 只查依赖
#   bash install.sh --update         # 重新 fetch + checkout 最新 tag
#   bash install.sh --no-build       # 装上游 + 拉源码,不 go build
#   bash install.sh --uninstall      # 清理 vendor(ENScan_GO/) + bin/
#
# 退出码:0/1/2/3/4 同 pdtm 模块

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ENScan_GO 上游 tag 锁定(改这里升级)
ENSCAN_GO_REPO="https://github.com/wgpsec/ENScan_GO.git"
ENSCAN_GO_TAG="v1.4.0"

VENDOR_DIR="$SCRIPT_DIR/ENScan_GO"
VENDOR_SRC="$VENDOR_DIR/code"
ENScan_BIN="$VENDOR_DIR/ENScan"
DBALIGN_BIN="$SCRIPT_DIR/bin/db_align"

log()  { printf "[db_align] %s\n" "$*"; }
warn() { printf "[db_align][warn] %s\n" "$*" >&2; }
err()  { printf "[db_align][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,18p" "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v git >/dev/null 2>&1 || { err "缺 git (clone ENScan_GO 用)"; rc=2; }
    command -v go >/dev/null 2>&1 || [ -x /usr/local/go/bin/go ] || {
        err "缺 go (build db_align + ENScan 用)"; rc=2;
    }
    [ -f "$SCRIPT_DIR/go.mod" ] || { err "go.mod 缺失"; rc=3; }
    return $rc
}

# ---- 拉/更新 ENScan_GO vendor ----
ensure_enscan_go() {
    if [ -d "$VENDOR_SRC" ]; then
        log "ENScan_GO/ 已存在,跳过 clone (用 --update 重 fetch)"
        return 0
    fi
    log "git clone ENScan_GO @ $ENSCAN_GO_TAG -> $VENDOR_DIR"
    if ! git clone --branch "$ENSCAN_GO_TAG" --depth 1 "$ENSCAN_GO_REPO" "$VENDOR_DIR"; then
        err "ENScan_GO clone 失败"
        return 3
    fi
    log "ENScan_GO OK: $(du -sh "$VENDOR_DIR" 2>/dev/null | cut -f1)"
    return 0
}

update_enscan_go() {
    if [ ! -d "$VENDOR_DIR" ]; then
        ensure_enscan_go || return $?
    fi
    log "git -C $VENDOR_DIR fetch + checkout $ENSCAN_GO_TAG"
    (
        cd "$VENDOR_DIR"
        git fetch --depth 1 origin tag "$ENSCAN_GO_TAG"
        git checkout "$ENSCAN_GO_TAG"
    )
    return 0
}

# ---- build ENScan_GO/code -> ENScan binary ----
build_enscan_go() {
    if [ -x "$ENScan_BIN" ]; then
        log "ENScan 已 build: $ENScan_BIN (跳过)"
        return 0
    fi
    if [ ! -d "$VENDOR_SRC" ]; then
        err "ENScan_GO/code 不存在,先 ensure_enscan_go"
        return 3
    fi
    log "go build ENScan_GO/code -> $ENScan_BIN"
    (
        cd "$VENDOR_SRC"
        go build -o "$ENScan_BIN" .
    )
    [ -x "$ENScan_BIN" ] || { err "ENScan build 完但 $ENScan_BIN 缺失"; return 4; }
    log "ENScan 已装: $ENScan_BIN"
    return 0
}

# ---- build db_align binary ----
build_db_align() {
    if [ -x "$DBALIGN_BIN" ]; then
        log "db_align 已 build: $DBALIGN_BIN (跳过)"
        return 0
    fi
    log "go build db_align -> $DBALIGN_BIN"
    mkdir -p "$SCRIPT_DIR/bin"
    (
        cd "$SCRIPT_DIR"
        go build -o "$DBALIGN_BIN" ./cmd/run
    )
    [ -x "$DBALIGN_BIN" ] || { err "db_align build 完但 $DBALIGN_BIN 缺失"; return 4; }
    log "db_align 已装: $DBALIGN_BIN"
    return 0
}

# ---- 询问是否构建(default N) ----
ask_build() {
    if [ ! -t 0 ]; then
        log "non-TTY:跳过 build (用 --yes 强制 build)"
        return 1
    fi
    local reply
    read -r -p "要 build db_align + ENScan 吗? [y/N]: " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                  return 1 ;;
    esac
}

verify() {
    local rc=0
    [ -x "$DBALIGN_BIN" ] && printf "  OK db_align\n" || { warn "db_align 未 build"; rc=1; }
    [ -x "$ENScan_BIN" ] && printf "  OK ENScan\n" || { warn "ENScan 未 build"; rc=1; }
    [ -d "$VENDOR_SRC" ] && printf "  OK ENScan_GO/vendor\n" || { warn "ENScan_GO/vendor 缺失"; rc=1; }
    return $rc
}

uninstall() {
    rm -rf "$VENDOR_DIR" "$SCRIPT_DIR/bin"
    log "已清理 ENScan_GO/ 与 bin/(源码保留,下次重装再 clone)"
    return 0
}

main() {
    AUTO_YES=0
    case "${1:-}" in
        --check)
            check_deps || exit $?
            log "db_align 模块依赖 OK"
            exit 0
            ;;
        --yes)
            AUTO_YES=1
            shift
            check_deps || exit $?
            ensure_enscan_go || exit $?
            build_enscan_go || exit $?
            build_db_align || exit $?
            verify || exit $?
            ;;
        --update)
            check_deps || exit $?
            update_enscan_go || exit $?
            # 重新 build
            rm -f "$ENScan_BIN" "$DBALIGN_BIN"
            build_enscan_go || exit $?
            build_db_align || exit $?
            ;;
        --no-build)
            check_deps || exit $?
            ensure_enscan_go || exit $?
            log "已 clone ENScan_GO,跳过 build (用 --yes 或单独 build)"
            ;;
        --uninstall) uninstall ;;
        "")
            check_deps || exit $?
            ensure_enscan_go || exit $?
            if [ "$AUTO_YES" = 1 ] || ask_build; then
                build_enscan_go || exit $?
                build_db_align || exit $?
            else
                log "已 clone ENScan_GO,跳过 build"
            fi
            verify || true
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
