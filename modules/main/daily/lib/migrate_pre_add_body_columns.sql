-- =============================================================================
-- 2026-10-08: sync web_subdomains_pre schema drift.
--   commit 565e925 (snapshot, 10/8) created web_subdomains_pre mirroring
--   web_subdomains 的 17 列 (id .. ips + _snapshot_at)。
--   但 web_subdomains 在 commit 4dc5956 (10/4) 期间, 已经在 db 上 ALTER TABLE
--   加了 curr_body / prev_body / content_type / last_comparison_ratio/kind/at
--   6 列, schema.sql 没同步 (drift 4 天)。
--   本 migration 给已存在的 web_subdomains_pre 表补这 6 列, 让 _pre 与
--   web_subdomains 23 列对齐, 使快照表与主表列结构一致, 便于
--   按同一主键从两表读取 prev_body / curr_body 做跨周期对比。
--
--   注意: daily_monitor.sh step 0 内联 CREATE TABLE IF NOT EXISTS 已经包含
--   这 6 列, 全新库无需跑本 migration; 只对 10/8 之前已经存在的 db 需要跑。
--
--   idempotent: 失败 ALTER TABLE 不会污染 db (IF NOT EXISTS 风格 SQLite
--   不支持, 用 PRAGMA table_info 探测后跳过)。
-- =============================================================================

-- prev_body: zlib level 6 压缩的上次 cycle body
ALTER TABLE web_subdomains_pre ADD COLUMN prev_body          BLOB;

-- curr_body: zlib level 6 压缩的本次 cycle body (snapshot 时复制)
ALTER TABLE web_subdomains_pre ADD COLUMN curr_body          BLOB;

-- content_type: HTTP Content-Type (text/html / application/json 等)
ALTER TABLE web_subdomains_pre ADD COLUMN content_type       TEXT;

-- last_comparison_ratio: 上次内容相似度比值
ALTER TABLE web_subdomains_pre ADD COLUMN last_comparison_ratio REAL;

-- last_comparison_kind: 上次比较的判定类型 (cold_start / body_identical / ...)
ALTER TABLE web_subdomains_pre ADD COLUMN last_comparison_kind  TEXT;

-- last_comparison_at: 上次比较完成的时间戳
ALTER TABLE web_subdomains_pre ADD COLUMN last_comparison_at    TEXT;