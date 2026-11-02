#!/usr/bin/env python3
# migrate_final_url.py — 给 web_subdomains / web_subdomains_pre 加 final_url 列
#
# 背景 (2026-10-28):
#   快字段比较里有一个 redirect 槽位, 需要单独记录跟随重定向后的最终 URL
#   (CompareInput.prev_redirect / curr_redirect), 但从来没被填充过 ——
#   pipeline.cpp 构造 CompareInput 时没赋值, sqlite_io.cpp 的 SELECT 也没取。
#   所以 STEP 1 实际只比 title/status_code/technologies/content_length 四个字段。
#
#   补这个槽位时不能用 web_subdomains.url 列: 它的形态不稳定 ——
#   httpx 连的是 IP 还是域名取决于扫描阶段的输入格式, 同一资产每天可能是
#   域名形态也可能是 IP 形态, 导致 url 不等 -> 误判 fast_field -> ct=2
#   (body 逐字节相同也被判成变更)。
#   httpx 的 final_url (跟随 -follow-redirects 后的最终落点) 实测稳定:
#     - 同一资产, IP 直连与域名直连两种输入下 final_url 完全相同
#     - 2xx 响应里该字段不存在 -> 天然为空 -> 比较时两边都空即相等,
#       自动跳过 (200 与 302 不需要分流, 空值自动跳过)
#
# 用法:
#   python3 migrate_final_url.py --db <path> --check
#   python3 migrate_final_url.py --db <path> --apply
#
# 幂等: 列已存在则跳过。

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")

# 表 -> 新增列
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
                conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} TEXT")
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
