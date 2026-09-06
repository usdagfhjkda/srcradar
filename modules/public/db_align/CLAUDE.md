# db_align — Claude 工作约定

本文件是 Claude Code 在本项目工作时必须遵守的硬性约定。与 `README.md` 并列，但优先级更高：冲突时以本文件为准。

## 1. 绝对不要碰 `../ENScan_GO/config.yaml`

- **禁止** 用 `Read` / `Bash` / `Grep` / `Glob` 任何方式读取、猜测、引用 `../ENScan_GO/config.yaml` 的内容。
- 这是上游 ENScan_GO 的本地凭据文件（cookie、token），即使只是 echo 一行字段名也不行。
- 已通过 `.claudeignore` + `.claude/settings.json` 的 `permissions.deny` 兜底拦截，但 Claude 必须从源头避免任何"试探性"读取（比如 `ls -la`、`file`、目录列表里出现文件名也算泄露）。
- 如果任务确实需要 ENScan 的某个配置项（例如数据源名称、超时），**问用户**，不要从那个文件里推断。

## 2. 数据源 Cookie 失效的处理

跑长任务时（典型：`db_align -n <业务> -all -scope -delay 2`）按下面的流程监控数据源健康度。

### 检测

- 每隔 **1–2 分钟** 至少检查一次当前 run 的日志文件（路径形如 `./logs/db_align_YYYYMMDD_HHMMSS.log`）。
- 在日志里抓以下关键字判定单个数据源是否失效：
  - `未登录` / `登录已过期` / `cookie` / `Cookie expired`
  - `aqc` / `tyc` / `rb` / `qimai` 任一后跟 `401` / `403` / `empty result`
  - ENScan 子进程 stderr 里的 `请先登录` / `请配置 cookie`
- 单次判定不要只看一行，至少看到 2 处一致信号再认定"失效"，避免误杀偶发空结果。

### 记录

- 一旦判定某数据源失效，**追加一行**到项目根目录的 `cookie.log`（路径：`./cookie.log`，首次写入前会自动创建），格式：

  ```
  2026-07-26T17:42:11 [aqc] cookie 失效 (未登录); 上次成功 run: db_align_20260726_173900.log
  ```

  字段含义：`时间戳` `[数据源]` `原因` `; 上次成功 run: <日志文件名>`。

- 同一数据源在同一个 run 内反复失效，**只记一次**，不要刷屏。

### 降级策略

- 当某个数据源出现在 `cookie.log` 中后，**后续对该数据源的调用一律跳过**：
  - 如果是调 `db_align`，把 `-type` 里把它去掉（例如 `-type aqc,tyc,rb,qimai` 退化为 `-type tyc,rb,qimai`），或者用 `-type` 的子集重启。
  - 如果是脚本/子流程直调 `ENScan`，构造命令时直接剔除那个数据源。
- 跨 run 也生效：`cookie.log` 是项目级的，下次启动本项目前先 `cat cookie.log`，把已知失效数据源从 `-type` 默认值里排除再开跑。
- 如果**所有**数据源都进了 `cookie.log`，**立刻停止**整个 run 并在 stderr 打印：

  ```
  [db_align] FATAL: all data sources flagged as cookie失效，see ./cookie.log
  ```

  不要尝试重试或换代理——cookie 问题不会自愈。

### 复位

- 用户告知某数据源 cookie 已修复后，**手动**从 `cookie.log` 删除对应行（保留其它未恢复的）。
- 不要自动复位——cookie 续期需要用户操作。

## 3. 调用方式：优先本项目

- **默认优先调 `./bin/db_align`**，而不是直接调上游 `ENScan`。`db_align` 已经把代理、 `-delay`、资产 section 编排、超时这些都封装好了。
- 只有在以下场景才允许直接调 `../ENScan_GO/ENScan`：
  - 调试 `db_align` 内部的某个 subprocess 调用阶段；
  - 用户明确要求"绕过 db_align 直接看 enscan 输出"；
  - `db_align` 自身的某个 section 行为异常，需要最小化复现。
- `db_align` **默认不传 `-proxy`**（默认值空）。如需访问受限网络，通过 CLI `-proxy http://...` 显式传入，或直调 `ENScan` 时按需带 `-proxy`。详见 `db_align/README.md` flag 说明。

## 4. ENScan 失败时清缓存

- `db_align`（或裸 `ENScan`）跑挂、但报错信息含糊（`exit 1`、JSON 解析失败、`empty result` 但又没有明显 cookie 字样）时，**第一步**就是：

  ```bash
  rm -f ../ENScan_GO/enscan.gob
  ```

  `enscan.gob` 是 ENScan 的内部 Go 二进制缓存（持久化部分 HTTP 响应 / 解析结果），陈旧条目经常导致"明明数据源恢复了却还是空"。
- 清完缓存后**重跑同一命令**，再观察日志。
- 清缓存只解决"陈旧缓存"这一类故障；如果重跑仍然失败，再回到第 2 节的 cookie 失效检测流程。
- 不要在每次运行前都无脑 `rm`——只在"上次成功 → 这次失败"且无 cookie 信号的场景下清。

## 5. 一些补充约束（避免常见踩坑）

- 长跑命令（`-all -scope -delay 2`）启动后，用 `run_in_background` + 周期 `Monitor`/`Bash` 轮询日志的方式跟进，不要塞前台阻塞会话。
- 修改本项目代码后，跑 `./bin/db_align -n ExampleCo -icp -delay 2` 做 smoke test 是最低验证门槛，不要只靠 `go build` / `go test ./...`。
- 任何对 `recon.sqlite3` 的写操作之前，先确认 `-db` 指向的是预期路径（默认 `../db/recon.sqlite3`），避免写到错的库。
- 不要把 `cookie.log` / `logs/*.log` 提交进 git（如果本项目将来纳入版本控制，应在 `.gitignore` 里忽略）。
