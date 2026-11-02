#!/usr/bin/env python3
"""
migrate_3pkg_body.py — idempotent schema migration: 给 web_subdomains /
web_subdomains_pre 加 curr_body_1 / curr_body_2 两列 (zlib BLOB)。

2026-10-25: 3 包噪声提取方案。
  当前只有 web_subdomains.curr_body (一次扫描的 body) 和
  web_subdomains_pre.curr_body (上一天的 body), 两点对比无法区分
  "动态噪声" 和 "同长度的真实变更"。灰区行 (ratio 落在 0.02~0.98)
  需要额外两次独立采样, 构成 3 包:
      包1 = curr_body      (原扫描)
      包2 = curr_body_1    (同一时刻连发第 1 次)
      包3 = curr_body_2    (同一时刻连发第 2 次)
  三包共同稳定的区域 = 真实内容; 每包都变的区域 = 动态噪声。

用法:
  python3 migrate_3pkg_body.py --check
  python3 migrate_3pkg_body.py --apply [--db PATH]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")

# 表 -> 新增列
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
                print(f"[check] {t} 不存在 (跳过)")
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
                print(f"[apply] {t} 不存在 (跳过)")
                continue
            for c in COLUMNS:
                if has_column(conn, t, c):
                    print(f"[apply] {t}.{c} 已存在, 跳过")
                    continue
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} BLOB")
                print(f"[apply] {t}.{c} 已添加")
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
