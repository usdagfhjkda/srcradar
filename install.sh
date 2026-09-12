#!/usr/bin/env bash
#
# install.sh — 外部版:遍历 modules/main 全装 + 遍历 modules/public 让用户选。
#
# 重构后(各模块自管 install.sh):
#   - 本脚本是统一调度入口
#   - main 模块必装,不询问
#   - public 模块询问一次,默认 N(回车=不装)
#   - 非 TTY(pipe/cron)默认不装 public
#
# 与 init.sh 的关系:
#   - init.sh --check-deps 装系统依赖(go/python3/docker/...)
#   - init.sh --init-db 建空 DB
#   - install.sh 调模块 install.sh 拉源码 + go build + 装二进制
#
# 与 install-internal.sh 的关系:
#   - install.sh          : 外部版(main + 询问 public)
#   - install-internal.sh : 内部版(main + public 全装,不询问)
#
# 用法:
#   ./install.sh                  # main 全装 + 询问 public
#   ./install.sh --no-public      # 只装 main(跳过询问 + 跳过 public)
#   ./install.sh --public-all     # main + public 全装,不询问(等价 install-internal)
#   ./install.sh --main-only      # 只跑 main 模块(无 init-db)
#   ./install.sh --pdtm-only      # 只装 modules/main/pdtm
#   ./install.sh --db-align-only  # 只装 modules/public/db_align
#   ./install.sh --ymicp-only     # 只装 modules/public/ymicp
#   ./install.sh --offline        # 跳过所有网络(假设已 clone)
#   ./install.sh --init-db        # 末尾:建空 DB(默认全装也跑)
#   ./install.sh --no-init-db     # 不建 DB
#   ./install.sh -h|--help
#
# 退出码:
#   0   全部成功
#   1   参数错误
#   2   check.sh 不通过
#   3   某个模块 install.sh 失败
#   4   缺 build 产物(模块 install.sh 内部报)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][warn] %s\n' "$*" >&2; }
err()  { printf '[install][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,32p' "$0"
}

# ---- 前置:跑 ./check.sh ----
run_check() {
    if [ ! -x "$SCRIPT_DIR/check.sh" ]; then
        err "check.sh 缺失或不可执行: $SCRIPT_DIR/check.sh"
        return 2
    fi
    log "前置:跑 ./check.sh ..."
    if ! "$SCRIPT_DIR/check.sh"; then
        err "check.sh 失败;按其提示升级 go/python3/git 后重跑"
        return 2
    fi
    log "check.sh OK"
    return 0
}

# ---- 询问是否装 public 模块(default N) ----
ask_public() {
    if [ ! -t 0 ]; then
        log "non-TTY:默认不装 public 模块 (用 --public-all 强制装)"
        return 1
    fi
    local reply
    read -r -p "需要装 public 模块 (db_align/ymicp)? [y/N]: " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                  return 1 ;;
    esac
}

# ---- 跑单个模块 install.sh ----
run_module() {
    local area="$1" name="$2"; shift 2
    local mdir="$SCRIPT_DIR/modules/$area/$name"
    local minstall="$mdir/install.sh"

    if [ ! -x "$minstall" ]; then
        warn "模块 $area/$name 的 install.sh 缺失或不可执行;跳过"
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

# ---- 装 main 模块(全部 --yes,不询问) ----
install_main() {
    if [ ! -d "$SCRIPT_DIR/modules/main" ]; then
        warn "modules/main 不存在;跳过"
        return 0
    fi
    for mdir in "$SCRIPT_DIR/modules/main"/*/; do
        [ -d "$mdir" ] || continue
        local name; name="$(basename "$mdir")"
        # db 模块特殊:后续 --init-db 跑,这里只 check
        if [ "$name" = "db" ]; then
            run_module main "$name" --check || return 3
        else
            run_module main "$name" --yes || return 3
        fi
    done
}

# ---- 装 public 模块(每个 --yes 强制,不询问) ----
install_public_all() {
    if [ ! -d "$SCRIPT_DIR/modules/public" ]; then
        warn "modules/public 不存在;跳过"
        return 0
    fi
    for mdir in "$SCRIPT_DIR/modules/public"/*/; do
        [ -d "$mdir" ] || continue
        local name; name="$(basename "$mdir")"
        run_module public "$name" --yes || return 3
    done
}

# ---- init-db(走 modules/main/db/install.sh --path) ----
do_init_db() {
    local db_install="$SCRIPT_DIR/modules/main/db/install.sh"
    if [ ! -x "$db_install" ]; then
        err "modules/main/db/install.sh 缺失"
        return 3
    fi
    log "init-db (via modules/main/db/install.sh --path)"
    bash "$db_install" --path || return 3
}

main() {
    MODE="all"
    DO_INIT_DB=1
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-public)      MODE="main-only"; shift ;;
            --public-all)     MODE="all-public"; shift ;;
            --main-only)      MODE="main-only"; shift ;;
            --pdtm-only)      MODE="pdtm-only"; shift ;;
            --db-align-only)  MODE="db-align-only"; shift ;;
            --ymicp-only)     MODE="ymicp-only"; shift ;;
            --offline)        # shellcheck disable=SC2034
                              OFFLINE=1; shift ;;
            --init-db)        DO_INIT_DB=1; shift ;;
            --no-init-db)     DO_INIT_DB=0; shift ;;
            -h|--help)        usage; exit 0 ;;
            *) err "unknown arg: $1"; usage; exit 1 ;;
        esac
    done

    run_check || { err "check 阶段失败,退出"; exit 2; }

    case "$MODE" in
        all)
            install_main || exit $?
            if ask_public; then
                install_public_all || exit $?
            else
                log "跳过 public 模块 (--public-all 强制装)"
            fi
            ;;
        main-only)
            install_main || exit $?
            ;;
        all-public)
            install_main || exit $?
            install_public_all || exit $?
            ;;
        pdtm-only)
            run_module main pdtm --yes || exit $?
            ;;
        db-align-only)
            run_module public db_align --yes || exit $?
            ;;
        ymicp-only)
            run_module public ymicp --yes || exit $?
            ;;
        *)
            err "unknown mode: $MODE"
            exit 1
            ;;
    esac

    if [ "$DO_INIT_DB" = 1 ]; then
        do_init_db || exit $?
    fi

    log "全部装完。PATH 提示: export PATH=\"\$PATH:\$HOME/go/bin:\$HOME/.pdtm/go/bin\""
    return 0
}

main "$@"