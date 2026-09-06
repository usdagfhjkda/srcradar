-- ============================================================================
-- db/schema.sql — srcradar 完整 SQLite schema 快照
--
-- 用途: ./init.sh --init-db  用 sqlite3 喂这份文件,生成一个空的
--        db/recon.sqlite3 (含 14 张表 + 索引 + 触发器)。
--
-- 维护原则 (A1 方案):
--   本文件是 srcradar 主仓里 schema 的权威快照。源文件的 CREATE TABLE / INDEX
--   仍保留各自的"拥有者"(ymicp / pdtm / db_align / daily/lib/migrate_*.sql),
--   但 init.sh --check-schema 会做一致性校对,防止漂移。
--
-- 表 → 来源映射:
--   businesses/companies/mapp_records   → ymicp/icp_mapp_query.py:39/44/54
--   service_type_map                    → db_align/internal/store/schema.sql:4
--   scopes/web_hashes/web_subdomains
--   tcp_assets/permutation_state
--   alterx_runs                         → pdtm/import_scan_results.py:398-467
--   web_hash_urls                       → daily/lib/migrate_urls.py:63
--   web_subdomain_scan_schedule         → daily/lib/migrate_schedule.py:54
--   run_markers                         → daily/lib/migrate_change_type.sql:末尾
--   recon_business_config               → manage/add_business.sh:89 (隐式)
--
-- 触发器来源:
--   trg_bz_ai/sc_ai/co_ai/mr_ai/wh_ai/ws_ai/ta_ai (AI 触发)
--     → daily/lib/migrate_change_type.sql
--   trg_sc_au/co_au/mr_au/ws_au/ta_au (AU 触发,旧)
--     → daily/lib/migrate_change_type.sql
--   trg_ws_au/ta_au (AU 触发,新 — 区分 true reactivation vs stayed alive)
--     → daily/lib/migrate_round2.sql  (覆盖上面的旧版本)
--   trg_whu_ai/whu_au (URL change_type)
--     → daily/lib/migrate_change_type_urls.sql
--
-- 索引来源:
--   db_align/internal/store/schema.sql        idx_mapp_records_company_type
--                                              idx_scopes_biz_asset
--   ymicp/icp_mapp_query.py:68-72             idx_mapp_company_id / _service_name / _record_updated_at
--                                              idx_companies_business_id (in code at :173)
--   pdtm/import_scan_results.py:394-395       idx_web_subdomains_hash_id / _subdomain
--   pdtm/import_scan_results.py:464           idx_perm_due
--   daily/lib/migrate_change_type.sql 末尾    idx_run_markers_finished
--   daily/lib/migrate_schedule.py:66-72       idx_schedule_business / _enabled / _last_run
--   daily/lib/migrate_urls.py:87-99,149-156  idx_web_hash_urls_* (5+3=8 个)
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
-- SECTION 1: businesses / companies / mapp_records
--   来源: ymicp/icp_mapp_query.py:39-72
--   拥有者: ymicp (db_align / pdtm 都 INSERT OR IGNORE 复用)
-- ============================================================================

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    business_name TEXT NOT NULL UNIQUE,
    change_type INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    business_id INTEGER REFERENCES businesses(id),
    unit_name TEXT NOT NULL UNIQUE,
    nature_name TEXT,
    main_licence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    change_type INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mapp_records (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    source_data_id INTEGER,
    service_name TEXT NOT NULL,
    service_licence TEXT NOT NULL UNIQUE,
    service_type INTEGER,
    content_type_name TEXT,
    domain TEXT,
    record_updated_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT,
    change_type INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_companies_business_id
    ON companies(business_id);

CREATE INDEX IF NOT EXISTS idx_mapp_records_company_type
    ON mapp_records(company_id, service_type);

CREATE INDEX IF NOT EXISTS idx_mapp_company_id
    ON mapp_records(company_id);
CREATE INDEX IF NOT EXISTS idx_mapp_service_name
    ON mapp_records(service_name);
CREATE INDEX IF NOT EXISTS idx_mapp_record_updated_at
    ON mapp_records(record_updated_at);

-- ============================================================================
-- SECTION 2: service_type_map
--   来源: db_align/internal/store/schema.sql:4
--   拥有者: db_align (service_type integer → human-readable)
-- ============================================================================

CREATE TABLE IF NOT EXISTS service_type_map (
    service_type INTEGER PRIMARY KEY,
    type_name     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'observed',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    note          TEXT
);

-- ============================================================================
-- SECTION 3: scopes / web_hashes / web_subdomains / tcp_assets / permutation_state / alterx_runs
--   来源: pdtm/import_scan_results.py:398-467
--   拥有者: pdtm
-- ============================================================================

CREATE TABLE IF NOT EXISTS scopes (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    scope_name TEXT NOT NULL CHECK (scope_name IN ('可测资产', '非可测资产')),
    asset TEXT NOT NULL,
    is_wildcard INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    change_type INTEGER NOT NULL DEFAULT 0,
    UNIQUE (business_id, scope_name, asset)
);

CREATE TABLE IF NOT EXISTS web_hashes (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    response_hash TEXT NOT NULL,
    subdomain_count INTEGER NOT NULL DEFAULT 0,
    url_count INTEGER NOT NULL DEFAULT 0,
    score INTEGER DEFAULT NULL,
    description TEXT NOT NULL DEFAULT '',
    score_initialized_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    change_type INTEGER NOT NULL DEFAULT 0,
    UNIQUE (business_id, response_hash)
);

CREATE TABLE IF NOT EXISTS web_subdomains (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    hash_id INTEGER NOT NULL REFERENCES web_hashes(id),
    subdomain TEXT NOT NULL,
    port INTEGER NOT NULL,
    url TEXT,
    status_code INTEGER,
    content_length INTEGER,
    title TEXT,
    technologies TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    change_type INTEGER NOT NULL DEFAULT 0,
    UNIQUE (business_id, subdomain, port)
);

CREATE TABLE IF NOT EXISTS tcp_assets (
    id INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    raw_value TEXT,
    hosts TEXT,
    change_type INTEGER NOT NULL DEFAULT 0,
    UNIQUE (business_id, ip, port)
);

CREATE TABLE IF NOT EXISTS permutation_state (
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    base_domain     TEXT NOT NULL,
    permutation     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('resolved','nxdomain','timeout','stale','wildcard_hit')),
    resolved_ip     TEXT,
    wordlist_hash   TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    next_attempt_at TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'alterx',
    PRIMARY KEY (business_id, base_domain, permutation)
);

CREATE INDEX IF NOT EXISTS idx_scopes_biz_asset
    ON scopes(business_id, asset);

CREATE INDEX IF NOT EXISTS idx_web_subdomains_hash_id
    ON web_subdomains(hash_id);
CREATE INDEX IF NOT EXISTS idx_web_subdomains_subdomain
    ON web_subdomains(subdomain);

CREATE INDEX IF NOT EXISTS idx_perm_due
    ON permutation_state(next_attempt_at)
    WHERE status IN ('nxdomain', 'timeout');

CREATE TABLE IF NOT EXISTS alterx_runs (
    business_id    INTEGER PRIMARY KEY REFERENCES businesses(id),
    last_ran_at    TEXT NOT NULL,
    wordlist_hash  TEXT NOT NULL,
    candidates     INTEGER,
    resolved       INTEGER
);

-- ============================================================================
-- SECTION 4: web_hash_urls
--   来源: daily/lib/migrate_urls.py:63 (DDL inline) + :127/:139 (ALTER 加列)
--   拥有者: daily (scan_urls.py 写入)
-- ============================================================================

CREATE TABLE IF NOT EXISTS web_hash_urls (
    id              INTEGER PRIMARY KEY,
    hash_id         INTEGER NOT NULL,
    business_id     INTEGER NOT NULL,
    subdomain       TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    scheme          TEXT,
    url             TEXT    NOT NULL,
    host            TEXT    NOT NULL,
    port            INTEGER,
    path            TEXT,
    status_code     INTEGER,
    title           TEXT,
    content_type    TEXT,
    content_length  INTEGER,
    response_hash   TEXT,
    word_count      INTEGER,
    redirect        TEXT,
    link_source     TEXT,
    risk_flag       TEXT    NOT NULL DEFAULT '',
    is_dangerous    INTEGER NOT NULL DEFAULT 0,
    danger_reason   TEXT,
    is_static       INTEGER,
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    fetched_at      TEXT    NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    change_type     INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_risk_flag
    ON web_hash_urls(risk_flag) WHERE risk_flag != '';

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_is_dangerous
    ON web_hash_urls(is_dangerous) WHERE is_dangerous = 1;

CREATE INDEX IF NOT EXISTS idx_web_hash_urls_is_static
    ON web_hash_urls(is_static) WHERE is_static IS NOT NULL;

-- ============================================================================
-- SECTION 5: web_subdomain_scan_schedule
--   来源: daily/lib/migrate_schedule.py:54
--   拥有者: daily
-- ============================================================================

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

-- ============================================================================
-- SECTION 6: run_markers
--   来源: daily/lib/migrate_change_type.sql 末尾 (建表)
--   拥有者: daily/lib/diff.py (写入)
-- ============================================================================

CREATE TABLE IF NOT EXISTS run_markers (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_markers_finished
    ON run_markers(finished_at);

-- ============================================================================
-- SECTION 7: recon_business_config
--   来源: manage/add_business.sh:89 (INSERT 时隐式建表;managed/managed 列见下)
--   拥有者: manage (add_business.sh / set_config.sh)
-- ============================================================================

CREATE TABLE IF NOT EXISTS recon_business_config (
    business_id INTEGER PRIMARY KEY REFERENCES businesses(id),
    enabled     INTEGER NOT NULL DEFAULT 1,
    web         INTEGER NOT NULL DEFAULT 1,
    tcp         INTEGER NOT NULL DEFAULT 0,
    icp         INTEGER NOT NULL DEFAULT 0
);

-- ============================================================================
-- SECTION 8: change_type AI 触发器 (新行 = change_type=1)
--   来源: daily/lib/migrate_change_type.sql
--   注意: web_subdomains / tcp_assets 的 AU 触发器被 round2 覆盖 (见 SECTION 9)
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_bz_ai AFTER INSERT ON businesses
BEGIN
  UPDATE businesses SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_sc_ai AFTER INSERT ON scopes
BEGIN
  UPDATE scopes SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_co_ai AFTER INSERT ON companies
BEGIN
  UPDATE companies SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_mr_ai AFTER INSERT ON mapp_records
BEGIN
  UPDATE mapp_records SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_wh_ai AFTER INSERT ON web_hashes
BEGIN
  UPDATE web_hashes SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_ws_ai AFTER INSERT ON web_subdomains
BEGIN
  UPDATE web_subdomains SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_ta_ai AFTER INSERT ON tcp_assets
BEGIN
  UPDATE tcp_assets SET change_type = 1 WHERE id = NEW.id;
END;

-- change_type AU 触发器 (旧版本 — 不区分 true reactivation vs stayed alive)
-- 适用于 businesses (无 AU) / scopes / companies / mapp_records / web_hashes (无 AU)
-- web_subdomains 和 tcp_assets 的 AU 触发器在 SECTION 9 被 round2 覆盖。

CREATE TRIGGER IF NOT EXISTS trg_sc_au AFTER UPDATE OF is_wildcard ON scopes
WHEN OLD.is_wildcard IS NOT NEW.is_wildcard
BEGIN
  UPDATE scopes SET change_type = 2 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_co_au AFTER UPDATE OF updated_at ON companies
WHEN OLD.updated_at IS NOT NEW.updated_at
BEGIN
  UPDATE companies SET change_type = 2 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_mr_au AFTER UPDATE OF raw_json ON mapp_records
WHEN OLD.raw_json IS NOT NEW.raw_json
BEGIN
  UPDATE mapp_records SET change_type = 2 WHERE id = NEW.id;
END;

-- ============================================================================
-- SECTION 9: web_subdomains / tcp_assets AU 触发器 (round2 覆盖版本)
--   来源: daily/lib/migrate_round2.sql
--   区分:
--     6 = true reactivation + content changed (last_seen < 上次成功 run)
--     4 = true reactivation (内容不变)
--     0 = stayed alive (bulk-deactivate-then-UPSERT 同 run)
--     2 = pure content change
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_ws_au AFTER UPDATE ON web_subdomains
WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
   OR (OLD.title IS NOT NEW.title)
   OR (OLD.status_code IS NOT NEW.status_code)
   OR (OLD.technologies IS NOT NEW.technologies)
   OR (OLD.url IS NOT NEW.url)
   OR (OLD.content_length IS NOT NEW.content_length)
   OR (OLD.hash_id IS NOT NEW.hash_id)
BEGIN
  UPDATE web_subdomains
     SET change_type = CASE
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
        AND (OLD.title IS NOT NEW.title OR OLD.status_code IS NOT NEW.status_code
             OR OLD.technologies IS NOT NEW.technologies OR OLD.url IS NOT NEW.url
             OR OLD.content_length IS NOT NEW.content_length OR OLD.hash_id IS NOT NEW.hash_id)
         THEN 6
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
         THEN 4
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 0
       ELSE 2
     END
   WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_ta_au AFTER UPDATE ON tcp_assets
WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
   OR (OLD.hosts IS NOT NEW.hosts)
   OR (OLD.raw_value IS NOT NEW.raw_value)
BEGIN
  UPDATE tcp_assets
     SET change_type = CASE
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
        AND (OLD.hosts IS NOT NEW.hosts OR OLD.raw_value IS NOT NEW.raw_value)
         THEN 6
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
         THEN 4
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 0
       ELSE 2
     END
   WHERE id = NEW.id;
END;

-- ============================================================================
-- SECTION 10: web_hash_urls change_type 触发器
--   来源: daily/lib/migrate_change_type_urls.sql
--   Gate: NEW.is_static = 0 (静态行不参与 change_type 标记)
--   复活基线: web_subdomain_scan_schedule.last_run_at (同 subdom + enabled=1)
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_whu_ai AFTER INSERT ON web_hash_urls
BEGIN
  UPDATE web_hash_urls SET change_type = 1 WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_whu_au AFTER UPDATE ON web_hash_urls
WHEN NEW.is_static = 0
   AND (
     (OLD.is_active = 0 AND NEW.is_active = 1)
     OR (OLD.status_code    IS NOT NEW.status_code)
     OR (OLD.content_length IS NOT NEW.content_length)
     OR (OLD.word_count     IS NOT NEW.word_count)
     OR (OLD.title          IS NOT NEW.title)
     OR (OLD.redirect       IS NOT NEW.redirect)
     OR (OLD.link_source    IS NOT NEW.link_source)
     OR (OLD.risk_flag      IS NOT NEW.risk_flag)
     OR (OLD.is_dangerous   IS NOT NEW.is_dangerous)
     OR (OLD.content_type   IS NOT NEW.content_type)
   )
BEGIN
  UPDATE web_hash_urls
     SET change_type = CASE
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT last_run_at FROM web_subdomain_scan_schedule
                WHERE business_id = NEW.business_id
                  AND subdomain   = NEW.subdomain
                  AND enabled     = 1
                LIMIT 1),
              OLD.last_seen)
        AND (OLD.status_code IS NOT NEW.status_code
             OR OLD.content_length IS NOT NEW.content_length
             OR OLD.word_count     IS NOT NEW.word_count
             OR OLD.title          IS NOT NEW.title
             OR OLD.redirect       IS NOT NEW.redirect
             OR OLD.link_source    IS NOT NEW.link_source
             OR OLD.risk_flag      IS NOT NEW.risk_flag
             OR OLD.is_dangerous   IS NOT NEW.is_dangerous
             OR OLD.content_type   IS NOT NEW.content_type)
         THEN 6
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT last_run_at FROM web_subdomain_scan_schedule
                WHERE business_id = NEW.business_id
                  AND subdomain   = NEW.subdomain
                  AND enabled     = 1
                LIMIT 1),
              OLD.last_seen)
         THEN 4
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 0
       ELSE 2
     END
   WHERE id = NEW.id;
END;
