#!/usr/bin/env python3
"""truncate_recon_db.py — delete every row from recon.sqlite3, keeping the table
structure and the indexes.

The leaf tables are deleted first, in reverse foreign key order: web_subdomains /
mapp_records / permutation_state / scopes / tcp_assets / web_hashes, and
businesses last. service_type_map is kept by default (it holds the service_type
name mapping that the runners accumulate, not business data); --all clears it
as well.

Usage:
  ./truncate_recon_db.py                  # the default ../db/recon.sqlite3
  ./truncate_recon_db.py -d /tmp/x.db     # a specific db
  ./truncate_recon_db.py -n               # dry-run: only print the plan
  ./truncate_recon_db.py --all            # also clear service_type_map
  ./truncate_recon_db.py --vacuum         # VACUUM after the delete (reclaim disk)
  ./truncate_recon_db.py -y               # skip the confirmation

Dependency: the Python 3 standard library only (sqlite3).
"""

import argparse
import os
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

DEFAULT_DB = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db", "recon.sqlite3")
)


def list_tables(con):
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    )
    return [r[0] for r in cur.fetchall()]


def leaf_first_order(con):
    """The foreign key topology order (the delete order): a table that nobody
    references is deleted first.

    That is the in-degree=0 priority of the Kahn algorithm: incoming[t] = the
    set of tables that reference t.
    """
    cur = con.cursor()
    tables = list_tables(con)
    incoming = {t: set() for t in tables}
    for t in tables:
        cur.execute(f"PRAGMA foreign_key_list({t})")
        for _, _, ref, *_ in cur.fetchall():
            if ref in tables:
                incoming[ref].add(t)
    order, remaining = [], set(tables)
    while remaining:
        progressed = False
        for t in sorted(remaining):
            if not (incoming[t] & remaining):
                order.append(t)
                remaining.remove(t)
                progressed = True
        if not progressed:
            # the FK cycle fallback (it does not actually happen)
            order.append(next(iter(remaining)))
            remaining.remove(order[-1])
    return order


def row_counts(con, tables):
    cur = con.cursor()
    return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def checkpoint_wal(con):
    cur = con.cursor()
    mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() == "wal":
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def reset_sequences(con):
    """Reset the auto-increment counter (the AUTOINCREMENT columns) when the sqlite_sequence table exists."""
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    )
    if not cur.fetchone():
        return
    cur.execute("DELETE FROM sqlite_sequence")


def truncate(db_path, *, include_type_map, vacuum, dry_run):
    if not os.path.exists(db_path):
        sys.exit(f"db 不存在: {db_path}")
    # uri= allows the read and the write on the same file; immutable=True is not advised, because we do want to write
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        tables = list_tables(con)
        if not include_type_map and "service_type_map" in tables:
            tables = [t for t in tables if t != "service_type_map"]
        order = leaf_first_order(con)
        before = row_counts(con, order)
        print(f"db: {db_path}")
        print(f"journal_mode: {con.execute('PRAGMA journal_mode').fetchone()[0]}")
        print(_t("main.truncate_recon_db.plan"))
        for i, t in enumerate(order, 1):
            print(f"  {i:2}. {t:<22} rows={before[t]}")
        print(
            f"包含 service_type_map: {'yes' if include_type_map else 'no (保留 runner 累积的类型映射)'}"
        )
        if vacuum:
            print(_t("main.truncate_recon_db.vacuum_note"))
        if dry_run:
            print()
            print(_t("main.truncate_recon_db.dryrun"))
            return 0

        # checkpoint before the write (so that no stale data stays in the WAL)
        checkpoint_wal(con)

        con.execute("PRAGMA foreign_keys = OFF")
        try:
            con.execute("BEGIN")
            for t in order:
                con.execute(f"DELETE FROM {t}")
            reset_sequences(con)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.execute("PRAGMA foreign_keys = ON")

        if vacuum:
            con.execute("VACUUM")

        after = row_counts(con, order)
        print()
        print(_t("main.truncate_recon_db.done"))
        for t in order:
            mark = "" if before[t] else " (空表)"
            print(f"  {t:<22} {before[t]:>8} -> {after[t]:>8}{mark}")
        return 0
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(
        description=_t("main.truncate_recon_db.summary"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "-d", "--db",
        default=os.environ.get("RECON_DB", DEFAULT_DB),
        help=_t("main.truncate_recon_db.arg_db", {"DEFAULT_DB": DEFAULT_DB}),
    )
    ap.add_argument("-n", "--dry-run", action="store_true", help=_t("main.truncate_recon_db.arg_plan_only"))
    ap.add_argument(
        "--all",
        action="store_true",
        help=_t("main.truncate_recon_db.arg_also_service_type"),
    )
    ap.add_argument(
        "--vacuum", action="store_true", help=_t("main.truncate_recon_db.arg_vacuum")
    )
    ap.add_argument("-y", "--yes", action="store_true", help=_t("main.truncate_recon_db.arg_skip_confirm"))
    args = ap.parse_args()

    print(f"即将清空: {args.db}")
    print(_t("main.truncate_recon_db.arg_keeps"))
    if not args.yes and not args.dry_run:
        try:
            ans = input("确认执行？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print(_t("main.truncate_recon_db.cancelled"))
            return 1

    sys.exit(truncate(
        args.db,
        include_type_map=args.all,
        vacuum=args.vacuum,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()