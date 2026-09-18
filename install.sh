#!/usr/bin/env bash
#
# install.sh — 外部版:交互式 checklist 选模块,默认全不勾。
#
# 行为:
#   - 先跑 check.sh
#   - 进入 checklist:每个模块 [ ]/[x] 切换;0 确认;q 退出
#   - 按 main / public / private 三段字母序排列,段间用 --- 分隔
#   - daily 默认不勾(避免默默注册 cron)
#
# 与 init.sh 的关系:
#   - init.sh --check-deps 装系统依赖(go/python3/docker/...)
#   - init.sh --init-db 建空 DB
#   - install.sh 调模块 install.sh 拉源码 + go build + 装二进制
#
# 用法:
#   ./install.sh                  # 交互式 checklist(默认)
#   ./install.sh --check          # 只跑 check.sh(不进入 checklist)
#   ./install.sh --skip-check     # 跳 check,直接进 checklist(知道环境已达标的用户)
#   ./install.sh --init-db        # 末尾建空 DB(默认开)
#   ./install.sh --no-init-db     # 不建 DB
#   ./install.sh --offline        # 跳过所有网络(假设已 clone)
#   ./install.sh -h|--help
#
# checklist 交互:
#   输入数字 N  → 切换 N 的状态(选中↔未选)
#   输入 0      → 确认,进入安装
#   输入 q      → 退出
#
# 退出码:
#   0   全部成功
#   1   参数错误 / 用户 q 退出
#   2   check.sh 不通过
#   3   某个模块 install.sh 失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][warn] %s\n' "$*" >&2; }
err()  { printf '[install][err]  %s\n' "$*" >&2; }

usage() {
    sed -n '2,30p' "$0"
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

# ---- init-db ----
do_init_db() {
    local db_install="$SCRIPT_DIR/modules/main/db/install.sh"
    if [ ! -x "$db_install" ]; then
        err "modules/main/db/install.sh 缺失"
        return 3
    fi
    log "init-db (via modules/main/db/install.sh --path)"
    bash "$db_install" --path || return 3
}

# ---- 收集所有可安装模块(按字母序,分 main / public / private) ----
# 输出:三段关联数组 AREA[N] / NAME[N] / DESC[N] / N 顺序
discover_modules() {
    local -a order=()
    for area in main public private; do
        local adir="$SCRIPT_DIR/modules/$area"
        [ -d "$adir" ] || continue
        local -a names=()
        for mdir in "$adir"/*/; do
            [ -d "$mdir" ] || continue
            names+=("$(basename "$mdir")")
        done
        # 字母序排序(空目录也能跑)
        local -a sort_names=()
        mapfile -t sort_names < <(printf '%s\n' "${names[@]:-}" | sort)
        for n in "${sort_names[@]}"; do
            [ -z "$n" ] && continue
            order+=("${area}/${n}")
        done
    done
    printf '%s\n' "${order[@]}"
}

# ---- checklist 交互 ----
# 输入:discover_modules 的输出(每行 area/name)
# 副作用:导出 SELECTED_M[area/name]=1 给后续 install 阶段用
# 输出:0 = 确认,1 = q 退出
ask_checklist() {
    local -a entries=()
    while IFS= read -r line; do
        [ -n "$line" ] && entries+=("$line")
    done < <(discover_modules)

    if [ "${#entries[@]}" -eq 0 ]; then
        err "无任何可安装模块;modules/ 目录为空"
        return 1
    fi

    # 段首分隔符宽度(对齐最长 name + area 前缀)
    local sep_width=40
    local last_area=""

    # 默认勾选规则:main/* 除 daily 外默认勾上(确保 ./install.sh 默认装核心);
    # daily 仍默认不勾(避免默默注册 cron);
    # public/* / private/* 默认不勾(用户主动选)。
    declare -A SELECTED
    for e in "${entries[@]}"; do
        case "$e" in
            main/*) [ "$e" != "main/daily" ] && SELECTED["$e"]=1 || SELECTED["$e"]=0 ;;
            *)      SELECTED["$e"]=0 ;;
        esac
    done

    # 模块描述(可读性增强)
    desc_for() {
        case "$1" in
            main/daily)   echo "日度 cron 03:00 + dashboard(默认不勾,免登 crontab)" ;;
            main/db)      echo "DB schema + 末尾建空 DB" ;;
            main/lib)     echo "共享 Python 工具库(load_config 等)" ;;
            main/manage)  echo "业务管理(register target / set_config)" ;;
            main/pdtm)    echo "主动测绘核心 dnsx+httpx+naabu+cdnmatch" ;;
            public/db_align) echo "ENScan_GO 集成(Apache-2.0,大依赖)" ;;
            public/ymicp) echo "小程序备案反查客户端(需自部署服务)" ;;
            *)            echo "" ;;
        esac
    }

    while true; do
        echo
        printf 'srcradar installer — 勾选要安装的模块\n'
        printf '回车切换状态;数字 0 确认开始安装;q 退出\n'
        echo

        local n=0
        for e in "${entries[@]}"; do
            n=$((n+1))
            local area="${e%%/*}"
            local name="${e##*/}"
            # 段头(只在换 area 时打一次)
            if [ "$area" != "$last_area" ]; then
                printf '\n%s ' "$area"
                printf -- '-%.0s' $(seq 1 "$sep_width")
                echo
                last_area="$area"
            fi
            local mark="○"
            [ "${SELECTED[$e]:-0}" = "1" ] && mark="●"
            local d; d=$(desc_for "$e")
            printf '[%s] %2d. %-12s — %s\n' "$mark" "$n" "$name" "$d"
        done

        echo
        printf 'Choice [1-%d 切换 / 0 确认 / q 退出,默认 0 退出]: ' "${#entries[@]}"
        local reply
        read -r reply
        reply="${reply:-q}"
        case "$reply" in
            0|"")
                # 0 或空 = 确认开始安装
                break
                ;;
            q|Q)
                log "用户退出"
                return 1
                ;;
            *)
                # 数字 → 切换对应条目状态
                if [[ "$reply" =~ ^[0-9]+$ ]] && [ "$reply" -ge 1 ] && [ "$reply" -le "${#entries[@]}" ]; then
                    local idx=$((reply-1))
                    local target="${entries[$idx]}"
                    if [ "${SELECTED[$target]}" = "1" ]; then
                        SELECTED["$target"]=0
                    else
                        SELECTED["$target"]=1
                    fi
                else
                    warn "无效输入: '$reply'(期望 1-${#entries[@]} / 0 / q)"
                fi
                ;;
        esac
    done

    # 把选中的条目 export 给 main 用
    for e in "${entries[@]}"; do
        [ "${SELECTED[$e]}" = "1" ] && SELECTED_M["$e"]=1
    done
    declare -p SELECTED_M >/dev/null || true
    export SELECTED_M_PRESENT=1
    return 0
}

main() {
    MODE="interactive"
    DO_INIT_DB=1
    SKIP_CHECK=0
    # shellcheck disable=SC2034  # OFFLINE is accepted as a no-op flag for compatibility
    while [ $# -gt 0 ]; do
        case "$1" in
            --check)        MODE="check-only"; shift ;;
            --skip-check)    SKIP_CHECK=1; shift ;;
            --init-db)      DO_INIT_DB=1; shift ;;
            --no-init-db)   DO_INIT_DB=0; shift ;;
            --offline)      OFFLINE=1; shift ;;
            -h|--help)      usage; exit 0 ;;
            *) err "unknown arg: $1"; usage; exit 1 ;;
        esac
    done

    if [ "$MODE" = "check-only" ]; then
        run_check || { err "check 阶段失败,退出"; exit 2; }
        log "check-only 模式完成;未进入 checklist"
        exit 0
    fi

    if [ "${SKIP_CHECK:-0}" != "1" ]; then
        run_check || { err "check 阶段失败,退出"; exit 2; }
    else
        warn "跳过 check.sh(--skip-check)"
    fi

    declare -A SELECTED_M=()
    export SELECTED_M
    if ! ask_checklist; then
        exit 1
    fi

    # 按 main → public → private 顺序执行选中的模块
    local -a areas=(main public private)
    local installed=0
    for area in "${areas[@]}"; do
        local adir="$SCRIPT_DIR/modules/$area"
        [ -d "$adir" ] || continue
        for mdir in "$adir"/*/; do
            [ -d "$mdir" ] || continue
            local name; name="$(basename "$mdir")"
            local key="$area/$name"
            if [ "${SELECTED_M[$key]:-0}" = "1" ]; then
                # db 模块特殊:check + 后续 --init-db 触发;其余 --yes
                if [ "$name" = "db" ]; then
                    run_module "$area" "$name" --check || exit 3
                else
                    run_module "$area" "$name" --yes || exit 3
                fi
                installed=$((installed+1))
            fi
        done
    done

    if [ "$installed" -eq 0 ]; then
        warn "未勾选任何模块;无操作"
    fi

    if [ "$DO_INIT_DB" = "1" ]; then
        do_init_db || exit $?
    fi

    log "全部装完($installed 模块)。PATH 提示: export PATH=\"\$PATH:\$HOME/go/bin:\$HOME/.pdtm/go/bin\""
    if [ "${SELECTED_M[main/daily]:-0}" != "1" ]; then
        warn "daily 模块未安装(cron 未注册)。需要监控请手动: ./srcradar daily install_cron"
    fi
    return 0
}

main "$@"