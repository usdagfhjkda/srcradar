#!/usr/bin/env python3
"""migrate_urls.py — create web_hash_urls table + add web_hashes.url_count column.

Idempotent: re-runs are no-ops. Mirrors migrate_score.py's style.

Usage:
    python3 lib/migrate_urls.py /path/to/recon.sqlite3

Side effects: schema only. No row data is modified.

变更历史
========
  Phase 1   - web_hash_urls 初版
  Phase 1.5 - URLFinder 完整字段(redirect / link_source / risk_flag /
              is_dangerous / danger_reason)
  Phase 2   - is_static 列(用户决策 2026-08-26)
              + 后续 migrate_change_type_urls.py 加 change_type + AI/AU 触发器

is_static 语义(用户 2026-08-26 拍板):
  1 = path 最后一个 '.' 之后 == 'js' 或 'css'
      (只检查这两个,其它 .png/.jpg 等不算静态)
  0 = 否则
  None = 旧行遗留(未参与判定),dashboard 染色忽略 None 行

持久化在 pdtm/scan_urls.py persist() 时算;
触发器 trg_whu_au 仅对 is_static=0 行写 change_type,
严格满足"只有满足当前子域 && 非 js/css 才可能修改 change_type"。

Notes:
    - No FK constraints to web_hashes/businesses/web_subdomains (Q7:
      "automate-side issues go to docs" — no hard FK enforcement).
    - ALTER TABLE ADD COLUMN url_count to web_hashes — kept unmaintained
      (no trigger bump). scan_urls.py persist should update it manually
      after batch INSERT.
"""
import sqlite3, sys


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
        print("usage: migrate_urls.py <db_path>", file=sys.stderr)
        sys.exit(2)
    db = sys.argv[1]
    conn = sqlite3.connect(db)
    try:
        # 1) web_hash_urls table (full DDL inline — no .sql file dependency)
        if has_table(conn, "web_hash_urls"):
            print("[migrate_urls] table web_hash_urls already exists, skip CREATE")
        else:
            conn.executescript("""
                CREATE TABLE web_hash_urls (
                    id              INTEGER PRIMARY KEY,
                    hash_id         INTEGER NOT NULL,
                    business_id     INTEGER NOT NULL,
                    subdomain       TEXT    NOT NULL,
                    source          TEXT    NOT NULL,
                    scheme          TEXT,
                    url             TEXT    NOT NULL,
                    host            TEXT    NOT NULL,
                    port            INTEGER,
                    path            TEXT,
                    status_code     INTEGER,
                    title           TEXT,
                    content_type    TEXT,
                    content_length  INTEGER,
                    response_hash   TEXT,
                    word_count      INTEGER,
                    first_seen      TEXT    NOT NULL,
                    last_seen       TEXT    NOT NULL,
                    fetched_at      TEXT    NOT NULL,
                    is_active       INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (hash_id, subdomain, url, source)
                );

                CREATE INDEX idx_web_hash_urls_hash_id
                    ON web_hash_urls(hash_id);

                CREATE INDEX idx_web_hash_urls_business_id
                    ON web_hash_urls(business_id);

                CREATE INDEX idx_web_hash_urls_subdomain
                    ON web_hash_urls(subdomain);

                CREATE INDEX idx_web_hash_urls_is_active
                    ON web_hash_urls(is_active);

                CREATE INDEX idx_web_hash_urls_source
                    ON web_hash_urls(source);
            """)
            print("[migrate_urls] created table web_hash_urls (+ 5 indexes)")

        # 2) web_hashes.url_count column
        if not has_column(conn, "web_hashes", "url_count"):
            conn.execute(
                "ALTER TABLE web_hashes ADD COLUMN url_count INTEGER NOT NULL DEFAULT 0"
            )
            print("[migrate_urls] added column web_hashes.url_count")
        else:
            print("[migrate_urls] column web_hashes.url_count already exists, skip")

        # 3) web_hash_urls 列扩展(URLFinder 完整字段 + 危险路由标记)
        #   - redirect:       URL 重定向目标(URLFinder 的 Redirect 字段)
        #   - link_source:    URL 来自哪个上游页面(URLFinder 的 Source 字段,链路溯源)
        #   - risk_flag:      path 里命中的高危关键词,逗号分隔;空 = 无(派生)
        #   - is_dangerous:   URLFinder 主动标记的危险路由(从 Title 字段抽取)
        #   - danger_reason:  危险原因原文(通常="疑似危险路由,已跳过验证")
        for col, decl in (
            ("redirect",      "TEXT"),
            ("link_source",   "TEXT"),
            ("risk_flag",     "TEXT NOT NULL DEFAULT ''"),
            ("is_dangerous",  "INTEGER NOT NULL DEFAULT 0"),
            ("danger_reason", "TEXT"),
        ):
            if not has_column(conn, "web_hash_urls", col):
                conn.execute(f"ALTER TABLE web_hash_urls ADD COLUMN {col} {decl}")
                print(f"[migrate_urls] added column web_hash_urls.{col}")
            else:
                print(f"[migrate_urls] column web_hash_urls.{col} already exists, skip")

        # 4) is_static 列(Phase 2 — 用户 2026-08-26 拍板)
        #   - 语义:path 最后一个 '.' 之后 == 'js' 或 'css' 才算静态(其它后缀不算)
        #   - 算的位置:scan_urls.py persist() 每次 INSERT/UPDATE 都同步算
        #   - 触发器 trg_whu_au(migrate_change_type_urls.py)用这个列 gate
        #   - 默认 NULL 表示"旧行遗留,未参与判定";dashboard 染色忽略 NULL
        #   - 不带 NOT NULL 是为了兼容已存在的 7k+ 行(不想一次性回填)
        if not has_column(conn, "web_hash_urls", "is_static"):
            conn.execute("ALTER TABLE web_hash_urls ADD COLUMN is_static INTEGER")
            print("[migrate_urls] added column web_hash_urls.is_static")
        else:
            print("[migrate_urls] column web_hash_urls.is_static already exists, skip")

        # 5) 索引:
        #   - risk_flag 索引(便于 dashboard 按风险关键词过滤)
        #   - is_dangerous 索引(便于 dashboard 列出 URLFinder 主动标记的危险路由)
        for idx_name, idx_sql in (
            ("idx_web_hash_urls_risk_flag",
             "CREATE INDEX idx_web_hash_urls_risk_flag "
             "ON web_hash_urls(risk_flag) WHERE risk_flag != ''"),
            ("idx_web_hash_urls_is_dangerous",
             "CREATE INDEX idx_web_hash_urls_is_dangerous "
             "ON web_hash_urls(is_dangerous) WHERE is_dangerous = 1"),
            # is_static 部分索引 — 触发器 / dashboard 过滤用,NULL 不索引
            ("idx_web_hash_urls_is_static",
             "CREATE INDEX idx_web_hash_urls_is_static "
             "ON web_hash_urls(is_static) WHERE is_static IS NOT NULL"),
        ):
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,),
            ).fetchone()
            if not row:
                conn.execute(idx_sql)
                print(f"[migrate_urls] created index {idx_name}")
            else:
                print(f"[migrate_urls] index {idx_name} already exists")

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()