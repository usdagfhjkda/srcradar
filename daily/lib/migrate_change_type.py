#!/usr/bin/env python3
"""Apply change_type migration to recon.sqlite3.

Idempotent: each ALTER uses a guard (column/trigger already exists → skip).

Usage:
    python3 lib/migrate_change_type.py [DB_PATH]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_change_type.sql"


def main() -> int:
    # Default DB: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[2] of this file).
    db_path = sys.argv[1] if len(sys.argv) > 1 \
        else str(Path(__file__).resolve().parents[2] / "db" / "recon.sqlite3")
    p = Path(db_path)
    if not p.exists():
        print(f"DB not found: {p}", file=sys.stderr)
        return 2

    # Read the migration SQL and strip our own BEGIN/COMMIT — Python's executescript
    # already wraps in an implicit transaction, and BEGIN inside executescript is
    # a no-op that some Python versions log as a warning.
    sql = SQL_PATH.read_text(encoding="utf-8")
    sql = "\n".join(
        line for line in sql.splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        # Pre-check: if change_type already on web_subdomains, assume migration done.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(web_subdomains)")}
        if "change_type" in cols:
            print("change_type already present on web_subdomains — migration is idempotent no-op.")
            return 0

        # Safety: take an exclusive lock so no writer is mid-flight.
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

    # Verify — PRAGMA results ignore row_factory, access by index
    conn = sqlite3.connect(str(p))
    try:
        for t in ("businesses", "scopes", "companies", "mapp_records",
                  "web_hashes", "web_subdomains", "tcp_assets"):
            # PRAGMA table_info columns: cid(0), name(1), type(2), ...
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            assert "change_type" in cols, f"change_type missing on {t}"
            trigs = [r[0] for r in conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='{t}'"
            )]
            print(f"  {t:18s} change_type=OK  triggers={trigs}")
    finally:
        conn.close()
    print("migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())