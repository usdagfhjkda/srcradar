-- migrate_schedule.sql — web_subdomain_scan_schedule table
-- (走 inline DDL,无 .sql 文件依赖 — 跟 migrate_score.py / migrate_urls.py 一致)
--
-- 1 subdomain = 1 行(UNIQUE(business_id, subdomain))
-- sources 默认 'urlfinder',逗号分隔子集
-- last_run_at 写入时机:run_one_business.sh 的 daily-url 阶段,
--   subprocess 退出码 0 才 UPDATE(失败保留旧值,下次以"上次成功"为基线)
-- dashboard "加入每日扫描" 按钮 = INSERT; "移除每日扫描" 按钮 = DELETE
--   (语义比 enabled=0 干净,audit 友好)

BEGIN;

CREATE TABLE IF NOT EXISTS web_subdomain_scan_schedule (
    id           INTEGER PRIMARY KEY,
    business_id  INTEGER NOT NULL,
    subdomain    TEXT    NOT NULL,
    sources      TEXT    NOT NULL DEFAULT 'urlfinder',
    last_run_at  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (business_id, subdomain)
);

CREATE INDEX IF NOT EXISTS idx_schedule_business
    ON web_subdomain_scan_schedule(business_id);

CREATE INDEX IF NOT EXISTS idx_schedule_enabled
    ON web_subdomain_scan_schedule(enabled) WHERE enabled = 1;

CREATE INDEX IF NOT EXISTS idx_schedule_last_run
    ON web_subdomain_scan_schedule(last_run_at);

COMMIT;