#!/usr/bin/env python3
"""Apply change_type migration to web_hash_urls table (Phase 2).

Adds:
  - change_type INTEGER DEFAULT 0
  - AI trigger  trg_whu_ai  → INSERT sets change_type=1
  - AU trigger  trg_whu_au  → UPDATE sets change_type to bitmask

Idempotent: each ALTER uses a guard (column/trigger already exists → skip).

Usage:
    python3 lib/migrate_change_type_urls.py [DB_PATH]

设计要点
========
对齐 web_subdomains 的 change_type bitmask 语义:
  0 = clean
  1 = inserted   (AI 触发)
  2 = content changed (普通 upsert + 字段差异)
  4 = reactivated (is_active 0→1 AND OLD.last_seen < 上次成功扫描)
  6 = reactivated + content changed

核心 gate(用户 2026-08-26 拍板):
  "只有满足当前子域 && 非 js/css 才可能修改 change_type"
  → AU 触发器 WHEN 子句加 AND NEW.is_static = 0
  → INSERT 没有 is_static 限制(新行可能是 js/css,需要入库;
     但永远不会被染色 / 算 changed — 因为它的 change_type=1 也只有
     在 is_static=0 时触发器才会"维持"它为 1;等等,AI 触发器是无条件的)
  → 实际:AI 触发器无条件置 1;AU 触发器 gate is_static=0;
     这样静态行的 change_type 永远是 1(INSERT 时),不会被 diff 标 changed
     (因为 diff 只对 change_type>0 行做内容 diff 二次校验时会跳过)

复活判定:
  OLD.last_seen < COALESCE(
    (SELECT last_run_at FROM web_subdomain_scan_schedule
       WHERE business_id=NEW.business_id AND subdomain=NEW.subdomain
         AND enabled=1 LIMIT 1),
    OLD.last_seen)
  → 没有 schedule 行(从未开启每日扫描):永远 OLD.last_seen < OLD.last_seen = FALSE,
    等价于 0→1 永远是 bulk-deactivate-then-UPSERT 语义,置 0
    (保持触发器对未启用子域的"无变化"语义,不影响手动 scan-urls)

内容字段差异(WHEN 子句):
  status_code / content_length / word_count / title / redirect /
  link_source / risk_flag / is_dangerous / content_type
  (覆盖三个 source 共有的可变字段;path / url 是 identity,不变)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_change_type_urls.sql"


def main() -> int:
    # Default DB: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[2] of this file).
    db_path = sys.argv[1] if len(sys.argv) > 1 \
        else str(Path(__file__).resolve().parents[2] / "db" / "recon.sqlite3")
    p = Path(db_path)
    if not p.exists():
        print(f"DB not found: {p}", file=sys.stderr)
        return 2

    sql = SQL_PATH.read_text(encoding="utf-8")
    sql = "\n".join(
        line for line in sql.splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(web_hash_urls)")}
        if "change_type" in cols:
            print("change_type already present on web_hash_urls — migration is idempotent no-op.")
            return 0

        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    # Verify
    conn = sqlite3.connect(str(p))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(web_hash_urls)")}
        assert "change_type" in cols, "change_type missing on web_hash_urls"
        trigs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='web_hash_urls'"
        )}
        required = {"trg_whu_ai", "trg_whu_au"}
        missing = required - trigs
        if missing:
            print(f"FAIL: missing triggers: {missing}", file=sys.stderr)
            return 1
        print(f"  web_hash_urls: change_type=OK  triggers={sorted(trigs)}")
    finally:
        conn.close()
    print("migration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())