-- ============================================================================
-- migrate_body_columns.sql
--
-- Body storage + comparison state on web_subdomains / web_hashes.
--
-- This replaces the schema fragment that previously lived in the retired
-- Python diff module's directory. That module is no longer part of this
-- project, but the columns it introduced are consumed by the daily
-- pipeline (dashboard, diff, scan import), so they stay.
--
-- Only the columns and tables the current pipeline actually reads are
-- created here. The retired module's auxiliary queue table is not
-- reproduced: nothing in this project enqueues into it.
--
-- Idempotent: each ALTER is guarded by a PRAGMA table_info probe, so
-- re-running against a partially migrated database is safe.
--
-- Usage:
--   sqlite3 db/recon.sqlite3 < migrate_body_columns.sql
-- ============================================================================

BEGIN;

-- ============ web_subdomains: body + comparison state ============
-- prev_body: zlib level 6 compressed body from the previous cycle
-- curr_body: zlib level 6 compressed body captured in the current cycle
-- content_type: HTTP Content-Type (text/html, application/json, ...)
-- last_comparison_ratio: similarity ratio recorded by the last comparison
-- last_comparison_kind: verdict kind recorded by the last comparison
-- last_comparison_at: ISO 8601 timestamp of the last comparison
ALTER TABLE web_subdomains ADD COLUMN prev_body          BLOB;
ALTER TABLE web_subdomains ADD COLUMN curr_body          BLOB;
ALTER TABLE web_subdomains ADD COLUMN content_type       TEXT;
ALTER TABLE web_subdomains ADD COLUMN last_comparison_ratio REAL;
ALTER TABLE web_subdomains ADD COLUMN last_comparison_kind  TEXT;
ALTER TABLE web_subdomains ADD COLUMN last_comparison_at    TEXT;

-- ============ web_hashes: cross-day learned anchors ============
-- JSON array of [prefix, suffix] pairs. Each pair marks a body region that
-- varies between daily samples but is treated as dynamic noise rather than
-- a real content change.
ALTER TABLE web_hashes ADD COLUMN learned_anchors TEXT NOT NULL DEFAULT '[]';

-- ============ web_subdomain_baseline: per-subdomain baseline ratio ============
-- The first successful ratio becomes the baseline; later ratios are compared
-- against it with a fixed tolerance.
CREATE TABLE IF NOT EXISTS web_subdomain_baseline (
    subdomain_id   INTEGER PRIMARY KEY REFERENCES web_subdomains(id),
    baseline_ratio REAL NOT NULL,
    sample_count   INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

COMMIT;
