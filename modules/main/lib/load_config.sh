#!/usr/bin/env bash
#
# load_config.sh — bash 包装, 调 load_config.py 解析 config/*.conf
#
# 接口:
#   source lib/load_config.sh
#   load_config_set <config_path> <lib_dir>
#
# 行为:
#   - 仅当 caller 同名变量未设时, 用 config 里的值(让 export 环境变量优先)
#   - config 文件不存在 = 静默返回 0, 走 caller 脚本默认
#   - parse 失败 = 报错到 stderr, 返回 3, caller 决定是否 exit
#
# 用法 (在 scan.sh / scanner.sh 顶部):
#   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "$SCRIPT_DIR/../lib/load_config.sh"
#   load_config_set "$SCRIPT_DIR/../../../config/pdtm.conf" "$SCRIPT_DIR/../lib"
#
# 实现: 不用 eval(eval 在函数内只设 local scope),
#       改用 read + assign: ${key}="${value}", 函数内赋值 + caller scope
#       (bash 动态变量名, 必须用 eval 才能设到 caller scope)
#

load_config_set() {
    local cfg="$1"
    local lib_dir="$2"
    [ -f "$cfg" ] || return 0
    if [ ! -d "$lib_dir" ]; then
        echo "[load_config] lib_dir not a dir: $lib_dir" >&2
        return 3
    fi

    local parsed
    if ! parsed="$(python3 "$lib_dir/load_config.py" "$cfg" 2>/dev/null)"; then
        echo "[load_config] parse failed for $cfg (config 跳过)" >&2
        return 3
    fi
    [ -z "$parsed" ] && return 3

    # parsed 形式: dnsx_bin='/path'  (单引号包裹的 key=value)
    # 用 eval 把每个 key=value 写入 caller scope
    # - 函数内 eval 默认写 local scope
    # - 用 declare -g 让变量写到 global scope (bash 4.2+)
    local line key val
    while IFS= read -r line; do
        key="${line%%=*}"
        val="${line#*=}"
        val="${val#\'}"
        val="${val%\'}"
        if [ -z "${!key:-}" ]; then
            # 用 printf -v 把 value 写到 caller scope 的 key
            printf -v "$key" '%s' "$val"
        fi
    done <<< "$parsed"
}