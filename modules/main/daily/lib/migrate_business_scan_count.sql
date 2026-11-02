-- migrate_business_scan_count.sql
-- 2026-10-01: 为冷启动期守护加 businesses.scan_count 列
--
-- 背景: 新增业务头几次扫描时,pipeline persist() 全清场 + 重新激活会
--   把大量"无业务变化"的 last_seen 刷新误报为 ct=1/2/4 (diff 框架的
--   "上次 vs 本次"比较会因 fetch_count 变化误判)。冷启动期(前3次扫描)
--   内的 ct 应全部 reset 为 0;第4次及之后正常报告。
--
-- 触发器:
--   1. trg_bz_scan_count_inc: 业务 daily 跑成功(独立信号)后 +1
--   2. 守卫: diff.py _recompute_change_type 写 ct 1/2/4/8 前加
--      WHERE business_id IN (SELECT id FROM businesses WHERE scan_count > 3)
--
-- 2026-10-07 refactor: trg_bz_ai 已 DROP,不再 INSERT 时设 ct=1。
--   本 migration 只动 scan_count 列, change_type 由 import 流水线显式设置 (commit 2)。
-- ORIGINAL: -- 不动 schema.sql 的 trigger trg_bz_ai (INSERT 时设 ct=1) — 冷启动期
-- 内新增子域照样报 ct=1,这是合理语义。

ALTER TABLE businesses ADD COLUMN scan_count INTEGER NOT NULL DEFAULT 0;

-- 老业务兜底: 按 operator 2026-10-01 决策,scan_count 初值按业务顺序:
--   业务 id 最小 = 4  (最早接入)
--   业务 id 次小 = 3  (接入较晚)
--   业务 id 第三 = 2  (最近接入)
-- 注意: ORDER BY id 假设业务按接入时间顺序排列。
-- 具体数值是 cron 部署时由 operator 评估并写入,不在仓里保留真实业务名。
-- backfill:

-- 老业务 scan_count backfill: 在 migrate_business_scan_count.py apply() 里完成.
--              SQLite 不支持标量子查询里写 OFFSET,改成 Python 调 SELECT + UPDATE。
--              顺序: ORDER BY id ASC (假设 id 反映接入顺序), 取前 N 行,
--              依次写入 4/3/2 (第 N+1 行起不写,保持 scan_count=0 = 新业务默认值)。
--              operator 决策 (2026-10-01): id-业务-N+1 = 4, id-业务-N+2 = 3,
--              id-业务-N+3 = 2, 其余 = 0 (默认)。
--              真实业务名不在本文件出现,具体数值由 apply() 里的 BACKFILL 常量决定。

-- 索引 (供 diff.py 守卫查询快速过滤)
CREATE INDEX IF NOT EXISTS idx_businesses_scan_count_cold
  ON businesses(id) WHERE scan_count <= 3;