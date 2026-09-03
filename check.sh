#!/usr/bin/env bash
# check.sh — 仅检查 srcradar 环境所需的 go / python3 / git 版本。
#
# 行为:
#   - 不安装任何东西。
#   - 三个工具逐项查,每项独立报告。
#   - 全部达标:exit 0 + 汇总 OK。
#   - 有任意一项不达标:exit 1 + 打印修法(不自动修)。
#
# 设计动机:
#   init.sh 的 check / check-deps 路径耦合了 6+ 项,能装则装、不能装则报告。
#   check.sh 职责单一 —— 只答"环境够不够跑 srcradar",答完即走。
#   装的事归 install.sh。
#
# 用法:
#   ./check.sh             # 查全部
#   ./check.sh -h|--help   # 用法
#
# 阈值:
#   GO_MIN  = 1.25    (db_align Go 1.21+ + cdnmatch / ENScan_GO latest tag 要求)
#   PY_MIN  = 3.12    (db/init_db.py / daily/lib/*.py / ymicp/*.py 实际只 >= 3.7,
#                       收紧到 3.12 是为了 pathlib modern 语法 + tomllib 安全冗余)
#   GIT_MIN = 2.0     (init 时 clone 上游 fork; 任何 >= 2.x 即可)
#
# 退出码:
#   0   三项全部达标
#   1   任意一项不达标(缺失或版本低)
#   2   参数错误
#
# 关联脚本:install.sh(会在入口处再跑一次 check.sh 的检查)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GO_MIN="1.25"
PY_MIN="3.12"
GIT_MIN="2.0"

log() { printf '[check] %s\n' "$*"; }
err() { printf '[check][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,30p' "$0"
}

# 跨平台:command -v + 已知漏报兜底路径
detect_cmd_path() {
    local cmd="$1"
    command -v "$cmd" 2>/dev/null && return 0
    case "$cmd" in
        go)      [ -x /usr/local/go/bin/go ]      && echo /usr/local/go/bin/go      && return 0
                 [ -x /opt/homebrew/bin/go ]       && echo /opt/homebrew/bin/go       && return 0
                 [ -x /usr/lib/go/bin/go ]         && echo /usr/lib/go/bin/go         && return 0 ;;
        python3) [ -x /opt/homebrew/bin/python3 ] && echo /opt/homebrew/bin/python3 && return 0 ;;
        git)     [ -x /usr/local/bin/git ]        && echo /usr/local/bin/git        && return 0 ;;
    esac
    return 1
}

parse_version() {
    local cmd="$1" binp out
    if ! binp="$(detect_cmd_path "$cmd")"; then
        return 1
    fi
    if [ "$cmd" = "go" ]; then
        out="$("$binp" version 2>/dev/null || true)"
    else
        out="$("$binp" --version 2>/dev/null | head -1 || true)"
        if [ -z "$out" ]; then
            out="$("$binp" -version 2>/dev/null | head -1 || true)"
        fi
    fi
    printf '%s\n' "$out" | grep -oE '[0-9]+(\.[0-9]+){1,3}' | head -1
}

ver_ge() {
    [ "$1" = "$2" ] && return 0
    local highest
    highest="$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)"
    [ "$highest" = "$1" ]
}

check_one() {
    local cmd="$1" min="$2" label="$3"
    local actual
    if ! actual="$(parse_version "$cmd" 2>/dev/null)" || [ -z "$actual" ]; then
        err "  X ${label} ${cmd}: NOT FOUND"
        case "$cmd" in
            go)      err "      fix: install Go >= ${min} (https://go.dev/dl/)" ;;
            python3) err "      fix: install Python >= ${min} (uv or official)" ;;
            git)     err "      fix: install Git >= ${min} (apt: git / brew: git)" ;;
        esac
        FAILED=1
        return 1
    fi
    if ver_ge "$actual" "$min"; then
        printf '  OK %-7s %-9s actual=%-9s required>=%s\n' "$cmd" "$label" "$actual" "$min"
        return 0
    fi
    err "  X ${label} ${cmd}: ${actual} (< ${min})"
    err "      fix: upgrade to >= ${min}"
    FAILED=1
    return 1
}

main() {
    case "${1:-}" in
        -h|--help|"") : ;;
        *) err "unknown arg: $1"; usage; exit 2 ;;
    esac
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi

    log "srcradar env check (no-install)"
    log "thresholds: go>=${GO_MIN}  python3>=${PY_MIN}  git>=${GIT_MIN}"
    printf '\n'

    FAILED=0

    check_one "go"      "$GO_MIN"  "Go"         || true
    check_one "python3" "$PY_MIN"  "Python3"    || true
    check_one "git"     "$GIT_MIN" "Git"        || true

    printf '\n'
    if [ "$FAILED" = 1 ]; then
        err "env NOT met; fix the X items above, then re-run ./check.sh"
        return 1
    fi
    log "env OK: all 3 checks passed; safe to run ./install.sh"
    return 0
}

main "$@"
