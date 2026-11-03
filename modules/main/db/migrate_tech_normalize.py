#!/usr/bin/env python3
# migrate_tech_normalize.py — 把 technologies 列的历史 LIST_LITERAL 形态回填成规范形式
#
# 背景 (2026-10-29):
#   import 侧把 httpx 的 tech 字段 (恒为 list[str]) 交给通用的 _s() 强转,
#   而 _s() 对 list 一律 str(v) -> Python 的 list 字面量表示被当成字符串存进
#   technologies 列:
#       str(['Alibaba Cloud CDN'])  ->  "['Alibaba Cloud CDN']"
#
#   同一份技术栈, 取决于当次扫描走的是 httpx JSONL 主路径还是 raw text
#   兜底路径, 落库形态在 "['X']" / "X" / NULL 之间跳。于是 STEP 1 的
#   technologies 快字段比较命中, 判成变更, 而 body 其实逐字节相同。
#
#   规范化格式沿用仓库内已有约定:
#       list -> ",".join(str(t) for t in v)      # ["A","B"] -> "A,B"
#       str  -> 原样
#       None -> None
#   导入侧 (import_scan_results.py) 已改为直接写规范形式, 本脚本负责把
#   历史存量回填 —— 否则新旧行形态不一致, 仍会误判。
#
# 用法:
#   python3 migrate_tech_normalize.py --db <path> --check
#   python3 migrate_tech_normalize.py --db <path> --apply
#   python3 migrate_tech_normalize.py --db <path> --apply --dry-run
#
# 幂等: 已规范的行不会被再次改动 (规范化后形态不变即跳过)。

import argparse
import ast
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = str(Path(__file__).resolve().parent / "recon.sqlite3")

# 表 -> 需要回填的列
TABLES = ("web_subdomains", "web_subdomains_pre")
COLUMN = "technologies"


def normalize(value):
    """把 list 字面量形态的 technologies 转成规范形式; 其余原样返回。

    返回 (new_value, changed) —— changed 为 True 表示需要写回。
    无法解析或不是 list 时原样返回 (不做猜测, 不改数据)。
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return value, False
    s = value.strip()
    # 只处理看起来像 list/dict 字面量的值; PLAIN 字符串不动
    if not (s.startswith("[") and s.endswith("]")):
        return value, False
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return value, False
    if not isinstance(parsed, list):
        return value, False
    # 只处理全字符串元素的 list (httpx 的 tech 就是这种)
    if not all(isinstance(x, str) for x in parsed):
        return value, False
    new = ",".join(parsed)
    return new, (new != value)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row and row[0])


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return False
    return any(r[1] == column for r in rows)


def check_only(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        for t in TABLES:
            if not table_exists(conn, t):
                print(f"[check] {t} 不存在 (跳过)")
                continue
            if not column_exists(conn, t, COLUMN):
                print(f"[check] {t}.{COLUMN} 不存在 (跳过)")
                continue
            total = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            need = 0
            for (v,) in conn.execute(f"SELECT {COLUMN} FROM {t}"):
                _, changed = normalize(v)
                if changed:
                    need += 1
            print(f"[check] {t}.{COLUMN}: 总 {total} 行, 待回填 {need} 行")
        return 0
    finally:
        conn.close()


def apply(db_path: str, dry_run: bool) -> int:
    conn = sqlite3.connect(db_path)
    try:
        for t in TABLES:
            if not table_exists(conn, t) or not column_exists(conn, t, COLUMN):
                print(f"[apply] {t}.{COLUMN} 不存在 (跳过)")
                continue
            # 先按 id 读出需要改的行, 再逐行 UPDATE (不依赖任何具体业务值)
            rows = conn.execute(
                f"SELECT id, {COLUMN} FROM {t} WHERE {COLUMN} IS NOT NULL"
            ).fetchall()
            changed = 0
            for rid, val in rows:
                new, did = normalize(val)
                if not did:
                    continue
                changed += 1
                if not dry_run:
                    conn.execute(f"UPDATE {t} SET {COLUMN}=? WHERE id=?", (new, rid))
            if not dry_run:
                conn.commit()
            verb = "dry-run 待回填" if dry_run else "已回填"
            print(f"[apply] {t}.{COLUMN}: {verb} {changed} 行")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2
    if args.apply:
        return apply(args.db, args.dry_run)
    return check_only(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
