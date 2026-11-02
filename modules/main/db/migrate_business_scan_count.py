#!/usr/bin/env python3
"""migrate_business_scan_count.py — idempotent schema migration.

2026-10-01: 给 businesses 加 scan_count 列 (冷启动期守护).
支持 --check (只检查, 不改 DB) 和 --apply (缺失时 ALTER TABLE).
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = str((Path(__file__).resolve().parent / "recon.sqlite3"))  # 同目录下的 recon.sqlite3
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


# Backfill 规则: 按 id ASC (假设 id 反映接入时间顺序), id-1 索引本表.
# operator 决策 (2026-10-01): id=1 -> 4, id=2 -> 3, id=3 -> 2, id>=4 -> 0.
# 真实业务名不在本文件出现,具体数值由本常量决定。
BACKFILL_SEQUENCE = [4, 3, 2]  # idx = (id - 1) -> scan_count target


def _do_backfill(conn: sqlite3.Connection) -> None:
    """Backfill 老业务 scan_count 初值 (id-based). 仅影响 scan_count==0 的行.
    Idempotent: 已设值的不动。
    """
    rows = conn.execute(
        "SELECT id, business_name FROM businesses WHERE scan_count = 0 ORDER BY id ASC"
    ).fetchall()
    if not rows:
        print("[apply][backfill] 无 scan_count=0 行 (可能已 backfill)")
        return
    for bid, name in rows:
        idx = bid - 1  # idx = id - 1
        if idx >= len(BACKFILL_SEQUENCE):
            print(f"[apply][backfill] id={bid} idx={idx} 超出 BACKFILL_SEQUENCE, 跳过 (新业务)")
            continue
        target = BACKFILL_SEQUENCE[idx]
        conn.execute("UPDATE businesses SET scan_count = ? WHERE id = ?", (target, bid))
        print(f"[apply][backfill] id={bid} -> scan_count={target}")


def apply(db_path: str) -> int:
    if not SQL_PATH.exists():
        print(f"[apply][err] {SQL_PATH} 不存在", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    try:
        if has_column(conn, "businesses", "scan_count"):
            print("[apply] businesses.scan_count 已存在, no-op")
            # 仍然跑 backfill: idempotent, scan_count=0 行才更新
            _do_backfill(conn)
            conn.commit()
            return 0
        sql = SQL_PATH.read_text(encoding="utf-8")
        conn.executescript(sql)
        _do_backfill(conn)
        conn.commit()
        print("[应用] 已迁移: businesses + scan_count + idx + 老业务 backfill")
        # 验证
        for r in conn.execute("SELECT id, business_name, scan_count FROM businesses ORDER BY id"):
            print(f"  biz_id={r[0]} {r[1]} scan_count={r[2]}")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true", help="只检查,不写 DB")
    ap.add_argument("--apply", dest="do_apply", action="store_true", help="缺失时 ALTER")
    args = ap.parse_args()
    if args.check:
        return check_only(args.db)
    if args.do_apply:
        return apply(args.db)
    # 默认: 等同 --apply (idempotent)
    return apply(args.db)


if __name__ == "__main__":
    raise SystemExit(main())