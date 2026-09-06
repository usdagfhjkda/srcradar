#!/usr/bin/env bash
#
# init.sh — 环境检查 + db 初始化。
#
# 重构后(init.sh 不再负责 clone/build;这部分拆给 modules/*/install.sh):
#   - 环境检查(go/python3/git + 完整依赖)
#   - --install-deps 自动装缺失依赖(apt/dnf/brew)
#   - --init-db [PATH] 调用 modules/main/db/install.sh --path
#   - --check-schema 校对 db/schema.sql vs 源文件 CREATE TABLE
#   - --offline      跳过所有网络(只 build/check;若已 git mv 完毕就 fast-pass)
#
# 模块 clone/build 改由 ./install.sh 或 ./install-internal.sh 统一调度
# (遍历 modules/main, modules/public 各模块 install.sh)。
#
# 用法:
#   ./init.sh                  # 仅检查 + 打印 next-step 提示
#   ./init.sh --check          # 只检查 git/curl/docker
#   ./init.sh --check-deps     # 详细环境检查
#   ./init.sh --install-deps   # 列出将要装的包 (DRY-RUN,不 sudo)
#   ./init.sh --install-deps --yes   # 真装 (sudo apt/dnf/brew)
#   ./init.sh --offline        # 跳过所有网络调用
#   ./init.sh --init-db [PATH] # 调用 modules/main/db/install.sh --path PATH
#   ./init.sh --check-schema   # 校对 db/schema.sql vs 源文件
#
# 退出码:
#   0   全部成功
#   1   参数错误
#   2   缺少必要命令(--check / --check-deps 失败)
#   3   某个上游 clone 失败(本脚本不负责)
#   4   --install-deps 自动安装失败
#   5/6/7  init-db / sqlite / schema.sql 缺失相关

set -euo pipefail

# ---- 配置(改这里升级 upstream 版本) ----
# 注意:ENScan_GO / cdncheck 等上游 vendor 由对应模块 install.sh 拉取
# (modules/public/db_align/install.sh / modules/main/pdtm/install.sh)

# ---- 完整功能跑通所需的工具清单 ----
# 类别:
#   - GO_REQUIRED  build 期一次性需要(Go >= 1.21)
#   - CORE         运行时必需(任何 mode 都要)
#   - SCAN         推荐装(没装 = 某些数据采不到,但不挂)
# 字段:
#   cmd  命令名
#   min  最低版本(空 = 不校验)
#   pkg  --install-deps 时按 OS 安装的包名(apt / dnf / brew)
GO_MIN_VERSION="1.21"
PY_MIN_VERSION="3.10"

# 完整功能跑通所需工具清单
# 格式: <cmd>|<min_version>|<apt_pkg>|<dnf_pkg>|<brew_pkg>|<category>|<note>
DEPS=(
    # --- build 期(Go 工具链)---
    "go|1.21|golang-go|golang|go|go|build 期:编译 Go 项目 (modules/main/pdtm/cdnmatch + modules/public/db_align)|"
    # --- 运行时必需 ---
    "bash|4.0|bash|bash|bash|core|关联数组 / [[ ]] / <() process substitution|"
    "python3|3.10|python3|python3|python3|core|daily/lib + pdtm/*.py 全栈 Python|"
    "docker|0.0|docker.io|podman|docker|core|可选用(ymicp 由用户自部署,本脚本不再 pull)|"
    "flock|0.0|util-linux|util-linux|flock|core|daily/install_cron + pdtm/pipeline 互斥锁|"
    "sqlite3|3.0|sqlite3|sqlite|sqlite3|core|DB 调试 + Python sqlite3 stdlib|"
    "git|0.0|git|git|git|core|模块 install.sh 拉取上游 vendor 用;日常不需要|"
    # --- 推荐装(扫描 / URL 资产)---
    "subfinder|0.0|subfinder|subfinder|subfinder|scan|modules/main/pdtm/scan.sh glob 目标派生候选|"
    "alterx|0.0|alterx|alterx|alterx|scan|关键词派生,和 subfinder 配合|"
    "naabu|0.0|naabu|naabu|naabu|scan|tcp_assets 表的端口扫描数据;SYN 模式需要 setcap|"
    "ffuf|0.0|ffuf|ffuf|ffuf|scan|modules/main/pdtm/scan_urls.py URL 爆破 -> web_hash_urls|"
    "gau|0.0|gau|gau|gau|scan|wayback / Common Crawl 历史 URL|"
    "URLFinder|0.0||URLFinder||scan|中文社区版 by pingc0y,GitHub 无官方同名包;无 apt/brew 包,需自下载二进制|"
)

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DB_DIR="$SCRIPT_DIR/modules/main/db"
DB_PATH_DEFAULT="$DB_DIR/recon.sqlite3"

# ---- 参数解析 ----
MODE="all"
PASSTHROUGH_ARGS=()
INIT_DB_PATH=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check)         MODE="check"; shift ;;
        --check-deps)    MODE="check-deps"; shift ;;
        --install-deps)  MODE="install-deps"; shift ;;
        --offline)       MODE="offline"; shift ;;
        --init-db)       MODE="init-db"; shift
                         [ $# -gt 0 ] && [[ "$1" != --* ]] && INIT_DB_PATH="$1" && shift ;;
        --check-schema)  MODE="check-schema"; shift ;;
        --yes|-y)        PASSTHROUGH_ARGS+=("$1"); shift ;;
        -h|--help)
            sed -n "2,32p" "$0"; exit 0 ;;
        *) echo "[init] unknown arg: $1" >&2; exit 1 ;;
    esac
done

log()  { printf "[init] %s\n" "$*"; }
warn() { printf "[init][warn] %s\n" "$*" >&2; }
err()  { printf "[init][err]  %s\n" "$*" >&2; }

# ---- 环境检查 ----
need_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        err "缺少命令: $1"
        return 1
    }
}

check_env() {
    local ok=1
    case "$MODE" in
        all|check)
            need_cmd git || ok=0
            need_cmd curl || ok=0
            ;;
    esac
    case "$MODE" in
        all|check)
            need_cmd docker || ok=0
            ;;
    esac
    [ "$ok" = 1 ] || return 1
    return 0
}

# ---- OS 探测 ----
detect_os() {
    case "$(uname -s)" in
        Linux)
            if   command -v apt-get >/dev/null 2>&1; then echo "apt"
            elif command -v dnf      >/dev/null 2>&1; then echo "dnf"
            elif command -v yum      >/dev/null 2>&1; then echo "dnf"
            else echo "unknown"; fi ;;
        Darwin) command -v brew >/dev/null 2>&1 && echo "brew" || echo "unknown" ;;
        *) echo "unknown" ;;
    esac
}

# ---- 版本比较 ----
ver_ge() {
    [ "$1" = "$2" ] && return 0
    local highest
    highest="$(printf "%s\n%s\n" "$1" "$2" | sort -V | tail -1)"
    [ "$highest" = "$1" ]
}

# ---- 工具探测 + 版本解析 ----
cmd_path() {
    local cmd="$1"
    command -v "$cmd" 2>/dev/null && return 0
    case "$cmd" in
        go)        [ -x /usr/local/go/bin/go ]    && echo /usr/local/go/bin/go    && return 0
                  [ -x /opt/homebrew/bin/go ]     && echo /opt/homebrew/bin/go     && return 0
                  [ -x /usr/lib/go/bin/go ]       && echo /usr/lib/go/bin/go       && return 0
                  ;;
        docker)    [ -x /usr/local/bin/docker ]    && echo /usr/local/bin/docker    && return 0 ;;
        python3)   [ -x /opt/homebrew/bin/python3 ] && echo /opt/homebrew/bin/python3 && return 0 ;;
        sqlite3)   [ -x /opt/homebrew/opt/sqlite/bin/sqlite3 ] && echo "$_" && return 0 ;;
    esac
    return 1
}

parse_version() {
    local cmd="$1" binp out ver
    binp="$(cmd_path "$cmd")" || return 1
    if [ "$cmd" = "go" ]; then
        out="$("$binp" version 2>/dev/null)"
    else
        out="$("$binp" --version 2>/dev/null | head -1)"
        if [ -z "$out" ]; then
            out="$("$binp" -version 2>/dev/null | head -1)"
        fi
    fi
    ver="$(printf "%s\n" "$out" | grep -oE "[0-9]+(\.[0-9]+){1,3}" | head -1)"
    printf "%s" "$ver"
}

check_one_dep() {
    local line="$1"
    local cmd min apt_pkg dnf_pkg brew_pkg category note
    IFS="|" read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"

    local mark status actual
    case "$category" in
        go)    mark="[B]" ;;
        core)  mark="[C]" ;;
        scan)  mark="[S]" ;;
        *)     mark="[?]" ;;
    esac

    if cmd_path "$cmd" >/dev/null 2>&1; then
        actual="$(parse_version "$cmd")"
        if [ -n "$min" ] && [ "$min" != "0.0" ] && [ -n "$actual" ]; then
            if ver_ge "$actual" "$min"; then
                status="OK"
                printf "  %s %-10s %-10s %s\n" "$status" "$cmd" "$actual" "[$category] $note"
                return 0
            else
                status="X"
                printf "  %s %-10s %-10s %s\n" "$status" "$cmd" "$actual(<$min)" "[$category] $note"
                if [ "$category" = "go" ] || [ "$category" = "core" ]; then
                    MISSING_DEP=1
                else
                    MISSING_OPT=1
                fi
                return 1
            fi
        fi
        status="OK"
        printf "  %s %-10s %s\n" "$status" "$cmd" "[$category] $note"
        return 0
    fi
    status="X"
    printf "  %s %-10s %s\n" "$status" "$cmd(NOT FOUND)" "[$category] $note"
    if [ "$category" = "go" ] || [ "$category" = "core" ]; then
        MISSING_DEP=1
    else
        MISSING_OPT=1
    fi
    return 1
}

check_deps() {
    MISSING_DEP=0
    MISSING_OPT=0
    printf "[init] 完整功能跑通 — 环境检查\n"
    printf "[init] 图例: [B]=build 期  [C]=运行时必需  [S]=推荐装(没装数据不全)\n\n"
    for line in "${DEPS[@]}"; do
        check_one_dep "$line" || true
    done
    printf "\n"
    if [ "$MISSING_DEP" = 0 ] && [ "$MISSING_OPT" = 0 ]; then
        log "环境 OK,所有依赖都齐"
        return 0
    fi
    if [ "$MISSING_DEP" = 1 ]; then
        err "缺必需依赖,跑 ./init.sh --install-deps 自动装,或参 README §五"
        return 1
    fi
    warn "依赖完整(必需项都装了);缺 [S] 类推荐项,数据采集中某些阶段会受影响"
    return 2
}

install_deps() {
    local auto_apply=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) auto_apply=1; shift ;;
            *) shift ;;
        esac
    done

    local os missing_pkgs cmd min apt_pkg dnf_pkg brew_pkg category note pkg_name
    os="$(detect_os)"
    log "OS 探测: $os"

    if [ "$os" = "unknown" ]; then
        err "无法识别 OS;不支持 Alpine / Arch / NixOS 等,自己装:bash python3 docker git sqlite3 flock subfinder alterx naabu ffuf gau"
        return 4
    fi

    MISSING_DEP=0
    MISSING_OPT=0
    missing_pkgs=()

    for line in "${DEPS[@]}"; do
        IFS="|" read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"
        if cmd_path "$cmd" >/dev/null 2>&1; then
            continue
        fi
        case "$os" in
            apt)   pkg_name="$apt_pkg" ;;
            dnf)   pkg_name="$dnf_pkg" ;;
            brew)  pkg_name="$brew_pkg" ;;
        esac
        if [ -z "$pkg_name" ]; then
            warn "$cmd 无对应 $os 包(URLFinder 等中文社区工具),请手动下载二进制放进 PATH"
            MISSING_OPT=1
            continue
        fi
        missing_pkgs+=("$pkg_name")
        if [ "$category" = "go" ] || [ "$category" = "core" ]; then
            MISSING_DEP=1
        else
            MISSING_OPT=1
        fi
    done

    if [ "${#missing_pkgs[@]}" = 0 ] && [ "$MISSING_OPT" = 0 ]; then
        log "依赖已装齐,无需安装"
        return 0
    fi

    if [ "$auto_apply" = 0 ]; then
        cat <<EOF
[init] DRY-RUN — 缺以下包(未执行安装):

  $os: ${missing_pkgs[*]:-(无可自动装的包)}

[init] 确认要装吗?加 --yes:
  ./init.sh --install-deps --yes
EOF
        return 0
    fi

    log "将安装: ${missing_pkgs[*]:-(无可自动装的包)}"

    case "$os" in
        apt)
            warn "需要 sudo 权限更新 apt 索引 + 装包"
            sudo apt-get update || { err "apt-get update 失败"; return 4; }
            sudo apt-get install -y "${missing_pkgs[@]}" || {
                err "apt-get install 失败(部分包可能在 universe/contrib,先 enable)"
                return 4
            }
            ;;
        dnf)
            warn "需要 sudo 权限装包"
            sudo dnf install -y "${missing_pkgs[@]}" || {
                err "dnf install 失败"
                return 4
            }
            ;;
        brew)
            brew install "${missing_pkgs[@]}" || {
                err "brew install 失败"
                return 4
            }
            ;;
    esac

    log "安装完成,跑 ./init.sh --check-deps 复核"
    return 0
}

# ---- init-db:转发给 modules/main/db/install.sh ----
init_db() {
    local db_path="${1:-$DB_PATH_DEFAULT}"
    local db_install="$DB_DIR/install.sh"

    if [ ! -x "$db_install" ]; then
        err "modules/main/db/install.sh 缺失或不可执行: $db_install"
        return 5
    fi

    log "init-db (via modules/main/db/install.sh): $db_path"
    if bash "$db_install" --path "$db_path"; then
        log "init-db OK: $db_path"
        return 0
    else
        err "init-db 失败(看 modules/main/db/install.sh 输出)"
        return 6
    fi
}

# ---- check-schema:转发给 modules/main/db/install.sh --check-schema ----
check_schema() {
    local db_install="$DB_DIR/install.sh"
    if [ ! -x "$db_install" ]; then
        err "modules/main/db/install.sh 缺失或不可执行: $db_install"
        return 7
    fi
    log "check-schema (via modules/main/db/install.sh)"
    if bash "$db_install" --check-schema; then
        return 0
    else
        return 7
    fi
}

# ---- next-step 提示 ----
next_step_hint() {
    cat <<EOF

[init] 下一步(模块安装已拆分到 ./install.sh):

  1. 装 main 模块(pdtm / daily / manage / db):
       ./install.sh
     内部部署(全装 main + public,不询问):
       ./install-internal.sh

  2. db_align(可选,法律实体反查):
       modules/public/db_align/install.sh
     内部装 ENScan_GO vendor + go build。

  3. ymicp(可选,小程序备案反查):
       modules/public/ymicp/install.sh

[init] 完成。
EOF
}

# ---- 主流程 ----
main() {
    case "$MODE" in
        check-deps)
            check_deps
            return $?
            ;;
        install-deps)
            install_deps "${PASSTHROUGH_ARGS[@]}"
            return $?
            ;;
        init-db)
            init_db "${INIT_DB_PATH:-}"
            return $?
            ;;
        check-schema)
            check_schema
            return $?
            ;;
        check)
            if check_env; then
                log "环境 OK (git/curl/docker)"
                return 0
            else
                err "环境检查失败"
                return 2
            fi
            ;;
        offline)
            log "--offline: 跳过所有网络调用"
            next_step_hint
            return 0
            ;;
    esac

    log "模式: $MODE"
    check_env || { err "环境检查失败,装齐再跑"; return 2; }

    if check_deps >/dev/null 2>&1; then
        :
    else
        rc=$?
        if [ "$rc" = 1 ]; then
            warn "缺必需依赖(/init core)。跑 init.sh --install-deps 装齐后再 ./install.sh"
        elif [ "$rc" = 2 ]; then
            warn "缺 [S] 类推荐项,./install.sh 仍可跑,数据采集中某些阶段会受影响"
        fi
    fi

    next_step_hint
    return 0
}

main "$@"