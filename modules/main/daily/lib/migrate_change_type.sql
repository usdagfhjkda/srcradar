-- change_type migration: bitmask column + triggers
-- 0 = clean, 1 = inserted, 2 = content changed, 4 = reactivated (0→1 on is_active)
-- bulk-deactivate (UPDATE web_subdomains SET is_active=0) must NOT fire — see WHEN clauses

BEGIN;

-- ============ businesses (only INSERT) ============
ALTER TABLE businesses ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_bz_ai AFTER INSERT ON businesses
BEGIN
  UPDATE businesses SET change_type = 1 WHERE id = NEW.id;
END;

-- ============ scopes ============
ALTER TABLE scopes ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_sc_ai AFTER INSERT ON scopes
BEGIN
  UPDATE scopes SET change_type = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_sc_au AFTER UPDATE OF is_wildcard ON scopes
WHEN OLD.is_wildcard IS NOT NEW.is_wildcard
BEGIN
  UPDATE scopes SET change_type = 2 WHERE id = NEW.id;
END;

-- ============ companies (icp_mapp_query bumps updated_at only on real change) ============
ALTER TABLE companies ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_co_ai AFTER INSERT ON companies
BEGIN
  UPDATE companies SET change_type = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_co_au AFTER UPDATE OF updated_at ON companies
WHEN OLD.updated_at IS NOT NEW.updated_at
BEGIN
  UPDATE companies SET change_type = 2 WHERE id = NEW.id;
END;

-- ============ mapp_records (write fires only when raw_json changes — already gated upstream) ============
ALTER TABLE mapp_records ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_mr_ai AFTER INSERT ON mapp_records
BEGIN
  UPDATE mapp_records SET change_type = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_mr_au AFTER UPDATE OF raw_json ON mapp_records
WHEN OLD.raw_json IS NOT NEW.raw_json
BEGIN
  UPDATE mapp_records SET change_type = 2 WHERE id = NEW.id;
END;

-- ============ web_hashes (only INSERT matters; subdomain_count bump is bookkeeping) ============
ALTER TABLE web_hashes ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_wh_ai AFTER INSERT ON web_hashes
BEGIN
  UPDATE web_hashes SET change_type = 1 WHERE id = NEW.id;
END;

-- ============ web_subdomains ============
ALTER TABLE web_subdomains ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_ws_ai AFTER INSERT ON web_subdomains
BEGIN
  UPDATE web_subdomains SET change_type = 1 WHERE id = NEW.id;
END;
-- AU trigger: fires only when (a) is_active 0→1, or (b) content fields differ.
-- Bulk-deactivate (is_active 1→0, all content unchanged) does NOT match → change_type stays 0.
CREATE TRIGGER IF NOT EXISTS trg_ws_au AFTER UPDATE ON web_subdomains
WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
   OR (OLD.title          IS NOT NEW.title)
   OR (OLD.status_code    IS NOT NEW.status_code)
   OR (OLD.technologies   IS NOT NEW.technologies)
   OR (OLD.url            IS NOT NEW.url)
   OR (OLD.content_length IS NOT NEW.content_length)
   OR (OLD.hash_id        IS NOT NEW.hash_id)
BEGIN
  UPDATE web_subdomains
     SET change_type = CASE
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND (OLD.title IS NOT NEW.title
             OR OLD.status_code IS NOT NEW.status_code
             OR OLD.technologies IS NOT NEW.technologies
             OR OLD.url IS NOT NEW.url
             OR OLD.content_length IS NOT NEW.content_length
             OR OLD.hash_id IS NOT NEW.hash_id)
         THEN 6
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 4
       ELSE 2
     END
   WHERE id = NEW.id;
END;

-- ============ tcp_assets ============
ALTER TABLE tcp_assets ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;
CREATE TRIGGER IF NOT EXISTS trg_ta_ai AFTER INSERT ON tcp_assets
BEGIN
  UPDATE tcp_assets SET change_type = 1 WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_ta_au AFTER UPDATE ON tcp_assets
WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
   OR (OLD.hosts     IS NOT NEW.hosts)
   OR (OLD.raw_value IS NOT NEW.raw_value)
BEGIN
  UPDATE tcp_assets
     SET change_type = CASE
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND (OLD.hosts IS NOT NEW.hosts OR OLD.raw_value IS NOT NEW.raw_value)
         THEN 6
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 4
       ELSE 2
     END
   WHERE id = NEW.id;
END;

-- run_markers: each diff writes a row; previous row's started_at is the
-- `before_ts` boundary for the NEXT diff.
CREATE TABLE IF NOT EXISTS run_markers (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_markers_finished ON run_markers(finished_at);

COMMIT;