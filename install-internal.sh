#!/usr/bin/env bash
#
# install-internal.sh — 内部部署版,遍历 modules/{main,public} 全装,无询问。
#
# 与 install.sh 的关系:
#   - install.sh          : 外部版(main 全装 + public 询问用户选)
#   - install-internal.sh : 内部版(main + public 全装,不询问)
#
# 用法:
#   ./install-internal.sh            # 全装
#   ./install-internal.sh --dry-run  # 只打印动作,不执行
#   ./install-internal.sh --main-only  # 只装 main(等同 install.sh --main-only)
#   ./install-internal.sh --help
#
# 退出码:
#   0   全部成功
#   1   参数错误
#   2   check.sh 不通过
#   3   某个模块 install.sh 失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf "[install-internal] %s\n" "$*"; }
warn() { printf "[install-internal][warn] %s\n" "$*" >&2; }
err()  { printf "[install-internal][err]  %s\n" "$*" >&2; }

usage() {
    sed -n "2,18p" "$0"
}

run_check() {
    if [ ! -x "$SCRIPT_DIR/check.sh" ]; then
        err "check.sh 缺失或不可执行: $SCRIPT_DIR/check.sh"
        return 2
    fi
    log "前置 ./check.sh ..."
    "$SCRIPT_DIR/check.sh" || return 2
    return 0
}

run_module() {
    local area="$1" name="$2"; shift 2
    local mdir="$SCRIPT_DIR/modules/$area/$name"
    local minstall="$mdir/install.sh"

    if [ ! -x "$minstall" ]; then
        warn "模块 $area/$name 的 install.sh 缺失或不可执行;跳过"
        return 0
    fi

    if [ "${DRY_RUN:-0}" = 1 ]; then
        printf "  [dry-run] %s %s\n" "$minstall" "$*"
        return 0
    fi
    log "[$area/$name] 跑 install.sh $*"
    if "$minstall" "$@"; then
        log "[$area/$name] OK"
        return 0
    else
        local rc=$?
        err "[$area/$name] 失败 (exit=$rc)"
        return 3
    fi
}

main() {
    DRY_RUN=0
    MAIN_ONLY=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)   DRY_RUN=1; shift ;;
            --main-only) MAIN_ONLY=1; shift ;;
            -h|--help)   usage; exit 0 ;;
            *) err "unknown arg: $1"; usage; exit 1 ;;
        esac
    done

    run_check || { err "check 阶段失败,退出"; exit 2; }

    # ---- main 全装 ----
    if [ -d "$SCRIPT_DIR/modules/main" ]; then
        for mdir in "$SCRIPT_DIR/modules/main"/*/; do
            [ -d "$mdir" ] || continue
            local name; name="$(basename "$mdir")"
            if [ "$name" = "db" ]; then
                # db 模块跳过 --yes(其 --yes 行为是 init-db),init-db 走末尾
                run_module main "$name" --check || exit 3
            else
                run_module main "$name" --yes || exit 3
            fi
        done
    fi

    # ---- public 全装(除非 --main-only)----
    if [ "$MAIN_ONLY" = 0 ] && [ -d "$SCRIPT_DIR/modules/public" ]; then
        for mdir in "$SCRIPT_DIR/modules/public"/*/; do
            [ -d "$mdir" ] || continue
            local name; name="$(basename "$mdir")"
            run_module public "$name" --yes || exit 3
        done
    fi

    # ---- 末尾:init-db ----
    local db_install="$SCRIPT_DIR/modules/main/db/install.sh"
    if [ -x "$db_install" ] && [ "$DRY_RUN" = 0 ]; then
        log "init-db (via modules/main/db/install.sh --path)"
        bash "$db_install" --path || exit 3
    fi

    log "全部装完"
    return 0
}

main "$@"