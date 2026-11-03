#!/usr/bin/env bash
#
# resolve_tools.sh — 解析外部工具二进制路径(httpx / dnsx 等)
#
# 优先级 (从高到低):
#   1. 环境变量 <TOOL>_BIN       (e.g. HTTPX_BIN / DNSX_BIN)
#   2. config/pdtm.conf 的 <tool>_bin (e.g. httpx_bin / dnsx_bin)
#      — 由 caller source 的 load_config_set 注入到 caller scope
#   3. 默认路径 <default>          (caller 传,通常 $HOME/.pdtm/go/bin/<tool>)
#
# 接口:
#   source "$(dirname "$0")/../lib/resolve_tools.sh"     # 注意:相对 caller 位置
#   HTTPX_BIN=$(resolve_tool httpx "$HOME/.pdtm/go/bin/httpx")
#   DNSX_BIN=$(resolve_tool dnsx "$HOME/.pdtm/go/bin/dnsx")
#
# 返回: 打印解析后的绝对路径到 stdout (供 $(...) 捕获)
# 退出码: 总是 0;调用方负责 [ -x "$BIN" ] 检查
#
# 2026-10-03: 新建,把 pdtm 模块的 httpx/dnsx 路径从硬编码
#   + env override 升级为 "config 自动读 + env 覆盖 + 默认 fallback"。

resolve_tool() {
    local tool_name="$1"           # httpx / dnsx / ...
    local default="$2"             # 默认路径

    # 1. 环境变量 <TOOL>_BIN (HTTPX_BIN / DNSX_BIN) — 最高优先级
    local env_var="${tool_name^^}_BIN"  # 转大写,加 _BIN
    if [ -n "${!env_var:-}" ]; then
        printf '%s\n' "${!env_var}"
        return 0
    fi

    # 2. config 变量 <tool>_bin (httpx_bin / dnsx_bin) — 由 load_config_set 注入
    local cfg_var="${tool_name}_bin"
    if [ -n "${!cfg_var:-}" ]; then
        printf '%s\n' "${!cfg_var}"
        return 0
    fi

    # 3. 默认路径
    printf '%s\n' "$default"
}
