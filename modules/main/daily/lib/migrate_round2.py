#!/usr/bin/env python3
"""Apply round-2 migration: reactivation classification fix.

Idempotent: drops + recreates the two AU triggers. Use:
    python3 lib/migrate_round2.py [DB_PATH]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_round2.sql"


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

    # Verify
    conn = sqlite3.connect(str(p))
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )}
        required = {"trg_ws_au", "trg_ta_au"}
        missing = required - names
        if missing:
            print(f"FAIL: missing triggers after migration: {missing}", file=sys.stderr)
            return 1
        print(f"  trg_ws_au: present")
        print(f"  trg_ta_au: present")
        print("migration applied.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())