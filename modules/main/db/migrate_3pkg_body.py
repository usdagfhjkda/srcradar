#!/usr/bin/env python3
"""
migrate_3pkg_body.py — idempotent schema migration: add the curr_body_1 /
curr_body_2 columns (zlib BLOB) to web_subdomains / web_subdomains_pre.

2026-10-25: 3-package noise extraction scheme.
  today there are only web_subdomains.curr_body (the body of one scan) and
  web_subdomains_pre.curr_body (the body of the previous day); comparing two
  points cannot tell "dynamic noise" from "a real change of the same length".
  Grey-zone rows (ratio between 0.02 and 0.98) need two extra independent samples, forming 3 packages:
      pkg1 = curr_body      (the original scan)
      pkg2 = curr_body_1    (1st of two extra requests fired back to back at the same moment)
      pkg3 = curr_body_2    (2nd of two extra requests fired back to back at the same moment)
  The region that is stable across all three packages = real content; a region that changes in every package = dynamic noise.

Usage:
  python3 migrate_3pkg_body.py --check
  python3 migrate_3pkg_body.py --apply [--db PATH]
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
from i18n import t as _t

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")

# table -> columns to add
TABLES = ("web_subdomains", "web_subdomains_pre")
COLUMNS = ("curr_body_1", "curr_body_2")


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return False
    return any(r[1] == column for r in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and row[0])


def check_only(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        ok = True
        for t in TABLES:
            if not table_exists(conn, t):
                print(_t("main.migrate_3pkg_body.check_missing", {"t": t}))
                continue
            for c in COLUMNS:
                exists = has_column(conn, t, c)
                ok = ok and exists
                print(f"[check] {t}.{c} exists = {exists}")
        return 0 if ok else 1
    finally:
        conn.close()


def apply(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        for t in TABLES:
            if not table_exists(conn, t):
                print(_t("main.migrate_3pkg_body.apply_missing", {"t": t}))
                continue
            for c in COLUMNS:
                if has_column(conn, t, c):
                    print(_t("main.migrate_3pkg_body.apply_exists", {"t": t, "c": c}))
                    continue
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} BLOB")
                print(_t("main.migrate_3pkg_body.apply_added", {"t": t, "c": c}))
        conn.commit()
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2
    if args.apply:
        return apply(args.db)
    return check_only(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
