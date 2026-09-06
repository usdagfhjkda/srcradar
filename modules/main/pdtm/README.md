# pdtm 资产测绘流水线

ProjectDiscovery 工具链编排的资产测绘系统:泛解析检测 → 子域枚举 + 派生 → CDN 研判 + 端口扫描 + Web 探测 → 结果入 SQLite。

> **依赖**:本模块调用的 `dnsx` / `httpx` / `naabu` / `subfinder` / `alterx` 等工具**不在本仓库分发**,由用户自行安装到 PATH(详见主 README §五 快速开始)。

## 目录

- [架构](#架构)
- [快速开始](#快速开始)
- [四阶段说明](#四阶段说明)
- [数据流与产物](#数据流与产物)
- [DB Schema](#db-schema)
- [中间产物清理](#中间产物清理)
- [附录 A：alterx 缓存 bug 与修复](#附录-aalterx-缓存-bug-与修复)
- [附录 B：让"每次跑都大概率找到新资产"的三档方案](#附录-b让每次跑都大概率找到新资产的三档方案)
- [附录 C：scanner 阶段 5/6 fusion 链路 bug](#附录-cscanner-阶段-56-fusion-链路-bug)
- [附录 D：scan_results 残留导致跨业务污染](#附录-dscan_results-残留导致跨业务污染)

## 架构

```
target.txt ──► check_wildcard.sh ──► wildcard.txt / no_wildcard.txt
                                            │
no_wildcard.txt ──► scan.sh ──► dnsx_output.txt
                       │              (subfinder → dnsx → alterx → dnsx)
                       │              + permutation_state 缓存
                       │
dnsx_output.txt ──► scanner.sh ──► scan_results/
                                    (CDN研判 → naabu → httpx)
                                    │
scan_results/ ──► import_scan_results.py ──► ../db/recon.sqlite3
                                                (web + tcp + scope)
```

`pipeline.sh` 串行调度上述脚本，支持 `-i` 输入、按业务名入库、跳过任意阶段、dry-run、自动清理。

## 快速开始

```bash
# 准备输入目录
mkdir -p input
cat > input/target.txt <<'EOF'
*.scanme.sh
cc-*.scanme.sh
demo.scanme.sh
EOF
# 可选: 任意含 * 的 glob 形式,见「target.txt / exclude.txt 语法」
cat > input/exclude.txt <<'EOF'
*.internal.scanme.sh
EOF

# 跑流水线
./pipeline.sh -b mybusiness -i ./input/

# 或从已有 DB 读 scope(业务必须在 businesses 表里)
./pipeline.sh -b mybusiness

# 跳过某阶段
./pipeline.sh -b mybusiness -i ./input/ --skip-check-wildcard
./pipeline.sh -b mybusiness -i ./input/ --dry-run-import
./pipeline.sh -b mybusiness -i ./input/ --keep-on-fail   # 失败保留现场
```

## 四阶段说明

### 阶段 1 — `check_wildcard.sh`

dnsx 探测 glob 域是否开启泛解析。对含 `*` 的 pattern（如 `*.example.com` /
`cc-*.example.com` / `aaa.*.bbb.com`），把 `*` 替换为随机 label 后探测 3 次；
任一能解析 IP 则判为泛解析。不含 `*` 的精确单子域不需要 wildcard 检测。

- 输出 `wildcard.txt`（泛解析 glob）/ `no_wildcard.txt`（非泛解析 glob + 精确单子域）
- 仅非泛解析域进入下一阶段

### target.txt / exclude.txt 语法

`target.txt` 和 `exclude.txt` 共用同一套 glob 语法。每行一条规则，`#` 开头为
注释，大小写不敏感；`target` 是可测资产，`exclude` 是非可测资产。

| 写法 | 语义 | 匹配示例 |
|---|---|---|
| `*.example.com` | 整域通配（任意段，含多级子域） | 命中 `a.example.com`、`a.b.example.com`；不命中 `example.com`（apex）、`notexample.com` |
| `cc-*.example.com` | 前缀通配 | 命中 `cc-api.example.com`、`sub.cc-x.example.com`；不命中 `cc.example.com`、`ccfoo.example.com` |
| `aaa.*.bbb.com` | 中段通配 | 命中 `aaa.x.bbb.com`、`aaa.x.y.bbb.com`；不命中 `aaa.bbb.com`（空通配）、`aaax.bbb.com` |
| `ccc.bbb.com` | 精确单子域（无 `*`） | 命中 `ccc.bbb.com` 及其子域；不命中 `notccc.bbb.com` |

**锚定规则**：所有 pattern 编译为 ERE `(^|\.)pattern$`，避免 `*.example.com` 误命中
`notexample.com` 之类。`*` 在 glob 语义下匹配任意字符（含 `.`），等价 ERE `.*`。

**base 提取**：subfinder 按 base 聚合调用（每个 base 只跑一次），base 是
pattern 中最右连续不含 `*` 的 label 段：

- `*.example.com` → base `example.com`
- `cc-*.example.com` → base `example.com`
- `aaa.*.bbb.com` → base `bbb.com`
- `ccc.bbb.com` → base `ccc.bbb.com`（自身）

**三处 grep 守门**（scan.sh 内部，杜绝非可测资产进入数据库）：

1. subfinder 输出后：先 `grep -E -f targets.regex`，再 `grep -v -E -f excludes.regex`
2. alterx 输出后：同样两道 grep
3. dnsx 输出后：同样两道 grep（最终把关）

`exclude.txt` 早期版本有「keyword 子串匹配」分支（`honey` / `kw:uat` 之类），
已**完全删除**，所有 exclude 行按 glob 处理。需要子串语义请写完整带点的模式
（`test\.example\.com` 等）。

### scope glob 迁移

2026-07-30 升级后,所有 scope 行（`scopes.asset` 列）应包含 glob 形式。
旧数据（裸域名如 `example.com`）可用 `migrate_scope_glob.py` 一次性加 `*.` 前缀:

```bash
# dry-run 预览
python3 migrate_scope_glob.py --db ../db/recon.sqlite3
# 真正写库
python3 migrate_scope_glob.py --db ../db/recon.sqlite3 --apply
```

### 阶段 2 — `scan.sh`

```
no_wildcard.txt
    └─► subfinder -d <root>  (被动枚举)
        └─► dnsx  (A 记录解析)
            └─► ALIVE_FILTERED  (按 exclude.txt 过滤)
                ├─► alterx -enrich -limit N×20  (富化派生)
                └─► alterx       -limit N×10  (基础派生)
                └─► 去重:剔除 ALTERX_OUT 中与 ALIVE_FILTERED 撞名的项 (见附录 A)
                └─► ALTERX_OUT ∪ ALIVE_FILTERED → dnsx
                                          └─► permutation_state 缓存写回 (仅 ALTERX_OUT)
```

**alterx 节奏闸门**(`scan.sh` 阶段 3 顶部,`alterx_runs.py should-run`):
- 默认 **每 30 天跑一次 alterx**(`ALTERX_CADENCE_DAYS=30`),中间周期内阶段 3/3.25/3.5/3.7/5 整体跳过,
  ALTERX_OUT 保持空,阶段 4 自动走 elif `cat "$ALIVE" >> "$DNSX_OUT"` 兜底,下游 scanner 仍能看到
  subfinder 已知子域,只是看不到 alterx 派生变体。期间 `permutation_state` 既不读也不写(冻结)。
- `ALTERX_CADENCE_DAYS=0` 等同关闭节奏,每跑都跑 alterx。
- `FORCE_ALTERX=1` 强制本轮跑,忽略节奏(适合业务合并/收购后立即生效)。
- 节奏点由 `alterx_runs` 表独立追踪(每业务 1 行 `last_ran_at`),不依赖 `permutation_state`,彻底
  消除 §八 #13 描述的"陈旧 cache 截断新候选"问题。

`permutation_state` 单条记录策略(单条 TTL,与上面节奏闸门正交):
- `resolved` / `wildcard_hit` — 永不再试
- `nxdomain` — 30 天冷却，到期重试
- 词表哈希变化 — 强制重试

> **已并入本方案**:之前挂在 §八 #13 的 "`PERM_CACHE_WIPE_DAYS` 整表 wipe" TODO 已被本节奏闸门取代——
> 节奏日内整条 alterx 链路不读不写 `permutation_state`,等价"整表不读不写";节奏日 regenerate
> 又是全新一轮,不依赖旧 cache 的"陈旧过滤"。

### 阶段 3 — `scanner.sh`

```
dnsx_output.txt
    └─► 阶段 1: 清洗 + DNS 解析 (A/AAAA/CNAME/NS)
    └─► 阶段 2: cdncheck 域名+IP 双重研判 → CDN/非CDN 划分
    └─► 阶段 3: 过滤 LB (>15 域名/IP)
    └─► 阶段 3.5: CDN+LB IP 走 httpx (带 Host 头)
    └─► 阶段 4: naabu 被动端口发现
    └─► 阶段 4.5: naabu 主动扫描 (-top-ports 100)
    └─► 阶段 5: httpx 主动探测 + 指纹
    └─► 阶段 6: Host 头映射
    └─► 阶段 7: 端口融合 (被动+主动+httpx+CDN/LB)
    └─► 阶段 8: Web 存活确认 (json)
    └─► 阶段 9: 分离 Web 端口 vs 纯 TCP 端口
```

输出 `scan_results/`：
- `non_cdn_web_summary.json` — Web 详情
- `cdn_lb_web_summary.json` — CDN/LB 探测结果
- `non_cdn_tcp_ports.txt` — 纯 TCP 爆破列表
- `tmp_domain_ip_pairs.txt` — IP→域名映射
- `non_cdn_list.txt` — 非 CDN 域名

### 阶段 4 — `import_scan_results.py` + `finalize_scope`

`import_scan_results.py` 解析 `scan_results/` 并入库：
- `web_subdomains` (按 response_hash 去重) + `web_hashes` (指纹库)
- `tcp_assets` (host:port + hosts)
- `scopes` (可测/非可测资产，is_wildcard 标记)
- 已有记录 `is_active=0` 后重插，新发现的自动激活

`finalize_scope` (pipeline 末) 再次显式 upsert scope，确保**删输入文件前 scope 已落库**。

## 数据流与产物

| 文件 | 来源 | 持久性 |
|------|------|--------|
| `target.txt`, `exclude.txt` | `-i` 输入 / DB | 流水线成功后删(若由本脚本生成) |
| `wildcard.txt`, `no_wildcard.txt` | check_wildcard | 删 |
| `alive.txt`, `alterx.txt`, `dnsx_output.txt` | scan | 删 |
| `filter.regex`, `*.tmp`, `*.bak` | 中间临时 | 删 |
| `scan_results/` | scanner | import 成功后删 |
| `../db/recon.sqlite3` | import + finalize_scope | 保留(共享 DB) |

## DB Schema

数据库位置：`../db/recon.sqlite3`（与本目录同级 `db/`，与其他项目共享 `businesses` 表）

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `businesses` *(共享)* | 业务字典 | `id`, `business_name` (本项目不主动 CREATE/ALTER) |
| `scopes` | 可测/非可测资产 | `business_id`, `scope_name`, `asset`, `is_wildcard` |
| `web_hashes` | Web 指纹库 | `business_id`, `response_hash`, `subdomain_count` |
| `web_subdomains` | Web 资产 | `hash_id`, `subdomain`, `port`, `url`, `status_code`, `title`, `technologies` |
| `tcp_assets` | TCP 端口资产 | `business_id`, `host`, `port`, `hosts`, `is_active` |
| `permutation_state` | alterx 派生状态缓存 | `business_id+base_domain+permutation` 复合主键,`status`, `wordlist_hash`, `next_attempt_at`, `attempts` |
| `alterx_runs` | alterx 节奏闸门(每业务 1 行) | `business_id` PK,`last_ran_at`(UTC ISO),`wordlist_hash`,`candidates`,`resolved` |

`pipeline.sh` 的 `bootstrap_db` 只校验 `businesses` 表存在并 `INSERT OR IGNORE` 业务行；不主动建/改共享表。

## 中间产物清理

`trap cleanup_input EXIT` 在流水线退出时清理：

- 默认：**成功 + 失败都清**（除 `--keep-on-fail`）
- `--skip-import` 时也清（用户明确不要数据）
- 清理对象：`target.txt`/`exclude.txt`(仅本脚本生成时) + 所有 `*.txt`/`*.tmp`/`*.bak` 中间文件 + `scan_results/`

清理顺序保证：**import → finalize_scope → 删文件**。删除输入文件前 scope 必然已落库。

### 跑前残留检测（`pipeline.sh:94-106`）

为防止上一次 run 留下的 `scan_results/` 被本次 import 误读（见附录 D），pipeline 在参数解析后、任何阶段开始前**主动拒绝非空 `scan_results/`**：

```bash
if [ -d "$SCAN_DIR" ] && [ -n "$(ls -A "$SCAN_DIR" 2>/dev/null)" ]; then
    echo "[-] 检测到 $SCAN_DIR/ 残留文件:" >&2
    ls -la "$SCAN_DIR/" >&2
    ...
    exit 2
fi
```

触发场景（典型）：上次跑用了 `--debug 1`（默认）或手动 `./pipeline.sh` 没传 `--debug 0`，且进程异常退出导致 `trap cleanup_input EXIT` 没跑到 → 残留进下次 cron。

遇到这个退出码（`2`）的处理：

```bash
# 看一眼残留是什么
ls -la scan_results/
# 确认是上一次本业务的产物,直接清
rm -rf scan_results/ && mkdir -p scan_results/
# 再跑
./pipeline.sh -b 业务名
```

## 附录 A：alterx 缓存 bug 与修复

### 现象

第二跑 `dnsx_output.txt` 残缺，subfinder 已发现的子域全丢。

### 根因

`scan.sh` 阶段 3 把 3 路输出合并到 `ALTERX_OUT`：
1. `alterx -enrich -limit N×20`
2. `alterx -limit N×10`
3. `cat ALIVE_FILTERED >> ALTERX_OUT` ← **已知子域**

第 3 路把 subfinder 发现的事实子域混进 alterx 候选 → `permutation_cache.record` 写入 `permutation_state`（`status=resolved`, `next_at=NULL`）→ 第二跑 filter 命中全部跳过 → 已知子域永远消失。

### 修复

```diff
 # scan.sh 阶段 3: 只生成 alterx 派生,不再混入已知子域
 cat "$ALIVE_FILTERED" | alterx ... >> "$ALTERX_OUT"   # -enrich N×20
 cat "$ALIVE_FILTERED" | alterx ... >> "$ALTERX_OUT"   # 基础 N×10
-cat "$ALIVE_FILTERED" >> "$ALTERX_OUT"                 # 删除

 # scan.sh 阶段 3.25 (新增): 剔除 alterx 派生中与已知子域撞名的项
+if [ -s "$ALTERX_OUT" ] && [ -s "$ALIVE_FILTERED" ]; then
+  comm -23 <(sort -u "$ALTERX_OUT") <(sort -u "$ALIVE_FILTERED") > "${ALTERX_OUT}.try"
+  mv "${ALTERX_OUT}.try" "$ALTERX_OUT"
+fi

 # scan.sh 阶段 4: 合并派生 + 已知子域,一起送 dnsx
-sort -u "$ALTERX_OUT" | dnsx ... -o "$DNSX_OUT"
+sort -u "$ALTERX_OUT" "$ALIVE_FILTERED" | dnsx ... -o "$DNSX_OUT"
```

`permutation_cache.py` 不动——`permutation_state` 表如其名，只存"派生"状态；事实子域每次跑由 subfinder 重新喂入 dnsx。

#### 为什么需要去重

alterx 是**按规则生成**工具：输入根域 + 词表 → 用词表里的 token 拼前缀/后缀/插入。默认词表包含 `demo`/`dev`/`test`/`admin` 等常见 token，所以从 `scanme.sh` 派生时**算法独立**就可能产出 `demo.scanme.sh`——恰好与 subfinder 已发现的子域重名。

- 修复前（`cat ALIVE_FILTERED >> ALTERX_OUT`）：5/5 已知子域进 cache，第二跑全丢
- 只删显式 cat 不去重：4/5 已知子域不再进 cache，但 `demo.scanme.sh`（alterx 算法撞名）仍进 cache，仍会被"resolved 永不再试"逻辑冻住
- 加去重后：0/5 已知子域进 cache，`permutation_state` 纯净

这些"撞名"项仍通过 stage 4 的 `sort -u ALTERX_OUT ALIVE_FILTERED | dnsx` 进入 dnsx 解析（事实子域不缓存但照常解析），所以**业务侧零损失**。

### 验证

清空 `permutation_state` 后连续跑 2 次（用 scanme.sh 5 个已知子域）：

| | Run 1 | Run 2 |
|---|---|---|
| `permutation_state` 总数 | 149 | 287（增量 138，第二次有新候选）|
| 已知子域命中 | **0** ✓ | **0** ✓ |
| `dnsx_output.txt` | 5 条（事实子域）| 5 条 |
| 工作目录残留 | 无 | 无 |

- `permutation_state` 数字增长说明 alterx 派生能力未受去重影响
- 已知子域每次都从 subfinder 喂入 `dnsx_output.txt`，下游 scanner 始终能看到
- dnsx 查询量不变（合并前后集合相同）

## 附录 B：让"每次跑都大概率找到新资产"的三档方案

**根本问题**：alterx 词表固定 → 输出确定 → 词表哈希稳定 → 缓存全命中 → 第二跑没有新候选可试。

三档独立方案叠加效果最好：

### A. 时间戳词表（必做，最关键）

每次跑动态生成 alterx 词表，词表哈希自动跟着变 → 缓存按词表哈希自然失效。

```bash
gen_wordlist() {
    cat "${WORDLIST:-wordlists/alterx-base.txt}" 2>/dev/null
    date +%Y           # 2026
    date +%V           # 26 (ISO 周)
    date +%Y%V         # 202626
    date +%Y-%m        # 2026-07
    echo "q$(($(date +%m)/4+1))"   # 季度 q3
    printf 'canary\nblue\ngreen\nv2\nv3\nstaging\nshadow\n'
}
```

候选里出现 `prod-2026.example.com` / `w26.example.com` / `green.example.com` 这类按周变化的子域。每跑 `wordlist_hash` 不同，`permutation_cache.py` 失效逻辑自动让陈旧项重试。

**改造成本：1 个函数 + scan.sh 调一行 `alterx -w <(gen_wordlist)`。立刻见效。**

### B. 概率性复活采样（捕"死而复生"子域）

DNS 记录常变：今天 nxdomain，下周突然注册了。当前 30 天冷却期间这种复活直接漏掉。

`permutation_cache.filter_permutations` 加复活采样分支：

```python
# 在 filter 里加
stale = [p for p, (s, n, h) in cached.items()
         if s == "nxdomain" and n and parse_iso(n) < now_ts]
import random
sample = set(random.sample(stale, max(1, len(stale) * 7 // 100)))
for perm in sample:
    kept.append(perm)
```

每跑多解析 ~7% 的"已死"项；复活子域 1-2 周内能捞回。改造成本：~5 行。

### C. 多被动源（最朴素但有效）

`scan.sh` 阶段 1 现在只用 subfinder。每天互联网有新注册，子域自然出现：

```bash
{
  for root in $ROOTS; do
    subfinder -d "$root" -silent
    amass enum -passive -d "$root" 2>/dev/null
    curl -s "https://crt.sh/?q=%25.${root}&output=json" 2>/dev/null \
        | python3 -c 'import json,sys
                      [print(n) for x in json.load(sys.stdin) if x.get("name_value") for n in x["name_value"].split("\n")]' 2>/dev/null
  done
} | sort -u > "$SUBS_TMP"
```

新种子进入 ALIVE_FILTERED → alterx 围绕新种子再派生 → 二次发现。改造成本：中（加工具/网络）。

### 推荐组合

| 方案 | 改造成本 | 新资产增益 | 适用 |
|-----|---------|----------|------|
| **A. 时间戳词表** | 小 | ★★★★★ | 通用,必做 |

## 附录 C：scanner 阶段 5/6 fusion 链路 bug

### 现象

`scanner.sh` 默认参数（不开 `-all`，即 TCP 端口信息收集关闭）跑完，`scan_results/` 里：
- `raw_httpx_active_ips.txt` 有 httpx 命中
- `raw_mapped_ports.txt` **为空**
- `non_cdn_all_ports.txt` **为空**
- `non_cdn_web_summary.json` **为空**

`import_scan_results.py` 报 `没有找到可入库的 Web 或 TCP 结果` 退出。

`-all` 时（TCP 开）所有值正常，因为 naabu 的 IP:port 直接进 fusion，绕开了出问题的链路。

### 根因

阶段 5 httpx 用 `-sc -cl -title -hash mmh3` 采集指纹时，输出格式是：
```
http://128.199.158.128 [200] [2] [-2088429648]
```
——URL 后面跟着 4 个空格分隔的 bracket 字段。

阶段 6 mapper（`scanner.sh:463-489`）解析逻辑：
```bash
clean=$(echo "$line" | sed 's|https\?://||g' | sed 's|/.*||g')   # → "IP [200] [2] [hash]"
ip=$(echo "$clean" | cut -d':' -f1)                               # ← 错在这
```

`cut -d':' -f1` 要求该行不含冒号才能正确抽出 `host:port` 中的 host 部分。带 bracket 的行不含冒号，**整行被吃成 $ip**。

随后 `grep "^<脏串> " FILE_IP` 在 `tmp_domain_ip_pairs.txt`（格式 `IP domain`）里查不到，**整条命中被静默丢弃**。

这是个**潜在 bug**——之前 TCP 默认开时，阶段 4/4.5 naabu 产 `IP:port` 通过 `raw_naabu_active_mapped.txt` 走自己的 IP→域名 映射进 fusion，完全绕开阶段 6 mapper；naabu 这个 fallback 一直掩盖了阶段 6 mapper 的脆弱。直到把 TCP 改为默认关，fusion 链第一次 100% 依赖 stage 5 → 6 → 7，bug 暴露。

### 修复

阶段 5 httpx 调用去掉会把输出变 `URL [bracketed fields]` 的 4 个 fingerprint 字段。fingerprint 仍由阶段 8 的 `-j -title -td -hash` 在 web 存活确认阶段补齐，最终入库口径不变。

```diff
 log "[*] 阶段 5: httpx 主动探测..."

 httpx -l "$OUTPUT_DIR/pure_ips_to_scan.txt" \
-      -p "$COMMON_WEB_PORTS" \
-      -rate-limit 50 \
-      -timeout 5 \
-      -sc -cl -title \
-      -hash mmh3 \
+      -p "$COMMON_WEB_PORTS" \
+      -rate-limit 50 \
+      -timeout 5 \
+      -no-color -silent \
       -o "$OUTPUT_DIR/raw_httpx_active_ips.txt"
```

输出变成干净的 `http://IP` / `https://IP`（默认端口省略，无 bracketed 字段）。阶段 6 mapper 的 `cut -d':' -f1` 直接成功抽到 host。

阶段 6 mapper 自身仍脆弱（依赖 `https?://` 前缀 + line 无空格）。如果以后想给阶段 5 加回 `-title -td` 等字段而不掉数据，需要把阶段 6 改成 JSON 解析（参考附录 C 的"待办"段）。

### 验证

用 `dnsx_output.txt = scanme.sh [A] [128.199.158.128]` 跑 `./scanner.sh` 默认参数：

| | 改前 | 改后 |
|---|---|---|
| 阶段 5 httpx 命中 | 2 | 2 |
| 阶段 7 fusion 端口数 | **0** | **2** |
| 阶段 8 web 资产数 | **0** | **2** |
| 阶段 9 纯 TCP 端口数 | 0 | 0（默认 TCP 关，预期）|
| `non_cdn_all_ports.txt` 内容 | _空_ | `scanme.sh:80\nscanme.sh:443` |
| `non_cdn_web_summary.json` | _空_ | 2 条 `host=scanme.sh` `status_code=200` |

跑 `./scanner.sh -all` 时数值与默认参数一致（TCP 开 → fusion 还多 naabu 的端口），证明未误伤带 TCP 路径。

### 待办 / 未来改进

- **阶段 6 mapper 容错**：能解析带 bracketed 字段的 httpx 输出，让阶段 5 可恢复 `-title -td -hash` 等 fingerprint 字段。当前阶段 5 raw 文件本身没 fingerprint，靠阶段 8 兜底；如果哪天阶段 8 改了格式，fingerprint 缺口会显出来。
- **阶段 5/6 走 JSON**：阶段 5 加 `-j` 输出 JSONL，阶段 6 改读 `port` 字段出 `domain:port`。port 是结构化字段，httpx 输出格式再变也不会断。

| B. 复活采样 | 小 | ★★ | 业务有大量临时子域 |
| C. 多源种子 | 中 | ★★★★ | 持续性资产发现 |

**建议：A 必做，B 选做，C 看精力**。A 一行调用就能立刻见效，配合附录 A 的"事实不进 permutation_state"那 2 行修改，构成本流水线"每次跑都大概率找到新资产"的完整方案。

## 附录 D：scan_results 残留导致跨业务污染

### 现象

业务 A 跑完后，DB 里业务 A 的 `web_subdomains` 出现业务 B 的子域名（如 ExampleCo 出现 `*.vendor-cdn-app.com`）。

观察特征：
- 污染的 subdomain 都带 `host_ip` 指向 CDN/CloudFront 共享 IP
- 它们的 `response_hash`（如通用 404 页面 `mmh3 = -1840324437`）在业务 A 和业务 B 各自的 `web_hashes` 里都独立存在（`UNIQUE (business_id, response_hash)` 允许同 hash 跨业务并存）
- 污染行 `is_active = 0`（下次正常 run 后自动脱钩），但**DB 不主动删**，越攒越多
- `first_seen` 时间戳集中在某一次 run 的 import 阶段（`fetched_at` 也是）

### 根因

`import_scan_results.py` 的读文件列表（`import_scan_results.py:277-282`）：

```python
paths = [
    scan_dir / "non_cdn_web_summary.json",
    scan_dir / "cdn_lb_web_summary.json",   # ← 当前 scanner.sh 不写
    scan_dir / "non_cdn_web_summary.txt",   # ← 当前 scanner.sh 不写
    scan_dir / "cdn_lb_web_summary.txt",
    scan_dir / "raw_httpx_active_ips.json", # ← 当前 scanner.sh 写的是 .txt
]
```

`scanner.sh` 当前版本实际产出：

| 文件 | 写入位置 |
|---|---|
| `non_cdn_web_summary.json` | `scanner.sh:558` httpx `>` 截断 |
| `cdn_lb_web_summary.txt` | `scanner.sh:322/329/334` httpx `>` 截断 |
| `non_cdn_web_summary.txt` | **不写**（只写 .json）|
| `cdn_lb_web_summary.json` | **不写**（只写 .txt）|
| `raw_httpx_active_ips.txt` | `scanner.sh:466` httpx `-o` |
| `raw_httpx_active_ips.json` | **不写**（写的是 .txt）|

**3 个文件 import 会读、当前 scanner.sh 不写**。这些文件如果因前一次 run 用 `--debug 1` 留在 `scan_results/` 里没清，下次 import 会当新结果入库——`business_id` 取自当前 `pipeline.sh -b` 参数，但**里面的 host 是上一次别的业务扫的**。

历史版本 scanner.sh（`_deprecated_scanners/scanner.sh.bak`）曾写过 `raw_httpx_active_ips.json`；旧版 import 的 `paths` 列表残留了这个文件名，scanner 改名 `.txt` 后 import 没跟上。**两边不严格对齐**就是污染源。

### 复现链

1. 有人手动跑业务 B 的 pipeline（`KEEP_OUTPUT=1` 默认 / 或用老版 scanner.sh 写了 `.json`）
2. `scan_results/` 留下 `cdn_lb_web_summary.json` 含 `*.vendor-cdn-app.com` 的探测结果
3. 业务 A 的 cron 启动 pipeline.sh：scanner.sh 只 `mkdir -p` 不 `rm -rf`，当前版本不写 `.json` 那 3 个文件
4. import 看到那 3 个文件还在 → 解析 → 用 `business_id=ExampleCo` 插入 → 数百条 `*.vendor-cdn-app.com` 进了业务 A 的 `web_subdomains`
5. `first_seen` 全部打上同一个时间戳，污染成型

### 修复

**根治（pipeline.sh 跑前检测）**：见上面「跑前残留检测」一节。`pipeline.sh:94-106` 在参数解析后立即检查 `scan_results/` 是否非空，非空直接 `exit 2` 拒绝跑，强制人工确认/清理。

**辅助（import 与 scanner 对齐）**：要么在 scanner.sh 显式截断那 3 个文件（`: > "$OUTPUT_DIR/cdn_lb_web_summary.json"` 等），要么从 `import_scan_results.py` 的 `paths` 列表删掉当前不再产出的文件名（`.backup/20260803_123155/import_scan_results.py:279` 仍残留 `cdn_lb_web_summary.json`，是同样隐患）。

**清理已被污染的行**（一次性 SQL）：

```sql
BEGIN;
-- 删 web_subdomain 污染行
DELETE FROM web_subdomains
WHERE hash_id IN (SELECT id FROM web_hashes WHERE business_id = :victim_biz_id)
  AND subdomain LIKE '%污染关键字%';
-- 删空了的 hash 行
DELETE FROM web_hashes
WHERE business_id = :victim_biz_id
  AND NOT EXISTS (SELECT 1 FROM web_subdomains WHERE hash_id = web_hashes.id);
COMMIT;
```

### 验证

修复后跑两次相邻业务（如先 `TestBiz` 留 KEEP_OUTPUT，再 `ExampleCo`）：

| | 修复前 | 修复后 |
|---|---|---|
| `pipeline.sh -b ExampleCo` 启动 | 静默继续 | `exit 2` + 提示 `rm -rf scan_results/ && mkdir -p scan_results/` |
| 业务 A 的 `web_subdomains` 中 `*.vendor-cdn-app.com` | 数百条（污染）| 0 |

### 待办 / 未来改进

- **scanner.sh 显式截断 `paths` 列表里所有被读文件**：即使未来换 scanner 也不依赖"用 `>` 自然覆盖"。在 `mkdir -p "$OUTPUT_DIR"` 后面加：
  ```bash
  : > "$OUTPUT_DIR/cdn_lb_web_summary.json"
  : > "$OUTPUT_DIR/non_cdn_web_summary.txt"
  : > "$OUTPUT_DIR/raw_httpx_active_ips.json"
  ```
- **import 与 scanner 文件列表强一致**：从 import 的 `paths` 列表里删掉不再产出的文件名（`cdn_lb_web_summary.json` / `non_cdn_web_summary.txt` / `raw_httpx_active_ips.json`），或反过来——scanner 改回写 `.json`。两边选一边定死。
- **scanner.sh 入口加同样的残留检测**：当前只在 `pipeline.sh` 入口挡。直接 `cd pdtm && ./scanner.sh` 跑会绕过这道闸门，撞上同一个污染。
