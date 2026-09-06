# db_align

> **模块定位**:enscan 插件,**可选用**。负责把"业务名" → 法律实体图谱(企业 / 子公司 / 资产),与上游 [`ENScan_GO`](../ENScan_GO) 协同。

## 导航

- **本文档**:Pipeline / §运行方式 / §Quick start / §Flags / §Logging / §Data model touched
- **安装**:执行主仓库根 `./install.sh --enscan-only`(标 LOCKED tag,需用户授权);详见 [`install.sh`](../install.sh) 头部注释
- **运维(Ops)**:见 §运维一节,涵盖 cookie 失效自愈、缓存清理、长跑监控建议
- **与 pdtm/ymicp 关系**(可选,无硬依赖):
  - 写表:`companies`、`mapp_records`(部分)、`scopes`(用 `-scope` flag)
  - 与 `pdtm`:不互斥。`scopes` 表有 **3 条独立写入路径**:`db_align -scope` / `pdtm/finalize_scope` / `manage/scope_import.sh`,任一即可
  - 与 `ymicp`:`db_align` 拉回法律实体(企业图谱),`ymicp` 反查小程序备案;两者**数据互补**,**流程上无先后硬依赖**
- **跳过它的场景**:不需要法律实体反查时,完全跳过;`pdtm` / `daily` / `ymicp` 仍可独立工作

`db_align` is a thin orchestrator on top of [ENScan_GO](../ENScan_GO) that
turns a free-form business name (e.g. `ExampleCo`) into a populated
`recon.sqlite3`: legal entity, holding tree, and per-entity asset records
(ICP, app, WeChat mini-program, ...). The output schema matches the
existing recon platform's tables so downstream tools (port scanners, domain
fuzzers, web crawlers) can pick it up.

## Pipeline

```
   business name (业务名)
        │   resolve  (resolver)
        ▼
   legal entity (unit_name, pid)
        │   walk invest / branch / holds  (crawler, 51%+ threshold, no depth cap)
        ▼
   holding tree  →  companies table
        │   for each company: opt-in asset reverse-lookup  (crawler)
        ▼
   mapp_records table
        │
        ├─→  mapper  →  companies.main_licence  (backfill)
        └─→  scope   →  scopes.asset            (-scope flag)
```

## 运行方式:半自动或全自动

`db_align` 阶段的多个子步骤，**整体无法 100% 端到端自动化**。需要人/AI 半自动介入的环节：

- **候选 PID 消歧** — AQC 一次返回多个疑似实体时，需人/AI 用 `-broad` 或 `-pid "name:pid"` 显式确认走哪个分支
- **cookie 失效恢复** — 数据源 cookie 过期后，需人在浏览器重新登录，然后从项目根 `cookie.log` 删除对应行后继续
- **陈旧缓存清理** — `../ENScan_GO/enscan.gob` 在"上次成功 → 这次失败"且无 cookie 信号时，需人/AI `rm -f` 后重跑
- **长跑监控** — `-all -scope -delay 2` 类长跑，推荐在 `run_in_background` + 周期轮询日志的方式下进行

如不接受这些半自动环节（例如不愿在公共环境输入凭据），这一阶段直接跳过即可 — 本仓库的 `pdtm`、`daily` 子模块不依赖 `db_align`。主 README 同样位置有更宽的上下文说明（§"运行方式"）。

## Quick start

```bash
# Build
cd /opt/srcradar/db_align
go build -o ./bin/db_align ./cmd/run

# Default: holding tree only (no asset collection)
./bin/db_align -n ExampleCo

# Add specific assets
./bin/db_align -n ExampleCo -icp -app -wx-app

# All asset sections
./bin/db_align -n ExampleCo -all

# Wide-mode disambiguation (Top-5 candidates + each walked independently)
./bin/db_align -n ExampleCo -broad -icp

# Multiple ENScan sources
./bin/db_align -n ExampleCo -type aqc,kc -icp -app

# Don't run keyword variants per company
./bin/db_align -n ExampleCo -no-permute -icp

# Extract main domains → scopes
./bin/db_align -n ExampleCo -icp -scope

# Custom db / enscan binary / delay (anti-ban)
./bin/db_align -n ExampleCo -db /tmp/recon.db -enscan /opt/ENScan -delay 3

# Bypass resolver (use known PID; e.g. when AQC returns only the group parent)
./bin/db_align -n ExampleCo -pid "ExampleCo子公司有限公司:00000000000000" -icp -app

# Cap tree depth (avoid blowing up on large groups)
./bin/db_align -n DemoCorp -pid "DemoCorp集团控股有限公司:00000000000000" -icp -max-depth 2
```

## Flags

| Flag                       | Default                  | Notes                                              |
| -------------------------- | ------------------------ | -------------------------------------------------- |
| `-n`                       | (required)               | 业务名                                              |
| `-db`                      | `../db/recon.sqlite3`    | sqlite path (or `$RECON_DB`)                       |
| `-enscan`                  | `../ENScan_GO/ENScan`    | binary path (or `$ENSCAN_BIN`)                     |
| `-type`                    | `aqc,tyc,rb,qimai`       | ENScan sources; multi-source fan-out spreads load and dodges AQC anti-bot |
| `-invest`                  | `51`                     | 控股比例下限 %; 0 = no filter                       |
| `-broad`                   | off                      | 宽进消歧: Top-5 candidates                         |
| `-pid`                     | empty                    | 跳过 resolver；`name:pid` 格式（多个用逗号分隔）  |
| `-max-depth`               | `0`                      | 树爬最大深度（0=不限）                             |
| `-tree-retry`              | `1`                      | 树爬瞬时错误重试次数                               |
| `-all`                     | off                      | enable every opt-in asset section                  |
| `-icp` `-app` `-wechat` `-wx-app` `-weibo` `-copyright` `-supplier` `-job` `-partner` | off | one section per flag            |
| `-no-permute`              | off                      | don't run keyword variants per company             |
| `-scope`                   | off                      | extract main domains → `scopes`                    |
| `-backfill-main-licence`   | on                       | derive `companies.main_licence` from mapp_records  |
| `-delay`                   | `0`                      | per-enscan-call delay in seconds                   |
| `-proxy`                   | （空）              | passed to enscan as `-proxy` (默认空 = 不传；如需代理显式传入) |
| `-log-file`                | `./logs/db_align_<时间>.log` | 落盘日志路径（含时间戳）；传 `-log-file ""` 关闭 |
| `-timeout`                 | `300`                    | per-call timeout in seconds                        |

## 运维(Ops)

把日常排错和自愈清单集中到这里。完整的人工/AI 半自动决策流程见文档头部"导航"链接的主 README + `db_align/CLAUDE.md`。

### Cookie 失效自愈

数据源(`aqc/tyc/rb/qimai`)的 cookie 会随登录态过期,长跑需要自愈:

- 监控关键字:日志里出现 `未登录` / `登录已过期` / `Cookie expired` / `aqc|tyc|rb|qimai 401|403|empty result`,**至少看到 2 处一致信号**再认定失效
- 失效后追加一行到项目根 `cookie.log`:`<时间戳> [<源>] <原因>; 上次成功 run: <日志文件名>`
- 跨 run 生效:下次启动从 `-type` 默认值里把已失效源剔除再开跑
- **所有源失效 → FATAL 停止**,不要换代理或重试(cookie 不会自愈)
- 复位:用户在浏览器重新登录后,**手动**从 `cookie.log` 删除对应行

### 缓存清理

陈旧的 `../ENScan_GO/enscan.gob` 可能造成"明明 cookie 已恢复却仍是空结果":

```bash
rm -f ../ENScan_GO/enscan.gob    # 仅在"上次成功 → 这次失败"且无 cookie 信号时清
```

### 长跑监控建议

`-all -scope -delay 2` 类长跑:

- 推荐 `run_in_background` + 周期轮询日志
- 阶段进度:日志里的 `resolve / tree / assets / backfill` 四阶段
- 不要塞前台阻塞会话

### 跳过路径(不想用 db_align 时)

- 跳过整个 enscan:主 README §Quick start `./install.sh --no-enscan`
- 跳过 db_align 的某个 `-type`:用 `set_config.sh --enscan 0`(对应 `recon_business_config.enscan`)
- 完全跳过:`./install.sh --no-enscan` 后,`pdtm` 仍可用,但 `scopes` 表只走 `pdtm/finalize_scope` 或 `manage/scope_import.sh` 两条路径

## Logging

每次运行默认同时输出到 **stderr** 和一个**带时间戳的日志文件**，便于事后排错。

**默认行为**

- 路径：`./logs/db_align_YYYYMMDD_HHMMSS.log`（每次启动独立文件，不会覆盖上次）
- 目录自动创建（`./logs/` 不存在则 `mkdir -p`）
- 启动时会在 stderr 打印一行 `log file: <路径>` 让你立刻知道写到哪儿
- 每行格式：`2026/07/26 12:34:56 [db_align] message` —— `LstdFlags` 自带日期+时间秒级时间戳
- stderr 和文件通过 `io.MultiWriter` 同时写入，项目内 30+ 处 `log.Printf/Fatalf` 调用点零改动

**典型用法**

```bash
# 默认：日志落到 ./logs/db_align_20260726_173900.log
db_align -n ExampleCo -all

# 自定义日志路径
db_align -n ExampleCo -log-file /var/log/db_align/run.log

# 仅终端输出（关闭文件日志）
db_align -n ExampleCo -log-file ""
```

**文件打不开怎么办**

只读文件系统 / 权限不足时，程序不会失败，而是降级为 stderr-only，并在 stderr 打印一行 warning：

```
[db_align] warning: cannot open log file "./logs/db_align_...log": ... (continuing with stderr only)
```

**查错误**

日志文件保留每次运行的全部输出，含 ENScan 子进程 stderr（cookie 过期、未登录等关键字），常用 grep：

```bash
# 抓所有错误关键字
grep -E "error|skipped|Fatal|failed|未登录|cookie|登录已过期" logs/db_align_*.log

# 只看 ENScan 子进程 stderr
grep "stderr:" logs/db_align_*.log

# 看阶段进度（resolve / tree / assets / backfill）
grep -E "step [0-9]/4" logs/db_align_*.log

# 列最近几次运行的日志
ls -lt logs/ | head -5
```

## Data model touched

| Table              | Operation  | Notes                                                  |
| ------------------ | ---------- | ------------------------------------------------------ |
| `businesses`       | upsert     | one row per `-n`                                       |
| `companies`        | upsert     | one row per (business, unit_name); `main_licence` filled by backfill |
| `mapp_records`     | upsert     | one row per (company, service_licence) or (company, service_name, service_type); APP/WeChat/微博 sections have no ICP licence, so the runner synthesises a `synth:<company_id>:<service_type>:<service_name>` placeholder to satisfy the schema's `NOT NULL UNIQUE` constraint — the logical identity remains `(company_id, service_name, service_type)` |
| `scopes`           | upsert     | one row per (business, asset); only when `-scope`      |
| `service_type_map` | insert     | one row per observed `service_type` int                |

The runner does **not** touch `web_subdomains`, `web_hashes`, `tcp_assets`
or `permutation_state` — those are owned by sibling recon tools and are
fed by `scopes.asset`.

## Tests

```bash
go test ./...            # resolver + permute + scope unit tests (no network)
```

The end-to-end smoke test is documented under "Smoke test" below; it
requires a working ENScan binary and may trigger AQC anti-bot if overused.

## Smoke test

1. Make sure ENScan is built and `~/.claude/config.yaml` has the `aiqicha`
   cookie set (see `../ENScan_GO/README.md`).
2. Pick a business whose name resolves cleanly, e.g. `scanme` (the existing
   test data uses a placeholder Chinese legal entity for `ExampleCo`).
3. Run:

   ```bash
   ./bin/db_align -n ExampleCo -all -scope -delay 2
   ```

4. Verify in sqlite:

   ```bash
   sqlite3 ../db/recon.sqlite3 \
     "SELECT id, unit_name, main_licence FROM companies WHERE business_id = 1"
   sqlite3 ../db/recon.sqlite3 \
     "SELECT id, company_id, service_name, service_licence, service_type, domain
      FROM mapp_records WHERE company_id IN (SELECT id FROM companies WHERE business_id = 1)"
   sqlite3 ../db/recon.sqlite3 \
     "SELECT id, business_id, asset, is_wildcard FROM scopes WHERE business_id = 1"
   ```

## Layout

```
db_align/
  go.mod
  README.md
  cmd/
    run/
      main.go        # CLI entry + pipeline orchestration
      flags.go       # asset-section flag table
  internal/
    store/           # sqlite + schema + upserts
      store.go
      schema.sql
    enscan/          # subprocess wrapper + JSON parser
      enscan.go
      io.go
    resolver/        # business name → legal entity
      resolver.go
      resolver_test.go
    crawler/         # holding-tree walker + asset reverse-lookup
      crawler.go
    permute/         # keyword variant generator
      permute.go
      permute_test.go
    mapper/          # backfill companies.main_licence
      mapper.go
    scope/           # main-domain extraction
      scope.go
      scope_test.go
```

## Caveats

- The `service_type` integers (`6/7/4/5/8/9/10/11` in `enscan.SectionToDBType`)
  are placeholders until the upstream AQC mapping is confirmed against a
  real icpinfoAjax response. The runner inserts the raw integer into
  `service_type_map` with a `type_<N>` label on first sight so the
  inventory builds up automatically.
- The tree walker uses a process-local `seen` set keyed by PID; restarting
  the runner on a large tree will re-walk from each seed. For HW
  engagements this is acceptable; for long-running batch jobs consider
  persisting the seen set to the database.
- `--broad` mode multiplies the number of ENScan calls; combined with
  `-all -scope` the per-business cost can be 200+ subprocess invocations.
  Always pass `-delay` (≥2s) in that mode to avoid AQC rate limits.
- **Strict mode rejects weak resolver matches.** When AQC only returns the
  group parent (e.g. `ExampleCo集团控股有限公司` for `ExampleCo`) the resolver
  flags it as a weak match (score < `MinAcceptScore`) and returns an error
  in strict mode. Rerun with `-broad` to inspect every candidate, or use
  `-pid "name:pid"` to bypass resolver and seed the correct entity
  directly. `-broad` against a 1-candidate AQC response is a no-op and
  the runner prints a "broad had no effect" note.
- **mapp_records.service_licence placeholder.** The schema (owned by
  `ymicp/icp_mapp_query.py`) is `service_licence TEXT NOT NULL UNIQUE`.
  APP / 微信小程序 / 微博 sections don't carry an ICP licence, so without
  a placeholder the second empty-licence insert under the same company
  would fail UNIQUE and be dropped. The runner synthesises
  `synth:<company_id>:<service_type>:<service_name>` which is
  deterministic per (company, section, name) — re-runs upsert instead
  of inserting duplicates. Logical identity for these sections is still
  `(company_id, service_name, service_type)`; treat `service_licence` as
  opaque when it starts with `synth:`.

## Design notes

These are the design decisions that came out of a manual ENScan run on
2026-07-24 against a real holding group (operating subsidiary + group parent).
Read this before assuming the tool will do something it doesn't.

### What the "holding tree" actually contains

AQC's per-company endpoint (the one ENScan wraps) returns these sections
(anonymised counts from the smoke test):

| Section      | Operating sub | Group parent |
|--------------|--------------:|-------------:|
| `branch`     | 146           | 50           |
| `invest`     | 0 (null)      | 0 (null)     |
| `holds`      | 0 (null)      | 0 (null)     |
| `partner`    | 2 (自然人)    | —            |
| `icp`        | 9             | 18           |
| `app/wechat` | 1/—           | 32/20        |

So for a real company:

- **`branch` is the only reliable downward signal.** It contains
  分公司 (regional offices), not separate legal entities. For SRC, all
  branches share the same SRC, so writing them is correct but verbose
  (one parent + N branches is the typical output).
- **`invest` and `holds` are null** for typical companies. The
  "企业图谱" / 控股 graph is a separate paid AQC feature that ENScan
  does not use. The runner therefore queries only `branch` in the
  default tree walk (`HoldingTreeSections` = `[SecInvest, SecBranch]`,
  `holds` excluded). Querying `holds` would only burn an HTTP round
  trip.
- **`partner` is natural persons, not companies.** AQC exposes
  individual shareholders (e.g. founder 17.122%), not corporate
  shareholders. There is no API path to walk "company X's parent
  company" — the only way to find the corporate parent is via a
  different data source (TYC 集团穿透, 启信宝 图谱) or manual lookup.
- **集团穿透 (group traversal) is therefore out of scope for AQC.**
  Running `db_align -n DemoCorp` will land on the operating subsidiary
  and write its branches; it will NOT walk to other DemoCorp-group
  subsidiaries because AQC's per-company endpoint doesn't expose that
  relationship. This is an AQC limitation, not a tool bug.

### Resolver matching: 数字 ↔ 汉字

User input like 12 must match AQC's 一二. The resolver tries the input as-typed AND the digits-to-Chinese form (1–99, etc.) and picks the best score. The conversion is exported from internal/permute.DigitsToChinese and is also reused by the permute package for keyword-variant generation.

This means 12 ↔ 一二 (full prefix, score 200) and TestBiz ↔ TestBiz科技有限责任公司 (full prefix, score 200) both resolve correctly.

### Business / SRC boundary

The `businesses` table is the SRC table: one row per security
response scope. Running `db_align -n ExampleCo` lands on the
operating subsidiary (e.g. `ExampleCo子公司有限公司`) and writes its branches.
AQC's per-company endpoint does not walk to the corporate parent
so the boundary is naturally respected. If you later switch
to a data source that DOES expose upward graph (e.g. TYC), revisit
this — the `crawler.RunTree` cycle guard is a PID-based seen set, which
only prevents infinite loops, not deliberate upward walks.

### Why a per-business `business_id`?

The 1:N from `businesses.id` to `companies.business_id` is what keeps
"ExampleCo's assets" separate from "DemoCorp's assets" in the same db.
`mapp_records.company_id` then points at the specific legal entity.
The SRC routing decision is left to the consumer — the tool records
the legal graph faithfully and does not assign a `src_platform`
column. Add a sidecar table or a config file if you need SRC-aware
filtering at query time.
