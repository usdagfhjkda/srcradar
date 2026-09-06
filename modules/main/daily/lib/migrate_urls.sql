-- migrate_urls.sql — add web_hash_urls table for URL-level asset tracking.
--
-- Schema rationale:
--   - 挂在 web_hashes 下(N:1),逻辑键 = (hash_id, subdomain, url, source)
--     当前阶段 1 hash ≈ 1 subdomain(用户决策 Q4),但 schema 留 1:N 余地。
--   - 不留 raw_json(用户决策 Q6)。扫描器输出只在 persist 时 parse,解析后丢弃。
--   - 触发器沿用 change_type bitmask 模式(0=clean / 1=added / 2=changed / 4=reactivated):
--     写入侧: INSERT 置 1;UPDATE 内容字段时置 2;is_active 0→1 复活按 web_subdomains
--     的语义分"真复活"(4)与"批量 deactivate 后 UPSERT"(0)。
--   - diff.py 不消费本表(用户决策 Q3 — cron 不参与,仅 dashboard 手动触发);
--     触发器只用于未来如果接 diff 时的兼容,本次不强制加触发器。
--     ⚠ 这里先不加 trigger:Phase 1 只加表结构,触发器等 Phase 4 真的接 diff 时再加
--     (避免现在加了一堆 trigger,后面发现不用就要清理)。FK 也不硬加 — 用户 Q7
--     明确写文档不强制自动化校验,schema 不带 REFERENCES 子句。

BEGIN;

CREATE TABLE IF NOT EXISTS web_hash_urls (
    id              INTEGER PRIMARY KEY,
    hash_id         INTEGER NOT NULL,
    business_id     INTEGER NOT NULL,
    subdomain       TEXT    NOT NULL,                  -- 扫描时的来源子域(Q4:冗余字段便于分析)
    source          TEXT    NOT NULL,                  -- 'ffuf' | 'urlfinder' | 'gau'
    scheme          TEXT,                              -- 'http' | 'https'
    url             TEXT    NOT NULL,                  -- 完整 URL
    host            TEXT    NOT NULL,                  -- 冗余 hostname,便于 dashboard JOIN
    port            INTEGER,                           -- host 端口(80/443/自定义)
    path            TEXT,                              -- 仅 path,便于聚合
    status_code     INTEGER,                           -- ffuf 实测 / urlfinder+gau NULL
    title           TEXT,                              -- ffuf 不抓 body,全 NULL;留字段为兼容
    content_type    TEXT,
    content_length  INTEGER,
    response_hash   TEXT,                              -- 单 URL 内容指纹(可空)
    word_count      INTEGER,                           -- ffuf 命中词数(可空)
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (hash_id, subdomain, url, source)
);

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_hash_id
    ON web_hash_urls(hash_id);

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_business_id
    ON web_hash_urls(business_id);

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_subdomain
    ON web_hash_urls(subdomain);

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_is_active
    ON web_hash_urls(is_active);

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_source
    ON web_hash_urls(source);

-- 派生表(URL 计数缓存到 hash,避免每次 dashboard 重数百万行)
-- 不加触发器维护(用户 Q3 不接 diff,暂缓),由 scan_urls.py persist 末尾显式 UPDATE。
ALTER TABLE web_hashes ADD COLUMN url_count INTEGER NOT NULL DEFAULT 0;

COMMIT;