-- Web hash score / description / score_initialized_at columns.
-- Idempotent: every ALTER is wrapped in a column-existence check so this
-- script can be re-run safely. Use `sqlite3 <db> ".read migrate_score.sql"`.
--
-- See README §"Web Hash 评分" for the rules. Three columns:
--   score               INT NULL      — manual or auto (baseline 50 ± adjustments)
--   description         TEXT NOT NULL — user-only, cron never reads/writes
--   score_initialized_at TEXT NULL     — NULL = never scored; cron init sets this
--                                        to lock the row from future batch updates

-- score
ALTER TABLE web_hashes ADD COLUMN score INTEGER DEFAULT NULL;

-- description (default empty string, never NULL)
ALTER TABLE web_hashes ADD COLUMN description TEXT NOT NULL DEFAULT '';

-- score_initialized_at: NULL = unrated. Cron init scripts set this to a
-- timestamp after computing score; this prevents re-scoring on subsequent runs.
ALTER TABLE web_hashes ADD COLUMN score_initialized_at TEXT DEFAULT NULL;