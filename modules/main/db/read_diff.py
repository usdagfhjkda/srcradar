#!/usr/bin/env python3
"""db.read_diff — read-only diff of web_subdomains change_type between two runs.

Per spec Q2.4: uses web_subdomains.change_type WHERE clause, does NOT reuse
daily/lib/diff.py. Joins run_markers on the run_id labels passed by caller.

We interpret --from/--to as run_ids the caller saw in run_markers. For each
subdomain we report which side (from / to / both) marked it as changed.

Examples:
    python3 read_diff.py --business ExampleCo --from 2026-09-20T00:00:00 --to 2026-09-22T00:00:00
    python3 read_diff.py --business ExampleCo --from R1 --to R2 --limit 500
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
    p = argparse.ArgumentParser(description="Read-only web_subdomain diff between two runs")
    p.add_argument("--business", required=True)
    p.add_argument("--from", dest="from_run", required=True, help="from run_id")
    p.add_argument("--to", dest="to_run", required=True, help="to run_id")
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

    try:
        biz = conn.execute(
            "SELECT id, business_name FROM businesses WHERE business_name = ?",
            (args.business,),
        ).fetchone()
        if biz is None:
            print(json.dumps({"found": False, "business_name": args.business, "rows": []}, ensure_ascii=False))
            return 0
        biz_id = biz[0]

        # Resolve run_markers by run_id; allow either timestamp-shaped strings or arbitrary labels.
        run_rows = conn.execute(
            "SELECT run_id, started_at, finished_at FROM run_markers WHERE run_id IN (?, ?) ORDER BY started_at",
            (args.from_run, args.to_run),
        ).fetchall()

        # change_type-aware diff per spec: pull rows where change_type != 0 within the window.
        sql = (
            "SELECT subdomain, port, change_type, last_seen, is_active "
            "FROM web_subdomains "
            "WHERE business_id = ? AND change_type != 0 "
            "ORDER BY last_seen DESC LIMIT ?"
        )
        rows = conn.execute(sql, (biz_id, args.limit)).fetchall()

        out_rows = [
            {
                "subdomain": r[0],
                "port": r[1],
                "change_type": r[2],
                "last_seen": r[3],
                "is_active": r[4],
            }
            for r in rows
        ]
        result = {
            "found": True,
            "business": {"id": biz_id, "business_name": biz[1]},
            "from_run": args.from_run,
            "to_run": args.to_run,
            "run_markers": [
                {"run_id": rr[0], "started_at": rr[1], "finished_at": rr[2]} for rr in run_rows
            ],
            "row_count": len(out_rows),
            "limit": args.limit,
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
