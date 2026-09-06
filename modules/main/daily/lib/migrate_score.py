#!/usr/bin/env python3
"""migrate_score.py — add score / description / score_initialized_at columns
to web_hashes. Idempotent: re-runs are no-ops.

Usage:
    python3 lib/migrate_score.py /path/to/recon.sqlite3

Side effects: only schema changes. No row data is modified.
"""
import sqlite3, sys

def has_column(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)

def main():
    if len(sys.argv) != 2:
        print("usage: migrate_score.py <db_path>", file=sys.stderr)
        sys.exit(2)
    db = sys.argv[1]
    conn = sqlite3.connect(db)
    try:
        for col, decl in (
            ("score", "INTEGER DEFAULT NULL"),
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("score_initialized_at", "TEXT DEFAULT NULL"),
        ):
            if not has_column(conn, "web_hashes", col):
                conn.execute(f"ALTER TABLE web_hashes ADD COLUMN {col} {decl}")
                print(f"[migrate_score] added column web_hashes.{col}")
            else:
                print(f"[migrate_score] column web_hashes.{col} already exists, skip")
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    main()