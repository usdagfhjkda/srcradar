#!/bin/bash
# ==============================================================================
# 脚本名称: RedTeam_Asset_Scanner_v10.sh
# 脚本描述: 工业级红队资产自动化测绘流水线
#
# 更新内容 (v10):
#   1. 阶段2 CDN研判去掉IP扩散（防止云IP共享场景下误过滤真实业务）
#   2. 阶段4 naabu被动扫描排除LB域名（LB已在阶段3.5单独处理）
#   3. 阶段5 httpx补充 -rate-limit 50
#   4. 阶段6 文件引用统一改为 $FILE_IP
#   5. dnsx补充 -t 50 限制并发防DNS封禁
#   ────── 2026-08-01 阶段 1+2 改造 ──────
#   6. 阶段 1 dnsx 加 -cname -j 产 JSONL
#   7. 阶段 2 用 ./bin/cdnmatch 离线接管原 cdncheck 阻塞调用
#      (彻底解决 2026-07-31 7h45m 挂死)
#   8. RESOLVERS 集中声明为 CSV,统一代替 `-r resolvers` 文件路径
# ==============================================================================

# pdtm 工具路径硬编码(由 pdtm 自管;Ubuntu .bashrc 头部 case $- 早 return,
#  source ~/.bashrc 进不去后面的 export,直接硬编码最可靠)
export PATH="$PATH:$HOME/.pdtm/go/bin"

set -e

# ==============================================================================
# 统一解析器 — 见 README §六 §八-10 §八-11
#
# 单一事实源 = `./resolvers`(文件)。本脚本调用的 -r 工具:
#   dnsx(阶段1)/ naabu(阶段4.5) 两形都接受,统一用 CSV 形式(file
#   形式也安全,CSV 排首位)。
#
# subfinder 走 file 形式,只在本仓库 scan.sh / pipeline.sh 调用,
# 这两个脚本里有独立的 RESOLVERS_FILE / RESOLVERS_CSV 派生。
#
# 陷阱:
#   - cdncheck 的 -r 把文件名当主机名 → 0 输出。本脚本不再调 cdncheck 二进制
#     (改走 ./bin/cdnmatch),仍保留 CSV 是为了将来 cdncheck 兜底不会撞坑。
#   - subfinder 反向:接受 file 但 CSV 静默失 0 命中。
#   - resolvers 文件按延迟高低排序(快 -> 慢),见仓库 pdtm/resolvers 文件本身。
# ==============================================================================
RESOLVERS_FILE="${RESOLVERS_FILE:-resolvers}"
# 从文件派生出 CSV(改 resolvers 文件,这里的 CSV 自动跟随)
RESOLVERS="$(tr '\n' ',' < "$RESOLVERS_FILE" | sed 's/,$//')"
[ -z "$RESOLVERS" ] && { echo "[-] 错误: $RESOLVERS_FILE 文件为空或读不出" >&2; exit 1; }

OUTPUT_DIR="./scan_results"
DNSX_INPUT_RAW="./dnsx_output.txt"

COMMON_WEB_PORTS="80,443,8000,8080,8081,8443,8888,9000,9090"

mkdir -p "$OUTPUT_DIR"

log() {
    echo -e "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

# ==============================================================================
# 参数解析
#   默认:    仅 Web 资产发现（dnsx + cdncheck + httpx），跳过 TCP 端口扫描
#   -all:    启用 TCP 端口信息收集（naabu 被动 + 主动）
# ==============================================================================
ENABLE_TCP=false

usage() {
    cat <<EOF
用法: $0 [-all]
  (默认)   仅做 Web 资产发现，跳过 TCP 端口扫描
  -all     启用 TCP 端口信息收集（naabu 被动 + 主动）
  -h       显示本帮助
EOF
}

for arg in "$@"; do
    case "$arg" in
        -all)      ENABLE_TCP=true ;;
        -h|--help) usage; exit 0 ;;
        *)         usage; log "[-] 未知参数: $arg"; exit 1 ;;
    esac
done

# TCP 统计占位变量（默认关闭时为 0）
NAABU_COUNT=0
NAABU_ACTIVE_COUNT=0
ACTIVE_MAPPED_COUNT=0

# ==============================================================================
# 检查环境依赖
# ==============================================================================
log "[*] 检查环境依赖..."

for cmd in dnsx naabu httpx cdncheck awk sed grep sort tr python3; do

    if ! command -v "$cmd" >/dev/null 2>&1; then
        log "[-] 错误: 缺少依赖 [ $cmd ]"
        exit 1
    fi
done

log "[+] 依赖检查通过"

# ==============================================================================
# 检测 naabu SYN 扫描权限
# 优先级: setcap cap_net_raw > root > 降级 CONNECT
# ==============================================================================
log "[*] 检测 naabu 扫描模式权限..."

NAABU_BIN=$(which naabu)

if getcap "$NAABU_BIN" 2>/dev/null | grep -q cap_net_raw; then
    SCAN_MODE="-s s"
    log "[+] 检测到 cap_net_raw 权限，使用 SYN 扫描模式"
elif [ "$(id -u)" -eq 0 ]; then
    SCAN_MODE="-s s"
    log "[+] 当前为 root 用户，使用 SYN 扫描模式"
else
    SCAN_MODE="-s c"
    log "[!] 无 raw socket 权限，降级为 CONNECT 扫描模式"
    log "[!] 如需 SYN 扫描，执行: sudo setcap cap_net_raw+ep \$(which naabu)"
fi

log "[+] ======================================================================"
log "[+] 红队资产自动化测绘流水线 v10 启动"
log "[+] ======================================================================"

# ==============================================================================
# 输入检查
# ==============================================================================
if [ ! -s "$DNSX_INPUT_RAW" ]; then
    log "[-] 错误: 输入文件不存在或为空"
    log "[-] 文件路径: $DNSX_INPUT_RAW"
    exit 1
fi

# ==============================================================================
# 阶段 0: 数据清洗
# ==============================================================================
log "[*] 阶段 0: 清洗输入数据..."

awk '{print $1}' "$DNSX_INPUT_RAW" \
| tr -d '\r' \
| sed -e 's|https\?://||g' -e 's|/.*||g' \
| grep -E '^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$' \
| sort -u \
> "$OUTPUT_DIR/pure_domains.txt"

DOMAIN_COUNT=$(grep -c . "$OUTPUT_DIR/pure_domains.txt" || true)

log "[+] 清洗完成，共 $DOMAIN_COUNT 个有效域名"

if [ "$DOMAIN_COUNT" -eq 0 ]; then
    log "[-] 错误: 清洗后域名为空"
    exit 1
fi

# ==============================================================================
# 阶段 1 + 阶段 2: DNS 解析 + CDN/WAF 离线研判
#
# 改造 (2026-08-01):
#   - dnsx 加 -cname -j:产出 JSONL(给 cdnmatch 消费),并把 CNAME 一起解析
#   - cdnmatch (./bin/cdnmatch) 离线接管原阶段 1+2:
#       * FILE_IP/FILE_IPV6/FILE_CNAME/FILE_NS/all_unique_ips.txt
#       * cdn_{ips,domains}.txt / waf_{ips,domains}.txt / cloud_{ips,domains}.txt
#       * non_cdn_list.txt / non_cdn_ips.txt
#       * 含 host<->IP 交叉映射 + NXDOMAIN 防丢的 non_cdn 兜底
#   - 取代 2026-07-31 事故链路:`cdncheck -i <domains>` 挂在 stdin,
#     7h45m 0 进展;且无 -timeout/-rate-limit,~0.9s/host 不可调。
#
# 下游契约保持:FILE_IP 仍是 `<ip> <domain>`,所以阶段 3-9 不变。
# ==============================================================================
log "[*] 阶段 1: DNS 解析 (jsonl, 含 CNAME) ..."

# dnsx 路径:硬编码默认 ~/.pdtm/go/bin/dnsx,可用环境变量 DNSX_BIN 覆盖
DNSX_BIN="${DNSX_BIN:-$HOME/.pdtm/go/bin/dnsx}"
[ -x "$DNSX_BIN" ] || { log "[-] 缺少 dnsx: $DNSX_BIN"; exit 1; }

DNSX_RAW_FILE="$OUTPUT_DIR/dnsx_raw_output.txt"
FILE_IP="$OUTPUT_DIR/tmp_domain_ip_pairs.txt"
FILE_IPV6="$OUTPUT_DIR/tmp_domain_ipv6_pairs.txt"
FILE_CNAME="$OUTPUT_DIR/tmp_domain_cname_pairs.txt"
FILE_NS="$OUTPUT_DIR/tmp_domain_ns_pairs.txt"
CDNMATCH_STATS="$OUTPUT_DIR/cdnmatch_stats.json"

"$DNSX_BIN" -l "$OUTPUT_DIR/pure_domains.txt" \
     -cname -a -resp \
     -t 50 \
     -rl 100 \
     -r "$RESOLVERS" \
     -j \
     -no-color \
     < /dev/null \
     -o "$DNSX_RAW_FILE" \
     || log "[!] dnsx 退出非零 — JSONL 部分输出仍可继续"

DNSX_OUTPUT_COUNT=$(wc -l < "$DNSX_RAW_FILE" | tr -d ' ')
log "[+] dnsx JSONL: $DNSX_OUTPUT_COUNT 行"
# ==============================================================================
# 阶段 2: CDN/WAF 研判 (离线 — ./bin/cdnmatch)
# 取代原 `cdncheck -i <hosts>` 阻塞调用。cdnmatch 直接 import cdncheck 的
# sources_data.json 表,对 dnsx JSONL 做内存查表,无网络开销。
# ==============================================================================
log "[*] 阶段 2: cdnmatch 离线研判 CDN/WAF/Cloud ..."

CDN_DOMAIN_FILE="$OUTPUT_DIR/cdn_domains.txt"
CDN_IP_FILE="$OUTPUT_DIR/cdn_ips.txt"
WAF_DOMAIN_FILE="$OUTPUT_DIR/waf_domains.txt"
WAF_IP_FILE="$OUTPUT_DIR/waf_ips.txt"
NON_CDN_DOMAIN_FILE="$OUTPUT_DIR/non_cdn_list.txt"
NON_CDN_IP_FILE="$OUTPUT_DIR/non_cdn_ips.txt"

if [ ! -x "./bin/cdnmatch" ]; then
    log "[-] 缺少 ./bin/cdnmatch, 请 cd cdnmatch && make build"
    exit 1
fi
./bin/cdnmatch \
    -in "$DNSX_RAW_FILE" \
    -domains "$OUTPUT_DIR/pure_domains.txt" \
    -out "$OUTPUT_DIR" \
    -stats "$CDNMATCH_STATS" \
    || { log "[-] cdnmatch 失败,中止 stage 1+2"; exit 1; }

AWK_EXTRACT_COUNT=$(cat "$FILE_IP" "$FILE_IPV6" 2>/dev/null | wc -l || echo 0)
UNIQUE_IP_COUNT=$(wc -l < "$OUTPUT_DIR/all_unique_ips.txt" | tr -d ' ')
CDN_IP_COUNT=$(cat "$CDN_IP_FILE" "$WAF_IP_FILE" 2>/dev/null | wc -l | tr -d ' ')
CDN_DOMAIN_COUNT=$(cat "$CDN_DOMAIN_FILE" "$WAF_DOMAIN_FILE" 2>/dev/null | wc -l | tr -d ' ')
NON_CDN_IP_COUNT=$(wc -l < "$NON_CDN_IP_FILE" | tr -d ' ')
NON_CDN_DOMAIN_COUNT=$(wc -l < "$NON_CDN_DOMAIN_FILE" | tr -d ' ')
# 兼容旧报告的字段名 (旧实现里 cdncheck 命中 IP 和 CDN/WAF 总数同义)
CDNCHECK_HIT_COUNT=$CDN_IP_COUNT
CNAME_NS_CDN_COUNT=$(wc -l < "$FILE_CNAME" | tr -d ' ')

log "[+] DNS解析完成,唯一IP: $UNIQUE_IP_COUNT 个"
log "[+] 研判结束: CDN/WAF IP: $CDN_IP_COUNT, CDN/WAF 域名: $CDN_DOMAIN_COUNT"
log "[+] 扫描目标: 非CDN IP: $NON_CDN_IP_COUNT, 非CDN 域名: $NON_CDN_DOMAIN_COUNT"

# 阶段 3: IP权重过滤（LB过滤）
# ==============================================================================
log "[*] 阶段 3: 过滤大型负载均衡..."

> "$OUTPUT_DIR/pure_ips_to_scan.txt"
> "$OUTPUT_DIR/lb_ips.txt"

FILTERED_LB=0

while read -r ip; do
    [ -z "$ip" ] && continue

    DOMAIN_BIND_COUNT=$(grep -c "^$ip " "$FILE_IP" || echo 0)

    if [ "$DOMAIN_BIND_COUNT" -gt 15 ]; then
        log "[!] 跳过LB核心IP: $ip (绑定 $DOMAIN_BIND_COUNT 个域名)"
        echo "$ip" >> "$OUTPUT_DIR/lb_ips.txt"
        FILTERED_LB=$((FILTERED_LB + 1))
    else
        echo "$ip" >> "$OUTPUT_DIR/pure_ips_to_scan.txt"
    fi

done < "$OUTPUT_DIR/non_cdn_ips.txt"

SCAN_IP_COUNT=$(wc -l < "$OUTPUT_DIR/pure_ips_to_scan.txt" | tr -d ' ')
LB_IP_COUNT=$(wc -l < "$OUTPUT_DIR/lb_ips.txt" | tr -d ' ')

log "[+] 过滤 $FILTERED_LB 个LB IP"
log "[+] 剩余 $SCAN_IP_COUNT 个IP进入扫描"

# ==============================================================================
# 阶段 3.5: CDN IP + LB核心IP 合并 → 带Host头 httpx 80/443 探测
# ==============================================================================
log "[*] 阶段 3.5: CDN+LB IP 合并带Host头探测..."

# 阶段 3.5/5/8 都用,放 stage 3.5 入口处一次性检查+赋值
# httpx 路径:硬编码默认 ~/.pdtm/go/bin/httpx,可用环境变量 HTTPX_BIN 覆盖
HTTPX_BIN="${HTTPX_BIN:-$HOME/.pdtm/go/bin/httpx}"
[ -x "$HTTPX_BIN" ] || { log "[-] 缺少 httpx: $HTTPX_BIN"; exit 1; }

cat "$OUTPUT_DIR/cdn_ips.txt" \
    "$OUTPUT_DIR/waf_ips.txt" \
    "$OUTPUT_DIR/cloud_ips.txt" \
    "$OUTPUT_DIR/lb_ips.txt" \
    2>/dev/null \
| sort -u \
> "$OUTPUT_DIR/cdn_lb_combined_ips.txt"

CDN_LB_TOTAL=$(wc -l < "$OUTPUT_DIR/cdn_lb_combined_ips.txt" | tr -d ' ')
log "[+] CDN+LB合并IP总数: $CDN_LB_TOTAL 个"

> "$OUTPUT_DIR/cdn_lb_domain_ports.txt"
CDN_LB_WEB_COUNT=0

if [ "$CDN_LB_TOTAL" -gt 0 ]; then

    while read -r ip; do
        [ -z "$ip" ] && continue
        grep "^$ip " "$FILE_IP" \
        | awk '{print $2":80"; print $2":443"}'
    done < "$OUTPUT_DIR/cdn_lb_combined_ips.txt" \
    | sort -u \
    > "$OUTPUT_DIR/cdn_lb_domain_ports.txt"

    CDN_LB_DOMAIN_PORT_COUNT=$(wc -l < "$OUTPUT_DIR/cdn_lb_domain_ports.txt" | tr -d ' ')
    log "[+] CDN+LB 域名:端口组合数: $CDN_LB_DOMAIN_PORT_COUNT"

    if [ "$CDN_LB_DOMAIN_PORT_COUNT" -gt 0 ]; then
        # httpx -l "$OUTPUT_DIR/cdn_lb_domain_ports.txt" \
        #       -sc \                         # [状态] 显示状态码 (200, 404, etc.)
        #       -cl \                         # [长度] 显示响应长度，用于区分相同状态码下的不同页面
        #       -title \                      # [标题] 显示网页标题，直观判断业务功能
        #       -td \                         # [指纹] 开启 Wappalyzer 技术识别，探测 Spring Boot, Vue, Nginx 等
        #   #  -favicon \                    # [图标] 探测并计算 favicon 的 mmh3 hash，用于 UI 聚类识别
        #       -hash \                # [去重] 计算页面 Body 的哈希值，识别重复的模板页面
        #   #  -cdn \                        # [CDN] 识别是否使用了 CDN 或云端 WAF
        #       -random-agent \               # [UA] 随机 User-Agent,httpx 标准选项
        #       -follow-redirects \           # [跳转] 跟随重定向，看到跳转后的真实业务页面
        #       -max-redirects 3 \            # [限制] 限制最大跳转次数为 3，防止进入无限跳转循环浪费流量
        #       -fd \ 
        #       -rate-limit 50 \
        #       -timeout 5 \
        #       -silent \
        #       -no-color \
        #       2>/dev/null \
        # > "$OUTPUT_DIR/cdn_lb_web_summary.txt"
        # FIX-2026-08-25: 加 -json 让 httpx 输出 JSONL,且改后缀到 .json。
        # 之前 .txt 输出走 import_scan_results 的 parse_text_web,其中 URL_RE 非贪婪
        # 正则会把 http://host.tld 切成 http://<首字符>,导致 web_subdomains.subdomain
        # 被截断成 1 个字符。(DB business_id=1~7, first_seen=2026-07-26~08-21 共 352 条)
        "$HTTPX_BIN" -l "$OUTPUT_DIR/cdn_lb_domain_ports.txt" -sc -cl -title -td -hash -random-agent -follow-redirects -max-redirects 3  -rate-limit 50 -timeout 5 -silent -no-color -json < /dev/null 2>/dev/null > "$OUTPUT_DIR/cdn_lb_web_summary.json"

        CDN_LB_WEB_COUNT=$(wc -l < "$OUTPUT_DIR/cdn_lb_web_summary.json" | tr -d ' ')
        log "[+] CDN+LB Web存活数: $CDN_LB_WEB_COUNT"

    else
        log "[!] 无域名映射，跳过探测"
        > "$OUTPUT_DIR/cdn_lb_web_summary.json"
    fi

else
    log "[!] 无CDN/LB IP，跳过探测"
    > "$OUTPUT_DIR/cdn_lb_web_summary.json"
fi

# FIX-2026-08-25: 上游已改为 -json 输出 JSONL,这里必须用 Python 解析,
# 否则 awk $1 会读到 "{",输出错乱。提取每行 JSONL 的 host:port 即可
# (此处输入是 cdn_lb_domain_ports.txt,即 domain:port,host 是域名/IP,port 是 80/443)。
# 空文件 / 上游 0 命中:直接空写,sort -u 阶段会跳过。
python3 - "$OUTPUT_DIR/cdn_lb_web_summary.json" <<'PYEOF' \
    | sort -u > "$OUTPUT_DIR/cdn_lb_mapped_ports.txt"
import json, sys
path = sys.argv[1]
try:
    fh = open(path, encoding="utf-8", errors="replace")
except OSError:
    sys.exit(0)
with fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        host = d.get("host") or ""
        port = d.get("port")
        if host and port:
            print(f"{host}:{port}")
PYEOF

# ==============================================================================
# 阶段 4: naabu 被动发现
# 修复: 排除LB域名（LB已在阶段3.5单独处理，避免重复）
# ==============================================================================
# ==============================================================================
# 阶段 4 - 4.5: TCP 端口信息收集（默认关闭，传 -all 启用）
# ==============================================================================
if [ "$ENABLE_TCP" = true ]; then

    log "[*] 阶段 4: naabu 被动资产发现 (直接扫 IP,含 LB、排除 CDN)..."

# 被动扫描不发包(查公开源),直接喂非CDN IP(含LB核心IP)。
# 输出原生即 IP:端口,与主动扫描口径统一,无需 IP→域名 反查映射。
# CDN IP 不纳入:其端口属于CDN厂商共享设施,入库会造成错误归属。
naabu -l "$OUTPUT_DIR/non_cdn_ips.txt" \
      -passive \
      -timeout 5 \
      -silent \
      -no-color \
      2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$' \
| sort -u \
> "$OUTPUT_DIR/raw_naabu_passive.txt"

NAABU_COUNT=$(grep -c . "$OUTPUT_DIR/raw_naabu_passive.txt" || true)

log "[+] naabu 被动发现 $NAABU_COUNT 个端口"

# ==============================================================================
# 阶段 4.5: naabu 主动扫描
# ==============================================================================
log "[*] 阶段 4.5: naabu 主动端口扫描 (模式: $SCAN_MODE, rate: 50)..."

NAABU_ACTIVE_FILE="$OUTPUT_DIR/raw_naabu_active.txt"

naabu -l "$OUTPUT_DIR/pure_ips_to_scan.txt" \
     -top-ports 100 \
     $SCAN_MODE \
     -rate 50 \
     -c 25 \
     -timeout 2 \
     -retries 2 \
     -silent \
     -no-color \
     -r "$RESOLVERS" \
     2>/dev/null \
| sed 's|https\?://||g' \
| sort -u \
> "$NAABU_ACTIVE_FILE"
NAABU_ACTIVE_COUNT=$(grep -c . "$NAABU_ACTIVE_FILE" || true)

log "[+] naabu 主动扫描发现 $NAABU_ACTIVE_COUNT 个端口"

# ------------------------------------------------------------------------------
# 主动结果 IP:Port → 域名:Port 反查映射
# 无域名映射时保留裸 IP:Port
# ------------------------------------------------------------------------------
log "[*] 阶段 4.5: 主动扫描结果 IP→域名 反查映射..."

> "$OUTPUT_DIR/raw_naabu_active_mapped.txt"

while read -r line; do

    [ -z "$line" ] && continue

    clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g' | tr -d '\r')
    ip=$(echo "$clean" | cut -d':' -f1)
    port=$(echo "$clean" | cut -d':' -f2)

    if ! echo "$ip" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        continue
    fi

    mapped=$(grep "^$ip " "$FILE_IP" \
             | awk -v p="$port" '{print $2":"p}')

    if [ -n "$mapped" ]; then
        echo "$mapped" >> "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
    else
        echo "$ip:$port" >> "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
    fi

done < "$NAABU_ACTIVE_FILE"

sort -u "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
     -o "$OUTPUT_DIR/raw_naabu_active_mapped.txt"

ACTIVE_MAPPED_COUNT=$(grep -c . "$OUTPUT_DIR/raw_naabu_active_mapped.txt" || true)

log "[+] 主动扫描映射完成，共 $ACTIVE_MAPPED_COUNT 条记录"

else
    log "[!] TCP 端口信息收集默认关闭，跳过 naabu 被动 + 主动扫描（传 -all 启用）"
    : > "$OUTPUT_DIR/raw_naabu_passive.txt"
    : > "$OUTPUT_DIR/raw_naabu_active.txt"
    : > "$OUTPUT_DIR/raw_naabu_active_mapped.txt"
fi

# ==============================================================================
# 阶段 5: httpx 主动探测
# 仅保留端口/rate-limit/timeout, 去掉 -sc -cl -title -hash mmh3
# 这 4 个 flag 会让输出变 `URL [200] [2] [hash]`, 含空格, 破坏阶段 6 mapper 的 cut -d':'
# 详见 README 附录 C。
# fingerprint 由阶段 8 的 `-j -title -td -hash` 重做, 最终入库口径不变.
# ==============================================================================
log "[*] 阶段 5: httpx 主动探测..."

# 构造 domain:port 输入（带 Host 头，避免共享 IP 默认页 404）
# 每个 non-CDN IP 取所有绑定域名 × 9 端口 = Host 自动对 + 端口全覆盖
> "$OUTPUT_DIR/non_cdn_domain_ports.txt"
while read -r ip; do
    [ -z "$ip" ] && continue
    grep "^$ip " "$FILE_IP" \
    | awk -v ports="$COMMON_WEB_PORTS" '
        BEGIN { n = split(ports, a, ","); for (i = 1; i <= n; i++) p[i] = a[i] }
        { for (i in p) print $2 ":" p[i] }' \
    >> "$OUTPUT_DIR/non_cdn_domain_ports.txt"
done < "$OUTPUT_DIR/pure_ips_to_scan.txt"
sort -u "$OUTPUT_DIR/non_cdn_domain_ports.txt" -o "$OUTPUT_DIR/non_cdn_domain_ports.txt"

DOMAIN_PORT_COUNT=$(wc -l < "$OUTPUT_DIR/non_cdn_domain_ports.txt" | tr -d ' ')
log "[+] domain:port 组合数: $DOMAIN_PORT_COUNT"

# 注: 不传 -p — 输入已经带端口,-p 会再次展开覆盖 URL 端口(runner.go UpdatePort)
# 加 < /dev/null:httpx 行为对 stdin 有依赖(即使 -l 已指定输入文件),
# pipeline.sh 调用时 stdin 可能没指向 tty。带 < /dev/null 强制 stdin EOF。
"$HTTPX_BIN" -l "$OUTPUT_DIR/non_cdn_domain_ports.txt" \
      -rate-limit 50 \
      -timeout 5 \
      -no-color -silent \
      < /dev/null \
      -o "$OUTPUT_DIR/raw_httpx_active_ips.txt"

ACTIVE_COUNT=$(wc -l < "$OUTPUT_DIR/raw_httpx_active_ips.txt" | tr -d ' ')

log "[+] 主动探测发现 $ACTIVE_COUNT 个存活端口"

# ==============================================================================
# 阶段 6: Host 虚拟主机映射
# 修复: 文件引用统一改为 $FILE_IP
#
# 注意: 本 mapper 依赖阶段 5 httpx 的输出格式 = `http(s)://IP` 或 `http(s)://IP:PORT`
#       (无 bracketed fingerprint 字段). 如阶段 5 恢复 -sc/-cl/-title/-hash mmh3,
#       输出会变 `URL [200] [2] [hash]` 含空格, 整行被吃成 $ip, 命中被静默丢.
#       详见 README 附录 C. 想动阶段 5/6 任一处请先看附录 C 的"待办 / 未来改进".
# ==============================================================================
log "[*] 阶段 6: URL 清洗（输入已是 domain:port,无需 IP 反查）..."

> "$OUTPUT_DIR/raw_mapped_ports.txt"

while read -r line; do

    [ -z "$line" ] && continue

    clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g')

    if [[ "$clean" == *":"* ]]; then
        echo "$clean" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
    else
        # URL 无端口:按 scheme 补默认端口
        if [[ "$line" == https://* ]]; then
            echo "${clean}:443" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
        else
            echo "${clean}:80" >> "$OUTPUT_DIR/raw_mapped_ports.txt"
        fi
    fi

done < "$OUTPUT_DIR/raw_httpx_active_ips.txt"

sort -u "$OUTPUT_DIR/raw_mapped_ports.txt" \
     -o "$OUTPUT_DIR/raw_mapped_ports.txt"

# ==============================================================================
# 阶段 7: 数据融合（被动 + 主动 + httpx映射 + CDN/LB）
# ==============================================================================
log "[*] 阶段 7: 数据融合..."

cat "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
    "$OUTPUT_DIR/raw_mapped_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_mapped_ports.txt" \
    2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^.+:[0-9]+$' \
| sort -u \
> "$OUTPUT_DIR/non_cdn_all_ports.txt"

ALL_COUNT=$(grep -c . "$OUTPUT_DIR/non_cdn_all_ports.txt" || true)

log "[+] 融合后共 $ALL_COUNT 个端口（被动+主动+httpx+CDN/LB）"

# ==============================================================================
# 阶段 8: Web存活确认
# ==============================================================================
log "[*] 阶段 8: Web存活确认..."

# httpx -l "$OUTPUT_DIR/non_cdn_all_ports.txt" \
#       -sc \                         # [状态] 显示状态码 (200, 404, etc.)
#       -cl \                         # [长度] 显示响应长度，用于区分相同状态码下的不同页面
#       -title \                      # [标题] 显示网页标题，直观判断业务功能
#       -td \                         # [指纹] 开启 Wappalyzer 技术识别，探测 Spring Boot, Vue, Nginx 等
# #  -favicon \                    # [图标] 探测并计算 favicon 的 mmh3 hash，用于 UI 聚类识别
#       -hash \                # [去重] 计算页面 Body 的哈希值，识别重复的模板页面
# #  -cdn \                        # [CDN] 识别是否使用了 CDN 或云端 WAF
#       -random-agent \               # [UA] 随机 User-Agent,httpx 标准选项
#       -follow-redirects \           # [跳转] 跟随重定向，看到跳转后的真实业务页面
#       -max-redirects 3 \            # [限制] 限制最大跳转次数为 3，防止进入无限跳转循环浪费流量
#       -fd \ 
#       -rate-limit 50 \
#       -timeout 5 \
#       -silent \
#       -no-color \
#       2>/dev/null \
# > "$OUTPUT_DIR/non_cdn_web_summary.txt"
"$HTTPX_BIN" -l "$OUTPUT_DIR/non_cdn_all_ports.txt" -sc -cl -title -hash mmh3 -random-agent -follow-redirects -max-redirects 3  -td -rate-limit 50 -timeout 5 -silent -no-color -json < /dev/null 2>/dev/null > "$OUTPUT_DIR/non_cdn_web_summary.json"
WEB_COUNT=$(grep -c . "$OUTPUT_DIR/non_cdn_web_summary.json" || true)

log "[+] Web资产数: $WEB_COUNT"

# ==============================================================================
# 阶段 9: 终极格式对齐并分离 TCP 端口
# ==============================================================================
log "[*] 正在标准化格式并分离 TCP 端口..."

# web 存活的 IP:端口 —— 从 httpx json 的 host(解析后IP)+ port 字段提取。
# 无 jq,用 python3 解析 JSONL。
python3 - "$OUTPUT_DIR/non_cdn_web_summary.json" <<'PYEOF' \
    | sort -u > "$OUTPUT_DIR/tmp_web_ip_ports.txt"
import json, sys
path = sys.argv[1]
try:
    fh = open(path, encoding="utf-8", errors="replace")
except OSError:
    sys.exit(0)
with fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        # host 字段可能是域名(喂域名时)或IP(喂IP时),不可靠;
        # host_ip / a[] 才是稳定的解析后IP,用它对齐 naabu 的 IP:端口。
        ip = d.get("host_ip") or ""
        if not ip:
            a = d.get("a") or []
            if a:
                ip = a[0]
        if not ip:
            ip = d.get("host") or ""
        port = d.get("port")
        if ip and port:
            print(f"{ip}:{port}")
PYEOF

# TCP 全集 = naabu 被动 + 主动 的原始 IP:端口(两者现在都原生输出 IP:端口)
cat "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active.txt" \
    2>/dev/null \
| sed 's|https\?://||g' \
| grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$' \
| sort -u \
> "$OUTPUT_DIR/tmp_all_ip_ports.txt"

log "[!] web IP:端口列表:"
cat "$OUTPUT_DIR/tmp_web_ip_ports.txt" | while read l; do log "    $l"; done
log "[!] 全部 IP:端口列表:"
cat "$OUTPUT_DIR/tmp_all_ip_ports.txt" | while read l; do log "    $l"; done

# 非 web TCP 端口 = 全集 - web
comm -23 \
    "$OUTPUT_DIR/tmp_all_ip_ports.txt" \
    "$OUTPUT_DIR/tmp_web_ip_ports.txt" \
> "$OUTPUT_DIR/non_cdn_tcp_ports.txt"

rm -f "$OUTPUT_DIR/tmp_web_ip_ports.txt" \
      "$OUTPUT_DIR/tmp_all_ip_ports.txt"

TCP_COUNT=$(wc -l < "$OUTPUT_DIR/non_cdn_tcp_ports.txt" | tr -d ' ')

log "[+] 阶段 9 完成，纯 TCP 端口数: $TCP_COUNT"

# ==============================================================================
# 清理临时文件
# ==============================================================================
rm -f \
    "$OUTPUT_DIR/raw_naabu_passive.txt" \
    "$OUTPUT_DIR/raw_naabu_active.txt" \
    "$OUTPUT_DIR/raw_naabu_active_mapped.txt" \
    "$OUTPUT_DIR/raw_httpx_active_ips.txt" \
    "$OUTPUT_DIR/raw_mapped_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_domain_ports.txt" \
    "$OUTPUT_DIR/cdn_lb_mapped_ports.txt" \
    "$OUTPUT_DIR/remaining_ips_for_cdncheck.txt" \
    "$OUTPUT_DIR/cdncheck_cdn_ips.txt" \
    "$OUTPUT_DIR/lb_domains.txt" \
    "$OUTPUT_DIR/non_cdn_non_lb_list.txt" \
    "$OUTPUT_DIR/tmp_web_ports.txt"

# ==============================================================================
# 最终报告
# ==============================================================================
log "[+] ======================================================================"
log "[+] 流水线执行完成"
log "[+] ======================================================================"

log "[✔] 输入域名数              : $DOMAIN_COUNT"
log "[✔] dnsx输出行数            : $DNSX_OUTPUT_COUNT"
log "[✔] awk提取映射数           : $AWK_EXTRACT_COUNT"
log "[✔] 唯一IP数                : $UNIQUE_IP_COUNT"
log "[✔] CNAME/NS识别CDN域名数   : $CNAME_NS_CDN_COUNT"
log "[✔] cdncheck命中IP数        : $CDNCHECK_HIT_COUNT"
log "[✔] 最终CDN IP总数          : $CDN_IP_COUNT"
log "[✔] 最终CDN域名总数         : $CDN_DOMAIN_COUNT"
log "[✔] 非CDN IP数              : $NON_CDN_IP_COUNT"
log "[✔] 非CDN域名数             : $NON_CDN_DOMAIN_COUNT"
log "[✔] LB核心IP数              : $LB_IP_COUNT"
log "[✔] 过滤LB IP数             : $FILTERED_LB"
log "[✔] CDN+LB合并IP数          : $CDN_LB_TOTAL"
log "[✔] CDN+LB Web存活数        : $CDN_LB_WEB_COUNT"
log "[✔] 进入扫描IP数            : $SCAN_IP_COUNT"
log "[✔] naabu被动发现端口数     : $NAABU_COUNT"
log "[✔] naabu主动扫描端口数     : $NAABU_ACTIVE_COUNT"
log "[✔] 主动扫描映射记录数      : $ACTIVE_MAPPED_COUNT"
log "[✔] httpx存活端口数         : $ACTIVE_COUNT"
log "[✔] 融合端口总数            : $ALL_COUNT"
log "[✔] Web资产数               : $WEB_COUNT"
log "[✔] TCP端口数               : $TCP_COUNT"
log "[✔] naabu扫描模式           : $SCAN_MODE"

log "[+] ======================================================================"

log "[✔] Web核心报告:"
log "    $OUTPUT_DIR/non_cdn_web_summary.json"

log "[✔] CDN+LB Web报告:"
log "    $OUTPUT_DIR/cdn_lb_web_summary.txt"

log "[✔] TCP爆破列表:"
log "    $OUTPUT_DIR/non_cdn_tcp_ports.txt"

log "[✔] IP→域名映射字典:"
log "    $OUTPUT_DIR/tmp_domain_ip_pairs.txt"

log "[✔] 非CDN域名列表:"
log "    $OUTPUT_DIR/non_cdn_list.txt"

log "[✔] CDN+LB合并IP列表:"
log "    $OUTPUT_DIR/cdn_lb_combined_ips.txt"

log "[+] ======================================================================"

