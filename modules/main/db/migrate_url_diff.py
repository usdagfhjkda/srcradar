"""migrate_url_diff.py — Add per-URL body storage columns to web_hash_urls.

Idempotent: inspects pragma_table_info('web_hash_urls') for each of
the four target columns (body / prev_body / curr_body / baseline_at)
and skips the corresponding ALTER if it is already present. This
matches the `pragma_table_info` guard pattern used elsewhere in the
project (the daily/lib migrate scripts do the same).

Usage:
    python3 migrate_url_diff.py [DB_PATH]

DB_PATH defaults to <repo>/db/recon.sqlite3 (parent of db/).

Returns 0 on success (whether all columns already existed, some
were added, or none), 2 on missing DB, 3 on sqlite error.

After the run, web_hash_urls has the new columns (if they were not
already present):
    body        BLOB  -- zlib(level=6) compressed current body
    prev_body   BLOB  -- zlib(level=6) compressed previous body
    curr_body   BLOB  -- zlib(level=6) compressed current snapshot
    baseline_at TEXT  -- ISO 8601 of last successful comparison
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_PATH = HERE / "migrate_url_diff.sql"

TARGET_COLUMNS = ("body", "prev_body", "curr_body", "baseline_at")


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names currently present on `table`."""
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    # pragma_table_info returns rows of (cid, name, type, notnull, dflt, pk)
    return {r[1] for r in rows}


def _missing(table_info: set[str], target: str) -> str | None:
    """Return the ALTER TABLE clause for `target` if it is missing, else None."""
    if target in table_info:
        return None
    return f"ALTER TABLE {TABLE} ADD COLUMN {target}{COLUMN_TYPES[target]}"


# Column types — mirrored 1:1 from migrate_url_diff.sql. Kept here so
# the runner can apply ALTER TABLE statements one at a time without
# re-reading the SQL file (the file is kept as the canonical record
# for operators running it by hand).
TABLE = "web_hash_urls"
COLUMN_TYPES = {
    "body":        " BLOB",
    "prev_body":   " BLOB",
    "curr_body":   " BLOB",
    "baseline_at": " TEXT",
}


def main() -> int:
    db_path = (
        Path(sys.argv[1]) if len(sys.argv) > 1
        else Path(__file__).resolve().parents[1] / "db" / "recon.sqlite3"
    )
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout = 30000")

        # Verify the table exists; if it does not, init_db.py has not
        # run yet and we cannot ALTER.
        existing_tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE,),
            ).fetchall()
        }
        if TABLE not in existing_tables:
            print(f"FAIL: table {TABLE!r} not present; run init_db.py first",
                  file=sys.stderr)
            return 3

        info = _existing_columns(conn, TABLE)

        added: list[str] = []
        skipped: list[str] = []
        for col in TARGET_COLUMNS:
            if col in info:
                skipped.append(col)
                continue
            try:
                conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col}{COLUMN_TYPES[col]}")
                added.append(col)
            except sqlite3.OperationalError as exc:
                # Race: another migration added it concurrently — treat as skip.
                if "duplicate column" in str(exc).lower():
                    skipped.append(col)
                    continue
                raise

        if added:
            conn.commit()
        else:
            # No-op on an already-migrated DB; we still want to commit
            # a no-op transaction so pragma_table_info sees the truth.
            conn.commit()
    finally:
        conn.close()

    # Verification — re-open and confirm.
    conn = sqlite3.connect(str(db_path))
    try:
        final = _existing_columns(conn, TABLE)
    finally:
        conn.close()

    missing = [c for c in TARGET_COLUMNS if c not in final]
    if missing:
        print(f"FAIL: columns still missing after migration: {missing}",
              file=sys.stderr)
        return 3

    print(
        f"OK: web_hash_urls url-diff columns "
        f"(added={added or '[]'}, skipped={skipped or '[]'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())