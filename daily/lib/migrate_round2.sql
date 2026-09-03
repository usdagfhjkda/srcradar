-- Round-2 migration: reactivation classification fix
-- The old trg_ws_au / trg_ta_au fired change_type=4 on every surviving row
-- (bulk-deactivate-then-UPSERT in pdtm pipeline). The new versions check
-- OLD.last_seen against the previous FINISHED run_marker to distinguish
-- "true reactivation" (row was inactive before this run) from "stayed alive".

BEGIN;

DROP TRIGGER IF EXISTS trg_ws_au;

CREATE TRIGGER trg_ws_au AFTER UPDATE ON web_subdomains
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
       -- True reactivation + content changed: row was inactive before this run
       -- (last_seen older than previous run start) AND something in the row
       -- content actually changed.
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
        AND (OLD.title IS NOT NEW.title OR OLD.status_code IS NOT NEW.status_code
             OR OLD.technologies IS NOT NEW.technologies OR OLD.url IS NOT NEW.url
             OR OLD.content_length IS NOT NEW.content_length OR OLD.hash_id IS NOT NEW.hash_id)
         THEN 6
       -- True reactivation (content unchanged)
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT started_at FROM run_markers
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1),
              OLD.last_seen)
         THEN 4
       -- "Stayed alive" (bulk-deactivate-then-UPSERT same run)
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 0
       -- Pure content change
       ELSE 2
     END
   WHERE id = NEW.id;
END;

DROP TRIGGER IF EXISTS trg_ta_au;

CREATE TRIGGER trg_ta_au AFTER UPDATE ON tcp_assets
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

COMMIT;