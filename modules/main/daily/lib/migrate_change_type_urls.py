"""Apply change_type migration to web_hash_urls table (Phase 2).

Adds:
  - change_type INTEGER DEFAULT 0
  - AI trigger  trg_whu_ai  → INSERT sets change_type=1
  - AU trigger  trg_whu_au  → UPDATE sets change_type to bitmask

Idempotent: each ALTER uses a guard (column/trigger already exists → skip).

Usage:
    python3 lib/migrate_change_type_urls.py [DB_PATH]

Design notes
========
Aligns with the change_type bitmask semantics of web_subdomains:
  0 = clean
  1 = inserted   (the AI trigger)
  2 = content changed (a plain upsert plus a field difference)
  4 = reactivated (is_active 0→1 AND OLD.last_seen < the last successful scan)
  6 = reactivated + content changed

The core gate (decided by the user on 2026-08-26):
  "only a subdomain that is in scope and is not js/css can have its
  change_type modified"
  → the AU trigger gets AND NEW.is_static = 0 in its WHEN clause
  → INSERT has no is_static restriction (a new row may be js/css and has to
     be stored; but it is never coloured and never counted as changed —
     because its change_type=1 is only "kept" at 1 by the trigger while
     is_static=0; wait, the AI trigger is unconditional)
  → in practice: the AI trigger sets 1 unconditionally; the AU trigger
     gates on is_static=0; so a static row keeps change_type = 1 (from the
     INSERT) and is never marked changed by diff (because the second
     content-diff check only runs on rows with change_type>0)

Reactivation test:
  OLD.last_seen < COALESCE(
    (SELECT last_run_at FROM web_subdomain_scan_schedule
       WHERE business_id=NEW.business_id AND subdomain=NEW.subdomain
         AND enabled=1 LIMIT 1),
    OLD.last_seen)
  → no schedule row (daily scanning was never enabled for it):
     always OLD.last_seen < OLD.last_seen = FALSE, which is the same as the
     0→1 case always following the bulk-deactivate-then-UPSERT semantics, so
     it stays 0
     (this keeps the trigger's "no change" semantics for subdomains that
     are not enabled, and does not affect the manual scan-urls)

Content field differences (the WHEN clause):
  status_code / content_length / word_count / title / redirect /
  link_source / risk_flag / is_dangerous / content_type
  (covers the mutable fields shared by the three sources; path / url are identity and do not change)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_change_type_urls.sql"


def main() -> int:
    # Default DB: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[2] of this file).
    db_path = sys.argv[1] if len(sys.argv) > 1 \
        else str(Path(__file__).resolve().parents[2] / "db" / "recon.sqlite3")
    p = Path(db_path)
    if not p.exists():
        print(f"DB not found: {p}", file=sys.stderr)
        return 2

    sql = SQL_PATH.read_text(encoding="utf-8")
    sql = "\n".join(
        line for line in sql.splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(web_hash_urls)")}
        if "change_type" in cols:
            print("change_type already present on web_hash_urls — migration is idempotent no-op.")
            return 0

        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    # Verify
    conn = sqlite3.connect(str(p))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(web_hash_urls)")}
        assert "change_type" in cols, "change_type missing on web_hash_urls"
        trigs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='web_hash_urls'"
        )}
        required = {"trg_whu_ai", "trg_whu_au"}
        missing = required - trigs
        if missing:
            print(f"FAIL: missing triggers: {missing}", file=sys.stderr)
            return 1
        print(f"  web_hash_urls: change_type=OK  triggers={sorted(trigs)}")
    finally:
        conn.close()
    print("migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())