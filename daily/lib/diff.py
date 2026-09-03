#!/usr/bin/env python3
"""Diff recon-DB since last run, reading change_type>0 rows directly from SQLite.

Replaces the old JSON-snapshot diff: reads only touched rows from the live DB,
classifies by `change_type` bitmask, atomically resets change_type=0 and writes
a new run_markers row in the same transaction.

Usage:
    diff.py <db_path> <report_dir> <run_id>

Report dir receives:
  summary.md
  added_<table>.csv / reactivated_<table>.csv / deactivated_<table>.csv
  changed_<table>.csv / deleted_<table>.csv
  full_snapshot.json (= a JSON dump of just the change_type>0 rows for offline view)

change_type bitmask (set by SQLite triggers in migrate_change_type.sql):
  0 = clean
  1 = newly inserted
  2 = content changed
  4 = reactivated (is_active 0→1)
  6 = reactivated + content changed

Classification:
  added        — change_type & 1
  reactivated  — web_subdomains/tcp_assets only; change_type & 4
  changed      — change_type & 2  (other bits don't matter)
  deactivated  — web_subdomains/tcp_assets only; is_active=0 AND change_type=0
                 AND last_seen >= before_ts (was active in or after the previous
                 run, no reactivation in this run)
  deleted      — never emitted (current schema doesn't hard-delete)
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import log  # noqa: E402

TIMESTAMP_NOISE = {
    "fetched_at", "updated_at", "last_seen", "first_seen", "created_at",
    "raw_json", "id", "hash_id", "company_id", "business_id", "change_type",
}


# ---------- per-table identity / content / display ----------

def _scope_key(r: dict) -> tuple:
    return (r["business_id"], r["scope_name"], r["asset"])


def _scope_content(r: dict) -> dict:
    return {"is_wildcard": r.get("is_wildcard")}


def _scope_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "scope_name": r["scope_name"],
        "asset": r["asset"],
        "is_wildcard": r.get("is_wildcard"),
        "fetched_at": r.get("fetched_at"),
    }


def _company_key(r: dict) -> tuple:
    return (r["business_id"], r["unit_name"])


def _company_content(r: dict) -> dict:
    return {"nature_name": r.get("nature_name"), "main_licence": r.get("main_licence")}


def _company_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "unit_name": r["unit_name"],
        "nature_name": r.get("nature_name"),
        "main_licence": r.get("main_licence"),
        "updated_at": r.get("updated_at"),
    }


def _mapp_key(r: dict) -> tuple:
    if r.get("service_licence"):
        return ("lic", r["company_id"], r["service_licence"])
    return ("nm", r["company_id"], r.get("service_name"), r.get("service_type"))


def _mapp_content(r: dict) -> dict:
    return {
        "domain": r.get("domain"),
        "content_type_name": r.get("content_type_name"),
        "record_updated_at": r.get("record_updated_at"),
    }


def _mapp_display(r: dict, biz: dict, co_by_id: dict) -> dict:
    return {
        "company": co_by_id.get(r["company_id"], ""),
        "service_licence": r.get("service_licence"),
        "service_name": r.get("service_name"),
        "service_type": r.get("service_type"),
        "domain": r.get("domain"),
        "fetched_at": r.get("fetched_at"),
    }


def _webhash_key(r: dict) -> tuple:
    return (r["business_id"], r["response_hash"])


def _webhash_content(r: dict) -> dict:
    return {"subdomain_count": r.get("subdomain_count")}


def _webhash_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "response_hash": r["response_hash"],
        "subdomain_count": r.get("subdomain_count"),
        "fetched_at": r.get("fetched_at"),
    }


def _websub_key(r: dict) -> tuple:
    return (r["business_id"], r["subdomain"], r["port"])


def _websub_content(r: dict) -> dict:
    return {
        "status_code": r.get("status_code"),
        "title": r.get("title"),
        "technologies": r.get("technologies"),
        "response_hash": r.get("response_hash"),
    }


def _websub_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "subdomain": r["subdomain"],
        "port": r["port"],
        "url": r.get("url"),
        "status_code": r.get("status_code"),
        "title": r.get("title"),
        "technologies": r.get("technologies"),
        "response_hash": r.get("response_hash"),
        "is_active": r.get("is_active"),
        "first_seen": r.get("first_seen"),
    }


def _tcp_key(r: dict) -> tuple:
    return (r["business_id"], r["ip"], r["port"])


def _tcp_content(r: dict) -> dict:
    return {"hosts": r.get("hosts"), "raw_value": r.get("raw_value")}


def _tcp_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "host": r["ip"],
        "port": r["port"],
        "hosts": r.get("hosts"),
        "is_active": r.get("is_active"),
        "first_seen": r.get("first_seen"),
    }


# 用户 2026-08-26 拍板:web_hash_urls 接 diff。
# - key=(business_id, subdomain, url, source)  与 UNIQUE 约束一致
# - content 字段与 AU 触发器 WHEN 子句对齐:status_code / content_length /
#   word_count / title / redirect / link_source / risk_flag / is_dangerous /
#   content_type (path/url 是 identity,不变;is_static 不参与 diff,纯粹是 gate)
# - has_active=True → deactivate 路径
def _whu_key(r: dict) -> tuple:
    return (r["business_id"], r["subdomain"], r["url"], r["source"])


def _whu_content(r: dict) -> dict:
    return {
        "status_code": r.get("status_code"),
        "content_length": r.get("content_length"),
        "word_count": r.get("word_count"),
        "title": r.get("title"),
        "redirect": r.get("redirect"),
        "link_source": r.get("link_source"),
        "risk_flag": r.get("risk_flag"),
        "is_dangerous": r.get("is_dangerous"),
        "content_type": r.get("content_type"),
    }


def _whu_display(r: dict, biz: dict) -> dict:
    return {
        "business": biz.get(r["business_id"], ""),
        "subdomain": r.get("subdomain", ""),
        "url": r["url"],
        "path": r.get("path", ""),
        "source": r.get("source", ""),
        "status_code": r.get("status_code"),
        "title": r.get("title"),
        "risk_flag": r.get("risk_flag", ""),
        "is_dangerous": r.get("is_dangerous", 0),
        "is_active": r.get("is_active"),
        "first_seen": r.get("first_seen"),
        "last_seen": r.get("last_seen"),
    }


TABLE_SPECS: dict[str, dict] = {
    "scopes": {
        "key": _scope_key, "content": _scope_content, "display": _scope_display,
        "has_active": False,
        "columns": ["business", "scope_name", "asset", "is_wildcard", "fetched_at"],
    },
    "companies": {
        "key": _company_key, "content": _company_content, "display": _company_display,
        "has_active": False,
        "columns": ["business", "unit_name", "nature_name", "main_licence", "updated_at"],
    },
    "mapp_records": {
        "key": _mapp_key, "content": _mapp_content, "display": _mapp_display,
        "has_active": False,
        "columns": ["company", "service_licence", "service_name", "service_type", "domain", "fetched_at"],
    },
    "web_hashes": {
        "key": _webhash_key, "content": _webhash_content, "display": _webhash_display,
        "has_active": False,
        "columns": ["business", "response_hash", "subdomain_count", "fetched_at"],
    },
    "web_subdomains": {
        "key": _websub_key, "content": _websub_content, "display": _websub_display,
        "has_active": True,
        "columns": ["business", "subdomain", "port", "url", "status_code", "title",
                    "technologies", "response_hash", "is_active", "first_seen"],
    },
    "tcp_assets": {
        "key": _tcp_key, "content": _tcp_content, "display": _tcp_display,
        "has_active": True,
        "columns": ["business", "host", "port", "hosts", "is_active", "first_seen"],
    },
    # 用户 2026-08-26:web_hash_urls 接 diff(Phase 2 — daily-url + toggle)
    "web_hash_urls": {
        "key": _whu_key, "content": _whu_content, "display": _whu_display,
        "has_active": True,
        "columns": ["business", "subdomain", "url", "path", "source",
                    "status_code", "title", "risk_flag", "is_dangerous",
                    "is_active", "first_seen", "last_seen"],
    },
}

# SELECT lists for change_type>0 reads. Joins replicate the JSON-snapshot shape
# so display functions (which expect business_id / response_hash on web_subdomains
# rows) work without modification.
READ_SQL: dict[str, str] = {
    "scopes": """
        SELECT id, business_id, scope_name, asset, is_wildcard,
               created_at, updated_at, fetched_at, change_type
          FROM scopes
         WHERE change_type > 0
    """,
    "companies": """
        SELECT id, business_id, unit_name, nature_name, main_licence,
               created_at, updated_at, change_type
          FROM companies
         WHERE change_type > 0
    """,
    "mapp_records": """
        SELECT id, company_id, source_data_id, service_name, service_licence,
               service_type, content_type_name, domain, record_updated_at,
               fetched_at, change_type
          FROM mapp_records
         WHERE change_type > 0
    """,
    "web_hashes": """
        SELECT id, business_id, response_hash, subdomain_count,
               created_at, updated_at, fetched_at, change_type
          FROM web_hashes
         WHERE change_type > 0
    """,
    "web_subdomains": """
        SELECT ws.id, ws.hash_id, ws.subdomain, ws.port, ws.url,
               ws.status_code, ws.content_length, ws.title, ws.technologies,
               ws.first_seen, ws.last_seen, ws.fetched_at, ws.is_active,
               wh.business_id AS business_id,
               wh.response_hash AS response_hash,
               ws.change_type AS change_type
          FROM web_subdomains ws
          JOIN web_hashes     wh ON wh.id = ws.hash_id
         WHERE ws.change_type > 0
    """,
    "tcp_assets": """
        SELECT id, business_id, ip, port, hosts,
               first_seen, last_seen, fetched_at, is_active, raw_value, change_type
          FROM tcp_assets
         WHERE change_type > 0
    """,
    # 用户 2026-08-26:web_hash_urls 接 diff;变化触发由 AI/AU trigger 写 change_type
    "web_hash_urls": """
        SELECT id, hash_id, business_id, subdomain, source,
               scheme, host, port, path, url,
               status_code, title, content_type, content_length, word_count,
               redirect, link_source, risk_flag, is_dangerous, danger_reason,
               first_seen, last_seen, fetched_at, is_active, change_type
          FROM web_hash_urls
         WHERE change_type > 0
    """,
}

# Deactivation read: only meaningful for has_active tables. Was active in or
# after the previous run, now is_active=0, wasn't re-flagged this run.
DEACTIVATE_SQL: dict[str, str] = {
    "web_subdomains": """
        SELECT ws.id, ws.hash_id, ws.subdomain, ws.port, ws.url,
               ws.status_code, ws.content_length, ws.title, ws.technologies,
               ws.first_seen, ws.last_seen, ws.fetched_at, ws.is_active,
               wh.business_id AS business_id,
               wh.response_hash AS response_hash
          FROM web_subdomains ws
          JOIN web_hashes     wh ON wh.id = ws.hash_id
         WHERE ws.is_active = 0
           AND ws.change_type = 0
           AND ws.last_seen >= ?
    """,
    "tcp_assets": """
        SELECT id, business_id, ip, port, hosts,
               first_seen, last_seen, fetched_at, is_active, raw_value
          FROM tcp_assets
         WHERE is_active = 0
           AND change_type = 0
           AND last_seen >= ?
    """,
    # 用户 2026-08-26:web_hash_urls 接 deactivate。
    #   之前的扫描里 is_active=1,本轮没扫到 → is_active 不会被 persist 翻回 1
    #   (scan_urls 现有逻辑只写 is_active=1),所以 deactivate 实际不会自然发生
    #   (除非后续加 "全清场" 逻辑)。但 diff 框架保留这个查询以备未来。
    "web_hash_urls": """
        SELECT id, hash_id, business_id, subdomain, source,
               scheme, host, port, path, url,
               status_code, title, content_type, content_length, word_count,
               redirect, link_source, risk_flag, is_dangerous, danger_reason,
               first_seen, last_seen, fetched_at, is_active
          FROM web_hash_urls
         WHERE is_active = 0
           AND change_type = 0
           AND last_seen >= ?
    """,
}


# ---------- CSV / Markdown output ----------

def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})


def _write_changed_csv(path: Path, rows: list[dict], base_columns: list[str]) -> None:
    cols = list(base_columns) + ["_before", "_after"]
    _write_csv(path, [{**{k: r.get(k, "") for k in base_columns},
                       "_before": r.get("_before", ""),
                       "_after": r.get("_after", "")} for r in rows],
               cols)


def _build_business_labels(conn: sqlite3.Connection) -> dict[int, str]:
    return {b["id"]: b["business_name"] or ""
            for b in conn.execute("SELECT id, business_name FROM businesses").fetchall()}


def _build_company_labels(conn: sqlite3.Connection) -> dict[int, str]:
    return {c["id"]: c["unit_name"] or ""
            for c in conn.execute("SELECT id, unit_name FROM companies").fetchall()}


def _safe_before(row: dict, content_fn: Callable) -> dict:
    return {k: v for k, v in content_fn(row).items()}


# ---------- diff core ----------

def _classify_table(
    name: str,
    rows: list[dict],
    spec: dict,
    biz_label: dict[int, str],
    co_label: dict[int, str],
) -> dict[str, list[dict]]:
    """Split change_type>0 rows into added/reactivated/changed buckets."""
    display_fn = spec["display"]
    content_fn = spec["content"]
    has_active = spec["has_active"]
    mapp = (name == "mapp_records")

    added, reactivated, changed = [], [], []
    for r in rows:
        ct = r.get("change_type") or 0
        disp = display_fn(r, biz_label) if not mapp else display_fn(r, biz_label, co_label)
        if ct & 1:
            added.append(disp)
        if has_active and (ct & 4):
            reactivated.append(disp)
        if ct & 2:
            # 'changed' rows only have current state — we don't store
            # _before/_after anymore (state_log not implemented). The CSV
            # row shows what's there now; consumers needing diff detail
            # can query the DB directly.
            changed.append(disp)
    return {"added": added, "reactivated": reactivated, "changed": changed}


def _deactivate_rows(
    name: str,
    rows: list[dict],
    spec: dict,
    biz_label: dict[int, str],
    co_label: dict[int, str],
) -> list[dict]:
    display_fn = spec["display"]
    mapp = (name == "mapp_records")
    out = []
    for r in rows:
        out.append(display_fn(r, biz_label) if not mapp else display_fn(r, biz_label, co_label))
    return out


# ---------- run_marker helpers ----------

def _read_before_ts(conn: sqlite3.Connection) -> str | None:
    """started_at of the most-recent FINISHED run_marker (= previous diff boundary)."""
    row = conn.execute(
        "SELECT started_at FROM run_markers "
        "WHERE finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------- main entry ----------

def write_report(
    conn: sqlite3.Connection,
    out_dir: Path,
    run_id: str,
    per_business_warnings: dict[str, list[str]] | None = None,
) -> int:
    """Read change_type>0 rows from `conn`, classify, emit report.
    Caller owns the transaction — pass a connection already in BEGIN IMMEDIATE."""
    before_ts = _read_before_ts(conn)
    if before_ts is None:
        log.warn("no finished run_markers row found; "
                 "first run after migration — before_ts=NULL, no deactivation events")

    biz_label = _build_business_labels(conn)
    co_label = _build_company_labels(conn)

    out_dir.mkdir(parents=True, exist_ok=True)

    summary_lines: list[str] = []
    summary_lines.append(f"# SRC 资产监控日报 — {run_id}")
    summary_lines.append("")
    summary_lines.append(f"- 对比基线: `run_markers.started_at < {before_ts or 'N/A'}`")
    summary_lines.append(f"- 本次 diff 时间: `{_now_iso()}`")
    summary_lines.append("")

    grand_total = 0
    snapshot_dump: dict[str, list[dict]] = {}

    for table, spec in TABLE_SPECS.items():
        rows = [dict(r) for r in conn.execute(READ_SQL[table]).fetchall()]
        snapshot_dump[table] = rows
        cls = _classify_table(table, rows, spec, biz_label, co_label)

        # Deactivation — separate query, only for has_active tables.
        deactivated: list[dict] = []
        if spec["has_active"] and before_ts is not None and table in DEACTIVATE_SQL:
            d_rows = [dict(r) for r in conn.execute(DEACTIVATE_SQL[table], (before_ts,)).fetchall()]
            deactivated = _deactivate_rows(table, d_rows, spec, biz_label, co_label)

        # CSVs
        if cls["added"]:
            _write_csv(out_dir / f"added_{table}.csv", cls["added"], spec["columns"])
        if cls["reactivated"]:
            _write_csv(out_dir / f"reactivated_{table}.csv", cls["reactivated"], spec["columns"])
        if deactivated:
            _write_csv(out_dir / f"deactivated_{table}.csv", deactivated, spec["columns"])
        if cls["changed"]:
            _write_changed_csv(out_dir / f"changed_{table}.csv", cls["changed"], spec["columns"])

        n_a = len(cls["added"])
        n_re = len(cls["reactivated"])
        n_de = len(deactivated)
        n_chg = len(cls["changed"])
        n_total = n_a + n_re + n_de + n_chg
        grand_total += n_total
        summary_lines.append(
            f"## {table}  (+{n_a} / ↻{n_re} / ↘{n_de} / ✎{n_chg})"
        )
        if n_total == 0:
            summary_lines.append("  - 无变化")
            summary_lines.append("")
            continue

        def _fmt(items: list[dict], n: int) -> list[str]:
            out = []
            shown = items[:n]
            for r in shown:
                keys = ["business", "company", "unit_name", "subdomain", "host",
                        "asset", "response_hash", "service_licence", "service_name"]
                desc = ", ".join(f"{k}={r[k]}" for k in keys if r.get(k))
                out.append(f"    - {desc}")
            if len(items) > n:
                out.append(f"    - … 还有 {len(items) - n} 条")
            return out

        if n_a:
            summary_lines.append(f"  - 新增 ({n_a}):")
            summary_lines += _fmt(cls["added"], 20)
        if n_re:
            summary_lines.append(f"  - 复活 ({n_re}):")
            summary_lines += _fmt(cls["reactivated"], 20)
        if n_de:
            summary_lines.append(f"  - 失活 ({n_de}):")
            summary_lines += _fmt(deactivated, 20)
        if n_chg:
            summary_lines.append(f"  - 内容变更 ({n_chg}):")
            summary_lines += _fmt(cls["changed"], 20)
        summary_lines.append("")

    summary_lines.insert(3, f"- 本次变更总数: **{grand_total}**")
    summary_lines.insert(4, "")

    if per_business_warnings:
        summary_lines.append("## 业务运行告警")
        summary_lines.append("")
        any_warn = False
        for biz, warns in per_business_warnings.items():
            if not warns:
                continue
            any_warn = True
            summary_lines.append(f"- **{biz}**: {', '.join(warns)}")
        if not any_warn:
            summary_lines.pop()
            summary_lines.pop()

    # Dump a tiny JSON snapshot of just the touched rows for offline inspection.
    # This is NOT used by diff; it's only for human eyeballing.
    (out_dir / "full_snapshot.json").write_text(
        json.dumps(snapshot_dump, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (out_dir / "summary.md").write_text("\n".join(summary_lines) + "\n",
                                       encoding="utf-8")
    log.info(f"report written: {out_dir}/summary.md ({grand_total} changes)")
    return 0


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: diff.py <db_path> <report_dir> <run_id>", file=sys.stderr)
        return 2
    db_path, report_dir, run_id = sys.argv[1], Path(sys.argv[2]), sys.argv[3]

    if not Path(db_path).exists():
        print(f"ERROR: db not found: {db_path}", file=sys.stderr)
        return 2

    # Allow warnings to be supplied via env (same TSV format as before).
    per_business_warnings: dict[str, list[str]] = {}
    wf = os.environ.get("WARNINGS_FILE")
    if wf and Path(wf).is_file():
        for line in Path(wf).read_text(encoding="utf-8").splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0]:
                per_business_warnings.setdefault(parts[0], []).append(parts[1])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rc = write_report(conn, report_dir, run_id, per_business_warnings)
            if rc != 0:
                conn.rollback()
                return rc
            # Reset change_type=0 across all 7 tables atomically with the diff.
            for t in ("businesses", "scopes", "companies", "mapp_records",
                      "web_hashes", "web_subdomains", "tcp_assets"):
                conn.execute(f"UPDATE {t} SET change_type = 0")
            # Stamp this run's marker. daily_monitor.sh passes RUN_START_AT
            # (the actual run start, before pipeline) so the subquery in
            # round-2 triggers sees the correct boundary for the next run.
            now = _now_iso()
            started_at = os.environ.get("RUN_START_AT") or now
            conn.execute(
                "INSERT INTO run_markers (run_id, started_at, finished_at) VALUES (?, ?, ?)",
                (run_id, started_at, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())