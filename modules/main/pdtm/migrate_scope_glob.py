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
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", required=True, help=t("main.migrate_scope_glob.arg_db"))
    ap.add_argument(
        "--apply",
        action="store_true",
        help=t("main.migrate_scope_glob.arg_apply"),
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

    print(t("main.migrate_scope_glob.to_migrate", {"plan": len(plan), "total": len(rows)}))
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
        print(t("main.migrate_scope_glob.dry_run"))

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
