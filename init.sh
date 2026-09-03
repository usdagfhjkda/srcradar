#!/usr/bin/env bash
#
# init.sh — 一键拉取 srcradar 的所有上游依赖。
#
# 用法:
#   ./init.sh                  # 拉全部上游(ENScan_GO;不拉 httpx/dnsx fork 和 ymicp,均用户自管)
#   ./init.sh --check          # 只检查 git/curl/docker(不拉),返回 0/非零
#   ./init.sh --check-deps     # 详细环境检查:列全 完整功能跑通 所需工具及状态
#   ./init.sh --install-deps   # 列将要装的包 (DRY-RUN,不 sudo)
#   ./init.sh --install-deps --yes   # 真装 (sudo apt/dnf/brew)
#   ./init.sh --enscan-only    # 只拉 ENScan_GO
#   ./init.sh --offline        # 跳过所有网络调用(假设上游已 clone 过,只 build)
#   ./init.sh --init-db [PATH] # 生成空 recon.sqlite3 (PATH 可省,默认 ./db/recon.sqlite3)
#                              #   调用 db/init_db.py 喂 db/schema.sql (14 表+索引+触发器)
#                              #   PATH 已存在 → 自动备份到 PATH.bak.YYYYMMDD_HHMMSS
#   ./init.sh --check-schema   # 校对 db/schema.sql vs 源文件 CREATE TABLE
#                              #   check-only;exit 0=一致,1=drift
#
# 为什么需要这个脚本?
# srcradar 是编排层,本身不重新发明 ENScan 这种轮子。
# httpx / dnsx 等扫描工具由用户自行安装到 PATH(本脚本不内置)。
# ymicp/ICP_Query 是用户自部署的第三方服务,见 ymicp/README.md。
# 我们 clone 上游项目 + 在其上加 patch(详见 pdtm/CLAUDE.md),init 时拉取。
#
# 关键约束:
#   1. ENScan_GO 的目录不进 srcradar 主仓(.gitignore 排除),本脚本负责 clone。
#   2. 上游用 tag 锁定,不追 main 分支(防 upstream 改 license / API 变更)。
#      想升级?改本脚本里的 ENScan_GO_TAG。
#
# ymicp/ 目录:
#   - 它是 srcradar 自己写的 Python 客户端(icp_mapp_query.py / README.md),
#     跟着 srcradar 主仓走,**不**被本脚本管。
#   - 它依赖的 ymicp 服务端是第三方独立项目,**非 srcradar 维护**(详见 ymicp/README.md)。
#   - 本脚本**不**再 pull ymicp 镜像(改为用户自行部署,与 ENScan_GO 的 miit 插件同模式)。
#
# 退出码:
#   0   全部成功
#   1   参数错误
#   2   缺少必要命令(--check / --check-deps 失败)
#   3   某个上游 clone/pull 失败
#   4   --install-deps 自动安装失败
#

set -euo pipefail

# ---- 配置(改这里升级 upstream 版本) ----
ENScan_GO_REPO="https://github.com/wgpsec/ENScan_GO.git"
ENScan_GO_TAG="v1.4.0"           # 上游最新稳定 tag;git ls-remote --tags ... 看新版本

# ---- 完整功能跑通所需的工具清单 ----
# 类别:
#   - GO_REQUIRED  build 期一次性需要(Go ≥ 1.21)
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
    "go|1.21|golang-go|golang|go|go|build 期:编译 2 个 Go 项目 (db_align / ENScan_GO)|"
    # --- 运行时必需 ---
    "bash|4.0|bash|bash|bash|core|关联数组 / [[ ]] / <() process substitution|"
    "python3|3.10|python3|python3|python3|core|daily/lib + pdtm/*.py 全栈 Python|"
    "docker|0.0|docker.io|podman|docker|core|可选用(ymicp 由用户自部署,本脚本不再 pull)|"
    "flock|0.0|util-linux|util-linux|flock|core|daily/install_cron + pdtm/pipeline 互斥锁|"
    "sqlite3|3.0|sqlite3|sqlite|sqlite3|core|DB 调试 + Python sqlite3 stdlib|"
    "git|0.0|git|git|git|core|init 阶段 clone 上游 fork;日常不需要|"
    # --- 推荐装(扫描 / URL 资产)---
    "subfinder|0.0|subfinder|subfinder|subfinder|scan|pdtm/scan.sh glob 目标派生候选|"
    "alterx|0.0|alterx|alterx|alterx|scan|关键词派生,和 subfinder 配合|"
    "naabu|0.0|naabu|naabu|naabu|scan|tcp_assets 表的端口扫描数据;SYN 模式需要 setcap|"
    "ffuf|0.0|ffuf|ffuf|ffuf|scan|pdtm/scan_urls.py URL 爆破 → web_hash_urls|"
    "gau|0.0|gau|gau|gau|scan|wayback / Common Crawl 历史 URL|"
    "URLFinder|0.0||URLFinder||scan|中文社区版 by pingc0y,GitHub 无官方同名包;无 apt/brew 包,需自下载二进制|"
)
# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 参数解析 ----
MODE="all"
PASSTHROUGH_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check)         MODE="check"; shift ;;
        --check-deps)    MODE="check-deps"; shift ;;
        --install-deps)  MODE="install-deps"; shift ;;
        --enscan-only)   MODE="enscan"; shift ;;
        --tools-only)    MODE="tools"; shift ;;
        --offline)       MODE="offline"; shift ;;
        --init-db)       MODE="init-db"; shift
                         [ $# -gt 0 ] && [[ "$1" != --* ]] && INIT_DB_PATH="$1" && shift ;;
        --check-schema)  MODE="check-schema"; shift ;;
        --yes|-y)        PASSTHROUGH_ARGS+=("$1"); shift ;;
        -h|--help)
            sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "[init] unknown arg: $1" >&2; exit 1 ;;
    esac
done

log()  { printf '[init] %s\n' "$*"; }
warn() { printf '[init][warn] %s\n' "$*" >&2; }
err()  { printf '[init][err]  %s\n' "$*" >&2; }

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

# ---- 完整功能环境检查 / 自动安装 ----
# OS 探测 → 选择 apt / dnf / brew
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

# 版本比较:返回 0 表示 $1 >= $2,1 表示 < (语义版本号 "1.21.5" / "3.10.12")
ver_ge() {
    [ "$1" = "$2" ] && return 0
    # sort -V 排好后,看 $1 在不在前两行(>= $2)
    local highest
    highest="$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)"
    [ "$highest" = "$1" ]
}

# 工具探测:command -v 优先,常见安装路径兜底(避免 PATH 没 export 导致漏报)
# 已知漏报案例:/usr/local/go/bin/go(Golang 官方包默认装这里,zshrc 需手加 PATH)
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

# 解析工具实际版本(只支持语义版本号 <digits.digits...>)
# 优先 cmd --version(标准),失败再试 cmd -version;Go 1.21+ 不接受 --version,只接受 `go version`
parse_version() {
    local cmd="$1" binp out ver
    binp="$(cmd_path "$cmd")" || return 1
    # Go 1.21+ 只接受 `go version`,不接受 `go --version`
    if [ "$cmd" = "go" ]; then
        out="$("$binp" version 2>/dev/null)"
    else
        out="$("$binp" --version 2>/dev/null | head -1)"
        if [ -z "$out" ]; then
            out="$("$binp" -version 2>/dev/null | head -1)"
        fi
    fi
    ver="$(printf '%s\n' "$out" | grep -oE '[0-9]+(\.[0-9]+){1,3}' | head -1)"
    printf '%s' "$ver"
}

# 单条 dep 检查 → 打印 + 设全局变量 MISSING_DEP / MISSING_OPT
# 行格式: cmd|min|apt|dnf|brew|category|note
check_one_dep() {
    local line="$1"
    local cmd min apt_pkg dnf_pkg brew_pkg category note
    IFS='|' read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"

    local mark status actual
    case "$category" in
        go)    mark='[B]' ;;
        core)  mark='[C]' ;;
        scan)  mark='[S]' ;;
        *)     mark='[?]' ;;
    esac

    if cmd_path "$cmd" >/dev/null 2>&1; then
        actual="$(parse_version "$cmd")"
        if [ -n "$min" ] && [ "$min" != "0.0" ] && [ -n "$actual" ]; then
            if ver_ge "$actual" "$min"; then
                status="✓"
                printf '  %s %-10s %-10s %s\n' "$status" "$cmd" "$actual" "[$category] $note"
                return 0
            else
                status="✗"
                printf '  %s %-10s %-10s %s\n' "$status" "$cmd" "$actual(<$min)" "[$category] $note"
                if [ "$category" = "go" ] || [ "$category" = "core" ]; then
                    MISSING_DEP=1
                else
                    MISSING_OPT=1
                fi
                return 1
            fi
        fi
        status="✓"
        printf '  %s %-10s %s\n' "$status" "$cmd" "[$category] $note"
        return 0
    fi
    status="✗"
    printf '  %s %-10s %s\n' "$status" "$cmd(NOT FOUND)" "[$category] $note"
    if [ "$category" = "go" ] || [ "$category" = "core" ]; then
        MISSING_DEP=1
    else
        MISSING_OPT=1
    fi
    return 1
}

# 详细环境检查 — 输出全表,退出码:0=全装、1=缺必需、2=缺可选(仍可跑)
check_deps() {
    MISSING_DEP=0
    MISSING_OPT=0
    printf '[init] 完整功能跑通 — 环境检查\n'
    printf '[init] 图例: [B]=build 期  [C]=运行时必需  [S]=推荐装(没装数据不全)\n\n'
    for line in "${DEPS[@]}"; do
        check_one_dep "$line" || true
    done
    printf '\n'
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

# 自动安装 — 默认 DRY-RUN(只列将要装的包,不执行 sudo)。
# 加 --yes 才真正装。这样防止误操作 sudo 安装一堆东西。
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

    # 先跑一遍 check_deps,收集缺失项
    MISSING_DEP=0
    MISSING_OPT=0
    missing_pkgs=()

    for line in "${DEPS[@]}"; do
        IFS='|' read -r cmd min apt_pkg dnf_pkg brew_pkg category note <<<"$line"
        if cmd_path "$cmd" >/dev/null 2>&1; then
            # 已装但版本可能不够(此处不重新校验,只装缺命令的;版本升级留给用户)
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
        # 默认 dry-run:只打印 plan,不 sudo
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

# ---- 0. 初始化空 DB(调外部 db/init_db.py,无 Python heredoc) ----
#   默认 PATH = $SCRIPT_DIR/db/recon.sqlite3
#   schema.sql 在 $SCRIPT_DIR/db/schema.sql
#   行为:已存在→备份→重建;build 完校验 14 张表
#   退出码:0=成功 / 5=init_db.py 缺失 / 6=sqlite 失败 / 7=schema.sql 缺失
init_db() {
    local db_path="${1:-$SCRIPT_DIR/db/recon.sqlite3}"
    local init_py="$SCRIPT_DIR/db/init_db.py"
    local schema_sql="$SCRIPT_DIR/db/schema.sql"

    [ -f "$init_py" ] || { err "init_db.py 缺失: $init_py"; return 5; }
    [ -f "$schema_sql" ] || { err "schema.sql 缺失: $schema_sql"; return 7; }

    log "init-db: $db_path (schema=$schema_sql)"
    if python3 "$init_py" "$db_path" "$schema_sql"; then
        log "init-db OK: $db_path"
        return 0
    else
        err "init-db 失败(看上面 [err] 行);exit=$?"
        return 6
    fi
}

# ---- 0.5 schema drift 校对(调外部 db/check_schema.py,无 Python heredoc) ----
#   check-only,不修改任何东西
#   退出码:0=一致 / 7=drift(check_schema.py 自身非零退出,这里透传)
check_schema() {
    local checker="$SCRIPT_DIR/db/check_schema.py"
    [ -f "$checker" ] || { err "check_schema.py 缺失: $checker"; return 7; }
    log "check-schema: 校对 db/schema.sql vs 源文件 CREATE TABLE..."
    if python3 "$checker" "$SCRIPT_DIR/db/schema.sql"; then
        return 0
    else
        return 7
    fi
}

# ---- 1. 拉 ENScan_GO ----
ensure_enscan_go() {
    if  [ -d ENScan_GO/code ]; then
        log "ENScan_GO/ 已存在,跳过 clone (如需升级: rm -rf ENScan_GO && 重新跑 init.sh)"
        return 0
    fi
    log "cloning ENScan_GO @ $ENScan_GO_TAG ..."
    if ! git clone --branch "$ENScan_GO_TAG" --depth 1 "$ENScan_GO_REPO" ENScan_GO; then
        err "ENScan_GO clone 失败"
        return 1
    fi
    log "ENScan_GO OK -> $(du -sh ENScan_GO 2>/dev/null | cut -f1)"
}

# ---- 4. (可选) 提示 build ----
post_clone_hint() {
    cat <<EOF

[init] 上游依赖已就位。下一步:

  1. (可选) build 必要的 Go 二进制:
       cd ENScan_GO/code && go build -o ../ENScan .
       (db_align 也需要 Go build,见 db_align/README.md)

  2. (可选) build ENScan_GO:
       cd ENScan_GO/code && go build -o ../ENScan .

  3. (可选) 如需小程序备案反查,自行部署 ymicp 服务:
       srcradar 不再自动 pull,详见 ymicp/README.md §部署
       docker run -d -p 127.0.0.1:16181:16181 --name ymicp yiminger/ymicp

  4. 跑 smoke test:
       cd db_align && go build ./...
       ./bin/db_align -n ExampleCo -icp -delay 2

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
            return 0
            ;;
    esac

    log "模式: $MODE"
    check_env || { err "环境检查失败,装齐再跑"; return 2; }

    # 完整功能依赖检查(非阻塞,但警告)
    #   - check_deps 缺必需依赖时会 fail,但 init.sh 的核心是 clone upstream,
    #     允许"只 clone 不 build"的用户跑通,所以这里只 warn 不硬退。
    #   - 想严格卡,自己跑 ./init.sh --check-deps。
    if check_deps >/dev/null 2>&1; then
        : # 全部 OK
    else
        rc=$?
        if [ "$rc" = 1 ]; then
            warn "缺必需依赖(/init core)。clone 阶段会过,build 阶段会失败。"
            warn "  修法: ./init.sh --install-deps   或看 README §五"
        elif [ "$rc" = 2 ]; then
            warn "缺 [S] 类推荐项,clone 阶段会过,完整跑业务时会少数据"
        fi
    fi

    local rc=0

    case "$MODE" in
        all)
            ensure_enscan_go   || rc=3
            ;;
        enscan) ensure_enscan_go   || rc=3 ;;
    esac

    if [ "$rc" = 0 ]; then
        post_clone_hint
    else
        err "部分上游拉取失败,看上面 [err] 行"
    fi
    return "$rc"
}

main "$@"
