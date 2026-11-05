#!/usr/bin/env python3
"""init_db.py — build an empty recon.sqlite3 from db/schema.sql.

Usage:
    python3 init_db.py [DB_PATH] [SCHEMA_SQL]

    DB_PATH      default <repo>/db/recon.sqlite3
    SCHEMA_SQL   default <repo>/db/schema.sql

Behaviour:
    - DB_PATH does not exist → create it directly
    - DB_PATH already exists → back it up to DB_PATH.bak.YYYYMMDD_HHMMSS, then overwrite
    - after building, verify that all 14 srcradar tables exist; if any is missing, error out with exit 2

Exit codes:
    0 = success
    2 = missing table / schema.sql missing / sqlite execution failed
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "db" / "recon.sqlite3"
DEFAULT_SCHEMA = REPO / "db" / "schema.sql"

NEED_TABLES = {
    "alterx_runs", "brute_runs", "businesses", "companies", "mapp_records",
    "permutation_state", "recon_business_config", "run_markers",
    "scopes", "service_type_map", "tcp_assets", "web_hash_urls",
    "web_hashes", "web_subdomain_scan_schedule", "web_subdomains",
}


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    schema_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SCHEMA

    if not schema_path.exists():
        print(t("main.init_db.schema_missing", {"schema_path": schema_path}), file=sys.stderr)
        return 2

    # already exists → back it up
    if db_path.exists():
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = db_path.with_suffix(db_path.suffix + f".bak.{ts}")
        print(t("main.init_db.backup_written", {"bak": bak}))
        shutil.move(str(db_path), str(bak))

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # feed the schema
    sql = schema_path.read_text(encoding="utf-8")
    try:
        con = sqlite3.connect(str(db_path))
        con.executescript(sql)
        con.commit()
    except sqlite3.Error as e:
        print(t("main.init_db.sqlite_failed", {"e": e}), file=sys.stderr)
        return 2
    finally:
        con.close()

    # verify
    con = sqlite3.connect(str(db_path))
    try:
        got = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = NEED_TABLES - got
        if missing:
            print(t("main.init_db.missing_tables", {"tables": sorted(missing)}), file=sys.stderr)
            print(t("main.init_db.schema_incomplete"), file=sys.stderr)
            return 2

        n_tbl   = len(got)
        n_idx   = len(list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")))
        n_trig  = len(list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")))
    finally:
        con.close()

    print(t("main.init_db.initialized", {"db_path": db_path}))
    print(f"     {n_tbl} tables / {n_idx} indexes / {n_trig} triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
