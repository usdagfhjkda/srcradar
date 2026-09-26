#!/usr/bin/env python3
"""db.read_business_summary — read-only summary of one or all businesses.

Outputs single-line JSON. Uses prepared statements only. No --sql passthrough.

Examples:
    python3 read_business_summary.py --business ExampleCo
    python3 read_business_summary.py --all
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
    if db_path.startswith("file:"):
        # Explicit uri form — still force read-only.
        uri = db_path if "mode=ro" in db_path else db_path + "?mode=ro"
    else:
        uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    # Defense-in-depth: even if mode=ro somehow degraded, refuse writes.
    conn.execute("PRAGMA query_only = ON;")
    return conn


def _emit_warning(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def _row_to_dict(row, cols):
    return {c: row[idx] for idx, c in enumerate(cols)}


def fetch_one(conn, business_name):
    cur = conn.execute(
        "SELECT id, business_name, change_type FROM businesses WHERE business_name = ?",
        (business_name,),
    )
    biz = cur.fetchone()
    if biz is None:
        return {"found": False, "business_name": business_name}

    biz_id = biz[0]
    cols_biz = ["id", "business_name", "change_type"]

    counts = {}
    for label, sql in (
        ("companies", "SELECT COUNT(*) FROM companies WHERE business_id = ?"),
        ("mapp_records", "SELECT COUNT(*) FROM mapp_records WHERE company_id IN (SELECT id FROM companies WHERE business_id = ?)"),
        ("web_subdomains_total", "SELECT COUNT(*) FROM web_subdomains WHERE business_id = ?"),
        ("web_subdomains_active", "SELECT COUNT(*) FROM web_subdomains WHERE business_id = ? AND is_active = 1"),
        ("tcp_assets_total", "SELECT COUNT(*) FROM tcp_assets WHERE business_id = ?"),
        ("tcp_assets_active", "SELECT COUNT(*) FROM tcp_assets WHERE business_id = ? AND is_active = 1"),
        ("scopes", "SELECT COUNT(*) FROM scopes WHERE business_id = ?"),
        ("web_hashes", "SELECT COUNT(*) FROM web_hashes WHERE business_id = ?"),
    ):
        c = conn.execute(sql, (biz_id,) if "company_id IN" not in sql else (biz_id,))
        counts[label] = c.fetchone()[0]

    cfg = conn.execute(
        "SELECT enabled, web, tcp, icp FROM recon_business_config WHERE business_id = ?",
        (biz_id,),
    ).fetchone()
    if cfg is None:
        config = {"enabled": None, "web": None, "tcp": None, "icp": None}
    else:
        config = {"enabled": cfg[0], "web": cfg[1], "tcp": cfg[2], "icp": cfg[3]}

    return {
        "found": True,
        "business": _row_to_dict(biz, cols_biz),
        "counts": counts,
        "config": config,
    }


def fetch_all(conn, limit):
    if limit > HARD_CAP:
        _emit_warning(f"--limit {limit} exceeds hard cap {HARD_CAP}; truncating")
        limit = HARD_CAP
    cur = conn.execute(
        "SELECT id, business_name, change_type FROM businesses ORDER BY id LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    cols = ["id", "business_name", "change_type"]
    return {"count": len(rows), "truncated": False, "businesses": [_row_to_dict(r, cols) for r in rows]}


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only business summary")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--business", help="business_name to look up")
    g.add_argument("--all", action="store_true", help="list all businesses (default limit 100, hard cap 10000)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"row limit (default {DEFAULT_LIMIT}, hard cap {HARD_CAP})")
    p.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or DEFAULT_DB)
    args = p.parse_args(argv)

    if not (1 <= args.limit <= HARD_CAP * 10):  # accept big, clamp below
        pass

    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(json.dumps({"error": {"code": 1001, "message": f"db open failed: {exc}"}}))
        return 2

    try:
        if args.business is not None:
            result = fetch_one(conn, args.business)
        else:
            result = fetch_all(conn, args.limit)
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
