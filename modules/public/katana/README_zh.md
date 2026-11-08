# modules/public/katana — katana 集成(可选 plugin)

> [English](README.md) | 中文

> **模块定位**:`katana` URL 资产扫描插件,**可选用**。
> 在 [`modules/main/pdtm/scan_urls.py`](../../main/pdtm/scan_urls.py)(URLFinder / gau / ffuf)之外,
> 提供与 httpx 字段对齐的 URL 级扫描能力,作为 `daily-url` stage 的**默认引擎**(2026-09-22 起)。

## 导航

- **本文档**:§模块边界 / §危险路由安全 / §字段对齐 / §部署 / §客户端使用 / §运维 / §文件说明
- **与 scan_urls.py 关系**(并存,非互斥):
  - URLFinder 仍是可手动调度的引擎(中文敏感信息提取 / 中文站点适配)
  - katana 接管 `daily-url` cron stage(字段丰富,可直接接 algo_diff 流水)
  - 两者都写 `web_hash_urls` 表(已存在,`modules/main/db/schema.sql:233-262`),不冲突
- **跳过它的场景**:不需要 katana 字段丰富性 / 不要切默认引擎 → 不装即可;
  `pdtm` / `daily` / `db_align` / `ymicp` 仍独立工作。

> **⚠️ 重要声明**:本目录仅包含 srcradar 编写的 Python 适配代码,**不重新分发 katana 二进制**。
> katana 由 [ProjectDiscovery](https://github.com/projectdiscovery/katana) 独立维护,MIT 协议。
> 本模块通过 `subprocess` 调 katana,**不直接 link Go 源码**,符合 MIT 边界要求。

---

## 一、模块边界

| 项 | 范围 | License |
|---|---|---|
| 本目录的 7 个文件 | srcradar 原创适配代码 | Apache-2.0 |
| katana CLI 二进制 | 由 ProjectDiscovery 独立维护,自部署 | MIT |
| URLFinder CLI(已存在) | 保留作为手动 / fallback / 中文敏感信息专用 | GPL(已 NOTICE) |

**srcradar 的职责**:
- 调 katana `-jsonl` 输出 → 解析 → 入 `web_hash_urls` 表
- **危险路由拦截双重保险**(沿用 URLFinder 5 词黑名单,详见 §二)
- 字段对齐 httpx(`urllib.parse` 补 host/port/path,`mmh3` 算 response_hash)

**srcradar 不做**:
- 不打包 katana 二进制(`install.sh` 用 `go install` 自取)
- 不接管 katana 升级(用户跑 `go install` 升级)
- 不维护 katana 上游 issue

---

## 二、危险路由安全(必读)

**沿用 URLFinder 5 词黑名单**(`~/tools/URLFinder/config/config.go:19`):

```python
RISKS = ("remove", "delete", "insert", "update", "logout")
```

**双重保险**:

1. **源头拦截**(`katana_enrich.sh` 拼命令时):`-crawl-out-scope` 用一条 regex 把含 5 词的 URL 全部排除
   ```bash
   katana ... -crawl-out-scope "/(remove|delete|insert|update|logout)/i"
   ```
2. **入库前 gate**(`lib/risks.py:is_dangerous_url`):katana 的 JSONL 输出再过一遍,命中的标
   `is_dangerous=1, danger_reason="疑似危险路由,已跳过验证"`,**写入** `web_hash_urls` 而不是丢弃
   (跟 URLFinder 行为对齐,方便 SRC 报告看到"哪些 URL 被跳过")。

> **已知缺陷**:子串匹配会有误伤(URL 含 `delete-check` 这种只读查询路径会被拦)。
> 这是 URLFinder 原生缺陷,本模块继承;不在本模块引入更智能判断(成本/复杂度不划算)。

---

## 三、字段对齐(httpx ↔ katana)

| httpx 字段 | 来源 | katana 字段 | 本模块策略 |
|---|---|---|---|
| `host` | httpx 自带 | ❌ 在 `request.endpoint` | `urllib.parse.urlparse(endpoint).hostname` |
| `port` | httpx 自带 | ❌ | `urlparse().port` 或 80/443 |
| `scheme` | httpx 自带 | ❌ | `urlparse().scheme` |
| `path` | httpx 自带 | ❌ | `urlparse().path` |
| `status_code` | ✅ | ✅ `response.status_code` | 直接取 |
| `content_type` | ✅ | ⚠️ `response.headers["content-type"]` | headers 兜底 |
| `content_length` | ✅ | ✅ | 直接取 |
| `title` | ✅ | ⚠️ 在 body 里 | `<title>(.*?)</title>` 正则(URLFinder 同款) |
| `technologies` | httpx 叫 `tech` | ✅ | key 改 `tech` |
| `header` | ✅ | ✅ `response.headers` | 直接取 |
| `body` | ✅ `-sr` | ✅ | 直接取(可 `KATANA_OMIT_BODY=1` 关) |
| `web_hash_urls.body` (BLOB) | ❌ 旧版无 | ✅ 默认落库 | 默认 `KATANA_OMIT_BODY=0` → zlib(level=6) 入库;`KATANA_OMIT_BODY=1` 时落 NULL;供 `url_diff` 跑 8 步算法 |
| `location` | ✅ | ⚠️ `headers["location"]` | headers 兜底 |
| `response_hash` | ✅ mmh3 | ❌ | **必算** `mmh3.hash128(body)`(algo_diff 依赖) |
| `time` | ✅ | ✅ 顶层 `timestamp` | 直接取 |
| `input` | ✅ | ❌ | 用 katana `request.endpoint` 当 input |

**核心约束**:`response_hash` **必须算** — algo_diff 8 步算法依赖它做 simhash/MMH3 字段屏蔽,
缺这字段 = 这条记录对 algo_diff 是死的。

---

## 四、部署

```bash
cd modules/public/katana
bash install.sh              # go install katana + 写 config example + 自检
bash install.sh --check      # 只查依赖
bash install.sh --uninstall  # 不删二进制(由 go 环境管),只删 config + cache
```

需要:Go ≥ 1.22(`go version` 自检)、Python ≥ 3.10、`bash` ≥ 4。

---

## 五、客户端使用

### 手动调 katana enrich

```bash
# 等价 srcradar dispatcher
./srcradar katana enrich -b ExampleCo

# 或直接
python3 modules/public/katana/lib/katana_runner.py \
    --business ExampleCo \
    --db db/recon.sqlite3 \
    --hosts-file <(echo "example.com")
```

### daily-url stage(默认引擎)

```bash
# 默认行为:走 katana
./srcradar daily daily_monitor -type daily-url

# 强制走 URLFinder(老行为,fallback / 中文敏感信息)
DAILY_URL_ENGINE=urlfinder ./srcradar daily daily_monitor -type daily-url

# auto:katana 失败时 fallback URLFinder(best-effort)
DAILY_URL_ENGINE=auto ./srcradar daily daily_monitor -type daily-url
```

### dashboard 手动按钮

`127.0.0.1:8765/<biz>/katana-enrich` — 跟现有 `scan-urls` 按钮平级。

---

## 六、运维

| 现象 | 排查 |
|---|---|
| `install.sh --check` 报 `缺 katana` | `go install github.com/projectdiscovery/katana/v2/cmd/katana@latest` |
| daily-url 跑完但 web_hash_urls 没新增 | 查 `web_subdomain_scan_schedule` 是否有 `enabled=1` 行 |
| `is_dangerous=1` 误伤只读 URL | 改 `lib/risks.py:RISKS` 收紧(默认 5 词) |
| katana 启动慢 | 加 `-crawl-duration 300 -timeout 10` 上限 |
| response_hash 全空 | body 被 `-ob` 关掉了,确认没设 `KATANA_OMIT_BODY=1` |
| `web_hash_urls.body` 全 NULL | 跟 `response_hash 全空` 同根因;`url_diff` 阶段会跳过无 body 行(ffuf / urlfinder / gau 老路径按用户拍板维持 body=NULL) |

---

## 七、文件说明

| 文件 | 行数 | 职责 |
|---|---|---|
| `install.sh` | ~120 | 装 katana 二进制 + 写 example conf + 自检 |
| `katana_enrich.sh` | ~80 | CLI 入口(daily-url stage 调用) |
| `config/katana.conf.example` | ~30 | 占位模板(operator 自填真值后另存为 `katana.conf`,`.gitignore` 屏蔽) |
| `lib/__init__.py` | ~1 | 空,让 `lib/` 成为 package |
| `lib/risks.py` | ~40 | 5 词黑名单 + `is_dangerous_url()` + 单测入口 |
| `lib/katana_runner.py` | ~250 | subprocess 调 katana + parse JSONL + 风险 gate |
| `lib/schema_map.py` | ~120 | katana JSON dict → web_hash_urls INSERT 参数(字段对齐 + 缺字段补算) |
