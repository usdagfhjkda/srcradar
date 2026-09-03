-- migrate_change_type_urls.sql — web_hash_urls change_type + triggers
-- Phase 2 (2026-08-26 用户决策):接 diff.py / daily-url 阶段 + toggle "仅显示新增/改变"
--
-- bitmask 语义对齐 web_subdomains:
--   0 = clean
--   1 = inserted   (AI 触发)
--   2 = content changed
--   4 = reactivated (is_active 0→1 AND last_seen < 上次成功扫描)
--   6 = reactivated + content changed
--
-- 核心 gate(用户拍板):只有 is_static=0 行才参与 change_type 标记。
-- → AI 触发器无条件置 1(新行入库,即使 is_static=1 也会被置 1;
--   但 dashboard "仅显示新增/改变" toggle 在前端 hardcode 排除 is_static=1,
--   所以静态行的"新增"也是噪音,不会被染色)
-- → AU 触发器 WHEN 子句加 AND NEW.is_static = 0 — 静态行永远不会被
--   UPDATE 内容变化标记(满足"只有满足当前子域 && 非 js/css 才可能修改 change_type")

BEGIN;

ALTER TABLE web_hash_urls ADD COLUMN change_type INTEGER NOT NULL DEFAULT 0;

-- AI 触发器:新行直接置 1
CREATE TRIGGER IF NOT EXISTS trg_whu_ai AFTER INSERT ON web_hash_urls
BEGIN
  UPDATE web_hash_urls SET change_type = 1 WHERE id = NEW.id;
END;

-- AU 触发器:内容字段差异 / 复活判定
-- is_static=0 gate(用户拍板):静态行不参与 change_type
-- 复活基线:web_subdomain_scan_schedule.last_run_at(同 subdom 启用时)
-- 内容字段:status_code / content_length / word_count / title / redirect /
--           link_source / risk_flag / is_dangerous / content_type
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
       -- True reactivation + content changed
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT last_run_at FROM web_subdomain_scan_schedule
                WHERE business_id = NEW.business_id
                  AND subdomain   = NEW.subdomain
                  AND enabled     = 1
                LIMIT 1),
              OLD.last_seen)
        AND (OLD.status_code    IS NOT NEW.status_code
             OR OLD.content_length IS NOT NEW.content_length
             OR OLD.word_count     IS NOT NEW.word_count
             OR OLD.title          IS NOT NEW.title
             OR OLD.redirect       IS NOT NEW.redirect
             OR OLD.link_source    IS NOT NEW.link_source
             OR OLD.risk_flag      IS NOT NEW.risk_flag
             OR OLD.is_dangerous   IS NOT NEW.is_dangerous
             OR OLD.content_type   IS NOT NEW.content_type)
         THEN 6
       -- True reactivation (content unchanged)
       WHEN (OLD.is_active = 0 AND NEW.is_active = 1)
        AND OLD.last_seen < COALESCE(
              (SELECT last_run_at FROM web_subdomain_scan_schedule
                WHERE business_id = NEW.business_id
                  AND subdomain   = NEW.subdomain
                  AND enabled     = 1
                LIMIT 1),
              OLD.last_seen)
         THEN 4
       -- "Stayed alive" (no schedule entry OR same-run upsert)
       WHEN OLD.is_active = 0 AND NEW.is_active = 1
         THEN 0
       -- Pure content change
       ELSE 2
     END
   WHERE id = NEW.id;
END;

COMMIT;