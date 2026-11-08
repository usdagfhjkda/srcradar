"""migrate_urls.py — create web_hash_urls table + add web_hashes.url_count column.

Idempotent: re-runs are no-ops. Mirrors migrate_score.py's style.

Usage:
    python3 lib/migrate_urls.py /path/to/recon.sqlite3

Side effects: schema only. No row data is modified.

Change history
========
  Phase 1   - the first version of web_hash_urls
  Phase 1.5 - the full URLFinder fields (redirect / link_source / risk_flag /
              is_dangerous / danger_reason)
  Phase 2   - the is_static column (user decision 2026-08-26)
              + change_type and the AI/AU triggers added later by
              migrate_change_type_urls.py

is_static semantics (decided by the user on 2026-08-26):
  1 = everything after the last '.' in the path is 'js' or 'css'
      (only these two are checked; other suffixes such as .png/.jpg do not
      count as static)
  0 = otherwise
  None = a leftover row from before the column existed (it took part in no
      decision), and the dashboard colouring ignores None rows

The value is computed when pdtm/scan_urls.py persist() runs;
the trg_whu_au trigger only writes change_type for rows with is_static=0,
which satisfies "only a subdomain that is in scope and is not js/css can
have its change_type modified".

Notes:
    - No FK constraints to web_hashes/businesses/web_subdomains (Q7:
      "automate-side issues go to docs" — no hard FK enforcement).
    - ALTER TABLE ADD COLUMN url_count to web_hashes — kept unmaintained
      (no trigger bump). scan_urls.py persist should update it manually
      after batch INSERT.
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

        # 3) the web_hash_urls column extensions (the full URLFinder fields
        #    plus the dangerous route markers)
        #   - redirect:       the URL redirect target (URLFinder's Redirect field)
        #   - link_source:    the upstream page the URL came from (URLFinder's
        #                    Source field, for link provenance)
        #   - risk_flag:      the high-risk keywords hit in the path,
        #                    comma separated; empty = none (derived)
        #   - is_dangerous:   a dangerous route flagged by URLFinder itself
        #                    (extracted from the Title field)
        #   - danger_reason:  the original reason text (usually "suspected
        #                    dangerous route, verification skipped")
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

        # 4) the is_static column (Phase 2 — decided by the user on
        #    2026-08-26)
        #   - semantics: only 'js' or 'css' after the last '.' in the path
        #     counts as static (other suffixes do not)
        #   - computed where: scan_urls.py persist() recomputes it on every
        #     INSERT/UPDATE
        #   - the trg_whu_au trigger (migrate_change_type_urls.py) uses this
        #     column as its gate
        #   - NULL by default means "a leftover row that took part in no
        #     decision"; the dashboard colouring ignores NULL
        #   - it is not NOT NULL so that the existing 7k+ rows stay
        #     compatible (no one-off backfill)
        if not has_column(conn, "web_hash_urls", "is_static"):
            conn.execute("ALTER TABLE web_hash_urls ADD COLUMN is_static INTEGER")
            print("[migrate_urls] added column web_hash_urls.is_static")
        else:
            print("[migrate_urls] column web_hash_urls.is_static already exists, skip")

        # 5) the indexes:
        #   - a risk_flag index (so the dashboard can filter by risk keyword)
        #   - an is_dangerous index (so the dashboard can list the dangerous
        #     routes URLFinder flagged itself)
        for idx_name, idx_sql in (
            ("idx_web_hash_urls_risk_flag",
             "CREATE INDEX idx_web_hash_urls_risk_flag "
             + "ON web_hash_urls(risk_flag) WHERE risk_flag != ''",)
            ("idx_web_hash_urls_is_dangerous",
             "CREATE INDEX idx_web_hash_urls_is_dangerous "
             + "ON web_hash_urls(is_dangerous) WHERE is_dangerous = 1",)
            ("idx_web_hash_urls_is_static",
             "CREATE INDEX idx_web_hash_urls_is_static "
             + "ON web_hash_urls(is_static) WHERE is_static IS NOT NULL",)
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