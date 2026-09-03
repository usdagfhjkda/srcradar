#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_scope_glob.py — 把现有 scope 行 (无 *) 前面加 *. 前缀。

背景: pdtm scope 格式从纯域名 (example.com) 升级为 glob (.*.example.com)。
对存量数据,所有无 * 的行统一在前面加 *. 即可向后兼容(因为 *.example.com
的语义包含 example.com 本身 + 所有子域,是「最宽松」的兼容映射)。

用法:
    python3 migrate_scope_glob.py --db recon.sqlite3            # dry-run (默认)
    python3 migrate_scope_glob.py --db recon.sqlite3 --apply    # 真正写库
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
