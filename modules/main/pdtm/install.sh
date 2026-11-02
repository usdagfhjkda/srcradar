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
    sed -n '2,15p' "${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
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

# ---- 跑 pdtm 装 PD 工具链(主线 5 工具 + cdncheck) ----
# 显式列 6 个工具,避免 `pdtm -ia` 自动装 pdtm 默认的全量 PD 工具集
# (Dockerfile:48 注释:约 25 个)。与 Dockerfile:50 口径一致。
# cdnmatch 的 `replace ../cdncheck` 由 install_cdnmatch() 自己 git clone vendor 满足。
install_pdtm_tools() {
    local pdtm_bin="$HOME/go/bin/pdtm"
    [ -x "$pdtm_bin" ] || pdtm_bin="$(command -v pdtm || true)"
    [ -n "$pdtm_bin" ] || { err "pdtm 未装;run install_pdtm_bin first"; return 3; }
    log "pdtm -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck"
    "$pdtm_bin" -duc -i dnsx,httpx,subfinder,alterx,naabu,cdncheck
    # shellcheck disable=SC2034 # miss counts missing tools; used in summary below
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

# ---- 装 massdns (shuffledns 的硬依赖, GPL-3.0) ----
install_massdns() {
    local dst="/usr/local/bin/massdns"
    if [ ! -w /usr/local/bin ] && [ ! -w "$(dirname "$dst")" ]; then
        # 无写权限时退回到用户 bin(本机/容器常见)
        dst="$HOME/.local/bin/massdns"
        mkdir -p "$(dirname "$dst")"
    fi
    if [ -x "$dst" ] || command -v massdns >/dev/null 2>&1; then
        log "massdns 已存在: $(command -v massdns || echo "$dst")"
        return 0
    fi
    log "git clone massdns -> /tmp/massdns"
    (cd /tmp && git clone --depth 1 https://github.com/blechschmidt/massdns.git)
    (cd /tmp/massdns && make)
    cp /tmp/massdns/bin/massdns "$dst"
    rm -rf /tmp/massdns
    [ -x "$dst" ] || { err "massdns install failed: $dst"; return 3; }
    log "massdns 已装: $dst"
    return 0
}

# ---- 装 wordlists (Top 模式 + Rolling 模式) ----
# 数据源:SecLists (MIT) + Jhaddix 公开词表(去噪清洗)
#
# 安装路径 fallback(仿 install_massdns 的写权检测风格):
#   /opt/srcradar/wordlists              ← docker / root(默认全局路径)
#   $SCRIPT_DIR/wordlists/               ← 源码版(普通用户,模块目录下)
#
# 实际装到的 dst 会写到 config/pdtm.conf 的 wordlists_dir=<abs_path>,
# 给 scan.sh / scanner.sh / pipeline.sh 下次跑时优先用。
install_wordlists() {
    local dst="/opt/srcradar/wordlists"
    # 写权检测:docker/root 走 /opt;普通用户(源码版常见)退到模块目录
    if [ ! -w /opt ] && [ ! -w "$(dirname "$dst")" ]; then
        dst="$SCRIPT_DIR/wordlists"
        mkdir -p "$dst"
    else
        mkdir -p "$dst"
    fi

    # 仓库内自带的词表(随发行物分发),优先使用:
    #   docker/wordlists/subdomains-top1million-20000.txt  上游原件,未改动
    #   docker/wordlists/dns-Jhaddix-clean.txt              上游文件的清洗产物
    # 两者都是 MIT 许可,声明见 NOTICE。找不到时回退到本机手动 clone 的
    # SecLists 副本(若存在)。
    local repo_root
    local repo_wl
    repo_root="$(cd "$SCRIPT_DIR/../../.." && pwd)"
    repo_wl="$repo_root/docker/wordlists"
    local src_top=""
    local src_rolling=""
    if [ -f "$repo_wl/subdomains-top1million-20000.txt" ]; then
        src_top="$repo_wl/subdomains-top1million-20000.txt"
    elif [ -f ~/tools/wordlists/SecLists-master/Discovery/DNS/subdomains-top1million-20000.txt ]; then
        src_top=~/tools/wordlists/SecLists-master/Discovery/DNS/subdomains-top1million-20000.txt
    fi
    if [ -f "$repo_wl/dns-Jhaddix-clean.txt" ]; then
        src_rolling="$repo_wl/dns-Jhaddix-clean.txt"
    elif [ -f ~/tools/wordlists/dns/dns-Jhaddix.txt ]; then
        # 本机有上游原件但仓库里没有清洗产物:现场清洗
        mkdir -p "$dst"
        awk '
            length($0) > 0 &&
            $0 !~ /^[.@*]/ &&
            $0 !~ /\./ &&
            length($0) <= 32
        ' ~/tools/wordlists/dns/dns-Jhaddix.txt \
          | sort -u > "$dst/dns-Jhaddix-clean.txt"
        src_rolling="$dst/dns-Jhaddix-clean.txt"
    fi

    if [ -n "$src_top" ]; then
        cp "$src_top" "$dst/"
        log "Top wordlist 已装: subdomains-top1million-20000.txt ($(wc -l < "$dst/subdomains-top1million-20000.txt") 行)"
    else
        warn "SecLists 词表缺失,Top 模式 brute 不可用"
    fi

    if [ -n "$src_rolling" ] && [ "$src_rolling" != "$dst/dns-Jhaddix-clean.txt" ]; then
        cp "$src_rolling" "$dst/"
    fi
    if [ -f "$dst/dns-Jhaddix-clean.txt" ]; then
        log "Rolling wordlist 已装: dns-Jhaddix-clean.txt ($(wc -l < "$dst/dns-Jhaddix-clean.txt") 行)"
    else
        warn "Rolling 词表缺失,Rolling 模式 brute 不可用"
    fi

    # 把实际装到的 dst 写入 config/pdtm.conf 的 wordlists_dir= 字段
    # (仿 modules/private/patches/install.sh write_config 的增量写模式)
    write_wordlists_config "$dst"
    return 0
}

# ---- 写 config/pdtm.conf 的 wordlists_dir=<abs_path> 行 ----
# 已有 wordlists_dir= 行替换 value;没有追加;chmod 600(与其他模块一致)
write_wordlists_config() {
    local wl_dir="$1"
    local cfg="$SCRIPT_DIR/../../../config/pdtm.conf"
    mkdir -p "$(dirname "$cfg")"

    local tmp
    tmp="$(mktemp)"
    if [ -f "$cfg" ]; then
        awk '/^wordlists_dir=/ { next } { print }' "$cfg" > "$tmp" || cp /dev/null "$tmp"
    fi

    cat >> "$tmp" <<EOF
wordlists_dir=$wl_dir
EOF

    mv "$tmp" "$cfg"
    chmod 600 "$cfg"
    log "config written: $cfg"
    log "  wordlists_dir=$wl_dir"
}

# ---- uninstall ----
uninstall() {
    # shellcheck disable=SC2115
    rm -rf -- "$SCRIPT_DIR/bin" "$SCRIPT_DIR/cdncheck"
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
            install_massdns || exit $?
            install_cdnmatch || exit $?
            install_wordlists
            verify
            ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown arg: $1"; usage; exit 1 ;;
    esac
    return 0
}

main "$@"
