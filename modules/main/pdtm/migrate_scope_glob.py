#!/usr/bin/env python3
"""migrate_scope_glob.py — prefix every existing scope row (without *) with a *. prefix.

Background: the pdtm scope format was upgraded from a plain domain (example.com) to a glob (.*.example.com).
For the existing data, prefixing every row that has no * with *. is enough to stay backward compatible
(because the semantics of *.example.com cover example.com itself plus all subdomains, which is the "most permissive" compatible mapping).

Usage:
    python3 migrate_scope_glob.py --db recon.sqlite3            # dry-run (default)
    python3 migrate_scope_glob.py --db recon.sqlite3 --apply    # really write to the DB
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", required=True, help="SQLite 数据库路径")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="真正写库 (默认 dry-run, 只打印会改的内容)",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, business_id, scope_name, asset, is_wildcard FROM scopes"
    ).fetchall()

    plan: list[tuple[int, int, str, str, str]] = []
    for r in rows:
        if "*" in r["asset"]:
            continue
        new = "*." + r["asset"]
        plan.append((r["id"], r["business_id"], r["scope_name"], r["asset"], new))

    biz_map = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, business_name FROM businesses").fetchall()
    }

    print(f"[migrate] 待迁移: {len(plan)} 行 (out of {len(rows)} total)")
    if plan:
        print(f"{'id':>4}  {'biz_id':>6}  {'biz_name':<20}  {'scope_name':<10}  {'old':<35}  -> new")
        print("-" * 110)
        for id_, bid, sn, old, new in plan:
            bname = biz_map.get(bid, "?")
            print(f"{id_:>4}  {bid:>6}  {bname:<20}  {sn:<10}  {old:<35}  -> {new}")

    if args.apply:
        ts = now()
        for id_, _, _, _, new in plan:
            conn.execute(
                "UPDATE scopes SET asset=?, updated_at=? WHERE id=?",
                (new, ts, id_),
            )
        conn.commit()
        print(f"\n[migrate] applied ({len(plan)} rows updated at {ts})")
    else:
        print("\n[migrate] dry-run, 加 --apply 真正执行")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
