-- service_type_map: maps mapp_records.service_type integer → human-readable name
-- Rows are inserted by the runner as new service_type values are observed.
-- Idempotent inserts via INSERT OR IGNORE; user can update the name column by hand.
CREATE TABLE IF NOT EXISTS service_type_map (
    service_type INTEGER PRIMARY KEY,
    type_name     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'observed',
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    note          TEXT
);

-- Index for the existing mapp_records lookup hot path (company_id + service_type)
CREATE INDEX IF NOT EXISTS idx_mapp_records_company_type
    ON mapp_records (company_id, service_type);

-- Index for scope asset lookups (business_id + asset) — current schema has no unique key
-- on (business_id, asset), so the runner dedups before insert.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scopes_biz_asset
    ON scopes (business_id, asset);
