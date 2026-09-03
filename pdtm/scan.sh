#!/usr/bin/env bash
# subfinder -> dnsx -> alterx -> dnsx 流水线 (glob scope 版本)
#
# 2026-07-30 glob 重构:
#   - target.txt / exclude.txt 支持 `*` 通配,任意位置 (aaa.*.bbb.com / cc-*.bbb.com / *.bbb.com / ccc.bbb.com)
#   - subfinder 按 base 聚合调用 (从 glob pattern 提取最右连续非 * label 段)
#   - 三处 grep target regex + 三处 grep -v exclude regex (subfinder 后 / alterx 后 / dnsx 后)
#   - exclude 全部按 glob 编译为 ERE,删除原 keyword 子串匹配分支
#
# 备注: subfinder -d 拿字面 base;alterx 拿已知子域派生,输出仍要 grep target 守门。

# pdtm 工具路径硬编码(由 pdtm 自管;Ubuntu .bashrc 头部 case $- 早 return,
#  source ~/.bashrc 进不去后面的 export,直接硬编码最可靠)
export PATH="$PATH:$HOME/.pdtm/go/bin"

set -euo pipefail

# ===== 配置 =====
# 单一事实源:`./resolvers` 文件。subfinder 走 RESOLVERS_FILE,其它走 RESOLVERS_CSV。
# 见 README §八-11 跨工具陷阱矩阵 (cdncheck=-file 坑,subfinder=-csv 坑)。
RESOLVERS_FILE="${RESOLVERS_FILE:-resolvers}"
RESOLVERS_CSV="$(tr '\n' ',' < "$RESOLVERS_FILE" | sed 's/,$//')"
[ -z "$RESOLVERS_CSV" ] && { echo "[-] 错误: $RESOLVERS_FILE 文件为空或不可读" >&2; exit 1; }
TARGET="target.txt"
EXCLUDE="exclude.txt"
ALIVE="alive.txt"
ALTERX_OUT="alterx.txt"
DNSX_OUT="dnsx_output.txt"
DB="${DB:-db/recon.sqlite3}"
BUSINESS_ID="${BUSINESS_ID:-1}"
WORDLIST="${WORDLIST:-}"
if [ -n "$WORDLIST" ] && [ -f "$WORDLIST" ]; then
    WORDLIST_HASH=$(sha256sum "$WORDLIST" | cut -c1-12)
else
    WORDLIST_HASH="${WORDLIST_HASH:-alterx-default}"
fi

# alterx 节奏控制:全局扫描/手动覆盖。
# ALTERX_CADENCE_DAYS=30 默认 -> alterx 派生每 30 天跑一次,中间只走 subfinder+dnsx。
#                              设 0 等于"永远过期"=每跑都跑 alterx(等同关闭节奏)。
# FORCE_ALTERX=1            强制本轮跑,忽略节奏(优先于 cadence)。
ALTERX_CADENCE_DAYS="${ALTERX_CADENCE_DAYS:-30}"
FORCE_ALTERX="${FORCE_ALTERX:-}"

# 检查 python3 (target_glob.py + permutation_cache.py 需要)
command -v python3 >/dev/null 2>&1 || {
    echo "[-] 缺少 python3 (target_glob / permutation_cache 需要)" >&2
    exit 1
}
[ -f "target_glob.py" ] || {
    echo "[-] 缺少 target_glob.py (在 $(pwd) 下找不到)" >&2
    exit 1
}

# ===== 准备：清空/创建产物 =====
: > "$ALIVE"
: > "$ALTERX_OUT"
: > "$DNSX_OUT"

# ===== 0) 拆分 target.txt:精确子域(无 *) / glob 模式(有 *) =====
# target 读入后第一时间:精确子域 dnsx → DNSX_OUT,在此之前不经过任何其它流程
# (不编译 ERE、不算 HAS_GLOB、不进 subfinder/alterx/permutation/wildcard)。
# 业务意图:既然只列了精确 host,就不扩展去查兄弟子域。下游 scanner.sh 从 DNSX_OUT
# 直接读 → 阶段 1+2+5+8 全跑,唯一省略 enumerate 三件套。
[ -s "$TARGET" ] || { echo "[-] $TARGET 为空或不存在" >&2; exit 1; }
[ -f "$EXCLUDE" ] || : > "$EXCLUDE"

PRECISE_HOSTS="$(mktemp)"
GLOB_HOSTS="$(mktemp)"
WWW_COUNT=0
while IFS= read -r t || [ -n "$t" ]; do
    [ -z "$t" ] && continue
    case "$t" in
        *\** ) echo "$t" >> "$GLOB_HOSTS"  ;;
        # 精确单子域(无 *):域名只含 1 个 "." 时,自动加 "www." 前缀
        # 例: scanme.sh → www.scanme.sh;example.com → www.example.com
        # 2+ 点(如 sub.example.com / a.b.c.example.com)或多 label 域名不动
        # 0 点(如 localhost)罕见但也放行
        *.*.* ) echo "$t"        >> "$PRECISE_HOSTS" ;;
        *.*   ) echo "www.$t"    >> "$PRECISE_HOSTS"; WWW_COUNT=$((WWW_COUNT + 1)) ;;
        *     ) echo "$t"        >> "$PRECISE_HOSTS" ;;
    esac
done < "$TARGET"

# ===== 精确子域直接 cat → DNSX_OUT (读入 target 之后的第一件事,不经任何流程) =====
# 不 dnsx、不编译 ERE、不算 HAS_GLOB、不进 subfinder/alterx/permutation/wildcard。
# 下游 scanner.sh 阶段 1 会再 dnsx 一次。
if [ -s "$PRECISE_HOSTS" ]; then
    sort -u "$PRECISE_HOSTS" -o "$PRECISE_HOSTS"
    PRECISE_COUNT=$(wc -l < "$PRECISE_HOSTS")
    cat "$PRECISE_HOSTS" >> "$DNSX_OUT"
    if [ "$WWW_COUNT" -gt 0 ]; then
        echo "[*] 精确子域(无 *, 共 ${PRECISE_COUNT} 条, 其中 ${WWW_COUNT} 条 1-dot 自动加 www. 前缀) 直接 cat 进 DNSX_OUT,跳过 enumerate/dnsx"
    else
        echo "[*] 精确子域(无 *, 共 ${PRECISE_COUNT} 条) 直接 cat 进 DNSX_OUT,跳过 enumerate/dnsx"
    fi
fi
rm -f "$PRECISE_HOSTS"

# ===== 早期快路径:target 全部为精确子域(无 *)→ 已入 DNSX_OUT,直接退出 =====
if [ ! -s "$GLOB_HOSTS" ]; then
    rm -f "$GLOB_HOSTS"
    [ -s "$DNSX_OUT" ] && sort -u "$DNSX_OUT" -o "$DNSX_OUT"
    echo "[+] done -> $DNSX_OUT ($(wc -l < "$DNSX_OUT") 条) (精确快路径)"
    exit 0
fi

# ===== 后续:glob 流程(ERE 编译 → subfinder → alterx → permutation_cache → wildcard) =====
echo "[*] 编译 target / exclude glob ..."

python3 target_glob.py targets-ere  --input "$TARGET"  > targets.regex
python3 target_glob.py excludes-ere --input "$EXCLUDE" > excludes.regex

# 空文件守护 (永不匹配,免得 grep -f 空文件出错)
[ -s targets.regex ]  || echo '^$)$' > targets.regex
[ -s excludes.regex ] || echo '^$)$' > excludes.regex

# BASES 只从 glob 模式提取 — 精确子域无需 subfinder 枚举(已在 DNSX_OUT)
BASES=$(python3 target_glob.py all-bases --input "$GLOB_HOSTS" || true)
[ -n "$BASES" ] || { echo "[-] target.txt 里没有可用的 glob base" >&2; exit 1; }
rm -f "$GLOB_HOSTS"

# 剔除泛解析 base：它们已被阶段 6 的 wildcard 独立 subfinder 路径覆盖
# (subfinder → 裸 host 写 DNSX_OUT → 下游 scanner.sh dnsx 解析)。
# 让 alterx 也吃到这些 base 会基于子域词模式(api/www/sale/v2...)派生大量
# `*.vendor-base.com` 形式的候选，99% 命中 catch-all IP → 污染 CDN 研判 + 撑大
# dnsx_output 候选 + 把 dnsx 阶段从分钟级拖到小时级。某 base 92 万候选 88 万
# 来自泛解析就是这个 bug。修法：直接从 BASES 里扣掉，stage 6 已经兜底。
WILDCARD_BASES=""
if [ -s wildcard.txt ]; then
    WILDCARD_BASES=$(python3 target_glob.py all-bases --input wildcard.txt || true)
fi
if [ -n "$WILDCARD_BASES" ]; then
    EXCLUDED=$(echo "$WILDCARD_BASES" | wc -l)
    BASES=$(comm -23 <(echo "$BASES" | sort -u) <(echo "$WILDCARD_BASES" | sort -u))
    echo "[*] 剔除 $EXCLUDED 个泛解析 base（阶段 6 单独覆盖），alterx 不再处理"
fi

# ===== 1) 子枚举:按 base 聚合,每 base 一次 subfinder =====
echo "[*] subfinder (按 base 聚合) ..."
# ALTERX_TMPDIR 在阶段 3 创建,提前声明以便 EXIT trap 引用(初始空 → 不误删 .)
ALTERX_TMPDIR=
SUBS_TMP="$(mktemp)"
trap '[ -n "$ALTERX_TMPDIR" ] && rm -rf "$ALTERX_TMPDIR"; rm -f "$SUBS_TMP" "$SUBS_TMP.f" targets.regex excludes.regex' EXIT

while IFS= read -r base; do
    [ -z "$base" ] && continue
    subfinder -d "$base" -silent -r "$RESOLVERS_FILE" 2>/dev/null >> "$SUBS_TMP" || true
done <<< "$BASES"

# 已知子域(原 target 行, 跳过含 * 的模式行 —— 它们是 glob pattern, 不是真实 host)
# 注: 模式行会被 subfinder 用 base 形式间接发现; 直接 cat 模式行会被后续 grep 误匹配自身。
#
# 精确子域已在更早步骤(scan.sh 阶段 0 的早期精确子域入 DNSX_OUT 块)走 dnsx 直入
# DNSX_OUT,这里不再重复推送。glob 子域保持原来的全流程(阶段 1 → 4)继续走。
sort -u "$SUBS_TMP" -o "$SUBS_TMP"

# ===== 1.5) 第一次过滤:subfinder 后,grep target + grep -v exclude =====
if [ -s "$SUBS_TMP" ]; then
    grep -E -f targets.regex "$SUBS_TMP" | sort -u > "$SUBS_TMP.f"
    mv "$SUBS_TMP.f" "$SUBS_TMP"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$SUBS_TMP" | sort -u > "$SUBS_TMP.f"
        mv "$SUBS_TMP.f" "$SUBS_TMP"
    fi
fi

# ===== 2) dnsx 解析存活 =====
# dnsx 路径:硬编码默认 ~/.pdtm/go/bin/dnsx,可用环境变量 DNSX_BIN 覆盖
DNSX_BIN="${DNSX_BIN:-$HOME/.pdtm/go/bin/dnsx}"
[ -x "$DNSX_BIN" ] || { echo "[-] 缺少 dnsx: $DNSX_BIN" >&2; exit 1; }
echo "[*] dnsx -> alive.txt ..."
if [ -s "$SUBS_TMP" ]; then
    "$DNSX_BIN" -rl 100 -t 150 -retry 2 -o "$ALIVE" -r "$RESOLVERS_CSV" < "$SUBS_TMP"
else
    : > "$ALIVE"
fi

LINES=$(wc -l < "$ALIVE")
echo "[*] alive: $LINES 条 (已 grep target + -v exclude)"

# ===== 节奏闸门:决定本轮是否跑 alterx 派生 =====
# 周期内(应跳过)→ ALTERX_OUT 保持空,阶段 4 自动走 elif "$ALIVE" >> "$DNSX_OUT" 兜底,
# 下游 scanner 仍能看到 subfinder 已知子域,只是看不到 alterx 变体。
ALTERX_RUN_THIS_TURN=0
if python3 alterx_runs.py should-run \
        --db "$DB" --business-id "$BUSINESS_ID" \
        --cadence-days "$ALTERX_CADENCE_DAYS" \
        ${FORCE_ALTERX:+--force}; then
    ALTERX_RUN_THIS_TURN=1
    echo "[*] alterx 节奏闸门:本轮跑 alterx 派生"
else
    echo "[*] alterx 节奏闸门:本轮跳过 alterx 派生 (cadence=${ALTERX_CADENCE_DAYS}d),仅 subfinder 入 dnsx"
fi

# ===== 3) alterx 生成候选 (按 base 拆分,避免跨 base 词模式污染 + per-base 自适应 -limit) =====
# base 可以是子域名(如 *.api.example.com → api.example.com),partition-alive 按
# 最长匹配分桶,每桶独立跑两轮 alterx (带/不带 -enrich),输出追加到 ALTERX_OUT。
# 不分桶的话:alterx -enrich 从混合 base 学词模式,base A 的 api/dev/v2 等词模式
# 会被应用到 base B → 大量 out-of-scope 候选由 grep targets.regex 兜底,但 dnsx
# 已经浪费;base 间 -limit 预算也无法按规模分配。
#
# 候选数上限保护:alterx -limit 只截输出,不阻止内部模式生成。极大输入下即使
# -limit 很小,内部仍可能生成百万级候选导致卡死。防御:先用 -es 预估,超
# ALTERX_MAX_EST(默认 100 万)就 shuf 随机减半重估,直到 ≤ 阈值。-enrich 与
# 非 -enrich 分支各自独立预估(词库不同,候选数不同)。
echo "[*] alterx enrichment (按 base 拆分) ..."
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ "$LINES" -gt 0 ]; then
    ALTERX_TMPDIR=$(mktemp -d)
    BASES_CSV=$(printf '%s' "$BASES" | tr '\n' ',' | sed 's/,$//')
    python3 target_glob.py partition-alive \
        --input "$ALIVE" --bases "$BASES_CSV" --output-dir "$ALTERX_TMPDIR" \
        || { echo "[-] partition-alive 失败" >&2; exit 1; }

    # alterx 候选数预估与减半辅助。-es 输出在 stderr:
    #   [INF] Estimated Payloads (including duplicates): <N>
    ALTERX_MAX_EST=${ALTERX_MAX_EST:-1000000}

    alterx_estimate() {
        # $1=input 文件, $2=enrich flag("-enrich" 或空);echo 候选数,失败回 0。
        # 注意:alterx -silent 会同时静音 [INF] 估计行,这里必须不带 -silent。
        cat "$1" | alterx -es ${2:-} 2>&1 \
            | awk '/Estimated Payloads \(including duplicates\)/{print $NF; exit}'
    }

    alterx_prepare_input() {
        # $1=input 文件, $2=enrich flag;echo 满足 ≤ ALTERX_MAX_EST 的 input 路径。
        # 返回路径可能是原文件,也可能是 ALTERX_TMPDIR/halved.<rand>(trap 清整个 tmpdir)。
        local orig="$1" enrich_flag="$2"
        local cur="$orig" attempts=0 est
        while :; do
            est=$(alterx_estimate "$cur" "$enrich_flag")
            est=${est:-0}
            if [ "$est" -le "$ALTERX_MAX_EST" ]; then break; fi
            # 减半:half < lines 才有效(lines=1 时 half=1,等于 lines,无进展)
            local lines half halved
            lines=$(wc -l < "$cur")
            half=$(( (lines + 1) / 2 ))
            if [ "$half" -lt 1 ] || [ "$half" -ge "$lines" ]; then
                echo "[alterx] WARNING: ${enrich_flag:-no-enrich} 输入 $lines 行但 est=$est 仍超阈值,放弃减半" >&2
                break
            fi
            halved="$ALTERX_TMPDIR/halved.$$.$RANDOM"
            if ! shuf -n "$half" "$cur" > "$halved" 2>/dev/null; then
                echo "[alterx] WARNING: shuf 减半失败,放弃" >&2
                rm -f "$halved"
                break
            fi
            attempts=$((attempts + 1))
            if [ "$cur" != "$orig" ]; then rm -f "$cur"; fi
            cur="$halved"
        done
        if [ "$attempts" -gt 0 ]; then
            echo "[alterx] ${enrich_flag:-no-enrich} 减半 $attempts 次,最终 est=$est (输入 $orig)" >&2
        fi
        echo "$cur"
    }

    while IFS= read -r base; do
        [ -z "$base" ] && continue
        partition="$ALTERX_TMPDIR/alive.base.$base"
        [ -s "$partition" ] || continue
        BLINES=$(wc -l < "$partition")
        [ "$BLINES" -gt 0 ] || continue
        # 两个分支各自准备 input(estimate + 必要减半),减半产物函数内已 rm,
        # 返回等于原 partition 时不需清理
        in_enrich=$(alterx_prepare_input "$partition" "-enrich")
        cat "$in_enrich" | alterx -silent -limit $(( BLINES * 20 )) -enrich -silent >> "$ALTERX_OUT"
        in_plain=$(alterx_prepare_input "$partition" "")
        cat "$in_plain" | alterx -silent -limit $(( BLINES * 10 ))            -silent >> "$ALTERX_OUT"
    done <<< "$BASES"
fi

# ===== 3.25) 去重:alterx 派生中与 subfinder 已知子域撞名的排除 =====
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ] && [ -s "$ALIVE" ]; then
    comm -23 <(sort -u "$ALTERX_OUT") <(sort -u "$ALIVE") > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
fi

# ===== 3.5) 第二次过滤:alterx 后,grep target + grep -v exclude (关键) =====
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ]; then
    grep -E -f targets.regex "$ALTERX_OUT" > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$ALTERX_OUT" > "${ALTERX_OUT}.try"
        mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
    fi
fi

# ===== 3.7) permutation_state 过滤(跳过已解析/缓存未到期/泛解析命中) =====
echo "[*] permutation cache filter ..."
if [ "$ALTERX_RUN_THIS_TURN" = "1" ] && [ -s "$ALTERX_OUT" ]; then
    python3 permutation_cache.py filter \
        --db "$DB" --business-id "$BUSINESS_ID" --wordlist-hash "$WORDLIST_HASH" \
        < "$ALTERX_OUT" > "${ALTERX_OUT}.try"
    mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
fi

# ===== 4) 解析候选(alterx 派生 + subfinder 已知子域,一起送 dnsx) =====
# 注: ALIVE 是 glob 模式导出的 SUBS_TMP 的 dnsx 结果;精确子域已在前置快速通道
# 写进 DNSX_OUT。这里改 -o 为 append,确保 glob 流与精确子域合并(去重在 4.5 之前
# 加 sort -u 完成)。
echo "[*] dnsx on candidates ..."
if [ -s "$ALTERX_OUT" ]; then
    sort -u "$ALTERX_OUT" "$ALIVE" \
        | "$DNSX_BIN" -rl 100 -t 150 -retry 2 -r "$RESOLVERS_CSV" >> "$DNSX_OUT" || true
elif [ -s "$ALIVE" ]; then
    cat "$ALIVE" >> "$DNSX_OUT" || true
fi
# 合并并去重(同源重复 + 精确子域已存在条目)
if [ -s "$DNSX_OUT" ]; then
    sort -u "$DNSX_OUT" -o "$DNSX_OUT"
fi

# ===== 4.5) 第三次过滤:dnsx 后(最终把关) =====
if [ -s "$DNSX_OUT" ]; then
    grep -E -f targets.regex "$DNSX_OUT" > "${DNSX_OUT}.try"
    mv "${DNSX_OUT}.try" "$DNSX_OUT"
    if [ -s excludes.regex ]; then
        grep -v -E -f excludes.regex "$DNSX_OUT" > "${DNSX_OUT}.try"
        mv "${DNSX_OUT}.try" "$DNSX_OUT"
    fi
fi

# ===== 5) permutation_state 写回(对照 alterx 候选 vs dnsx 结果;同事务标记 alterx_runs) =====
# 即使 ALTERX_OUT 被阶段 3.7 filter 全清空(所有候选都已 cached),record 仍需被调用一次以
# 让 alterx_runs 同事务标记 — 否则 FORCE_ALTERX 不会重置节奏时钟。
echo "[*] permutation cache record ..."
if [ "$ALTERX_RUN_THIS_TURN" = "1" ]; then
    python3 permutation_cache.py record \
        --db "$DB" --business-id "$BUSINESS_ID" --wordlist-hash "$WORDLIST_HASH" \
        --candidates "$ALTERX_OUT" --resolved "$DNSX_OUT" || true
fi

# ===== 6) 泛解析域：只走 subfinder,按 base 去重后追加 =====
# 原始写法 `cat wildcard.txt | subfinder` 把 `a-*.b.com` 当字面域名 `a-b.com` 喂给
# subfinder,完全没按 glob 语义。改为先 extract_base 再去重,`a-*.b.com` / `d-*.b.com`
# 都抽到 `b.com` → subfinder 只查 1 次;之后 grep target regex 把匹配各自 glob 的
# 子域(a-foo.b.com / d-bar.b.com)过滤出来,语义与 scan.sh 阶段 1 完全一致。
if [ -s wildcard.txt ]; then
    echo "[*] wildcard -> subfinder (按 base 去重) ..."
    WILDCARD_BASES=$(python3 target_glob.py all-bases --input wildcard.txt || true)
    if [ -n "$WILDCARD_BASES" ]; then
        echo "$WILDCARD_BASES" | subfinder -silent -r "$RESOLVERS_FILE" | \
            grep -E -f targets.regex | \
            grep -v -E -f excludes.regex \
            >> "$DNSX_OUT" || true
        echo "[*] wildcard 子域已追加"
    else
        echo "[*] wildcard.txt 无可用 base,跳过"
    fi
else
    echo "[*] 无 wildcard.txt,跳过"
fi

# 去重最终结果
if [ -s "$DNSX_OUT" ]; then
    sort -u "$DNSX_OUT" -o "$DNSX_OUT"
fi

echo "[+] done -> $DNSX_OUT ($(wc -l < "$DNSX_OUT") 条)"
