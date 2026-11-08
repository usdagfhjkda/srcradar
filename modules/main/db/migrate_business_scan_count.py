#!/usr/bin/env python3
"""migrate_business_scan_count.py — idempotent schema migration.

2026-10-01: add a scan_count column to businesses (cold-start guard).
Supports --check (check only, do not touch the DB) and --apply (ALTER TABLE when missing).
"""
import argparse
import sqlite3
import sys
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t

DEFAULT_DB = str((Path(__file__).resolve().parent / "recon.sqlite3"))  # recon.sqlite3 in the same directory
SQL_PATH = Path(__file__).resolve().parent.parent / "daily" / "lib" / "migrate_business_scan_count.sql"


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def check_only(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        ok = has_column(conn, "businesses", "scan_count")
        print(f"[check] businesses.scan_count exists = {ok}")
        return 0 if ok else 1
    finally:
        conn.close()


# Backfill rule: by id ASC (assuming id reflects the onboarding order), id-1 indexes this table.
# operator decision (2026-10-01): id=1 -> 4, id=2 -> 3, id=3 -> 2, id>=4 -> 0.
# the real business names do not appear in this file, the concrete values are decided by this constant.
BACKFILL_SEQUENCE = [4, 3, 2]  # idx = (id - 1) -> scan_count target


def _do_backfill(conn: sqlite3.Connection) -> None:
    """Backfill the initial scan_count of the old businesses (id-based). Only affects rows where scan_count==0.
    Idempotent: rows that already have a value are left alone.
    """
    rows = conn.execute(
        "SELECT id, business_name FROM businesses WHERE scan_count = 0 ORDER BY id ASC"
    ).fetchall()
    if not rows:
        print(t("main.migrate_business_scan_count.nothing_to_backfill"))
        return
    for bid, name in rows:
        idx = bid - 1  # idx = id - 1
        if idx >= len(BACKFILL_SEQUENCE):
            print(t("main.migrate_business_scan_count.out_of_range", {"bid": bid, "idx": idx}))
            continue
        target = BACKFILL_SEQUENCE[idx]
        conn.execute("UPDATE businesses SET scan_count = ? WHERE id = ?", (target, bid))
        print(f"[apply][backfill] id={bid} -> scan_count={target}")


def apply(db_path: str) -> int:
    if not SQL_PATH.exists():
        print(t("main.migrate_business_scan_count.sql_missing", {"SQL_PATH": SQL_PATH}), file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    try:
        if has_column(conn, "businesses", "scan_count"):
            print(t("main.migrate_business_scan_count.exists"))
            # still run the backfill: idempotent, only rows with scan_count=0 are updated
            _do_backfill(conn)
            conn.commit()
            return 0
        sql = SQL_PATH.read_text(encoding="utf-8")
        conn.executescript(sql)
        _do_backfill(conn)
        conn.commit()
        print(t("main.migrate_business_scan_count.applied"))
        # verify
        for r in conn.execute("SELECT id, business_name, scan_count FROM businesses ORDER BY id"):
            print(f"  biz_id={r[0]} {r[1]} scan_count={r[2]}")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true", help=t("main.migrate_business_scan_count.check_only"))
    ap.add_argument("--apply", dest="do_apply", action="store_true", help=t("main.migrate_business_scan_count.alter_when_missing"))
    args = ap.parse_args()
    if args.check:
        return check_only(args.db)
    if args.do_apply:
        return apply(args.db)
    # default: same as --apply (idempotent)
    return apply(args.db)


if __name__ == "__main__":
    raise SystemExit(main())