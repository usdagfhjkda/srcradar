# health 运维监控模块 (可选插件)

> **模块定位**:运维监控 plugin,**完全可选**。每日 cron 跑一次,做
> 5 项健康检查(**磁盘 / CPU / 内存 / Cron 任务 / Dashboard 进程**),
> 全 OK 不发邮件;任一异常 → 直接调 `modules/public/notify/notify_send.sh`
> 复用 GPG+Resend 推送通道(正文脱敏 + 附件含完整数值并加密)。
> 设计目标:**异常捕获 + 静默成功**,不与 `daily_monitor` 抢资源,不污染 DB。

## 导航

- **本文档**:§模块边界 / §检查项 / §安装 / §配置 / §邮件格式 / §与 notify 关系 / §安全 / §运维 / §集成点 / §文件说明
- **依赖上游**:无 — health 模块是 leaf,**不依赖 daily / pdtm / db**,只读 `df` / `uptime` / `top` / `/proc/meminfo` / `crontab -l` / `pgrep` 等 OS 级信息
- **依赖兄弟**:`modules/public/notify/`(复用其 `notify_send.sh` + `notify.conf` 凭据)
- **二进制依赖**:`python3` + `bash ≥4` + `pgrep` + `df` + `uptime` + `nproc` + `awk` + `crontab` + (通知侧)`gpg` + `curl`
- **与 daily_monitor 关系**:**完全独立**的 cron entry,不在 `daily_monitor.sh` 里 hook,也不写 DB

## 一、模块边界

| 项 | 范围 | License |
|---|---|---|
| `health_check.sh` / `install.sh` / `install_cron.sh` | srcradar 原创集成层 | Apache-2.0 |
| `lib/health_check.py` / `lib/health_config.py` / `lib/thresholds.py` | srcradar 原创 | Apache-2.0 |
| `notify_send.sh` / `lib/notify_config.py` (复用) | srcradar 兄弟模块 | Apache-2.0 |
| `gpg` (GnuPG) | OS 包(apt: gnupg),RFC 4880 OpenPGP | GPL-3.0 |
| `df` / `uptime` / `nproc` / `pgrep` / `awk` | coreutils / procps | GPL / MIT-style |
| `crontab` | OS 包(apt: cron) | cron license (BSD-style) |
| Resend HTTP API | 第三方事务邮件投递 | Resend.com ToS |

**srcradar 不维护**:GnuPG / coreutils / cron daemon / Resend API。
本模块只做 **5 项 check + 阈值判定 + 报告渲染 + 复用 notify 推送** 四件事。
**不**做:磁盘清理 / 进程拉起 / 自动修复 — 只检测并报警。

## 二、检查项(5 项)

### 1. 磁盘 (disk)
- **输入**:`df -P`(POSIX 格式,避免 GNU 扩展列名在不同 distro 间不一致)
- **解析**:第 5 列 `Capacity` 字段,正则 `^(\d+)%$`,第 6 列起 `Mounted on`
- **判定**:对每个挂载点 `used_pct` 与阈值表对比
  - `used_pct >= DISK_ALERT_PCT` → alert
  - `DISK_WARN_PCT <= used_pct < DISK_ALERT_PCT` → warn
  - 否则 ok
- **示例**(v1 当前输出):
  ```
  /dev/vda2   61806672 45674456  13494684     78% /
  tmpfs       374360   16304     358056       5% /run
  ```
  模块会挑出 `>= 80%` 的挂载点(此处 vda2 在 78% < 80% 阈值 → 不告警)。
- **跨平台**:`df -P` 在 Linux/macOS/AIX/Solaris 都一致,Windows 不支持(本模块设计目标 = Linux server)

### 2. CPU (cpu)
- **load average**:`uptime` 解析 `load average: x, y, z`(1m / 5m / 15m)
  - 阈值:`load5 / nproc >= CPU_LOAD5_PER_NPROC`(默认 2.0)
  - `nproc` 命令读 `/proc/cpuinfo` 兜底
  - 判定:超阈值 → warn(不区分 alert;load 高通常意味着有负载但未必故障)
- **瞬时占用**:`top -bn1 | grep '^%Cpu'`,正则抓 `id`(idle),`used = 100 - idle`
  - 阈值:`used >= CPU_PCT_ALERT`(默认 95)
  - 判定:超阈值 → warn
- **设计权衡**:5min load 比 1min 平滑,可避免 cron 偶发 spike 误报;瞬时占用只
  反映采样时刻,作为辅助信息

### 3. 内存 (memory)
- **输入**:`/proc/meminfo`,取 `MemTotal` + `MemAvailable`
- **公式**:`used_pct = (MemTotal - MemAvailable) / MemTotal * 100`
- **判定**:`used_pct >= MEM_ALERT_PCT`(默认 90)→ alert
- **为何用 MemAvailable 而不是 MemFree**:Linux kernel 3.14+ 的 `MemAvailable`
  是估计的「应用实际可用」内存,扣除 reclaimable cache,比 `MemFree` 更准

### 4. Cron 任务 (cron)
- **解析**:`crontab -l` 输出,简化版解析器:
  - 跳过空行、`#` 注释、`KEY=VAL` 环境变量行
  - 识别 5 个时间字段(数字/`*`/`,`/`-`/`/`)
  - 时间字段后整段当作 command
- **预置监控日志**:`config/health.conf` 的 `WATCHED_LOGS`,默认:
  ```
  modules/main/daily/logs/dashboard_watchdog.log
  ```
  (相对路径以 srcradar 项目根为基准;绝对路径也接受)
- **判定**:对每个 `WATCHED_LOGS` 路径 `stat` 看 mtime,`age_hours > CRON_MAX_AGE_HOURS`(默认 25h)→ warn
- **设计权衡**:25h 而不是 24h,跨午夜 cron 漂移不会假阳性

### 5. Dashboard 进程 (dashboard)
- **进程检测**:`pgrep -f 'python3 lib/dashboard.py'`
  - 命中 ≥1 个 PID → ok
  - 0 个 PID → alert(进程挂了)
- **watchdog 日志 mtime**:`modules/main/daily/logs/dashboard_watchdog.log` 的 mtime
  - 与 `CRON_MAX_AGE_HOURS` 同阈值(25h)对比
  - 超时 → alert(说明 dashboard_watchdog cron 也挂了,联动告警)
- **设计权衡**:同时检测进程 + 日志 mtime,避免 watchdog 死了但 dashboard 还在跑的盲区

### 容错设计

每个 check 函数独立 try/except(`run_all` 里再包一层),**任何 check 自身抛异常
也只会产出 1 条 alert Finding,而不会拖垮其他 check**。例如 `top` 命令
被 seccomp 拦了,只会让 cpu check 报「top 命令不存在」,磁盘/内存检查不受影响。

## 三、安装

```bash
# 1. 装 OS 依赖
sudo apt-get install -y python3 coreutils procps cron gnupg curl
# python3 / df / uptime / nproc / awk / pgrep 大多发行版自带

# 2. 写 example + 自检
bash modules/public/health/install.sh --check
# 期望:全部 "OK ...";没有 "缺失"/"FAIL"

# 3. operator 自填真阈值(可选;example 默认值够用)
cp modules/public/health/config/health.conf.example \
   modules/public/health/config/health.conf
chmod 600 modules/public/health/config/health.conf
$EDITOR modules/public/health/config/health.conf

# 4. 注册 cron(每日 03:15 Beijing)
bash modules/public/health/install_cron.sh install
crontab -l | grep health   # 验证已注册

# 5. 手动 dryrun 一次(不发邮件)
bash modules/public/health/health_check.sh --dryrun
cat modules/public/health/logs/health_report.md
```

> **关键**:`config/health.conf` 已经在 `.gitignore` 内
> (`modules/public/health/config/health.conf`),真阈值不会进任何 commit。

## 四、配置(config/health.conf)

字段说明(均可选;留空 → 用 `lib/thresholds.py` 的 `DEFAULT_THRESHOLDS`):

| 字段 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `DISK_WARN_PCT` | int | 80 | 磁盘占用 >= 该值记 warn |
| `DISK_ALERT_PCT` | int | 90 | 磁盘占用 >= 该值记 alert |
| `CPU_LOAD5_PER_NPROC` | float | 2.0 | 5min load / nproc 上限 |
| `CPU_PCT_ALERT` | int | 95 | top -bn1 瞬时占用告警线 |
| `MEM_ALERT_PCT` | int | 90 | 内存占用告警线 |
| `CRON_MAX_AGE_HOURS` | float | 25 | cron 日志 mtime 超时阈值 |
| `WATCHED_LOGS` | 空格分隔 list | dashboard_watchdog.log | 监控的日志路径列表 |

**凭据不在本文件**:`RESEND_API_KEY` / `FROM_ADDR` / `TO_ADDR` / `SUBJECT` /
`GPG_RECIPIENT` **不**在本文件,统一在 `../notify/config/notify.conf`,
health 模块直接复用,**不让 operator 填第二遍**。

## 五、邮件格式

**触发条件**:5 项 check 中任一产出 warn / alert Finding。
**不触发**:全 OK(只写 INFO 到 `logs/health.log`,不发邮件)。

### 正文(text 字段,脱敏版)

```
[srcradar] health 2026-05-15

overall: WARN (alert=0 warn=2 ok=10)

- [WARN][disk] 挂载点 /data 占用 85% (>80%)
- [WARN][cron] 日志 dashboard_watchdog.log 已 26.5h 未更新 (>25h)

Encrypted attachment: health_report.md.gpg (OpenPGP, requires OpenKeychain to decrypt)
```

**正文不含**:
- 详细数值(`85%` 之上不暴露 `used=450GB` / `avail=80GB`)
- 绝对路径(`/data` 之上不暴露 `/dev/sda1` / UUID)
- 进程 PID
- mountpoint 上的 device 原始行(`df` 完整行)

### 附件(明文 health_report.md,经 GPG 加密)

```markdown
# health report — run_id=20260515-031500

- generated_at: 2026-05-15T03:15:01+08:00
- date_placeholder: 2026-05-15
- overall: **WARN**  (alert=0 warn=2 ok=10)

## Summary

- [WARN][disk] 挂载点 /data 占用 85% (>80%)
- [WARN][cron] 日志 dashboard_watchdog.log 已 26.5h 未更新 (>25h)

## Detailed findings

| Severity | Check | Message | Detail |
|----------|-------|---------|--------|
| OK | disk | 所有挂载点 < 80% | 共 6 个挂载点,最大占用 85% |
| WARN | disk | 挂载点 /data 占用 85% (>80%) | <details>...</details> |
| WARN | cron | 日志 dashboard_watchdog.log 已 26.5h 未更新 | <details>...</details> |
| OK | dashboard | dashboard 运行中 (pid=12345) | <details>...</details> |
...
```

附件在推送前由 `notify_send.sh` 用 `gpg -r <recipient> -e health_report.md`
加密为 `health_report.md.gpg`,收件端用 OpenKeychain / gpg 解密。

## 六、与 notify 模块的关系

```
modules/public/health/health_check.sh
    │
    ├─ 调 lib/health_check.py
    │   ├─ 5 项 check → list[Finding]
    │   ├─ render_report(findings)  → logs/health_report.md   (附件明文)
    │   └─ render_body(findings)    → logs/health_report.body.md  (正文脱敏版)
    │
    └─ 若有异常 → 调 ../notify/notify_send.sh:
           bash notify_send.sh health_report.md health <run_id> <date_ph>
                                       │
                                       ├─ 读 ../notify/config/notify.conf
                                       │   (operator 已在 notify 模块填过,
                                       │    health 模块不再让 operator 填)
                                       ├─ gpg -r <GPG_RECIPIENT> -e health_report.md
                                       │   → health_report.md.gpg
                                       ├─ 写 ~/.cache/notify/payload.json
                                       └─ curl POST https://api.resend.com/emails
```

**复用边界**:
- health 模块**不**读 `notify.conf` 内容(真凭据绝不进 health 模块的内存)
- health 模块**不**写 `notify.conf`(让 notify 模块自己管自己)
- health 模块只**调用** `notify_send.sh` 命令(参数:`report.md`, `business_name='health'`, `run_id`, `date_ph`),与 `daily_monitor` 调用方式完全一致

**失败兜底**:`health_check.sh` 用 `|| true` 包住 notify 调用,
推送失败不影响健康结论本身(日志里 WARN,退出码仍 0)。

## 七、安全

- **凭据隔离**:`config/health.conf` 走 `chmod 600`,在 `.gitignore`(`config/*` 通配 + 显式 `modules/public/health/config/health.conf`)内
- **复用 notify.conf**:真凭据只在 `modules/public/notify/config/notify.conf` 出现一次,health 模块直接调用,避免双份真值维护
- **正文脱敏**:邮件 `text` 字段只含 `[WARN][disk] 挂载点 /data 占用 85%` 这种摘要,不含 `used=450GB` / `avail=80GB` / PID / device 路径
- **附件加密**:完整 `health_report.md` → `gpg --batch --yes --trust-model always -r <recipient> -e` → `health_report.md.gpg`,收件端用 OpenKeychain / gpg 解密
- **私钥不出收件端**:推送端只 import 公钥,从不 import 私钥(同 notify 模块)
- **失败兜底**:notify 推送失败 → health_check.sh 仍退出 0(避免 cron 因为 push 失败误判健康)
- **不持久化 cookie / token**:不写 `~/.bashrc` / `~/.netrc` / `~/.config/httpie*`
- **commit message 卫生**:任何后续 commit 不暴露内部路径 / token / 真实业务名;本 README 已用占位符 `your-domain.example` 代替真域

## 八、运维

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `bash install.sh --check` 报"缺 pgrep" | 没装 procps | `sudo apt install -y procps` |
| `bash install.sh --check` 报"依赖缺失: ../notify/notify_send.sh" | notify 模块没装 | 先 `bash modules/public/notify/install.sh --check` |
| `config/health.conf 不存在,用 example 作占位` | operator 没复制 example | 一般 OK(默认值够用);要改阈值再复制 |
| `health_check.py 失败,详见 health.log` | Python 异常 | 看 `modules/public/health/logs/health.log` 栈跟踪 |
| 收不到邮件,`logs/health.log` 有 `notify 失败 rc=4` | Resend HTTP ≥400 | 看 `~/.cache/notify/payload.json` 和 Resend 后台;常见:`attachments[].content` 不是 base64(本模块走 `base64 -w 0`,无换行) |
| 收不到邮件但 report 生成 OK | `notify.conf` 字段是 `REPLACE_WITH_*` | 在 `modules/public/notify/config/notify.conf` 填真值 |
| 误报:dashboard_alert 但 dashboard 实际在跑 | `pgrep -f` 没匹配上 | 看 `ps aux | grep dashboard` 实际命令行;阈值 `pattern='python3 lib/dashboard.py'` 与 `bin/dashboard_watchdog.sh` 启动命令必须一致 |
| 误报:cron_alert 但 dashboard_watchdog 在跑 | watchdog 没写新行(没 restart) | 看 `modules/main/daily/logs/dashboard_watchdog.log` 最近一行;如果 5min 一次且 mtime 新 → 调阈值或路径写错 |
| 跨午夜误报(00:30 cron_alert) | `CRON_MAX_AGE_HOURS=24` 太紧 | 用默认 25h,或调更大 |
| 磁盘 df 输出挂载点含空格解析错 | 罕见(Windows mount) | 本模块目标平台 = Linux,Windows 不支持 |

## 九、集成点

`health` 模块的设计是**独立的 cron entry**,**不嵌入** `daily_monitor.sh`。
理由:

1. **解耦**:健康检查属于运维层,与 daily diff 是不同的失败域(daily 失败
   不影响 health,反之亦然)。任一挂掉,另一个仍能报警
2. **错峰**:daily_monitor 在 03:00 跑(可能持续 30min+),health 在 03:15
   跑,错开 15min,避免同一时刻争 disk IO
3. **独立收件人策略**:将来可让 health 走单独的主题前缀(`[srcradar] health`),
   与 daily diff(`[srcradar] daily diff`)区分

如确需在 daily_monitor 内嵌入 health(不推荐):

```bash
# modules/main/daily/daily_monitor.sh (示例,不入仓)
HEALTH="${RECON_ROOT}/public/health/health_check.sh"
if [ -x "$HEALTH" ]; then
    bash "$HEALTH" --dryrun 2>>"$LOG_FILE" || true
fi
```

## 十、文件说明

| 文件 | 说明 |
|---|---|
| `health_check.sh` | 主入口(调 lib/health_check.py → 调 notify_send.sh) |
| `install.sh` | 写 example + 自检依赖(python3 / pgrep / df / uptime / nproc / awk / crontab / gpg / curl) |
| `install_cron.sh` | 注册每日 03:15 cron(独立于 daily_monitor) |
| `lib/health_check.py` | 5 项 check 函数 + Finding dataclass + 报告渲染 |
| `lib/health_config.py` | config/health.conf 读取器(同 notify_config.py 模式) |
| `lib/thresholds.py` | `DEFAULT_THRESHOLDS` 常量 + 类型安全的 merge |
| `config/health.conf.example` | 占位模板,所有阈值字段给合理默认(入仓) |
| `config/health.conf` | operator 自填真阈值,`chmod 600`,`.gitignore` 屏蔽(不入仓) |
| `logs/health.log` | 每次运行的 stdout / stderr 追加(本地缓存,不入 git) |
| `logs/health_report.md` | 每次运行的完整报告(下次运行会被覆盖,不入 git) |
| `logs/health_report.body.md` | 每次运行的脱敏正文(下次运行会被覆盖,不入 git) |
| `logs/health_cron.log` | cron 触发的 stdout / stderr(本地缓存,不入 git) |
| `logs/.gitkeep` | 占位,确保 logs/ 目录在仓内存在 |
| `README.md` | 本文档 |