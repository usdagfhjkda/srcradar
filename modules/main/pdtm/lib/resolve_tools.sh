#!/usr/bin/env bash
#
# resolve_tools.sh — resolve the paths of the external tool binaries (httpx / dnsx etc.)
#
# Priority (highest to lowest):
#   1. environment variable <TOOL>_BIN       (e.g. HTTPX_BIN / DNSX_BIN)
#   2. <tool>_bin in config/pdtm.conf (e.g. httpx_bin / dnsx_bin)
#      — injected into the caller scope by the caller's load_config_set
#   3. default path <default>          (passed by the caller, usually $HOME/.pdtm/go/bin/<tool>)
#
# Interface:
#   source "$(dirname "$0")/../lib/resolve_tools.sh"     # note: relative to the caller location
#   HTTPX_BIN=$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")
#   DNSX_BIN=$(resolve_tool dnsx "$HOME/.pdtm/go/bin/dnsx")
#
# Returns: prints the resolved absolute path to stdout (for $(...) to capture)
# Exit code: always 0; the caller is responsible for the [ -x "$BIN" ] check
#
# 2026-10-03: new file, upgrades the pdtm module's httpx/dnsx paths from hardcoded
#   + env override to "config auto-read + env override + default fallback".

resolve_tool() {
    local tool_name="$1"           # httpx / dnsx / ...
    local default="$2"             # default path

    # 1. environment variable <TOOL>_BIN (HTTPX_BIN / DNSX_BIN) — highest priority
    local env_var="${tool_name^^}_BIN"  # upper case, add _BIN
    if [ -n "${!env_var:-}" ]; then
        printf '%s\n' "${!env_var}"
        return 0
    fi

    # 2. config variable <tool>_bin (httpx_bin / dnsx_bin) — injected by load_config_set
    local cfg_var="${tool_name}_bin"
    if [ -n "${!cfg_var:-}" ]; then
        printf '%s\n' "${!cfg_var}"
        return 0
    fi

    # 3. default path
    printf '%s\n' "$default"
}
