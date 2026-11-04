#!/usr/bin/env bash
#
# load_config.sh — bash wrapper, calls load_config.py to parse config/*.conf
#
# Interface:
#   source lib/load_config.sh
#   load_config_set <config_path> <lib_dir>
#
# Behaviour:
#   - use the value from config only when the caller has not set a variable of the same name (exported env vars win)
#   - config file missing = return 0 silently, the caller script keeps its own defaults
#   - parse failure = report to stderr, return 3, the caller decides whether to exit
#
# Usage (at the top of scan.sh / scanner.sh):
#   SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "$SCRIPT_DIR/../lib/load_config.sh"
#   load_config_set "$SCRIPT_DIR/../../../config/pdtm.conf" "$SCRIPT_DIR/../lib"
#
# Implementation: no eval (eval inside a function only sets local scope),
#       use read + assign instead: ${key}="${value}", assigned in the function + caller scope
#       (bash dynamic variable names need eval to reach the caller scope)
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

    # parsed form: dnsx_bin='/path'  (key=value wrapped in single quotes)
    # use eval to write each key=value into the caller scope
    # - eval inside a function writes to local scope by default
    # - use declare -g to write the variable to global scope (bash 4.2+)
    local line key val
    while IFS= read -r line; do
        key="${line%%=*}"
        val="${line#*=}"
        val="${val#\'}"
        val="${val%\'}"
        if [ -z "${!key:-}" ]; then
            # use printf -v to write the value to the key in the caller scope
            printf -v "$key" '%s' "$val"
        fi
    done <<< "$parsed"
}