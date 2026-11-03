"""Apply hash_simhash_map migration.

Idempotent: CREATE TABLE IF NOT EXISTS. Use:
    python3 migrate_hash_simhash_map.py [DB_PATH]

After successful run, the table will exist:
    hash_simhash_map(response_hash TEXT PK, body_simhash INTEGER NOT NULL)

Verification: prints row count of the new table (0 on fresh migration,
unchanged on re-run since CREATE TABLE IF NOT EXISTS is no-op).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_hash_simhash_map.sql"


def main() -> int:
    # Default DB: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[1] of this file).
    db_path = sys.argv[1] if len(sys.argv) > 1 \
        else str(Path(__file__).resolve().parents[1] / "db" / "recon.sqlite3")
    p = Path(db_path)
    if not p.exists():
        print(f"DB not found: {p}", file=sys.stderr)
        return 2

    sql = SQL_PATH.read_text(encoding="utf-8")
    # Strip outer BEGIN/COMMIT: migrate_round2.py does the same — Python owns
    # the transaction via BEGIN IMMEDIATE below, so SQL-level BEGIN/COMMIT
    # would conflict.
    sql = "\n".join(
        line for line in sql.splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )

    conn = sqlite3.connect(str(p))
    try:
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

    # Verify: hash_simhash_map table exists, schema correct
    conn = sqlite3.connect(str(p))
    try:
        rows = list(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hash_simhash_map'"
        ))
        if not rows:
            print("FAIL: hash_simhash_map table not found after migration", file=sys.stderr)
            return 3
        cols = list(conn.execute("PRAGMA table_info(hash_simhash_map)"))
        expected_cols = {("response_hash", "TEXT", 1, 1),
                         ("body_simhash", "INTEGER", 1, 0)}
        actual_cols = {(name, typ, notnull, pk) for (cid, name, typ, notnull, dflt, pk) in cols}
        missing = expected_cols - actual_cols
        if missing:
            print(f"FAIL: missing/unexpected columns: {missing}", file=sys.stderr)
            return 4
        n = conn.execute("SELECT COUNT(*) FROM hash_simhash_map").fetchone()[0]
    finally:
        conn.close()

    print(f"OK: hash_simhash_map exists, row count = {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
