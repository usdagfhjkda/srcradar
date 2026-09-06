#!/usr/bin/env python3
"""init_db.py — 根据 db/schema.sql 生成空 recon.sqlite3。

用法:
    python3 init_db.py [DB_PATH] [SCHEMA_SQL]

    DB_PATH      默认 <repo>/db/recon.sqlite3
    SCHEMA_SQL   默认 <repo>/db/schema.sql

行为:
    - DB_PATH 不存在 → 直接建
    - DB_PATH 已存在 → 备份到 DB_PATH.bak.YYYYMMDD_HHMMSS 再覆盖
    - 建完校验 14 张 srcradar 表都存在;缺则报错退出 2

退出码:
    0 = 成功
    2 = 缺表 / schema.sql 缺失 / sqlite 执行失败
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "db" / "recon.sqlite3"
DEFAULT_SCHEMA = REPO / "db" / "schema.sql"

NEED_TABLES = {
    "alterx_runs", "businesses", "companies", "mapp_records",
    "permutation_state", "recon_business_config", "run_markers",
    "scopes", "service_type_map", "tcp_assets", "web_hash_urls",
    "web_hashes", "web_subdomain_scan_schedule", "web_subdomains",
}


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    schema_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SCHEMA

    if not schema_path.exists():
        print(f"[err] schema.sql 缺失: {schema_path}", file=sys.stderr)
        return 2

    # 已存在 → 备份
    if db_path.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = db_path.with_suffix(db_path.suffix + f".bak.{ts}")
        print(f"[warn] DB 已存在 → 备份到 {bak}")
        shutil.move(str(db_path), str(bak))

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 喂 schema
    sql = schema_path.read_text(encoding="utf-8")
    try:
        con = sqlite3.connect(str(db_path))
        con.executescript(sql)
        con.commit()
    except sqlite3.Error as e:
        print(f"[err] sqlite 执行失败: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()

    # 校验
    con = sqlite3.connect(str(db_path))
    try:
        got = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = NEED_TABLES - got
        if missing:
            print(f"[err] DB 建好后缺表: {sorted(missing)}", file=sys.stderr)
            print(f"[err]  schema.sql 不完整,看 db/schema.sql", file=sys.stderr)
            return 2

        n_tbl   = len(got)
        n_idx   = len(list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")))
        n_trig  = len(list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")))
    finally:
        con.close()

    print(f"[ok] DB 初始化: {db_path}")
    print(f"     {n_tbl} tables / {n_idx} indexes / {n_trig} triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
