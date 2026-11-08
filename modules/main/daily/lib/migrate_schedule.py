"""Apply schedule migration: web_subdomain_scan_schedule table.

Idempotent: re-runs are no-ops (CREATE TABLE IF NOT EXISTS + ALTER guards).

Usage:
    python3 lib/migrate_schedule.py /path/to/recon.sqlite3

Side effects: schema only. No row data is modified.

Design notes
============
- 1 subdomain = 1 schedule row (the same subdomain is not shared by two businesses)
- composite PK: (business_id, subdomain)
- sources TEXT: comma-separated subset, default 'urlfinder'
- last_run_at: completion time of the last "successful" scan (ISO 8601); a failed run is not written,
  the next run takes the last success as its baseline to decide resurrection (stayed-alive vs true reactivation)
- enabled: 0 = cron skips it; used by the dashboard toggle "add to / remove from daily scan";
  removing twice in a row = DELETE the row (cleaner semantics than enabled=0; audit friendly)

Triggers / diff
===============
- no change_type trigger (the schedule itself is not a scan product, it is config)
- diff.py does not consume this table (config changes do not go into the business daily report)
- the only place cron reads it: the daily-url stage of run_one_business.sh
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