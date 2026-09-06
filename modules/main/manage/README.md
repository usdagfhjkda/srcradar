# manage/ — 业务级运维入口

集中管理"新增业务"和后续业务级手工操作。所有 biz-specific 数据（业务名、阶段开关、seed 公司、scope）只在 DB / `seeds/` 出现一次 —— 各模块（`db_align` / `pdtm` / `ymicp` / `daily`）一律从 DB 读取，不写死业务名。

## 目录

```
manage/
├── README.md                # 本文件
├── add_business.sh          # 新增业务主脚本
├── set_config.sh            # 查看 / 改单个业务的 recon_business_config
└── seeds/                   # 业务 seed 数据 (TSV, git 跟踪)
```

## 新增业务：`add_business.sh`

```bash
./manage/add_business.sh -n <业务名>
                         [--enabled 0|1] [--web 0|1] [--tcp 0|1] [--icp 0|1]
                         [-s <seed.tsv>] [-i <input_dir>] [-d <db.sqlite3>]
```

`--enabled/--web/--tcp/--icp` 默认 `1, 1, 0, 1`（不动 spec 时省略）。值必须 `0` 或 `1`。

**注意**：config 行用 `INSERT OR IGNORE`，**已存在**的 config 行**不会**被 flag 覆盖 —— 保护 operator 此后的手调。要改现成 config 用 `set_config.sh`。

执行步骤（顺序固定）：

| 步骤 | 表 / 动作 | 默认 |
|---|---|---|
| 1 | `INSERT OR IGNORE INTO businesses` | — |
| 2 | `INSERT OR IGNORE INTO recon_business_config` | enabled=1, web=1, tcp=0, icp=1 |
| 3 | 若 `-s`: 从 seed TSV 读 company → `companies` 表 | group 列透传 |
| 4 | 若 `-i`: 调 `pdtm/scope_import.sh` 灌 scope | target.txt → 可测, exclude.txt → 非可测 |

**故意不**自动跑 `db_align -n -all`：那一阶段耗时长、可能触发 AQC 风控，应由操作员显式触发。脚本末尾会打印下一步命令。

### seed TSV 格式

- 列分隔 `\t`（TSV），首行必填 header
- 必填列：`name`（→ `companies.unit_name`）
- 可选列：`group`（→ `companies.group`）
- 多余列（如老格式的 `pid` / `legal_person` / `status`）会被忽略
- `#` 开头行视为注释，跳过

示例 `seeds/example.tsv`（如果以后重建）：

```tsv
name	group
ExampleCo子公司有限公司	核心
ExampleCo(湖南)信息技术有限公司	核心
某弱关联公司	E组-弱关联
```

### input_dir 格式

参考 `../pdtm/README.md` 与 `scope_import.sh` 头部注释：

- 必填：`target.txt`（一行一个域 / asset；可测资产）
- 可选：`exclude.txt`（一行一个；非可测资产）

`scope_import.sh` 会先跑 `check_wildcard.sh` 探测泛解析，命中后 `is_wildcard=1`。

## 后续每日 cron

业务行入库后：

- 默认 `enabled=1`，cron（每天 03:00，`daily/install_cron.sh`）会按 `recon_business_config` 自动跑 pdtm + icp（参见 `../daily/README.md` §安装/卸载 cron）
- enscan 阶段**不在 cron 里**，需要资产拉新时手动跑：
  ```bash
  cd ../db_align && ./bin/db_align -n '<业务名>' -all
  ```

## 业务级配置调整：`set_config.sh`

```bash
./manage/set_config.sh -n <业务名>                            # 仅查看
./manage/set_config.sh -n <业务名> --disable                  # 暂停该业务
./manage/set_config.sh -n <业务名> --enable                   # 恢复
./manage/set_config.sh -n <业务名> --web 0 --icp 1            # 改多个字段
```

只给 `-n` = read-only；至少一个变更 flag 才写库；`--enable` / `--disable` 互斥；`--web/--tcp/--icp` 值必须 `0` 或 `1`。每条变更都打印 before / after + 标出真正变化 / 已相同的字段。

业务行 / config 行不存在时直接报错（提示先跑 `add_business.sh`），不会自动建。

### 直接 SQL（escape hatch）

极少数场景（加弱关联公司等）`set_config.sh` 不覆盖，仍走 SQL：

```sql
-- 临时加入弱关联公司 (绕过 noise 过滤)
INSERT OR IGNORE INTO companies (unit_name, business_id, nature_name, "group", created_at, updated_at)
VALUES ('某公司',
        (SELECT id FROM businesses WHERE business_name='业务名'),
        '企业', '临时', datetime('now'), datetime('now'));
```

## 已知问题

### `companies.unit_name` 是全局 UNIQUE

当前 schema 是 `UNIQUE(unit_name)` 不带 `business_id`，所以同一公司名只能挂在一个业务下。`add_business.sh -s` 用 `INSERT OR IGNORE`，遇到跨业务的同名公司会**跳过**（不重绑）。

修复方向（待定）：把 UNIQUE 改成 `UNIQUE(business_id, unit_name)`，并加一次性迁移脚本处理存量冲突。

### seed 元数据 (pid / legal_person / status) 暂不持久化

老 `db_align/` 下的多列元数据 TSV（5 列带 pid/legal_person/status 等），本脚本只透传 `name` 和 `group`；`pid` / `legal_person` / `status` 直接丢弃（该上游消费命令已移除，无下游消费者）。

若以后需要这些字段，建议新增 `biz_seeds(business_id, name, pid, group, legal_person, status)` 表，`add_business.sh -s` 顺带 INSERT。
