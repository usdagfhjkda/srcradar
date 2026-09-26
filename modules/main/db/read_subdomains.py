#!/usr/bin/env python3
"""db.read_subdomains — read-only subdomains for one business.

Outputs single-line JSON. Uses prepared statements only. No --sql passthrough.

Examples:
    python3 read_subdomains.py --business ExampleCo
    python3 read_subdomains.py --business ExampleCo --since 2026-01-01
    python3 read_subdomains.py --business ExampleCo --limit 500
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_LIMIT = 100
HARD_CAP = 10000
DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "db" / "recon.sqlite3")
# Resolution matches pdtm bash scripts (e.g. pdtm/pipeline.sh:49):
#   1) $DB env  2) $RECON_DB env  3) DEFAULT_DB (module-local relative path)
# The dispatcher (./srcradar) and install.sh handle config/db.conf +
# /opt/srcradar/db/recon.sqlite3 fallback before invoking subprocess.


def _connect(db_path: str) -> sqlite3.Connection:
    uri = db_path if (db_path.startswith("file:") and "mode=ro" in db_path) else (
        db_path if db_path.startswith("file:") else f"file:{db_path}?mode=ro"
    )
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _emit_warning(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only subdomain listing")
    p.add_argument("--business", required=True)
    p.add_argument("--since", help="ISO date YYYY-MM-DD, filter last_seen >= since")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"limit (default {DEFAULT_LIMIT}, hard cap {HARD_CAP})")
    p.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or DEFAULT_DB)
    args = p.parse_args(argv)

    if args.limit > HARD_CAP:
        _emit_warning(f"--limit {args.limit} exceeds hard cap {HARD_CAP}; truncating to {HARD_CAP}")
        args.limit = HARD_CAP

    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(json.dumps({"error": {"code": 1001, "message": f"db open failed: {exc}"}}))
        return 2

    cols = [
        "id", "subdomain", "port", "url", "status_code", "content_length",
        "title", "technologies", "first_seen", "last_seen", "fetched_at",
        "is_active", "change_type",
    ]

    try:
        biz = conn.execute(
            "SELECT id, business_name FROM businesses WHERE business_name = ?",
            (args.business,),
        ).fetchone()
        if biz is None:
            print(json.dumps({"found": False, "business_name": args.business, "rows": []}, ensure_ascii=False))
            return 0
        biz_id = biz[0]

        sql = (
            "SELECT id, subdomain, port, url, status_code, content_length, title, "
            "technologies, first_seen, last_seen, fetched_at, is_active, change_type "
            "FROM web_subdomains WHERE business_id = ?"
        )
        params = [biz_id]
        if args.since:
            sql += " AND last_seen >= ?"
            params.append(args.since)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(args.limit)

        rows = conn.execute(sql, params).fetchall()
        out_rows = [{c: r[i] for i, c in enumerate(cols)} for r in rows]
        result = {
            "found": True,
            "business": {"id": biz_id, "business_name": biz[1]},
            "row_count": len(out_rows),
            "limit": args.limit,
            "since": args.since,
            "rows": out_rows,
        }
    except sqlite3.Error as exc:
        print(json.dumps({"error": {"code": 1002, "message": f"query failed: {exc}"}}))
        return 3
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — defensive cleanup
            pass

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
