#!/usr/bin/env python3
"""migrate_body_columns.py — idempotent schema migration.

Adds body storage + comparison state to web_subdomains / web_hashes, and
creates the per-subdomain baseline table.

These columns were introduced together with the retired Python diff module.
That module is gone, but the daily pipeline still reads and writes the
columns, so the schema keeps them. The retired module's auxiliary work
queue table is deliberately not reproduced -- nothing in this project
enqueues into it.

Supports --check (inspect only) and --apply (add what is missing).
"""
from __future__ import annotations

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

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")
SQL_PATH = Path(__file__).resolve().parent.parent / "daily" / "lib" / "migrate_body_columns.sql"

# table -> columns this migration is responsible for
COLUMNS = {
    "web_subdomains": (
        "prev_body",
        "curr_body",
        "content_type",
        "last_comparison_ratio",
        "last_comparison_kind",
        "last_comparison_at",
    ),
    "web_hashes": ("learned_anchors",),
}

# tables this migration creates
TABLES = ("web_subdomain_baseline",)


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchall()
    return bool(rows)


def check_only(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        missing = 0
        for table, cols in COLUMNS.items():
            if not has_table(conn, table):
                print(t("main.migrate_body_columns.check_table_missing", {"table": table}))
                missing += len(cols)
                continue
            for col in cols:
                ok = has_column(conn, table, col)
                print(f"[check] {table}.{col} exists = {ok}")
                if not ok:
                    missing += 1
        for table in TABLES:
            ok = has_table(conn, table)
            print(t("main.migrate_body_columns.check_table_exists", {"table": table, "ok": ok}))
            if not ok:
                missing += 1
        return 0 if missing == 0 else 1
    finally:
        conn.close()


def apply(db_path: str) -> int:
    if not SQL_PATH.exists():
        print(t("main.migrate_body_columns.sql_missing", {"SQL_PATH": SQL_PATH}), file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    try:
        if not has_table(conn, "web_subdomains"):
            print(t("main.migrate_body_columns.web_missing"), file=sys.stderr)
            return 3

        added: list[str] = []
        for table, cols in COLUMNS.items():
            if not has_table(conn, table):
                print(t("main.migrate_body_columns.apply_table_missing", {"table": table}), file=sys.stderr)
                return 3
            for col in cols:
                if has_column(conn, table, col):
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                added.append(f"{table}.{col}")
        conn.commit()

        # CREATE TABLE IF NOT EXISTS is idempotent on its own.
        sql = SQL_PATH.read_text(encoding="utf-8")
        # strip the ALTERs: they were applied one by one above so that
        # re-running against a partially migrated DB stays safe
        body = "\n".join(
            l for l in sql.splitlines()
            if not l.strip().upper().startswith("ALTER TABLE")
        )
        conn.executescript(body)
        conn.commit()

        if added:
            print(t("main.migrate_body_columns.added", {"cols": ", ".join(added)}))
        else:
            print(t("main.migrate_body_columns.noop"))
        for table in TABLES:
            print(t("main.migrate_body_columns.table_state", {
                "table": table,
                "state": t("main.migrate_body_columns.exists") if has_table(conn, table)
                         else t("main.migrate_body_columns.missing"),
            }))
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true", help=t("main.migrate_body_columns.check_only"))
    ap.add_argument("--apply", dest="do_apply", action="store_true", help=t("main.migrate_body_columns.fill_missing"))
    args = ap.parse_args()
    if args.check:
        return check_only(args.db)
    if args.do_apply:
        return apply(args.db)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
