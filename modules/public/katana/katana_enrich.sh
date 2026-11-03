#!/usr/bin/env bash
# katana_enrich.sh — CLI 入口(给 daily-url stage + dashboard 手动调用)
#
# 用法:
#   katana_enrich.sh --db <path> --business <name> --hosts-file <file>
#   katana_enrich.sh --help
#
# 退出码:
#   0  = ok
#   3  = 缺 katana 二进制
#   4  = db / hosts-file 不存在
#   5  = hosts 文件为空
#   6  = business 不在 businesses 表
#   非0 = katana_runner.py 自身异常
#
# 环境变量:
#   KATANA_BIN            二进制路径(env > PATH > 报错)
#   KATANA_TIMEOUT        单 host 超时(默认 600s,跟 scan_urls 同款)
#   KATANA_EXTRA_RISKS    追加风险词(逗号分隔,默认 5 词)
#   KATANA_OMIT_BODY=1    关闭 body 落库(response_hash 退化为 status|length)
#   KATANA_EXTRA_FLAGS    追加 katana flags(空格分隔)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
RUNNER="${LIB_DIR}/katana_runner.py"

log()  { printf '[katana] %s\n' "$*"; }
err()  { printf '[katana][err] %s\n' "$*" >&2; }
usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}"
}

DB=""
BUSINESS=""
HOSTS_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --db)         DB="$2"; shift 2 ;;
        --business)   BUSINESS="$2"; shift 2 ;;
        --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *) err "unknown arg: $1"; usage >&2; exit 2 ;;
    esac
done

if [ -z "$DB" ] || [ -z "$BUSINESS" ] || [ -z "$HOSTS_FILE" ]; then
    err "missing required: --db / --business / --hosts-file"
    usage >&2
    exit 2
fi

if [ ! -f "$RUNNER" ]; then
    err "runner missing: $RUNNER"
    exit 3
fi

# exec 是有意的:让 python 进程直接接管,信号(Ctrl+C)更干净地传到 runner;
# 后续无任何逻辑,SC2093 误报。
# shellcheck disable=SC2093
exec python3 "$RUNNER" enrich \
    --db "$DB" \
    --business "$BUSINESS" \
    --hosts-file "$HOSTS_FILE"
