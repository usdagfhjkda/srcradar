#!/usr/bin/env python3
# migrate_tech_normalize.py — backfill the historical LIST_LITERAL form of the technologies column into the canonical form
#
# Background (2026-10-29):
#   the import side hands the httpx tech field (always list[str]) to the generic
#   _s() coercion, and _s() applies str(v) to a list, so the Python list literal
#   representation ends up stored as a string in the
#   technologies column:
#
#   The same technology stack, depending on whether the scan took the httpx JSONL
#   main path or the raw text
#   fallback path, lands in the database as "['X']" / "X" / NULL. The STEP 1
#   technologies fast field comparison then hits and calls it a change, while the
#   body is in fact byte-for-byte identical.
#
#   The canonical format follows the convention already used in the repo:
#       list -> ",".join(str(t) for t in v)      # ["A","B"] -> "A,B"
#       str  -> unchanged
#
#   The import side (import_scan_results.py) now writes the canonical form
#   directly; this script backfills
#   the existing rows - otherwise the old and the new rows differ in form and it
#   still misjudges them.
#
# Usage:
#   python3 migrate_tech_normalize.py --db <path> --check
#   python3 migrate_tech_normalize.py --db <path> --apply
#   python3 migrate_tech_normalize.py --db <path> --apply --dry-run
#
# Idempotent: a row that is already canonical is not changed again (it is skipped when the form does not change after the normalisation).

import argparse
import ast
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")

# table -> the columns that need the backfill
TABLES = ("web_subdomains", "web_subdomains_pre")
COLUMN = "technologies"


def normalize(value):
    """Turn a technologies value in the list literal form into the canonical
    form; everything else is returned unchanged.

    Returns (new_value, changed) - changed is True when it has to be written
    back.
    When it cannot be parsed, or when it is not a list, it is returned
    unchanged (no guessing, no data change).
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return value, False
    s = value.strip()
    # only the values that look like a list/dict literal are handled; a PLAIN string is left alone
    if not (s.startswith("[") and s.endswith("]")):
        return value, False
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return value, False
    if not isinstance(parsed, list):
        return value, False
    # only a list whose elements are all strings is handled (the httpx tech is exactly that)
    if not all(isinstance(x, str) for x in parsed):
        return value, False
    new = ",".join(parsed)
    return new, (new != value)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and row[0])


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return False
    return any(r[1] == column for r in rows)


def check_only(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        for t in TABLES:
            if not table_exists(conn, t):
                print(f"[check] {t} 不存在 (跳过)")
                continue
            if not column_exists(conn, t, COLUMN):
                print(f"[check] {t}.{COLUMN} 不存在 (跳过)")
                continue
            total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            need = 0
            for (v,) in conn.execute(f"SELECT {COLUMN} FROM {t}"):
                _, changed = normalize(v)
                if changed:
                    need += 1
            print(f"[check] {t}.{COLUMN}: 总 {total} 行, 待回填 {need} 行")
        return 0
    finally:
        conn.close()


def apply(db_path: str, dry_run: bool) -> int:
    conn = sqlite3.connect(db_path)
    try:
        for t in TABLES:
            if not table_exists(conn, t) or not column_exists(conn, t, COLUMN):
                print(f"[apply] {t}.{COLUMN} 不存在 (跳过)")
                continue
            # the rows to change are read out by id first, then updated one by one (it does not depend on any concrete business value)
            rows = conn.execute(
                f"SELECT id, {COLUMN} FROM {t} WHERE {COLUMN} IS NOT NULL"
            ).fetchall()
            changed = 0
            for rid, val in rows:
                new, did = normalize(val)
                if not did:
                    continue
                changed += 1
                if not dry_run:
                    conn.execute(f"UPDATE {t} SET {COLUMN}=? WHERE id=?", (new, rid))
            if not dry_run:
                conn.commit()
            verb = "dry-run 待回填" if dry_run else "已回填"
            print(f"[apply] {t}.{COLUMN}: {verb} {changed} 行")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2
    if args.apply:
        return apply(args.db, args.dry_run)
    return check_only(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
