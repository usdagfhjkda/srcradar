#!/usr/bin/env bash
# install.sh — 装 srcradar 的所有上游依赖。
#
# 与 init.sh 的关系:
#   - 本脚本接管 init.sh 的"装"职责:
#       pdtm 二进制 -> pdtm -ia (dnsx/httpx/subfinder/alterx/naabu/...)
#       cdnmatch   -> git clone cdncheck (vendor) + go mod tidy + go build
#       ENScan_GO  -> git clone (tag-locked) + go build
#       init-db    -> 末尾自动 db/init_db.py 建空 DB(默认 all 分支)
#   - check.sh 是"前置检查,仅查不装";本脚本入口会跑一次 check.sh。
#   - init.sh 现在保留 --init-db / --check-schema 入口(与"装"无关,单独判定的工具)。
#
# 用法:
#   ./install.sh                  # 装全部上游(ENScan_GO 会问;默认 N=不装)+ 自动 init-db
#   ./install.sh --no-enscan      # 装全部上游,但不要 ENScan_GO(跳过询问)+ 自动 init-db
#   ./install.sh --enscan-only    # 只装 ENScan_GO(跳过询问,强制装)
#   ./install.sh --cdnmatch-only  # 只装 pdtm/cdnmatch
#   ./install.sh --pdtm-only      # 只装 pdtm (-ia)
#   ./install.sh --offline        # 跳过所有网络(假设已经 clone 过,只 build)
#   ./install.sh -h|--help
#
# ENScan_GO 询问规则:
#   - TTY 交互:      显式问 y/N,默认 N(回车=不装)
#   - 非 TTY(pipe/cron): 静默默认不装(用 --enscan-only 强制装)
#   - 任何 --enscan-only / --no-enscan 都跳过询问,直接走对应分支
#
# db_align + ENScan_GO 阶段的运行语义:
#   - 这一阶段涉及商业数据源(爱企查/天眼查/七麦)反爬 + cookie 凭据管理,
#     推荐"半自动"模式:人/AI 在路上介入——候选 PID 消歧、cookie 失效恢复、
#     缓存清理等。主 README §"运行方式"段有详细说明。
#   - 不接受半自动环节,可彻底不装(--no-enscan);其余模块(pdtm / daily)不依赖它。
#
# 装到哪:
#   - pdtm:        ~/go/bin/pdtm (go install 默认 GOBIN)
#   - PD 工具:     ~/.pdtm/go/bin/   (pdtm 自管,与 README §六"工具路径硬编码"一致)
#   - cdnmatch:    pdtm/bin/cdnmatch (与 pdtm/bin 同级, srcradar 仓库内)
#   - ENScan_GO:   srcradar/ENScan_GO/ENScan (子仓级,不在主仓 git 里)
#   - DB:          db/recon.sqlite3 (默认路径;已存在 -> 自动备份到 .bak.YYYYMMDD_HHMMSS)
#
# 退出码:
#   0   全部成功
#   1   参数错误
#   2   check.sh 不通过 (go/python/git 缺或低)
#   3   上游 clone / go mod / go build / pdtm -ia / init-db 失败
#   4   缺 build 产物(某阶段成功但二进制没出)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 上游 tag 锁定 ----
ENSCAN_GO_REPO="https://github.com/wgpsec/ENScan_GO.git"
ENSCAN_GO_TAG="v1.4.0"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][warn] %s\n' "$*" >&2; }
err()  { printf '[install][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,32p' "$0"
}

# =============================================================================
# 0. 前置:跑 ./check.sh;不达标直接退出
# =============================================================================
run_check() {
    if [ ! -x "$SCRIPT_DIR/check.sh" ]; then
        err "check.sh 缺失或不可执行: $SCRIPT_DIR/check.sh"
        return 2
    fi
    log "前置:跑 ./check.sh..."
    if ! "$SCRIPT_DIR/check.sh"; then
        err "check.sh 失败;按其提示升级 go/python3/git 后重跑"
        return 2
    fi
    log "check.sh OK"
    return 0
}

# =============================================================================
# 0.5 ENScan_GO 询问(默认 N)
# =============================================================================
ask_enscan() {
    if [ ! -t 0 ]; then
        log "non-TTY:默认不装 ENScan_GO (用 --enscan-only 强制装)"
        return 1
    fi
    local reply
    read -r -p "需要装 ENScan_GO 吗? [y/N]: " reply
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *)                  return 1 ;;
    esac
}

# =============================================================================
# 1. 装 pdtm:go install 到 ~/go/bin/
# =============================================================================
install_pdtm_bin() {
    if command -v pdtm >/dev/null 2>&1 || [ -x "$HOME/go/bin/pdtm" ]; then
        log "pdtm 已存在,跳过 go install ($HOME/go/bin/pdtm)"
        return 0
    fi
    log "go install pdtm -> $HOME/go/bin/pdtm"
    (cd /tmp && go install -v github.com/projectdiscovery/pdtm/cmd/pdtm@latest)
    if [ ! -x "$HOME/go/bin/pdtm" ]; then
        err "pdtm go install 完成但 $HOME/go/bin/pdtm 不存在"
        return 3
    fi
    log "pdtm 已装: $($HOME/go/bin/pdtm -version 2>&1 | head -1)"
    return 0
}

# =============================================================================
# 2. pdtm -ia 装 PD 工具链
# =============================================================================
install_pdtm_tools() {
    local pdtm_bin="$HOME/go/bin/pdtm"
    [ -x "$pdtm_bin" ] || pdtm_bin="$(command -v pdtm || true)"
    if [ -z "$pdtm_bin" ]; then
        err "pdtm 未装;install_pdtm_bin 必须先成功"
        return 3
    fi
    log "pdtm -ia (装 dnsx httpx subfinder alterx naabu cdncheck 等 -> $HOME/.pdtm/go/bin/)"
    "$pdtm_bin" -ia
    log "pdtm -ia 完成;验证关键工具..."
    local miss=0
    for t in dnsx httpx subfinder alterx naabu; do
        if [ ! -x "$HOME/.pdtm/go/bin/$t" ]; then
            err "  X $t 未在 $HOME/.pdtm/go/bin/"
            miss=1
        else
            printf '  OK %s\n' "$t"
        fi
    done
    [ "$miss" = 1 ] && return 4
    return 0
}

# =============================================================================
# 3. 装 pdtm/cdnmatch(git clone cdncheck vendor + go mod tidy + go build)
# =============================================================================
install_cdnmatch() {
    local cdn_dir="$SCRIPT_DIR/pdtm/cdnmatch"
    local vendor_dir="$SCRIPT_DIR/pdtm/cdncheck"
    if [ ! -d "$cdn_dir" ]; then
        err "pdtm/cdnmatch 目录缺失: $cdn_dir"
        return 3
    fi
    if [ -x "$SCRIPT_DIR/pdtm/bin/cdnmatch" ]; then
        log "pdtm/bin/cdnmatch 已存在,跳过 rebuild"
        return 0
    fi
    # 0) go.mod 里 replace github.com/projectdiscovery/cdncheck => ../cdncheck
    #    (in-tree vendor copy,见 go.mod 注释)。先 git clone cdncheck 到 sibling 目录。
    #    vendor 目录命中 .gitignore (pdtm/cdncheck/ 与 pdtm/dnsx/ 同段),不入主仓。
    if [ ! -d "$vendor_dir" ]; then
        log "git clone cdncheck (vendor, 不入仓) -> $vendor_dir"
        git clone --depth 1 https://github.com/projectdiscovery/cdncheck.git "$vendor_dir"
    fi
    if [ ! -d "$vendor_dir" ]; then
        err "cdncheck clone 失败: $vendor_dir 缺失"
        return 3
    fi
    log "go mod tidy + go build cdnmatch ..."
    (
        cd "$cdn_dir"
        go mod tidy
        mkdir -p "$SCRIPT_DIR/pdtm/bin"
        go build -o "$SCRIPT_DIR/pdtm/bin/cdnmatch" .
    )
    if [ ! -x "$SCRIPT_DIR/pdtm/bin/cdnmatch" ]; then
        err "cdnmatch build 完但 $SCRIPT_DIR/pdtm/bin/cdnmatch 缺失"
        return 4
    fi
    log "cdnmatch 已装: $SCRIPT_DIR/pdtm/bin/cdnmatch"
    return 0
}

# =============================================================================
# 3.5 初始化空 DB(调 db/init_db.py;建 srcradar 14 表+索引+触发器的权威 schema)
#   行为:已存在 -> init_db.py 自己备份到 PATH.bak.YYYYMMDD_HHMMSS 再覆盖
#   退出码:init_db.py 自身非零即失败
# =============================================================================
init_db() {
    local db_path="${1:-$SCRIPT_DIR/db/recon.sqlite3}"
    local init_py="$SCRIPT_DIR/db/init_db.py"
    local schema_sql="$SCRIPT_DIR/db/schema.sql"

    if [ ! -f "$init_py" ]; then
        err "init_db.py 缺失: $init_py"
        return 3
    fi
    if [ ! -f "$schema_sql" ]; then
        err "schema.sql 缺失: $schema_sql"
        return 3
    fi

    log "init-db: $db_path (schema=$schema_sql)"
    if python3 "$init_py" "$db_path" "$schema_sql"; then
        log "init-db OK"
        return 0
    else
        err "init-db 失败(见上面 [err] 行)"
        return 3
    fi
}

# =============================================================================
# 4. 装 ENScan_GO
# =============================================================================
install_enscan_go() {
    local dest="$SCRIPT_DIR/ENScan_GO"
    local src_dir="$dest/code"
    if [ -x "$dest/ENScan" ]; then
        log "ENScan 二进制已存在: $dest/ENScan (跳过 build)"
        return 0
    fi
    if [ ! -d "$dest" ]; then
        log "git clone ENScan_GO ($ENSCAN_GO_TAG)..."
        git clone --branch "$ENSCAN_GO_TAG" --depth 1 "$ENSCAN_GO_REPO" "$dest"
    fi
    if [ ! -d "$src_dir" ]; then
        err "ENScan_GO/code 目录缺失: 期望 clone 后存在"
        return 3
    fi
    log "go build ENScan_GO/code -> $dest/ENScan ..."
    (
        cd "$src_dir"
        go build -o "$dest/ENScan" .
    )
    if [ ! -x "$dest/ENScan" ]; then
        err "ENScan build 完但 $dest/ENScan 缺失"
        return 4
    fi
    log "ENScan 已装: $dest/ENScan"
    return 0
}

# =============================================================================
# main
# =============================================================================
main() {
    MODE="all"
    while [ $# -gt 0 ]; do
        case "$1" in
            --enscan-only)    MODE="enscan";    shift ;;
            --no-enscan)      MODE="all-no-enscan"; shift ;;
            --cdnmatch-only)  MODE="cdnmatch";  shift ;;
            --pdtm-only)      MODE="pdtm";      shift ;;
            --offline)        OFFLINE=1;        shift ;;
            -h|--help)        usage; exit 0 ;;
            *) err "unknown arg: $1"; usage; exit 1 ;;
        esac
    done

    run_check || { err "check 阶段失败,退出"; exit 2; }

    export PATH="$PATH:$HOME/go/bin"

    # offline 模式
    if [ "${OFFLINE:-0}" = 1 ]; then
        log "--offline:跳过所有网络调用(只 build)"
        case "$MODE" in
            all|cdnmatch|all-no-enscan) install_cdnmatch || exit $? ;;
            enscan) install_enscan_go || exit $? ;;
            pdtm)   : ;;
            *)      err "--offline 下 mode=$MODE 无意义"; exit 1 ;;
        esac
        log "offline build 完成"
        exit 0
    fi

    case "$MODE" in
        all)
            install_pdtm_bin    || exit $?
            install_pdtm_tools  || exit $?
            install_cdnmatch    || exit $?
            if ask_enscan; then
                install_enscan_go || exit $?
            else
                log "跳过 ENScan_GO (用 --enscan-only 强制装)"
            fi
            # 末尾:初始化空 DB(走 db/init_db.py)
            init_db || exit $?
            log "全部装完(含 init-db). PATH 提示: export PATH=\\"$PATH:$HOME/go/bin:$HOME/.pdtm/go/bin\\""
            ;;
        all-no-enscan)
            install_pdtm_bin    || exit $?
            install_pdtm_tools  || exit $?
            install_cdnmatch    || exit $?
            # all-no-enscan 与 all 一致:末尾也 init-db(用户说默认末尾)
            init_db || exit $?
            log "全部装完(不含 ENScan_GO, 含 init-db). PATH 提示: export PATH=\\"$PATH:$HOME/go/bin:$HOME/.pdtm/go/bin\\""
            ;;
        pdtm)
            install_pdtm_bin    || exit $?
            install_pdtm_tools  || exit $?
            ;;
        cdnmatch)
            install_cdnmatch    || exit $?
            ;;
        enscan)
            install_enscan_go   || exit $?
            ;;
        *)
            err "unknown mode: $MODE"
            exit 1
            ;;
    esac
    return 0
}

main "$@"
