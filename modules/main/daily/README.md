# daily/ — SRC 资产监控日报

每日 03:00 自动跑 `pdtm/pipeline.sh`（+ 可选的 `db_align` / `ymicp`），与上次快照对比后落盘生成增量报告（不上发邮件 / webhook）。

## 三个项目（`-type` 选择）

通过 `-type <pdtm[,icp[,enscan]]>` 单参数选本次要跑的阶段，逗号分隔（默认 `pdtm`）：

| `-type` 值 | 项目 | 路径 | 用途 |
|---|---|---|---|
| `pdtm`   | pdtm  | `../pdtm/pipeline.sh`        | 子域 / 端口 / 指纹扫描 |
| `icp`    | ymicp | `../ymicp/icp_mapp_query.py` | 小程序 / 公众号 备案刷新 |
| `enscan` | db_align | `../db_align/bin/db_align`  | 主体 / 子公司 / 资产 拉新（含 `-scope`） |

**`-type` 只能写一个**，多个 `-type` 会被拒（typo guard）。例如 `-type pdtm -type icp` 报"only takes one value; use comma-separated form like -type pdtm,icp"。

阶段是**按固定顺序**跑的（不看你写 `-type` 的顺序）：
`enscan` → `pdtm` → `icp`。
**顺序固定（启用 enscan 时）**：enscan 先写 `scopes`，pdtm 才能读 scope 作 target。
空 scope 时 pdtm 会直接退出 1（pipeline.sh:91-95），所以颠倒顺序会让第一次跑 pdtm 直接失败。
`icp` 跑最后 —— 它只读 `companies` 表，不依赖其他阶段的输出。

## 目录结构

```
daily/
├── daily_monitor.sh            # cron 调用入口（顶层编排）
├── run_one_business.sh         # 单业务：enscan → pdtm → icp（按固定顺序）
├── install_cron.sh             # install (无参) / uninstall
├── lib/
│   ├── snapshot.py             # 全量快照 6 张表到 JSON
│   ├── diff.py                 # 对比 + 输出 CSV / summary.md
│   ├── dashboard.py            # ARL 4-tab 本地可视化（仅绑 127.0.0.1）
│   └── log.py                  # stderr + log 文件双写
├── snapshots/                  # previous.json + 本轮快照
├── reports/<run-id>/           # 本轮报告
└── logs/<run-id>.log           # 本轮日志
```

## 一次完整运行的产物

`reports/20260725-030000/`：

```
summary.md                       # 总览（默认打开这个）
added_scopes.csv                 # 新增 scope
added_mapp_records.csv           # 新增 ICP / APP / 小程序
added_web_subdomains.csv         # 新增 Web 资产
added_tcp_assets.csv             # 新增 TCP 端口
added_companies.csv              # 新增主体
added_web_hashes.csv             # 新增指纹
reactivated_web_subdomains.csv   # 失而复得
reactivated_tcp_assets.csv
deactivated_web_subdomains.csv   # 上一轮在线、本轮失活
deactivated_tcp_assets.csv
changed_companies.csv            # 主体字段变了（main_licence / nature_name）
changed_mapp_records.csv         # 资产字段变了
changed_web_hashes.csv           # subdomain_count 等变了
changed_web_subdomains.csv       # status / title / tech / response_hash 变了
changed_tcp_assets.csv
deleted_<table>.csv              # 物理删除（基本不会有）
full_snapshot.json               # = after.json 副本，方便离线看
```

只有当本类有数据时才生成对应 CSV；空类别不会留空文件。

## 安装 / 卸载 cron

```bash
# 装（无参，cron 跑 pdtm+icp，业务级开关见 `recon_business_config` 表）
./install_cron.sh

# 验证
crontab -l | grep daily_monitor

# 卸
./install_cron.sh uninstall
```

cron 行写死 `-type pdtm,icp` —— 哪些业务实际触发哪些阶段，由 `recon_business_config` 表按业务决定：

| 字段 | 控哪个阶段 | 默认 |
|---|---|---|
| `enabled` | 整业务开关（0 = cron 跳过该业务） | 1 |
| `web` | pdtm 阶段（子域/域名扫描） | 1 |
| `tcp` | pdtm 阶段（端口扫描，pdtm 共享同 stage） | 0 |
| `icp` | ymicp 阶段（小程序/公众号 备案刷新） | 1 |

pdtm 阶段由 `web OR tcp` 任一为 1 触发；缺 config 行的业务按全 0 处理（opt-in）。

**enscan 不在 cron 里** —— 是 `db_align` 数据拉新阶段（手动 `run_one_business.sh -type enscan 业务名` 触发），未走 config gate，cron 也不会自动跑。

**daily-url 同样不在 cron 行里**（用户 2026-08-26 拍板）—— 它读 `web_subdomain_scan_schedule` 跑手动标记的子域，
默认 cron 不跑。需要时手动 `./daily_monitor.sh -type daily-url` 或 `crontab -e` 加独立行。
详见 §"每日 URL 扫描"。

`flock -n` 锁确保手动触发 + cron 不会并发；`.lock` 文件路径固定。
重装时 `install` 会替换同 marker 的旧 cron 行（幂等）。

## 手动运行

```bash
# 默认（仅 pdtm）
./daily_monitor.sh

# 多阶段（逗号分隔）
./daily_monitor.sh -type pdtm,icp
./daily_monitor.sh -type pdtm,icp,enscan

# 不跑任何阶段（只做快照 + diff，适合排查）
./daily_monitor.sh dryrun

# 单独跑某个业务
./run_one_business.sh ExampleCo
./run_one_business.sh -type icp ExampleCo
./run_one_business.sh -type pdtm,enscan ExampleCo

# 单点 httpx 扫描 + 入库（跳过 dnsx/cdnmatch/naabu/icp，不动同业务 is_active=1 行）
cat > /tmp/api_hosts.txt <<EOF
api.example.com
api2.example.com
EOF
./run_one_business.sh -onesite /tmp/api_hosts.txt ExampleCo
```

### 单点扫描模式 (`-onesite`)

绕过 `pdtm/pipeline.sh` 的 dnsx + cdnmatch + naabu + scanner 整套前置，直接 subprocess 跑 `httpx` 写 `web_hashes + web_subdomains`：

- **不**触发 `persist()` 里的 `UPDATE ... SET is_active=0` 全清场；只新增 / 刷新传入主机对应的行，同业务下其它 `is_active=1` 行纹丝不动
- **不**写 `scopes`、`tcp_assets`、`companies`、`mapp_records` 等其它表
- 适合"加一个新域名验证一下 / 重扫少数几个看 httpx 怎么响"的对点场景
- 与 `-type` 互斥；与 cron 全量 baseline 互不影响

`hosts.txt` 每行一个域名，`#` 开头为注释；上限 200 行；只接受 FQDN 形式（不含 `http://`）。

### Dashboard 表单

`/<业务名>` 页面（默认打开的"任务状态" tab 顶部）有一个折叠面板 **「扫描并入库」**：

- 点击展开 → 文本框 + 「扫描并入库」按钮
- 浏览器 POST 到 `/<业务名>/scan`（`application/x-www-form-urlencoded`，字段 `hosts=<newline-separated>`）
- dashboard 同步等待（典型 5-30s）；返回一张小结果页含「返回 /<业务名>」链接
- 成功后 dashboard 自动 reload，下一次访问业务页就能看到新数据
- 与上述 `-onesite` CLI 走同一条后端（dashboard subprocess 调 `import_scan_results.py scan-onesite`），语义、退出码、不动 `is_active=1` 行的承诺完全一致

可手动用 `curl` 复现同一请求（用于调试 / 自动化）：

```bash
curl -si -X POST -d $'hosts=baidu.com\nqq.com' \
  http://127.0.0.1:8765/ExampleCo/scan
# 200 OK，HTML 含「扫描完成 · 2 条写入」
```

### 退出码（位掩码）

`run_one_business.sh` 用 4 位 bit-mask 报告每阶段的结果（用户 2026-08-26 加 daily-url）：

| 位 | 值 | 含义 |
|---|---|---|
| bit0 | 1 | pdtm 失败 |
| bit1 | 2 | enscan (db_align) 失败 |
| bit2 | 4 | icp (ymicp) 失败 |
| bit3 | 8 | daily-url 失败 |

合起来：`0` = 全部 ok，`3` = pdtm+enscan 失败，`5` = pdtm+icp 失败，`7` = 三个全失败，`15` = 四个全失败。
未请求的阶段不出现在掩码里。`daily_monitor.sh` 把每个失败阶段写到 `summary.md` 的 "业务运行告警" 段落。

## 监控范围

`businesses` 表里 `TRIM(business_name) != ''` 的所有业务。当前：
- `ExampleCo`

跑 `enscan` (db_align) 时会自动调 AQC，可能需要 cookie / proxy；详见 `../db_align/README.md`。

## 监控表

| 表 | 逻辑键 (identity) | 内容字段（判断 changed） |
|---|---|---|
| scopes | (business_id, scope_name, asset) | is_wildcard |
| companies | (business_id, unit_name) | nature_name, main_licence |
| mapp_records | (company_id, service_licence) 或 (company_id, service_name, service_type) | domain, content_type_name, record_updated_at |
| web_hashes | (business_id, response_hash) | subdomain_count |
| web_subdomains | **(business_id, subdomain, port)** ← 逻辑键，不用 hash_id | status_code, title, technologies, response_hash |
| tcp_assets | (business_id, host, port) | hosts, raw_value |
| **web_hash_urls** | **(business_id, subdomain, url, source)** | status_code, content_length, word_count, title, redirect, link_source, risk_flag, is_dangerous, content_type |

`web_subdomains` 用逻辑键 `(business_id, subdomain, port)` 而非自然键
`(hash_id, subdomain, port)`：因为 `import_scan_results.py` 按响应指纹分组，
hash_id 每次跑都可能漂移。
`web_hash_urls` 同样按逻辑键 `(business_id, subdomain, url, source)` —— 与
`UNIQUE` 约束一致，便于按 source 拆分历史。

每张表还区分 is_active 翻转：

- `added`      新出现且 active=1
- `reactivated` 之前 active=0，本轮 active=1
- `deactivated` 之前 active=1，本轮 active=0
- `changed`    一直 active，但内容字段变了
- `deleted`    物理消失（极少见）

时间戳字段（`fetched_at` / `last_seen` / `updated_at` / `first_seen` /
`created_at` / `raw_json`）每次跑都动，不算 changed。

`web_hash_urls` 的 change 判定有一处特殊：**仅 `is_static=0` 行（path 最后一个
`.` 之后不等于 `js` / `css`）参与 change_type 标记**。触发器 `trg_whu_au`
在 WHEN 子句里 `AND NEW.is_static = 0`，确保静态行永远不会被标 changed /
reactivated。详见 §"URL 资产扫描" → "is_static 语义"。

## 可视化 Dashboard

`lib/dashboard.py` 仿 ARL 灯塔的 4 tab 布局，**直接读 SQLite 数据库**渲染单文件 HTML。**纯本地**，**只绑 127.0.0.1**，零外部调用。

### 服务管理（systemd --user，用户 2026-08-28 拍板）

dashboard 由 `systemd --user` 接管，**不再**用 `*/5 * * * * dashboard_watchdog.sh` 的 cron watchdog 兜底
（cron 行已移除）。systemd unit 文件：

```
~/.config/systemd/user/dashboard.service    (源文件)
~/.config/systemd/user/default.target.wants/dashboard.service  (已 enable)
```

unit 关键配置：

| 字段 | 值 | 说明 |
|---|---|---|
| `Type` | `simple` | 直接前台跑 `python3 lib/dashboard.py` |
| `Restart` | `on-failure` | 异常退出自动拉起 |
| `RestartSec` | `30` | 避免 OOM kill 后 socket TIME_WAIT 的 bind 竞态（errno 98） |
| `MemoryMax` | `3G` | 稳态 ~800M，reload 峰值 1.5G；超 3G 视为泄漏 → 重启 |
| `DASHBOARD_RELOAD` | `300` | env 注入 dashboard.py，5 分钟 reload |
| `StandardOutput/Error` | `append:~/.../logs/dashboard_service.log` | stdout/stderr 都进 service log |

日常操作：

```bash
systemctl --user status dashboard        # 当前 PID / 状态 / 内存
systemctl --user restart dashboard       # 改 lib/*.py 后手动重启
systemctl --user stop dashboard          # 维护停服
journalctl --user -u dashboard -n 50    # 查日志（取代直接 tail log 文件）
```

首次启用（重建环境时跑一次）：

```bash
# 1. 确认 unit 文件 + enable 软链都在
ls ~/.config/systemd/user/dashboard.service ~/.config/systemd/user/default.target.wants/dashboard.service

# 2. reload + enable + start
systemctl --user daemon-reload
systemctl --user enable dashboard
systemctl --user start dashboard

# 3. 验证
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/health   # 应 200
```

> ⚠ **unit 真实名字是 `dashboard.service`**，不是 `recon-dashboard.service`。
> `systemctl --user status recon-dashboard` 会 "Unit could not be found"。

### 快速开始（手动起服，调试 / 不走 systemd 时）

```bash
# 起服（默认 127.0.0.1:8765，直接读 SQLite 数据库）
python3 lib/dashboard.py
```

要从本机访问：

```bash
ssh -L 8765:127.0.0.1:8765 user@recon-host
# 浏览器开 http://localhost:8765
```

后台跑（关掉 SSH 也不断）：

```bash
ssh -fN -L 8765:127.0.0.1:8765 user@recon-host
```

多用户各自开隧道，本机端口可以不同（如你 8765、队友 8766，都映射到 recon 的 8765）。

### CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port N` | `8765` | 监听端口 |
| `--host ADDR` | `127.0.0.1` | **保持 127.0.0.1**；改成 `0.0.0.0` 会让 recon 数据裸奔，启动时会 WARN |

### 端点

| 路径 | 用途 |
|---|---|
| `GET /` | HTML 主页（每 `reload_interval` 秒自动重渲，或 `GET /refresh` 立即刷新）|
| `GET /health` | `200 ok` |
| `GET /api/snapshot` | 原 JSON 快照（供脚本 / curl 取数）|

无 POST/PUT/DELETE，纯只读。

### 4 个 tab 详解

#### 任务状态

```
┌── 卡片 ─────────────────────────────────────┐
│ 业务 · Scope · ICP/APP · Web 子域 · TCP 端口 │
└─────────────────────────────────────────────┘
┌── 左：Web 资产分析 ─────┐ ┌── 右：TCP 端口分类 ──┐
│ · 状态码分布（绿条）     │ │ ⚠ 80/443 实际是 web │
│ · 技术栈 Top 10（紫条）  │ │ 类别+占比 表格      │
│ · 注册根域（蓝条）       │ │                     │
│ · 指纹浓度桶（4 色桶）    │ │                     │
└─────────────────────────┘ └─────────────────────┘
```

**指纹浓度桶**颜色：🟢 unique (1 子域) / 🔵 2-10 / 🟡 11-50 / 🔴 ≥51。
**口径**：覆盖越广价值越低 — `≥51` 多为泛解析/默认页/Wildcard cert。

**TCP 端口分类**旁的 ⚠ 注：表中 `web` 类别（80/443 等）实际已在 `web_subdomains` 落库，本表保留用于交叉验证，做纯 TCP 分析时建议剔除。

#### 站点详情

所有 `web_subdomains` 平铺显示，2269 行 × 8 列。

**URL 列**按端口拼接 scheme：

| 端口 | URL 格式 |
|---|---|
| 80 | `http://subdomain/` |
| 443 | `https://subdomain/` |
| 其它 | `http://subdomain:port/` |

每行 URL 是 `<a target="_blank">`，点击新窗口打开。

**Hash 列**折叠：`<a class="hash-link">hash</a><span>(count)</span>`，count 在 `<a>` 标签外；
多个子域共享同一 hash 时，每行都显示该 hash，count 提示合并数量。
**点击 hash** 弹窗（按 Esc / 点 × / 点遮罩关闭），列出该 hash 下全部子域。

**行背景色**按 `status_code`：

| 状态码 | 颜色 |
|---|---|
| 5xx | 🔴 红 (high) |
| 4xx | 🟡 黄 (med) |
| 3xx | ⚪ 灰 (info) |
| 2xx | 🟢 绿 (low) |
| None | ⚪ 灰 (info) |

**3 个筛选开关**（搜索框右侧，可叠加）：

| 开关 | 行为 |
|---|---|
| 隐藏 http | 隐藏 `port=80` 行（只保留 https）|
| 仅 unique | 只显示 `hash_count=1` 的行（即每个指纹只对应 1 个子域）|
| 中文优先 | 可见行重排，title 含中文字符的行排到前面 |

**搜索**：URL / title / tech / hash 子串匹配（不区分大小写）。

**列头点击**：切升降序（▲ / ▼），数字列按数值，文本列按 `localeCompare('zh-Hans-CN')`。

#### 端口服务

所有 `tcp_assets` 平铺显示，2797 行 × 7 列。列序：

```
业务 · port · host · ip · category · service · risk
```

**`port` 自动打标**：

| 类别 | 典型端口 | 风险 |
|---|---|---|
| web | 80/443/8080/8443/8000-8999 | 🟢 低 |
| db | 3306/5432/27017/6379/1433/9200/11211/... | 🔴 高 |
| remote | 22 (SSH) / 23 (Telnet) / 3389 (RDP) / 5900-5901 (VNC) | SSH🟡 中，其余🔴 高 |
| mail | 25/110/143/465/587/993/995 | 🟡 中 |
| snmp | 161/162 | 🟡 中 |
| tftp | 69 | 🟡 中 |
| high-port | > 10000 且未识别 | 🟡 中 |
| other | 其它未识别 | ⚪ 提示 |

行首左侧色条按 risk 上色。**搜索**：host / ip / service / category 子串匹配。
**列头点击**：升降序切换（port 数值，其余文本；与站点详情 tab 共用同一套排序逻辑）。

**`ip` 列来源**：`tcp_assets` 表本身没有 IP 列（只有 host 域名），dashboard 用 `snapshot.host_ip_map`
派生字段回填 —— 该 map 在 `snapshot.py` 里通过
`json_extract(web_subdomains.raw_json, '$.host_ip')` 解析得到，**first-seen wins**
（首次见到的 IP 保留，IP 实际极少变）。如果某 host 没在 web_subdomains 里出现过，IP 列
显示 `—`（实测 2797 条里只有 2 条空着）。

#### 风险等级

三段独立列出，按严重程度排序：

1. **暴露的高危端口** — 所有 risk=high 的 tcp_assets（DB / RDP / Telnet / VNC / 无认证服务），按 host 列出
2. **批量指纹告警** — `response_hash` 覆盖 ≥50 子域的指纹（这些是泛解析/默认页/未配置独立站点，不是独立资产）
3. **端口展开过宽的主机** — 单 host 开放 ≥50 端口（看起来像端口段扫描产物，web 视角可剔除）

### 局限性

- **所有判断纯本地启发式**，无外部探测：
  - 风险分基于端口字典写死，不知道实际服务版本（22 不一定是 OpenSSH 9.x + 禁密 + fail2ban）
  - "暴露" 仅指 `tcp_assets.is_active=1`，**不等于真的对外开放** — 实际可能在内网 / 有 VPC 隔离 / 域名解析到 0.0.0.0 占位
  - 中文标题检测只看 CJK 区间 U+4E00-U+9FFF
- **派生字段**：snapshot 顶层新增 `host_ip_map`（subdomain→IP），dashboard 用它给 tcp_assets 回填 IP 列。
  派生源是 `web_subdomains.raw_json.host_ip`；**first-seen wins**，域名换 IP 时显示旧值（实际极少变）。
  如果未来 `raw_json` 结构变（如 `host_ip` 改名），snapshot 这条 SQL 要跟着改。
- **没有真正的截图** — "站点详情"是元数据视图；站点截图 tab 在 v1 早期被替换掉了，真截图需要外接 `gowitness` / `aquatone` 扫描产物落 `web_screenshots` 表
- **页面全量嵌入** — 站点详情 tab 渲染 2269 行 × 8 列 ≈ 1MB HTML；hash→subdomains 弹窗数据 ≈ 50KB 内嵌 JSON；总页面 ~1.8MB，纯客户端过滤无网络往返

### 与 snapshot 的关系

dashboard 启动时**直接读 SQLite 数据库**并缓存渲染结果。**缓存刷新由时间触发**，不再是文件 mtime 触发：

| 触发方式 | 行为 | 频率/时机 |
|---|---|---|
| 时间触发 | 每次请求检查距上次 reload 是否已过 `reload_interval` 秒，超过则重渲 | 默认 30s；用 env `DASHBOARD_RELOAD=N` 覆盖 |
| 主动触发 | `GET /refresh` 立刻重渲，返回 `reloaded` | 不限频率 |

所以：

```bash
# 1) 浏览器刷新 → 大多数情况下 30s 内会看到新数据（受 reload_interval 控制）
# 2) 想立刻刷新：
curl http://127.0.0.1:8765/refresh
# 浏览器再刷一次即可

# 3) 想调刷新频率（比如改成 10s）：
DASHBOARD_RELOAD=10 python3 lib/dashboard.py
```

无需重启 dashboard。

### 排错

```bash
# 起不来？通常是端口占用
python3 lib/dashboard.py
# 日志会写 "dashboard listening on http://127.0.0.1:8765"

# 端口冲突
ss -tlnp | grep 8765         # 看谁占了
python3 lib/dashboard.py --port 9999   # 换个端口，-L 也跟着改

# SSH 隧道通了但页面打不开
curl -I http://localhost:8765/health   # 本机
curl -I http://127.0.0.1:8765/health   # 远端 SSH 进去后
# 哪一步 200 哪一步 fail 就定位在哪头

# systemd 接管后 dashboard 不响应（用户 2026-08-28 之后）
systemctl --user status dashboard     # 看是不是 active / 内存 / 最近错误
journalctl --user -u dashboard -n 100 --no-pager   # 看启动报错
systemctl --user restart dashboard    # 改完代码 / 怀疑卡死后手动重启
# 进程在但 health 不返回 → 看 fd 状态
lsof -p $(pgrep -f 'lib/dashboard.py' | head -1) | grep TCP
# CLOSE_WAIT 大量堆积 → socket 未回收,kill 重启
pkill -9 -f 'lib/dashboard.py'    # systemd 30s 后会按 Restart=on-failure 自动拉起
```

## Onboarding 新业务

`run_one_business.sh` 在发现 `businesses` 表里有某业务名但**没有任何 scope**（scope_name='可测资产'）时，会**跳过 pdtm** 并在 summary.md 标 `needs_onboarding`，而不是让它因 "DB 中业务 X 没有可测资产" 直接退出。这样 cron 跑不会因为有个新业务就炸掉。

### 首次 onboard 一个新业务（手动）

```bash
# 1. 准备 input/ 目录：target.txt 是可测域，exclude.txt 是非可测域
mkdir -p input/业务名
cat > input/业务名/target.txt <<EOF
main.example.com
api.example.com
EOF
cat > input/业务名/exclude.txt <<EOF
mail.example.com
EOF

# 2. 用 pipeline.sh 的 -i 入口：会自动 INSERT 业务行 + 把 target/exclude 写进 scopes
cd /opt/srcradar/pdtm
./pipeline.sh -b 业务名 -i /opt/srcradar/input/业务名

# 3. 验证
sqlite3 ../db/recon.sqlite3 "SELECT asset FROM scopes WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名')"

# 4. 之后每日 cron 即可：run_one_business.sh 业务名 走 DB 路径
/opt/srcradar/daily/run_one_business.sh 业务名
```

### 后续每日 cron 自动跑

业务在 `businesses` 表里有行 + 至少一条 `可测资产` scope → cron 会自动按 `recon_business_config` 跑（默认 `enabled=1, web=1, tcp=0, icp=1`，enscan 是手动阶段）。
`-type` 参数是 `daily_monitor.sh / run_one_business.sh` **手动运行**时的单次覆盖，对 cron 无影响（cron 写死 `-type pdtm,icp`，enscan 不在 cron 里）。

### 跳过不想跑的业务

把 `recon_business_config` 对应行的 `enabled` 改 0，cron 就跳过该业务（其它字段保留，恢复时只改回 1）：

```sql
-- 暂停
UPDATE recon_business_config SET enabled=0 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
-- 恢复
UPDATE recon_business_config SET enabled=1 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
-- 关掉某业务 icp 备案刷新（保留 web/tcp）
UPDATE recon_business_config SET icp=0 WHERE business_id=(SELECT id FROM businesses WHERE business_name='业务名');
```

直接调 `run_one_business.sh 业务名`（不走调度器）会绕过 config —— 手动调用是显式操作，不受 gate。

## 已知问题

### 单字符 subdomain / Wildcard DNS 噪声（`http://j/` 这类）

**现象**：dashboard「站点详情」tab 里出现 N 行形如 `http://a/` / `http://j/` / `http://0/` 的条目 —— URL 无法访问，对资产盘点无价值。

**证据**（DB 实查，2026-07-26 那批数据）：

```sql
SELECT subdomain, port, url, raw_json FROM web_subdomains
WHERE instr(subdomain, '.') = 0 AND subdomain='a' AND port=80 LIMIT 3;
```

| subdomain | url | raw_json |
|---|---|---|
| `'a'` | `'http://a'` | `"http://aliondemandfiles.example.com [403] [238] ..."` |
| `'a'` | `'http://a'` | `"http://al.example.com [200] [50331] ..."` |

字段对不上：`subdomain='a'` 是扫描器喂进去的输入前缀，但 `raw_json` 里 HTTP 实际命中的是 `aliondemandfiles.example.com`（wildcard DNS 重定向目标）。`url='http://a'` 缺 `.example.com` 后缀，是 scanner 拼接 bug。

**信息密度**（典型一批）：

```
215 条 → 72 个 hash → 6 个 IP（全是 wildcard CDN 收口）
所有 first_seen/fetched_at 同一次扫描批次（< 1 分钟内）
全部 is_active=1
response_hash 是占位符 '<status_code>|<content_length>' 字符串，不是真内容指纹
```

**根因**（涉及三层）：

1. **`pdtm/check_wildcard.sh`** 检测到 `*.example.com` 有泛解析，但单字符 permutation 候选没在 HTTP 探测前被剔除
2. **httpx 探测** 时 DNS 被 wildcard 收口到 CDN IP，HTTP 请求落到真实域名（如 `aliondemandfiles.example.com`）的服务器
3. **`pdtm/import_scan_results.py`** 存库时只保留输入前缀 `a` 作为 `subdomain`，没把真实命中域名写到 canonical 字段

**当前缓解**（已生效）：

- `lib/dashboard.py:_build_sites` 第一行过滤 `if '.' in subdomain` —— 215 条 no-dot 全部从显示剔除
- 按 hash 去重 + count 列：sites-table 从 46,911 行 → 179 行；gzip 后页面 45.8 KB（之前 25 MB）

**治本（待办，建议在 pdtm 侧做）**：

两种方案，二选一：

1. **`pdtm/permutation_cache.py` filter 阶段丢单字符 permutation**（推荐）：
   ```python
   parts = permutation.lower().rstrip(".").split(".")
   if len(parts[0]) <= 2:
       continue   # 单 / 双字符 permutation 跳过
   ```
   直接不探测，每天少 200+ 次 httpx 请求（实测单字符扫描耗时几分钟）。

2. **`pdtm/import_scan_results.py` 检测 wildcard redirect 后写真实命中域名**：
   ```python
   if item.get("wildcard_redirect"):
       record["subdomain"] = item["resolved_domain"]
   ```
   保留数据但替换为 canonical 名字，dashboard 当前过滤规则可同步放宽。

**判断有没有意义**：

| 用途 | 价值 |
|---|---|
| 真实资产盘点 | ❌ 215 条 ≠ 215 站点，真相是 6 个 CDN IP |
| 监控内容变化 | ❌ 占位符 hash，无 diff 价值 |
| 检测 wildcard DNS 状态 | ✅ 这些条目本身就是泛解析开启的证据 |
| 历史趋势 | ❌ 同批次产生，重跑还是同样的 215 条 |

## 失败处理

| 失败点 | 行为 |
|---|---|
| `snapshot.py` before 失败 | 直接退出，previous.json 不轮换 |
| 单业务某阶段失败 | 对应位 bit 置 1，继续下一业务；summary.md 标 ⚠（按阶段列） |
| snapshot after 失败 | 退出，previous.json 不轮换（before 也保留） |
| diff.py 失败 | 报告失败，但 after.json 保留，下次可手工重 diff |
| 没有 previous.json | 写 baseline + summary.md「首次运行」 |

如果整轮都失败，**previous.json 不轮换**——下次还能有 baseline 可比。

## 清理历史

报告默认全保留。如需定期清理：

```bash
# 保留最近 30 天的 reports
find /opt/srcradar/daily/reports -maxdepth 1 -mindepth 1 -mtime +30 -exec rm -rf {} +
# 保留最近 30 天的 snapshots
find /opt/srcradar/daily/snapshots -maxdepth 1 -name '2*_after.json' -mtime +30 -delete
# 日志同
find /opt/srcradar/daily/logs -name '2*.log' -mtime +30 -delete
```

## 排错

```bash
# 看最近一次跑的日志
tail -f /opt/srcradar/daily/logs/$(ls -t /opt/srcradar/daily/logs/ | grep -v cron | head -1)

# 当前 baseline 时间戳
python3 -c "import json; d=json.load(open('/opt/srcradar/daily/snapshots/previous.json')); print('captured_at:', d['captured_at']); print('rows:', sum(d['row_counts'].values()))"

# 锁被占？
ls -la /opt/srcradar/daily/.lock
fuser /opt/srcradar/daily/.lock 2>/dev/null   # 看持有锁的 PID
```

## Web Hash 评分

`web_hashes` 表有三个评分相关字段：

| 字段 | 类型 | 谁写 | 说明 |
|---|---|---|---|
| `score` | INTEGER NULL | cron / 手动 | 0-100 的人工/自动评分；NULL = 未评分 |
| `description` | TEXT NOT NULL DEFAULT '' | **仅手动** | 运营备注；cron 完全不读不写 |
| `score_initialized_at` | TEXT NULL | cron | NULL = 未评分；非 NULL = 该 hash 已被 cron 评过分（**不会再被自动重评**）|

### 评分规则（baseline 50；INT 0..100）

```
score = 50
  + 20  if any active web_subdomain has CJK chars in title (U+4E00-U+9FA5)
  + 20  if subdomain_count == 1
         OR COUNT(DISTINCT subdomain) over active web_subdomains == 1
         （即 HTTP/HTTPS 同一站点视为一个站点）
  - 20  if ALL active web_subdomains have empty/null title
```

### Cron 行为

cron（每日 `daily_monitor.sh` 跑 → 触发 `pdtm/import_scan_results.py`）：

- **只给新增 hash 设初始 score**：`pdtm/import_scan_results.py:_upsert_web_records`
  检测"INSERT vs UPDATE"，对**新插入**的 hash id 调
  `daily/lib/score.py score-new --ids <ids>` 写 score + `score_initialized_at`
- **不动 description**：cron 完全不读不写该字段
- **不动已有 hash 的 score**：SQL 里有 `score_initialized_at IS NULL` 守门，
  已有分的不被覆盖（哪怕下次扫到 hash 内容变了）
- **失活的 hash 也评分**：算法用所有 ws（含 `is_active=0` 的历史 ws），
  失活 hash 仍能拿到一个分数（通常 30 或 50，作为"已失活"信号）
- **subprocess 失败吞掉**（best-effort）：写库失败本身已经 commit，评分失败不重试；
  下次 cron 跑时仍未评分的 hash 会被补评

### 一次性初始化

schema 改完后已对所有现存 hash 跑过 init。重新跑：

```bash
python3 lib/score.py init --db /opt/srcradar/db/recon.sqlite3
# {"mode": "init", "updated": N}
```

幂等：只动 `score_initialized_at IS NULL` 的行。

### 手动改分（dashboard REST endpoint）

```
POST /api/hash/<id>/edit
Content-Type: application/x-www-form-urlencoded

score=<int 0..100>          # 必填
description=<str>           # 可选，默认 ''
```

响应 JSON：
```json
{"ok": true, "id": 2, "score": 85, "description": "..."}
```

错误：`400 bad params` / `404 hash not found` / `500 db error`。

写入成功后立即局部更新 dashboard `_State.cached_snap` + `_State.cached_snap_json`，
**不需要等 reload**。`description` 长度上限 500 字符。

`curl` 示例：
```bash
curl -si -X POST -d "score=85&description=核心业务网关" \
  http://127.0.0.1:8765/api/hash/42/edit
```

### 跨项目依赖（pdtm → daily）

`pdtm/import_scan_results.py:persist()` 写完库后，通过 **subprocess** 调
`daily/lib/score.py score-new --db <db> --ids <id1,id>` 给新 hash 打分。

**这是 pdtm → daily 的单向 subprocess 调用**：

- daily 失败不会影响 pdtm（写库已 commit）
- daily 升级 score.py 时 pdtm 不需要改动
- 路径解析：`Path(import_scan_results.py).parent.parent / "daily" / "lib" / "score.py"`
  假设两者并列在 `~/tools/<repo>/` 下

`cmd_scan_onesite()`(dashboard "加一行看看" 轻量路径)**不调** score.py:
手动加的 hash 留给运营在 dashboard 里手动设分。

## URL 资产扫描(ffuf / URLFinder / gau)→ `web_hash_urls`

URL 粒度的资产盘点。**dashboard 手动触发**(`POST /<业务>/scan-urls`)
**+ 可选的每日自动 stage**(`daily-url`,用户 2026-08-26 拍板;见 §"每日 URL 扫描")。
2026-08-26 之前仅手动;之后**接 diff.py**,扫描后与上次结果对比,产出 `added/changed/deactivated` CSV。

### 表 `web_hash_urls`

挂在 `web_hashes` 之下,N:1 扩展(同一 hash 多个子域可共享 URL 集合)。

```
字段                类型            说明
hash_id             INTEGER NOT NULL  → web_hashes.id
business_id         INTEGER NOT NULL  → businesses.id
subdomain           TEXT NOT NULL     扫描时的来源子域(Q4:冗余字段便于分析)
source              TEXT NOT NULL     'ffuf' | 'urlfinder' | 'gau'
scheme/host/port    可空              拆 urlparse 得到
url                 TEXT NOT NULL     完整 URL
path                TEXT              仅 path(便于聚合)
status_code         INTEGER           ffuf 实测;urlfinder+gau NULL
content_type/length INTEGER           ffuf 实测;urlfinder+gau NULL
word_count          INTEGER           ffuf 命中词数
first_seen/last_seen/fetched_at  TEXT NOT NULL
is_active           INTEGER NOT NULL DEFAULT 1
is_static           INTEGER NULL      路径后缀 .js / .css 判定(用户 2026-08-26)
change_type         INTEGER NOT NULL DEFAULT 0  接 diff 后(用户 2026-08-26)
UNIQUE (hash_id, subdomain, url, source)   ← 逻辑键,无 raw_json
```

逻辑键:`(hash_id, subdomain, url, source)` — 同 URL 三次扫描(三个 source)算三条独立行,
便于按 source 切分历史。`web_hashes.url_count` 是 `web_hash_urls.is_active=1` 的 COUNT 缓存,
由 `scan_urls.py:persist()` 末尾显式 UPDATE(无 trigger 自动维护)。

迁移:`python3 lib/migrate_urls.py /path/to/db`(幂等)。

#### `is_static` 语义(用户 2026-08-26 拍板)

`scan_urls.py:_detect_is_static()` 算每个 path:
- 取 path 中最后一个 `.` 之后的小写后缀
- 没有 `.` →` 0 (非静态)
- 后缀 == `js` 或 `css` → 1 (静态)
- 其它后缀(`.png` / `.jpg` / `.svg` / `.woff` 等)→` 0 (非静态,只看 js/css)
- 后缀为空(路径以 `.` 结尾,如 `/v1/`)→` 0 (非静态)

写入时机:`persist()` 内每次 INSERT / ON CONFLICT UPDATE 同步计算(不会漂)。
旧行(`is_static IS NULL`)在 dashboard 染色逻辑里**忽略**,但路径后缀字符串仍按
旧逻辑兼容(`p.endswith('.js') || p.endswith('.css')`)。

触发器 `trg_whu_au` 在 WHEN 子句 `AND NEW.is_static = 0`,**静态行永远不参与 change_type 标记**,
严格满足"只有满足当前子域 && 非 js/css 才可能修改 change_type"。详见 §"每日 URL 扫描"。

#### `change_type` bitmask(用户 2026-08-26 拍板,接 diff.py)

对齐 web_subdomains:
| 值 | 含义 |
|---|---|
| 0 | clean |
| 1 | inserted (AI 触发) |
| 2 | content changed |
| 4 | reactivated(is_active 0→1 且 OLD.last_seen < 上次成功扫描) |
| 6 | reactivated + content changed |

触发器:`trg_whu_ai`(INSERT 置 1) + `trg_whu_au`(UPDATE 走 bitmask 逻辑)。
diff.py 在每次 `daily_monitor.sh` 末尾把 `change_type > 0` 的行读出来分类 + 原子重置为 0,
报告产出 `added_web_hash_urls.csv` / `changed_web_hash_urls.csv` /
`deactivated_web_hash_urls.csv`(新增三张 csv,与现有 6 张表一致格式)。

### 三个扫描器(`pdtm/scan_urls.py`)

| 工具 | 路径 | 命令 |
|---|---|---|
| ffuf | `/home/ubuntu/go/bin/ffuf` | `ffuf -u https://host/FUZZ -w wordlist ...` |
| URLFinder(中文版 by pingc0y) | `/home/ubuntu/tools/URLFinder/URLFinder` | `URLFinder -m 3 -s all -o <dir> -u <url>` |
| gau | `/home/ubuntu/go/bin/gau` | `gau --threads 5 --timeout 30 <domain>` |

**注意 gau 的 `--timeout` 是纯数字秒**(不是 `"30s"`)。

工具路径 hardcode 在 `scan_urls.py` 顶部常量,目录搬动需手动改常量。
URLFinder 是个人工具,**输出格式不锁定**;`run_urlfinder()` 兼容 JSON dict / JSON list / 纯文本三种退路。

### Dashboard 入口

| 路径 | 用途 |
|---|---|
| `POST /<业务>/scan-urls` | 表单提交 → subprocess 调 `pdtm/scan_urls.py scan-urls` |
| `GET /<业务>/urls/<hash_id>` | URL 详情页,**按 hash_id 实时查 SQL**(严格 lazy,不进 snapshot) |
| 业务页"站点详情" tab 末尾新增列 | 每行末尾 `URL 详情` 按钮 → 跳到上面 GET 页面(新窗口) |

业务页"任务状态" tab 顶部新增折叠面板「URL 资产扫描」,字段:
- `hosts`(必填,≤ 50 子域,每行一个)
- `sources`(多选 checkbox:ffuf / urlfinder / gau,至少 1 个)
- `wordlist`(仅 ffuf 生效,默认 `~/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt`)

整体超时 600s(用户拍板:大字典 ffuf 单 host 可能 5min+)。进程超时 / 异常 / 失败
都通过统一的 `_render_scan_result` 绿/红 panel 返回,**不主动 reload**(把
`_State.last_reload_ts = 0`,下次 GET 自动 reload)。

### URL 详情页 (`/<业务>/urls/<hash_id>`)

`lib/dashboard.py:_serve_urls_detail()` 渲染,实时查 `web_hash_urls`(独立
SQLite 连接,5s timeout,只读)。每张表对应一个 `source`,**展示顺序硬编码**:
**urlfinder → ffuf → gau**(用户决策),空 source 自动跳过。

#### 视觉风格(ant-table)

提炼自 `~/tools/URLFinder/demo.html`(~3KB inline CSS,不引入完整 Ant Design
库)。特征:浅灰表头 `#fafafa`、1px 浅灰分隔线、tabular-nums 数字列、风险行底色
贴近 ant-design token(red `#fff1f0` / yellow `#fffbe6` / green `#fcffe6`)。
详见 `_serve_urls_detail` 末尾 `<style>` 块。

**列宽自适应**(`table-layout: auto` + `word-break: break-all`),由该列最长
内容决定。长 URL / 路径 / title **多行展示**,不横向滚动;表头 `white-space: nowrap`
保持整齐;数字列 `min-width: 48px` 防被空字符串压到 0 宽度。

#### 各 source 列差异

| 列 | urlfinder | ffuf | gau |
|---|---|---|---|
| URL / path / status / bytes / host:port / risk / danger / last_seen | ✓ | ✓ | ✓ |
| **title** | ✓ | — | — |
| **words** | ✓(留位) | ✓ | 有数据时显示,空值 — |
| **redirect** | ✓ | — | — |
| **link_source** | ✓ | — | — |

ffuf / gau 没 urlfinder_extra 字段(title/字段 / redirect / link_source)的数据,
所以隐藏;`words` 列所有 source 都保留以视觉对齐。

#### 4 个 checkbox 筛选(URL 详情页顶)

| checkbox | 默认 | 过滤字段 | 备注 |
|---|---|---|---|
| 仅显示当前子域 | **未勾** | `tr.data-host === sample_subdomain` | 早期版本用 `data-subdomain` 比较,因为同一 hash 下所有 URL 的 `subdomain` 字段相等,**永远全匹配**,看似失效。改用 URL 实际 host 后能正确过滤掉杂 host(如从 large-assets 子域扫到的 test-internal.example 这种) |
| 仅显示 status=200 | 未勾 | `tr.data-status === '200'` | — |
| 去除 .js / .css | 未勾 | `tr.data-static === '1'` | 后端按 `path` 后缀判;`?query` / `#fragment` 已在 `scan_urls._parse_url()` 阶段剥离 |
| **仅显示新增或改变**(用户 2026-08-26) | 未勾 | `ct & 1` 或 `ct & 2` 且 `host == sample_subdomain` 且 `is_static=0` | 联动**当前子域 && 非静态**(硬编码,用户拍板**分离**于「去除 .js / .css」toggle)。语义:只看本轮 scan-urls / daily-url 阶段产生的"新增 / 内容变"行 |

4 个 checkbox **叠加生效**;`applyFilters()` 在每次 change 事件里跑,顺序遍历所有
checkbox 状态。过滤不修改 DOM,只设 `tr.style.display = 'none'`。

每张表的 `<h3 class="src-heading">` 实时显示 `· 可见 N/M 条`(`count-chip`),
让用户能直观看到 checkbox 触发了过滤。

#### 默认染色(用户 2026-08-26)

URL 详情页的 `<tr>` **默认就对** "当前子域 && 非 js/css && change_type > 0" 的行加
CSS class 染色,**不需要勾任何 toggle** 就直接可见:

| class | 触发条件 | 颜色 |
|---|---|---|
| `row-new` | `change_type & 1`(INSERT 新增) | 浅绿 `#f6ffed` |
| `row-changed` | `change_type & 2`(内容变) | 浅蓝 `#e6f7ff` |
| `row-reactivated` | `change_type & 4` 或 `6`(复活) | 浅橙 `#fff7e6` |

染色 class 与现有 `.risk-high/.risk-med/.risk-low/.risk-info`(按 status_code)
叠加:CSS 是 `tr.risk-X.row-Y`,互不冲突。
具体见 `_serve_urls_detail` 末尾 `<style>` 块的 `.urls-table tbody > tr.row-*` 段。

#### 列头排序(只 status / bytes)

仅 `status` 和 `bytes` 两列可点击排序,其它列不可点击。三态循环:

```
none → click → asc  → click → desc → click → none
```

- `status` / `bytes` 都是数字列,直接数值比较
- **空值排末尾**(asc / desc 都一样):`status_code IS NULL` / `content_length IS NULL` 的行被推到表底
- 排序前给每行打 `data-_idx` 标签,点第 3 次恢复**原始入库顺序**
- 排序不重置筛选状态(两个独立维度,可叠加)
- 表头视觉:默认 `⇅` 灰、active `▲` / `▼` 蓝色

#### `sample_subdomain` 来源

URL 详情页顶显示当前 hash 的「来源子域」,取数优先级:

1. `web_subdomains` 表里该 hash 的 subdomain(`ORDER BY is_active DESC, subdomain`)
2. fallback: `web_hash_urls` 里该 hash 第一行 URL 的 `subdomain`(同一 hash 下所有
   URL 通常来自同一 subdomain)

也用作「仅显示当前子域」checkbox 的过滤基准。

### 不接 cron / diff.py 的设计取舍

~~历史版本(2026-08-26 之前)~~:URL 资产扫描仅 dashboard 手动,不接 cron / diff.py。
**当前状态**:已接 diff.py(产出 `added/changed/deactivated_web_hash_urls.csv`),
并提供 `daily-url` stage 让 cron 自动跑 — 详见 §"每日 URL 扫描"。

## 每日 URL 扫描(daily-url stage,用户 2026-08-26 拍板)

dashboard 手动 scan-urls 适合单点验证;**批量、每日**的资产追踪由 `daily-url` stage
负责。读 `web_subdomain_scan_schedule` 表,按 `enabled=1` 的 subdomain 自动跑,
结果通过 trigger 写 `change_type`,再由 diff.py 产出 CSV。

### 配置:`web_subdomain_scan_schedule` 表

```sql
CREATE TABLE web_subdomain_scan_schedule (
    id, business_id, subdomain,
    sources     TEXT DEFAULT 'urlfinder',  -- 逗号分隔,默认 urlfinder
    last_run_at TEXT,                      -- 上次成功扫描时间
    enabled     INTEGER DEFAULT 1,
    created_at, updated_at,
    UNIQUE (business_id, subdomain)
);
```

**1 subdomain = 1 行**。`enabled=0` 不会被 cron 跑(但 row 保留)。
迁移:`python3 lib/migrate_schedule.py /path/to/db`(幂等)。

### dashboard 加入/移除 schedule

`POST /<业务>/schedule/toggle`,body:
```
subdomain=<FQDN>&action=add|remove
```
返回纯 JSON:`{ok, action, created|removed, schedule_state}`。
**add** 是 `INSERT OR IGNORE`(同 (biz, sub) 重复 → created=0 幂等)。
**remove** 是 `DELETE`(行不存在也算 ok)。
操作成功后前端 `location.reload()` 反映新状态 + 按钮文案。

按钮位置:URL 详情页(`/<业务>/urls/<hash_id>`)顶部,chip 显示当前状态:

| 状态 | 按钮文案 | data-action |
|---|---|---|
| `enabled`(已加入) | "移除每日扫描" | `remove` |
| `absent`(未加入) | "加入每日扫描" | `add` |
| `disabled`(暂停) | "移除每日扫描" | `remove` |

### `daily-url` stage

放在 `run_one_business.sh` 的固定顺序最末:`enscan → pdtm → icp → daily-url`。

```bash
./daily_monitor.sh -type pdtm,icp,daily-url   # 手动跑全部
./daily_monitor.sh -type daily-url             # 只跑 daily-url
```

> ⚠ **cron 行不变**(写死 `-type pdtm,icp`)。daily-url 默认**不进 cron**,由用户手动
> 触发或单独挂 cron。需要时:` install_cron.sh` 加 `-type pdtm,icp,daily-url`,或
> `crontab -e` 加独立行。

`run_stage_daily_url()` 内部:
1. 读 `enabled=1` 的 schedule 行 → 子域列表 + sources
2. 分 batch(每批 ≤ `DAILY_URL_BATCH_MAX`,**用户拍板:50000**,文档常量)
3. 每 batch subprocess 调 `pdtm/scan_urls.py scan-urls --sources <batch sources>`
4. **batch 退出码 0 才**为该 batch 涉及的所有 sub UPDATE `last_run_at`(= 当前时间)
5. 失败保留旧 `last_run_at`(下次补跑按"上次成功"为基线)
6. 任一 batch 失败 → 设 bit3(退出码 8)

**batch 上限 50000 是文档常量**(写在 `run_one_business.sh` 顶部 `DAILY_URL_BATCH_MAX`,
需要调整时改一处)。**实际限制在 `scan_urls.py:MAX_HOSTS = 50`** —— 单次
subprocess 调用子域上限是 50 个;`DAILY_URL_BATCH_MAX` 是 wrapper 层 batch 上限,
理论上应该远小于此(避免单 subprocess 超时)。两者关系:
- `DAILY_URL_BATCH_MAX=50000` 是 wrapper 层的"单次跑多少批就退出"上限(防失控)
- `scan_urls.MAX_HOSTS=50` 是 scan_urls 内部的"单进程最多多少 host"
- 因此 daily-url 通常一次只发 1 个 batch(≤ 50 sub),剩下的明天再跑

**典型流**(例:`ExampleCo` 业务 schedule 表有 5 个 enabled 子域):
```
run_stage_daily_url: 5 sub(s), 1 batch(es)
daily-url batch: 1-5 / 5, sources=urlfinder
[scan_urls] .../urlfinder: start
[scan_urls] .../urlfinder: got 304 urls
[scan_urls] .../urlfinder: persisted (new=304)
run_one] daily-url ok: 5 sub(s) scanned
```

无 schedule 行时返回 0(rc=0),不设 bit3,日志一行 "no schedule rows"。

### 复活 vs bulk-deactivate-then-UPSERT 语义(与 web_subdomains 对齐)

`web_hash_urls.is_active` 当前**只由 ON CONFLICT UPDATE 翻 1**(不会自然 0→1 复活)。
触发器 `trg_whu_au` 区分两种场景:
- **真正复活**(`OLD.last_seen < COALESCE(schedule.last_run_at, OLD.last_seen)`)→ 4 / 6
- **bulk-deactivate-then-UPSERT 同 run**:无 schedule 行 → 等价于 `last_seen < last_seen` = FALSE → 置 0(不变)

意味着:首次开启每日扫描时,**第一次跑的所有扫描结果都不会被标复活**(因为没有 last_run_at 基线)。
这是有意为之 —— 第一次跑 = baseline,后续才能 diff。

### 失败处理

| 失败点 | 行为 |
|---|---|
| `scan_urls.py` 退出码非 0 | batch 失败计数 +1;**不**写该 batch 的 `last_run_at` |
| 任 batch 失败 | exit 8;`summary.md` 标 "daily-url FAILED" |
| 所有 batch 都失败 | 同上;次日会按"上次成功"补跑 |
| 业务无 enabled schedule | 跳过,exit 0(不设 bit3) |
| `daily_monitor.sh` 调用 `-type` 不含 `daily-url` | 该 stage 不跑;不影响其它 stage |

### 已知问题(Q7)

| 现象 | 状态 |
|---|---|
| 扫描器如果传入了不在 `web_subdomains.is_active=1` 里的 subdomain,会把"孤儿 URL"写到 `web_hash_urls` | dashboard 前端校验 subdomain,但 bypass 后会污染。后续若加 soft FK check,需要加自动 deactivate |
| `URLFinder` 输出格式不稳定(个人工具,version 不锁) | parser 容错 + 三种退路 JSON dict / list / 纯文本 |
| gau 在 wayback 被 429 时仍输出 0 URL | gau 失败属于环境问题,代码层面已捕获并 log,不影响其它 source 写入 |
| ffuf 对外网/外业务乱扫 | **手动触发**,风险远低于 cron;但建议在 dashboard 表单上保持默认不勾 ffuf(默认勾 urlfinder) |
| daily-url 阶段没进 cron 行(用户 2026-08-26 决策) | 手动触发或单独挂 cron。需要时 `crontab -e` 加 `-type daily-url` 行;不要直接改 `install_cron.sh` 默认值 |
| `web_hash_urls` 增长快(每子域每天 ~3000 行,5w sub/天 = 15w 行) | 当前**无清理**;长期会爆表。建议运营按需清理老数据(待办:加 retention 阶段,30 天前自动归档) |
| toggle "仅显示新增或改变" 必须在 trigger 写过 change_type 后才生效 | 每次 daily_monitor.sh 跑完 diff 后 change_type 被 reset=0;**下次 cron 跑前 toggle 永远空**。如果想"持续看到上一轮 diff",需要保留 run_marker 状态(或改 trigger 为不 reset — 与 web_subdomains 走同一条路) |