#!/usr/bin/env bash
#
# check_wildcard.sh - 用 dnsx 判断 glob 域是否开启了泛解析（wildcard DNS）
#
# 原理：对含 `*` 的 glob 模式（如 `*.example.com` / `cc-*.example.com` /
#       `aaa.*.bbb.com`），把 `*` 替换为随机 label 后 DNS 探测；能解析则为泛解析。
#       对不含 `*` 的精确单子域（如 `demo.scanme.sh`），不需要 wildcard 检测
#       （它就是一个具体 host，不存在"通配"语义）。
#
# 用法：
#   ./check_wildcard.sh target.txt
#   cat target.txt | ./check_wildcard.sh
#
# 输出：
#   wildcard.txt     -> 开启了泛解析的 glob 模式
#   no_wildcard.txt  -> 没有开启泛解析 / 精确单子域（不需要检测）
#
set -euo pipefail

# ---- 配置 ----
INPUT="${1:-/dev/stdin}"
WILDCARD_OUT="wildcard.txt"
NORMAL_OUT="no_wildcard.txt"
# 解析器来源与 ./resolvers 文件保持一致(见 README §八-11)
RESOLVERS_FILE="${RESOLVERS_FILE:-resolvers}"
RESOLVERS="$(tr '\n' ',' < "$RESOLVERS_FILE" | sed 's/,$//')"
[ -z "$RESOLVERS" ] && { echo "[-] 错误: $RESOLVERS_FILE 文件为空或不可读" >&2; exit 1; }
PROBES=3

# ---- 依赖检查 ----
command -v dnsx >/dev/null 2>&1 || {
  echo "[!] 未找到 dnsx，请先安装：go install github.com/projectdiscovery/dnsx/cmd/dnsx@latest" >&2
  exit 1
}

# 清空输出文件
: > "$WILDCARD_OUT"
: > "$NORMAL_OUT"

# 生成一个随机字符串（用于替换 glob 中的 *）
rand_label() {
  head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-16
}

while IFS= read -r line || [ -n "$line" ]; do
  # 去空白、跳过空行和注释
  line="$(echo "$line" | tr -d '[:space:]')"
  [ -z "$line" ] && continue
  case "$line" in \#*) continue ;; esac

  is_wildcard="no"

  if [[ "$line" == *\** ]]; then
    # 含 * 的 glob pattern:把 * 替换为随机 label 再 dnsx 探测
    # 例: *.example.com       -> <rand>.example.com
    #     cc-*.example.com    -> cc-<rand>.example.com
    #     aaa.*.bbb.com       -> aaa.<rand>.bbb.com
    for _ in $(seq "$PROBES"); do
      probe="${line//\*/$(rand_label)}"
      result="$(echo "$probe" | dnsx -silent -a -r "$RESOLVERS" -rl 30 -t 10 2>/dev/null || true)"
      if [ -n "$result" ]; then
        is_wildcard="yes"
        break
      fi
    done
  else
    # 精确单子域:不存在"通配"语义,直接归到 no_wildcard
    is_wildcard="no"
  fi

  if [ "$is_wildcard" = "yes" ]; then
    echo "$line" >> "$WILDCARD_OUT"
    echo "[泛解析]   $line"
  else
    echo "$line" >> "$NORMAL_OUT"
    if [[ "$line" == *\** ]]; then
      echo "[非泛解析] $line"
    else
      echo "[精确单域] $line"
    fi
  fi
done < "$INPUT"

echo
echo "[+] 完成。"
echo "    泛解析 glob  -> $WILDCARD_OUT ($(wc -l < "$WILDCARD_OUT") 个)"
echo "    非泛解析/精确 -> $NORMAL_OUT ($(wc -l < "$NORMAL_OUT") 个)"
