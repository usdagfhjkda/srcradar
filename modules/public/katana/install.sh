#!/usr/bin/env bash
#
# modules/public/katana/install.sh — 装 katana 模块(可选 URL 扫描插件)
#
# katana 模块组成:
#   - katana_enrich.sh         CLI 入口(daily-url stage / dashboard 调用)
#   - lib/katana_runner.py     subprocess + JSONL 解析 + 入 web_hash_urls
#   - lib/risks.py             危险路由 5 词黑名单(沿用 URLFinder)
#   - lib/schema_map.py        katana JSONL → web_hash_urls 字段对齐
#   - config/katana.conf.example 占位模板(operator 自填真值后另存为
#                              config/katana.conf,.gitignore 屏蔽)
#
# 用法:
#   bash install.sh              # go install katana + 写 example + 自检
#   bash install.sh --check      # 只查依赖 + 自检,不动 config / 不装二进制
#   bash install.sh --uninstall  # 不删二进制(由 go env 管),只删 config
#
# 退出码:
#   0 = OK
#   2 = 缺依赖 (go / python3 / mmh3 / curl / git)
#   3 = katana 模块自检失败(关键文件缺失)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

EXAMPLE_CONF="${SCRIPT_DIR}/config/katana.conf.example"
TARGET_CONF="${SCRIPT_DIR}/config/katana.conf"

log()  { printf '[katana] %s\n' "$*"; }
warn() { printf '[katana][warn] %s\n' "$*" >&2; }
err()  { printf '[katana][err]  %s\n' "$*" >&2; }
usage() {
    sed -n '2,16p' "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
}

check_deps() {
    local rc=0
    command -v go >/dev/null 2>&1        || { err "缺 go (apt: golang-go;建议 >= 1.22)"; rc=2; }
    command -v python3 >/dev/null 2>&1   || { err "缺 python3"; rc=2; }
    command -v curl >/dev/null 2>&1      || { err "缺 curl"; rc=2; }
    command -v git >/dev/null 2>&1       || { err "缺 git"; rc=2; }

    # mmh3 Python 模块(algo_diff + response_hash 算法依赖)
    python3 -c "import mmh3" 2>/dev/null \
        || { err "缺 python3 mmh3 (pip install mmh3)"; rc=2; }

    # 关键文件
    [ -f "${SCRIPT_DIR}/katana_enrich.sh" ] \
        || { err "katana_enrich.sh 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/katana_runner.py" ] \
        || { err "lib/katana_runner.py 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/risks.py" ] \
        || { err "lib/risks.py 缺失"; rc=3; }
    [ -f "${SCRIPT_DIR}/lib/schema_map.py" ] \
        || { err "lib/schema_map.py 缺失"; rc=3; }

    return $rc
}

install_katana_bin() {
    if command -v katana >/dev/null 2>&1; then
        log "katana 已在 PATH: $(command -v katana)"
        return 0
    fi
    log "go install projectdiscovery/katana/v2/cmd/katana@latest"
    if ! go install github.com/projectdiscovery/katana/v2/cmd/katana@latest; then
        err "go install 失败;检查 GOPATH / GOBIN / 网络"
        return 1
    fi
    # katana 默认装到 $(go env GOPATH)/bin;PATH 通常已含,显式 echo 一次
    local gobin
    gobin="$(go env GOPATH)/bin"
    if [ -x "${gobin}/katana" ] && ! command -v katana >/dev/null 2>&1; then
        warn "katana 已装到 ${gobin},但不在 PATH — 加到 ~/.bashrc 或设 KATANA_BIN"
    fi
}

write_example() {
    if [ -f "$EXAMPLE_CONF" ]; then
        log "example 已存在: $EXAMPLE_CONF"
    else
        log "写入 example: $EXAMPLE_CONF"
        mkdir -p "$(dirname "$EXAMPLE_CONF")"
        cat > "$EXAMPLE_CONF" <<'CONFEOF'
# katana 模块配置占位模板
#
# 真值请复制本文件为 config/katana.conf(同目录)再编辑。
# config/katana.conf 在 .gitignore 内,不入仓。
#
# 字段:
#   KATANA_BIN            二进制绝对路径;留空走 PATH 探测(默认)
#   KATANA_TIMEOUT        单 host 超时秒数(默认 600;大字典场景用户拍板)
#   KATANA_EXTRA_RISKS    追加风险词(逗号分隔),默认: remove,delete,insert,update,logout
#                         例: KATANA_EXTRA_RISKS=signout,drop,truncate
#   KATANA_OMIT_BODY      1=不存 body(节省存储,response_hash 退化为 status|length)
#                         0=存 body(默认;algo_diff 需要)
#   KATANA_EXTRA_FLAGS    追加 katana flags(空格分隔),例: --no-sandbox
#
# 鉴权面:
#   - katana 自身无鉴权;本模块通过 subprocess 调,不 link Go 源码(MIT 边界)
#   - 5 词风险黑名单沿用 URLFinder(URLFinder/config/config.go:19),双重保险:
#     (1) katana -crawl-out-scope regex 源头拦截
#     (2) lib/risks.py 入库前再过一遍
KATANA_BIN=
KATANA_TIMEOUT=600
KATANA_EXTRA_RISKS=remove,delete,insert,update,logout
KATANA_OMIT_BODY=0
KATANA_EXTRA_FLAGS=
CONFEOF
    fi
}

uninstall() {
    # 不删 katana 二进制(用户可能装到全局)
    rm -f "$TARGET_CONF"
    log "已删 $TARGET_CONF(真值;example 保留)"
    log "katana 二进制未删;如需: rm \$(go env GOPATH)/bin/katana"
}

# ---- main ----
MODE="${1:-install}"
case "$MODE" in
    --check)
        log "检查依赖 + 自检..."
        check_deps
        rc=$?
        [ $rc -eq 0 ] && log "自检通过"
        exit $rc
        ;;
    --uninstall)
        uninstall
        exit 0
        ;;
    install|--yes|"")
        log "检查依赖..."
        if ! check_deps; then
            err "依赖不满足,exit"
            exit 2
        fi
        install_katana_bin
        write_example
        log "自检..."
        check_deps >/dev/null
        log "OK. 用法: bash katana_enrich.sh --help"
        exit 0
        ;;
    *)
        err "unknown arg: $MODE"
        usage >&2
        exit 2
        ;;
esac
