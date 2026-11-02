-- migrate_hash_simhash_map.sql — 二级查表 hash_simhash_map
--
-- 用途:web_subdomains UPSERT 时,Python persist() 需要一张 (mmh3 → simhash)
--   映射表,做"hash 不等但 simhash 近邻 → 改 change_type=0"的二级判别。
--
-- 加表的原因:
--   - httpx v1.10.0 原生支持 -hash mmh3,simhash,同一 body 同时输出两个 fingerprint
--   - 但当前 schema 只存了 mmh3(response_hash),simhash 没地方落
--   - 用一张独立小表 + 单独 response_hash PK,跨业务共享,避免在 web_hashes
--     / web_subdomains / web_hash_urls 三处都加列
--
-- 表设计取舍:
--   - PK 只用 response_hash(不绑 business_id):body 决定 simhash,跨业务一致
--   - 不存 fetched_at:simhash 由 body 决定,不会随时间过期
--   - INTEGER(64-bit)而非 TEXT:实测 httpx 输出在 int64 范围,索引/比较更高效
--   - 不补存量:77k 行历史 hash 没 simhash,NULL/查不到 → 跳过二级判别
--
-- 幂等:CREATE TABLE IF NOT EXISTS;PK 单独 response_hash 无 UNIQUE 冲突
-- 兼容性:本表独立,不修改任何现有 schema;新行写入才填,旧行不受影响

BEGIN;

CREATE TABLE IF NOT EXISTS hash_simhash_map (
    response_hash TEXT    NOT NULL,
    body_simhash  INTEGER NOT NULL,
    PRIMARY KEY (response_hash)
);

COMMIT;
