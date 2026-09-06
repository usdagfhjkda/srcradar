#!/usr/bin/env python3
"""truncate_recon_db.py — 清空 recon.sqlite3 中所有数据，保留表结构与索引。

按外键依赖逆序删除：先删叶子表（web_subdomains / mapp_records /
permutation_state / scopes / tcp_assets / web_hashes），最后删 businesses。
service_type_map 默认保留（存的是 runner 累积的 service_type 命名映射，
不是业务数据）；用 --all 一并清。

用法:
  ./truncate_recon_db.py                  # 默认 ../db/recon.sqlite3
  ./truncate_recon_db.py -d /tmp/x.db     # 指定 db
  ./truncate_recon_db.py -n               # dry-run：只打印计划
  ./truncate_recon_db.py --all            # 同时清 service_type_map
  ./truncate_recon_db.py --vacuum         # 删除后 VACUUM（回收磁盘）
  ./truncate_recon_db.py -y               # 跳过确认

依赖: 仅 Python 3 标准库（sqlite3）。
"""

import argparse
import os
import sqlite3
import sys

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
    """外键拓扑序（删除序）：没有别人引用我的表先删。

    即 Kahn 算法的 in-degree=0 优先：incoming[t] = 引用 t 的表集。
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
            # FK 循环兜底（实际不会发生）
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
    """sqlite_sequence 表存在时重置自增计数（AUTOINCREMENT 列）。"""
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
    # uri= 同名时允许读写；不建议加 immutable=True，因为我们就是要写
    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        tables = list_tables(con)
        if not include_type_map and "service_type_map" in tables:
            tables = [t for t in tables if t != "service_type_map"]
        order = leaf_first_order(con)
        before = row_counts(con, order)
        print(f"db: {db_path}")
        print(f"journal_mode: {con.execute('PRAGMA journal_mode').fetchone()[0]}")
        print(f"计划删除顺序（叶子表先删）:")
        for i, t in enumerate(order, 1):
            print(f"  {i:2}. {t:<22} rows={before[t]}")
        print(
            f"包含 service_type_map: {'yes' if include_type_map else 'no (保留 runner 累积的类型映射)'}"
        )
        if vacuum:
            print("完成后会跑 VACUUM（回收磁盘，可能较慢）")
        if dry_run:
            print("\nDRYRUN: 未做任何修改。")
            return 0

        # 写入前 checkpoint（避免 WAL 里残留旧数据）
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
        print("\n删除完成:")
        for t in order:
            delta = after[t] - before[t]
            mark = "" if before[t] else " (空表)"
            print(f"  {t:<22} {before[t]:>8} -> {after[t]:>8}{mark}")
        return 0
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(
        description="清空 recon.sqlite3 中所有数据，保留表结构。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "-d", "--db",
        default=os.environ.get("RECON_DB", DEFAULT_DB),
        help=f"sqlite 路径（默认: {DEFAULT_DB}，也读 $RECON_DB）",
    )
    ap.add_argument("-n", "--dry-run", action="store_true", help="只打印计划，不动数据")
    ap.add_argument(
        "--all",
        action="store_true",
        help="同时清 service_type_map（默认保留 runner 累积的 type_<N> 命名）",
    )
    ap.add_argument(
        "--vacuum", action="store_true", help="删除后 VACUUM 回收磁盘（较慢）"
    )
    ap.add_argument("-y", "--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    print(f"即将清空: {args.db}")
    print("  （表结构、索引、PRAGMA 设置保持不变；AUTOINCREMENT 计数重置）")
    if not args.yes and not args.dry_run:
        try:
            ans = input("确认执行？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消。")
            return 1

    sys.exit(truncate(
        args.db,
        include_type_map=args.all,
        vacuum=args.vacuum,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()