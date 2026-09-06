#!/usr/bin/env python3
"""Apply schedule migration: web_subdomain_scan_schedule table.

Idempotent: re-runs are no-ops (CREATE TABLE IF NOT EXISTS + ALTER guards).

Usage:
    python3 lib/migrate_schedule.py /path/to/recon.sqlite3

Side effects: schema only. No row data is modified.

设计要点
========
- 1 subdomain = 1 行 schedule(同一子域不被两个业务复用)
- 复合 PK: (business_id, subdomain)
- sources TEXT: 逗号分隔子集,默认 'urlfinder'
- last_run_at: 上次"成功"扫描完成时间(ISO 8601);失败不写,下次以
  上次成功为基线判定复活(stayed-alive vs true reactivation)
- enabled: 0 = cron 跳过;用于 dashboard toggle "加入 / 移除每日扫描"
  按两次移除 = DELETE 行(语义比 enabled=0 更干净;audit 友好)

触发器 / diff
============
- 不接 change_type 触发器(schedule 本身不是扫描产物,是配置)
- diff.py 不消费本表(配置变更不入业务日报)
- 唯一被 cron 读的位置:run_one_business.sh 的 daily-url 阶段
"""
import sqlite3
import sys


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def has_column(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def main():
    if len(sys.argv) != 2:
        print("usage: migrate_schedule.py <db_path>", file=sys.stderr)
        sys.exit(2)
    db = sys.argv[1]
    conn = sqlite3.connect(db)
    try:
        if has_table(conn, "web_subdomain_scan_schedule"):
            print("[migrate_schedule] table web_subdomain_scan_schedule already exists, skip")
        else:
            conn.executescript("""
                CREATE TABLE web_subdomain_scan_schedule (
                    id           INTEGER PRIMARY KEY,
                    business_id  INTEGER NOT NULL,
                    subdomain    TEXT    NOT NULL,
                    sources      TEXT    NOT NULL DEFAULT 'urlfinder',
                    last_run_at  TEXT,
                    enabled      INTEGER NOT NULL DEFAULT 1,
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                    UNIQUE (business_id, subdomain)
                );

                CREATE INDEX idx_schedule_business
                    ON web_subdomain_scan_schedule(business_id);

                CREATE INDEX idx_schedule_enabled
                    ON web_subdomain_scan_schedule(enabled) WHERE enabled = 1;

                CREATE INDEX idx_schedule_last_run
                    ON web_subdomain_scan_schedule(last_run_at);
            """)
            print("[migrate_schedule] created table web_subdomain_scan_schedule (+ 3 indexes)")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()