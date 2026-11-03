-- migrate_url_diff.sql — URL-level body storage.
--
-- Adds 4 columns to web_hash_urls so body comparison can run at the
-- URL grain (one row per
-- (hash_id, subdomain, url, source) tuple). body holds the latest
-- raw HTTP body bytes compressed with zlib level=6 (matches the
-- zlib level used elsewhere in the project); prev_body and
-- curr_body mirror the same rolling-buffer semantics as
-- web_subdomains.{prev_body, curr_body}.
--
-- baseline_at is the ISO 8601 timestamp of the last comparison that
-- produced a successful ratio (analogous to web_subdomain_baseline).
-- The comparison timestamp is written on each row so operators can audit
-- when each URL was last evaluated.
--
-- Idempotent: guarded by `IF NOT EXISTS` via the migration runner
-- (migrate_url_diff.py) which inspects pragma_table_info before
-- issuing the ALTER, so re-running this script on a DB that already
-- has the columns is a no-op.

ALTER TABLE web_hash_urls ADD COLUMN body         BLOB;          -- zlib(level=6) compressed current body
ALTER TABLE web_hash_urls ADD COLUMN prev_body    BLOB;          -- zlib(level=6) compressed previous body (rolling)
ALTER TABLE web_hash_urls ADD COLUMN curr_body    BLOB;          -- zlib(level=6) compressed current body snapshot at compare-time
ALTER TABLE web_hash_urls ADD COLUMN baseline_at  TEXT;          -- ISO 8601 of last successful comparison