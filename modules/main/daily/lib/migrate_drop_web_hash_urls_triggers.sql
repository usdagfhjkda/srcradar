-- ============================================================================
-- migrate_drop_web_hash_urls_triggers.sql
--
-- The change_type triggers on web_hash_urls belong to a URL-grain comparison
-- path that is no longer part of this project. They are dropped together
-- with the other change_type triggers; see
-- migrate_drop_change_type_triggers.sql for the full list and for how to
-- restore them.
--
-- Usage:
--   sqlite3 db/recon.sqlite3 < migrate_drop_web_hash_urls_triggers.sql
-- ============================================================================

BEGIN;

DROP TRIGGER IF EXISTS trg_whu_ai;
DROP TRIGGER IF EXISTS trg_whu_au;

COMMIT;
