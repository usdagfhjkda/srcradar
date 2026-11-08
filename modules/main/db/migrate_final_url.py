#!/usr/bin/env python3
# migrate_final_url.py — add a final_url column to web_subdomains / web_subdomains_pre
#
# Background (2026-10-28):
#   the fast-field comparison has a redirect slot that has to record the final URL after following the redirect
#   (CompareInput.prev_redirect / curr_redirect), but it was never populated —
#   pipeline.cpp does not assign it when building CompareInput, and the SELECT in sqlite_io.cpp does not read it either.
#   so STEP 1 actually compares only the four fields title/status_code/technologies/content_length.
#
#   the web_subdomains.url column cannot be used to fill this slot: its form is unstable —
#   whether httpx connects to the IP or to the domain depends on the input format of the scan stage, the same asset may be
#   a domain on one day and an IP on another, so url differs -> false fast_field -> ct=2
#   (a byte-identical body is then judged as changed).
#   httpx's final_url (the final landing point after following -follow-redirects) is stable in practice:
#     - for the same asset, final_url is identical for both the IP-direct and the domain-direct input form
#     - the field does not exist in a 2xx response -> naturally empty -> both sides empty means equal,
#       so it is skipped automatically (200 and 302 need no split, empty values are skipped automatically)
#
# Usage:
#   python3 migrate_final_url.py --db <path> --check
#   python3 migrate_final_url.py --db <path> --apply
#
# Idempotent: skipped when the column already exists.

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

# table -> new column
TABLES = ("web_subdomains", "web_subdomains_pre")
COLUMNS = ("final_url",)


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
                print(_t("main.migrate_final_url.check_missing", {"t": t}))
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
                print(_t("main.migrate_final_url.apply_missing", {"t": t}))
                continue
            for c in COLUMNS:
                if has_column(conn, t, c):
                    print(_t("main.migrate_final_url.apply_exists", {"t": t, "c": c}))
                    continue
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} TEXT")
                print(_t("main.migrate_final_url.apply_added", {"t": t, "c": c}))
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
