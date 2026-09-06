#!/usr/bin/env python3
"""score.py — web_hashes score computation.

Two entry points:

  1. score_init(conn)       — one-shot script. Initializes ALL web_hashes
                              that have score_initialized_at IS NULL.
                              Run once after migrate_score.py.

  2. score_new(conn, ids)    — cron hook. Sets score for the given hash ids
                              (the set newly inserted by _upsert_web_records).
                              Skips rows that already have a score (no
                              overwriting manual edits — see README §"评分规则").

Rules (baseline 50; INT 0..100):
  +20  any active web_subdomain has CJK chars in title (U+4E00-U+9FA5)
  +20  subdomain_count == 1, OR all active web_subdomains.subdomain
       are the same string (HTTP+HTTPS same-site merge)
  -20  ALL active web_subdomains have empty/null title

Description column is NEVER touched by this module.

Cross-project contract (see README §"跨项目依赖"):
  import_scan_results.py calls `score_new` as a subprocess:
      python3 lib/score.py score-new --db <path> --ids <id1,id2,...>
  The subprocess is best-effort: a failure here does NOT break the import.
  Failures are logged to stderr and exit with non-zero code.
"""
import argparse
import json
import sqlite3
import sys

# SQLite GLOB is bytewise — use the CJK Unified Ideographs block as a
# character class. [一-龥] covers U+4E00 to U+9FA5 (basic CJK only — no
# extension A/B). That covers ~99% of titles we'd see in practice. If we
# later want extension blocks, add another GLOB branch in the SQL.
_CJK_GLOB = "*[一-龥]*"

_SCORE_SQL = f"""
UPDATE web_hashes
   SET score = 50
        + CASE WHEN EXISTS (
            SELECT 1 FROM web_subdomains ws
            WHERE ws.hash_id = web_hashes.id
              AND ws.title GLOB '{_CJK_GLOB}'
          ) THEN 20 ELSE 0 END
        + CASE WHEN web_hashes.subdomain_count = 1
               OR (
                 SELECT COUNT(DISTINCT ws.subdomain)
                 FROM web_subdomains ws
                 WHERE ws.hash_id = web_hashes.id
               ) = 1
          THEN 20 ELSE 0 END
        - CASE WHEN NOT EXISTS (
            SELECT 1 FROM web_subdomains ws
            WHERE ws.hash_id = web_hashes.id
              AND ws.title IS NOT NULL AND ws.title != ''
          ) THEN 20 ELSE 0 END,
       score_initialized_at = datetime('now', 'localtime')
 WHERE id = ?
"""


def score_new(conn: sqlite3.Connection, hash_ids: list[int]) -> int:
    """Score specific hash ids. Returns count of rows updated.

    Skips rows that already have score_initialized_at NOT NULL — protects
    manual edits from being overwritten when the same id re-enters the
    batch (it shouldn't, but defensive).

    Scores every hash regardless of web_subdomain activation state — even
    deactivated hashes get a score. (For those, the "empty title" rule
    triggers -20 and "single subdomain" rule never fires because all
    their ws are is_active=0; they'll typically land at 30, which is
    fine as a "this hash is dead" signal.)
    """
    if not hash_ids:
        return 0
    placeholders = ",".join("?" * len(hash_ids))
    cur = conn.execute(
        f"""
        SELECT id FROM web_hashes
         WHERE id IN ({placeholders})
           AND score_initialized_at IS NULL
        """,
        hash_ids,
    )
    to_score = [r[0] for r in cur.fetchall()]
    if not to_score:
        return 0
    for hid in to_score:
        conn.execute(_SCORE_SQL, (hid,))
    conn.commit()
    return len(to_score)


def score_init(conn: sqlite3.Connection) -> int:
    """Initialize ALL unrated web_hashes. Idempotent: only touches rows
    with score_initialized_at IS NULL — every hash, active or not.
    """
    rows = conn.execute(
        "SELECT id FROM web_hashes WHERE score_initialized_at IS NULL"
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return 0
    for hid in ids:
        conn.execute(_SCORE_SQL, (hid,))
    conn.commit()
    return len(ids)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="score.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser("init", help="one-shot: score all unrated hashes")
    init_p.add_argument("--db", required=True)

    new_p = sub.add_parser("score-new", help="score a list of hash ids")
    new_p.add_argument("--db", required=True)
    new_p.add_argument("--ids", required=True, help="comma-separated hash ids")

    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        if args.cmd == "init":
            n = score_init(conn)
            print(json.dumps({"mode": "init", "updated": n}))
        elif args.cmd == "score-new":
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
            n = score_new(conn, ids)
            print(json.dumps({"mode": "score-new", "requested": len(ids), "updated": n}))
    except Exception as e:
        print(f"[score.py] error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))