-- ============================================================================
-- migrate_drop_change_type_triggers.sql
--
-- The change_type triggers have been retired: the column is written by the
-- comparison engine (an optional plugin in its own repository), which
-- selects its input by change_type = 0. A trigger that marks new rows as 1
-- would hide them from the engine.
--
-- schema.sql no longer creates these triggers, so a database created fresh
-- does not need this script. Run it against a database that predates the
-- change, to remove the triggers it still carries.
--
-- Usage:
--   sqlite3 db/recon.sqlite3 < migrate_drop_change_type_triggers.sql
--
-- To restore the triggers: remove the comment markers in schema.sql and in
-- the migrate_change_type*.sql / migrate_round2.sql files, then run
-- init_db.py.
-- ============================================================================

BEGIN;

-- 7 AI triggers (mark the row as added on INSERT)
DROP TRIGGER IF EXISTS trg_bz_ai;
DROP TRIGGER IF EXISTS trg_sc_ai;
DROP TRIGGER IF EXISTS trg_co_ai;
DROP TRIGGER IF EXISTS trg_mr_ai;
DROP TRIGGER IF EXISTS trg_wh_ai;
DROP TRIGGER IF EXISTS trg_ws_ai;
DROP TRIGGER IF EXISTS trg_ta_ai;

-- 3 older AU triggers (scopes / companies / mapp_records)
DROP TRIGGER IF EXISTS trg_sc_au;
DROP TRIGGER IF EXISTS trg_co_au;
DROP TRIGGER IF EXISTS trg_mr_au;

-- 2 AU triggers overridden by migrate_round2.sql
DROP TRIGGER IF EXISTS trg_ws_au;
DROP TRIGGER IF EXISTS trg_ta_au;

-- 2 URL-grain AU/AI triggers
DROP TRIGGER IF EXISTS trg_whu_ai;
DROP TRIGGER IF EXISTS trg_whu_au;

COMMIT;
