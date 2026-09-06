#!/usr/bin/env python3
"""Recon-DB dashboard — ARL 4-tab style, served read-only on localhost.

Reads recon.sqlite3 directly via SQLite (no JSON snapshot layer). The cached
HTML is rebuilt every `reload_interval` seconds (default 30, override with
DASHBOARD_RELOAD env), or on demand via GET /refresh.

Usage:
  dashboard.py [--db PATH] [--port PORT] [--host HOST]

Defaults:
  --db    <repo>/db/recon.sqlite3
  --port  8765
  --host  127.0.0.1   <-- intentionally; see SECURITY below

SECURITY
========
The server binds to 127.0.0.1 ONLY. To access it from your laptop, forward
the port over SSH:

    ssh -L 8765:127.0.0.1:8765 user@recon-host
    open http://localhost:8765

No auth is enforced — the assumption is that the SSH tunnel authenticates the
caller. If you change --host to 0.0.0.0 you will expose the recon data to
everyone on the network; don't.

Endpoints
=========
  GET /                HTML page (rebuilt on reload interval / /refresh)
  GET /api/snapshot    raw JSON dump of current DB state (same shape the old
                       snapshot.py produced, for downstream scripts)
  GET /refresh         force an immediate reload from DB; returns "reloaded"
  GET /<业务名>        per-business view (e.g. /ExampleCo, /DemoCorp)
  GET /<业务名>/new    per-business "new since last diff" view
  GET /diff            global "new since last diff" view
  GET /health          "ok"

The page is read-only — only GET is implemented.
"""
from __future__ import annotations

import argparse
import ast
import csv
import gzip
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import log  # noqa: E402

# ---------------------------------------------------------------------------
# Service classification (port → category + risk)
# ---------------------------------------------------------------------------
# risk: 'high' (red), 'med' (yellow), 'low' (green), 'info' (gray).
# Categories: web / db / remote / mail / other.
# Numbers from common port-knowledge base — not exhaustive, but enough to
# flag the obvious exposures.

_DB_PORTS = {
    3306:   "MySQL",         5432:  "PostgreSQL",   27017: "MongoDB",
    6379:   "Redis",         1433:  "MSSQL",        9200:  "Elasticsearch",
    11211:  "Memcached",     8529:  "RethinkDB",    5984:  "CouchDB",
    28017:  "MongoDB-HTTP",  1521:  "OracleDB",     50000: "SAP",
}

_REMOTE_PORTS = {
    22: "SSH", 23: "Telnet", 3389: "RDP", 5900: "VNC", 5901: "VNC-1",
}

_MAIL_PORTS = {
    25: "SMTP", 110: "POP3", 143: "IMAP", 465: "SMTPS",
    587: "SMTP-submission", 993: "IMAPS", 995: "POP3S",
}

_WEB_ALWAYS_LOW = {80, 443, 8080, 8443, 8000, 8888, 9090}

_DB_HIGH = {1433, 3306, 5432, 6379, 9200, 11211, 27017, 28017, 5984, 8529, 1521, 50000}
_REMOTE_HIGH = {23, 3389, 5900, 5901}


def classify_port(port: int) -> dict:
    """Return {category, service, risk} for a TCP port."""
    if port in _WEB_ALWAYS_LOW or 8000 <= port <= 8999:
        return {"category": "web", "service": "http", "risk": "low"}
    if port in _DB_PORTS:
        risk = "high" if port in _DB_HIGH else "med"
        return {"category": "db", "service": _DB_PORTS[port], "risk": risk}
    if port in _REMOTE_PORTS:
        risk = "high" if port in _REMOTE_HIGH else "med"
        return {"category": "remote", "service": _REMOTE_PORTS[port], "risk": risk}
    if port in _MAIL_PORTS:
        return {"category": "mail", "service": _MAIL_PORTS[port], "risk": "med"}
    if port in (161, 162):
        return {"category": "snmp", "service": "SNMP", "risk": "med"}
    if port == 69:
        return {"category": "tftp", "service": "TFTP", "risk": "med"}
    if port > 10000:
        return {"category": "high-port", "service": "unknown", "risk": "med"}
    return {"category": "other", "service": "unknown", "risk": "info"}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _biz_label(businesses: list[dict]) -> dict[int, str]:
    return {b["id"]: b.get("business_name") or f"#{b['id']}" for b in businesses}


def _host_of(r: dict) -> str:
    """Derive a single 'host' key from a tcp_assets row.

    New schema has `hosts` (comma-separated multi-domain string) and no
    `host` column; we take the first entry. Older snapshots may still carry
    a bare `host` field — prefer it when present.
    """
    if r.get("host"):
        return r["host"]
    hn = (r.get("hosts") or "").split(",")
    return hn[0].strip() if hn and hn[0].strip() else "(unknown)"


def _aggregate(snap: dict) -> dict:
    tables = snap.get("tables", {})
    biz = _biz_label(tables.get("businesses", []))

    tcp = tables.get("tcp_assets", [])
    ws = tables.get("web_subdomains", [])
    wh = tables.get("web_hashes", [])
    scopes = tables.get("scopes", [])

    # Per-host port counts and risk rollup
    by_host: dict[str, list[dict]] = defaultdict(list)
    risk_by_host: dict[str, Counter] = defaultdict(Counter)
    port_class_counter: Counter = Counter()
    risk_counter: Counter = Counter()
    risky_open: list[dict] = []
    for r in tcp:
        h = _host_of(r)
        cls = classify_port(r["port"])
        by_host[h].append({**r, **cls})
        port_class_counter[cls["category"]] += 1
        risk_counter[cls["risk"]] += 1
        if cls["risk"] == "high":
            risky_open.append({
                "host": h,
                "port": r["port"],
                "service": cls["service"],
                "category": cls["category"],
                "business": biz.get(r["business_id"], ""),
            })

    # Web fingerprint concentration
    fp_count = Counter()
    fp_first_sub: dict[str, str] = {}
    fp_first_title: dict[str, str] = {}
    fp_first_status: dict[str, int] = {}
    fp_first_tech: dict[str, str] = {}
    for r in ws:
        h = r.get("response_hash") or ""
        fp_count[h] += 1
        if h not in fp_first_sub:
            fp_first_sub[h] = r["subdomain"]
            fp_first_title[h] = r.get("title") or ""
            fp_first_status[h] = r.get("status_code") or 0
            fp_first_tech[h] = r.get("technologies") or ""
    fp_rows = sorted(
        [{"hash": h, "count": c, "sample": fp_first_sub.get(h, ""),
          "title": fp_first_title.get(h, ""),
          "status": fp_first_status.get(h, 0),
          "tech": fp_first_tech.get(h, "")}
         for h, c in fp_count.items()],
        key=lambda x: -x["count"],
    )

    # Hosts with too many open ports — likely a port scan range, not web
    fat_hosts = sorted(
        [{"host": h, "port_count": len(rows),
          "sample_ports": ", ".join(str(r["port"]) for r in rows[:8])}
         for h, rows in by_host.items() if len(rows) >= 50],
        key=lambda x: -x["port_count"],
    )

    # Web analysis (separate from TCP)
    web_status_codes: Counter = Counter()
    web_techs: Counter = Counter()
    web_root_domains: Counter = Counter()
    for r in ws:
        web_status_codes[r.get("status_code") or 0] += 1
        techs_raw = r.get("technologies") or ""
        if techs_raw:
            try:
                techs = ast.literal_eval(techs_raw)
            except (ValueError, SyntaxError):
                techs = []
            for t in techs:
                web_techs[t] += 1
        parts = r["subdomain"].split(".")
        if len(parts) >= 2:
            web_root_domains[".".join(parts[-2:])] += 1

    # Fingerprint concentration buckets — high coverage = low value (batch /
    # default landing page), low coverage = high value (unique site).
    fp_buckets = {"unique (1 子域)": 0, "2-10 子域": 0,
                  "11-50 子域": 0, "≥51 子域 (批量/默认页)": 0}
    for c in fp_count.values():
        if c == 1:
            fp_buckets["unique (1 子域)"] += 1
        elif c <= 10:
            fp_buckets["2-10 子域"] += 1
        elif c <= 50:
            fp_buckets["11-50 子域"] += 1
        else:
            fp_buckets["≥51 子域 (批量/默认页)"] += 1

    # Subdomains per host
    sub_per_host = Counter(r["subdomain"].split(".", 1)[-1] if "." in r["subdomain"] else r["subdomain"]
                           for r in ws)

    # ICP-filing assets: 子公司 (companies) + 小程序/公众号 (mapp_records).
    # mapp_records.service_type == 7 → 微信小程序; 其它值归为公众号/其它服务。
    companies = tables.get("companies", [])
    mapp = tables.get("mapp_records", [])
    comp_name = {c["id"]: c.get("unit_name") or "" for c in companies}
    mp_records: list[dict] = []   # 小程序
    oa_records: list[dict] = []   # 公众号 / 其它
    comp_counts = {c["id"]: {"unit_name": c.get("unit_name") or "",
                             "business": biz.get(c.get("business_id"), ""),
                             "mp": 0, "oa": 0} for c in companies}
    for r in mapp:
        rec = {**r, "unit_name": comp_name.get(r.get("company_id"), "")}
        is_mp = r.get("service_type") == 7
        (mp_records if is_mp else oa_records).append(rec)
        cc = comp_counts.get(r.get("company_id"))
        if cc is not None:
            cc["mp" if is_mp else "oa"] += 1

    return {
        "captured_at": snap.get("captured_at", ""),
        "row_counts": snap.get("row_counts", {}),
        "tables": {
            "scopes": scopes, "tcp_assets": tcp, "web_subdomains": ws,
            "web_hashes": wh,
        },
        "biz": biz,
        "by_host": {h: rows for h, rows in by_host.items()},
        "risk_by_host": dict(risk_by_host),
        "port_class": dict(port_class_counter),
        "risk_counter": dict(risk_counter),
        "risky_open": risky_open,
        "fp_rows": fp_rows,
        "fat_hosts": fat_hosts,
        "web_status_codes": dict(web_status_codes),
        "web_techs": dict(web_techs),
        "web_root_domains": dict(web_root_domains),
        "web_fp_buckets": fp_buckets,
        "companies": companies,
        "mp_records": mp_records,
        "oa_records": oa_records,
        "comp_counts": list(comp_counts.values()),
        "biz_stats": _per_biz_stats(tables, biz),
    }


def _per_biz_stats(tables: dict, biz: dict[int, str]) -> list[dict]:
    """Per-business asset counts — used by the home page 业务 card link list.
    Empty rows (total == 0) are included so callers can decide to hide them.
    mapp_records are counted via companies.company_id → business_id join."""
    _BID_TABS = ("scopes", "companies", "web_subdomains", "web_hashes",
                 "tcp_assets")
    bid_counter: dict[str, Counter] = {
        t: Counter(r.get("business_id") for r in tables.get(t, [])
                   if r.get("business_id") is not None)
        for t in _BID_TABS
    }
    comp_to_bid = {c["id"]: c.get("business_id")
                   for c in tables.get("companies", [])
                   if c.get("business_id") is not None}
    mapp_by_bid = Counter(comp_to_bid.get(r.get("company_id"))
                          for r in tables.get("mapp_records", [])
                          if comp_to_bid.get(r.get("company_id")) is not None)

    out = []
    for b in tables.get("businesses", []):
        bid = b["id"]
        name = b.get("business_name") or f"#{bid}"
        if not b.get("business_name"):
            continue
        counts = {t: bid_counter[t].get(bid, 0) for t in _BID_TABS}
        counts["mapp_records"] = mapp_by_bid.get(bid, 0)
        out.append({
            "id": bid,
            "name": name,
            "href": "/" + urllib.parse.quote(name),
            **counts,
            "total": sum(counts.values()),
        })
    out.sort(key=lambda x: x["name"])
    return out


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #f4f5f7; color: #1f2328; }
header { background: #1f2328; color: #fff; padding: 14px 24px;
         display: flex; justify-content: space-between; align-items: center; }
header h1 { margin: 0; font-size: 18px; font-weight: 600; }
header .meta { font-size: 12px; opacity: 0.7; }
nav.tabs { background: #fff; border-bottom: 1px solid #d0d7de; padding: 0 24px;
           display: flex; gap: 0; position: sticky; top: 0; z-index: 10; }
nav.tabs button { background: none; border: none; padding: 14px 18px;
                  font-size: 14px; color: #57606a; cursor: pointer;
                  border-bottom: 2px solid transparent; }
nav.tabs button:hover { color: #1f2328; }
nav.tabs button.active { color: #0969da; border-bottom-color: #0969da; }
main { padding: 24px; max-width: 1400px; margin: 0 auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 16px; margin-bottom: 24px; }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
        padding: 16px; }
.card .label { font-size: 12px; color: #57606a; text-transform: uppercase;
               letter-spacing: 0.5px; }
.card .value { font-size: 28px; font-weight: 600; margin-top: 4px; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden;
        font-size: 13px; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eaecef; }
th { background: #f6f8fa; font-weight: 600; color: #57606a; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { background: #eaeef2; color: #1f2328; }
th.sortable.asc::after  { content: " ▲"; color: #57606a; font-size: 11px; }
th.sortable.desc::after { content: " ▼"; color: #57606a; font-size: 11px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f6f8fa; }
.risk-high { background: #ffebe9; }
.risk-high td:first-child { border-left: 3px solid #cf222e; }
.risk-med  { background: #fff8c5; }
.risk-med  td:first-child { border-left: 3px solid #9a6700; }
.risk-low  td:first-child { border-left: 3px solid #1a7f37; }
.risk-info td:first-child { border-left: 3px solid #6e7781; }
.bar { display: inline-block; height: 14px; background: #0969da;
       border-radius: 2px; vertical-align: middle; }
.bar-wrap { background: #eaecef; border-radius: 2px;
            display: inline-block; width: 220px; height: 14px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 12px;
       font-size: 11px; font-weight: 500; }
.tag-high  { background: #ffebe9; color: #cf222e; }
.tag-med   { background: #fff8c5; color: #9a6700; }
.tag-low   { background: #dafbe1; color: #1a7f37; }
.tag-info  { background: #eaeef2; color: #57606a; }
.tag-new     { background: #0969da; color: #fff; }   /* /new: 新 (added or reactivated) */
.tag-changed { background: #8250df; color: #fff; }   /* /new: 变 (any field diff) */
.note { background: #fff8c5; border-left: 3px solid #9a6700;
        padding: 10px 14px; border-radius: 4px; margin-bottom: 16px;
        font-size: 13px; }
.note-allnew { background: #dafbe1; border-left: 3px solid #1a7f37; }
.section-title { font-size: 16px; font-weight: 600; margin: 24px 0 12px; }
.sub-title   { font-size: 13px; font-weight: 600; color: #57606a;
               margin: 16px 0 8px; text-transform: uppercase; letter-spacing: 0.5px; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
.muted { color: #57606a; font-size: 12px; }
code { background: #eaeef2; padding: 1px 6px; border-radius: 3px;
       font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }
.filter { margin-bottom: 12px; }
.filter input { padding: 6px 10px; border: 1px solid #d0d7de;
                border-radius: 4px; width: 280px; font-size: 13px; }
.fp-warn { background: #fff8c5; padding: 12px; border-radius: 6px;
           border-left: 3px solid #9a6700; margin-bottom: 12px; }
.fp-warn b { color: #9a6700; }
.scroll-x { overflow-x: auto; }
.mono { font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }
.toggle { padding: 5px 12px; border: 1px solid #d0d7de;
          background: #fff; border-radius: 4px; cursor: pointer;
          font-size: 13px; color: #1f2328; }
.toggle:hover { background: #f6f8fa; }
.toggle.on { background: #0969da; color: #fff; border-color: #0969da; }
.hash-link { color: #0969da; text-decoration: none; cursor: pointer; }
.hash-link:hover { text-decoration: underline; }
.hash-count { color: #57606a; font-size: 11px; margin-left: 2px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.modal { position: fixed; inset: 0; background: rgba(31,35,40,0.5);
         z-index: 100; display: flex; align-items: center;
         justify-content: center; }
.modal-card { background: #fff; border-radius: 8px;
              max-width: 800px; max-height: 80vh; width: 90%;
              display: flex; flex-direction: column; overflow: hidden; }
.modal-header { padding: 12px 16px; border-bottom: 1px solid #d0d7de;
                display: flex; justify-content: space-between;
                align-items: center; font-weight: 600; }
#hash-modal-close { background: none; border: none; font-size: 24px;
                    cursor: pointer; padding: 0 8px; line-height: 1;
                    color: #57606a; }
#hash-modal-close:hover { color: #1f2328; }
#hash-modal-body { padding: 12px 16px; overflow: auto; font-size: 13px; }
#hash-modal-body table { font-size: 13px; }
#hash-modal-body a { color: #0969da; text-decoration: none; }
#hash-modal-body a:hover { text-decoration: underline; }
.biz-bar a.active { background:#0969da;color:#fff !important; }
.biz-bar a:hover { background:#444c56; }
.biz-card .value { font-size: 13px; font-weight: 400; margin-top: 6px; }
.biz-list { display: flex; flex-direction: column; gap: 4px; margin-top: 4px; }
.biz-link { display: flex; justify-content: space-between; align-items: center;
            padding: 5px 8px; border-radius: 4px; color: #0969da;
            text-decoration: none; font-size: 13px; }
.biz-link:hover { background: #f6f8fa; text-decoration: none; }
.biz-link .biz-name { font-weight: 500; }
.biz-link .biz-count { font-size: 11px; font-variant-numeric: tabular-nums; }

/* scan-onesite 表单 + 结果页 */
.scan-form { background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px;
             padding:10px 12px; margin:0 0 14px 0; font-size:13px; }
.scan-form summary { cursor:pointer; font-weight:600; color:#1f2328;
                     user-select:none; }
.scan-form textarea { width:100%; min-height:80px; padding:6px 10px;
                      border:1px solid #d0d7de; border-radius:4px;
                      font-family: ui-monospace, "SF Mono", monospace;
                      font-size:13px; box-sizing:border-box; }
.scan-form .hint { color:#57606a; font-size:12px; margin:6px 0 6px 0; }
.scan-form .submit-row { margin-top:8px; display:flex; align-items:center;
                         gap:10px; }
.scan-form .muted { color:#57606a; font-size:12px; }

.btn { padding:6px 14px; border:1px solid #d0d7de; background:#fff;
       border-radius:4px; cursor:pointer; font-size:13px; color:#1f2328;
       text-decoration:none; display:inline-block; }
.btn:hover { background:#f6f8fa; }
.btn.primary { background:#0969da; color:#fff; border-color:#0969da; }
.btn.primary:hover { background:#0550ae; border-color:#0550ae; }

.scan-result { background:#fff; border:1px solid #d0d7de; border-radius:6px;
               padding:18px 22px; margin:14px 0; max-width:720px; }
.scan-result.ok { border-left:4px solid #1a7f37; }
.scan-result.err { border-left:4px solid #cf222e; background:#fff5f5; }
.scan-result pre { background:#fff; border:1px solid #d0d7de; padding:8px;
                   border-radius:4px; max-height:320px; overflow:auto;
                   font-size:12px; white-space:pre-wrap; word-break:break-all; }
.scan-result h2 { margin:0 0 10px 0; font-size:18px; }

.score-cell { white-space:nowrap; }
.score-cell .score-input { width:4em; padding:2px 4px; border:1px solid #d0d7de;
                          border-radius:3px; font-size:12px; text-align:center;
                          font-variant-numeric: tabular-nums; }
.score-cell .score-input:focus { outline:none; border-color:#0969da; }
.score-cell .score-save { padding:0 6px; cursor:pointer; border:1px solid #d0d7de;
                          background:#f6f8fa; border-radius:3px; font-size:11px;
                          margin-left:2px; }
.score-cell .score-save:hover { background:#eaeef2; }
.desc-cell { min-width:240px; }
.desc-cell .desc-input { width:80%; padding:2px 4px; border:1px solid #d0d7de;
                          border-radius:3px; font-size:12px; }
.desc-cell .desc-status { display:inline-block; margin-left:6px; font-size:11px;
                           color:#57606a; min-width:6em; }
"""


# scan-onesite 表单 / POST 处理用的常量
SCAN_HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")
SCAN_MAX_HOSTS = 200
SCAN_MAX_BODY = 64 * 1024              # 64 KiB
SCAN_TIMEOUT = 180                      # subprocess.run 全局超时（秒）
SCAN_RES_OK_RE = re.compile(r"扫描完成：(\d+) 条")

# URL 资产扫描(scan-urls)相关常量(用户决策:cron 不参与,仅手动 dashboard 触发)
SCAN_URLS_MAX_HOSTS = 50                  # 单次 scan-urls host 上限
SCAN_URLS_MAX_BODY = 64 * 1024            # 64 KiB
SCAN_URLS_TIMEOUT = 600                   # subprocess.run 全局超时(用户拍板:大字典场景)
SCAN_URLS_RES_OK_RE = re.compile(r'"total_new_rows":\s*(\d+)')
SCAN_URLS_VALID_SOURCES = ("ffuf", "urlfinder", "gau")
SCAN_URLS_DEFAULT_WORDLIST = (
    # 优先 SCAN_URLS_WORDLIST env;否则看 $RECON_ROOT/tools/wordlists 或
    # ~/tools/wordlists 下的 SecLists common.txt。找不到就让运维明确,
    # 不要静默 fallback 到写死的 /home/ubuntu/...
    os.environ.get("SCAN_URLS_WORDLIST", "")
)


def _scan_urls_default_wordlist_suggestion() -> str:
    """表单建议值 —— 与 scan_urls.py._resolve_default_wordlist_path 同款探测。

    env 已显式设置时直接用 env;否则探测两个候选路径,有就用第一个命中的;
    都探测不到返回空串(表单渲染空 input,提交后 scan_urls.py 那边再报错)。
    """
    env = os.environ.get("SCAN_URLS_WORDLIST")
    if env:
        return env
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        project_root / "tools" / "wordlists" / "SecLists-master"
        / "Discovery" / "Web-Content" / "common.txt",
        Path.home() / "tools" / "wordlists" / "SecLists-master"
        / "Discovery" / "Web-Content" / "common.txt",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return ""


def _e(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ""


def _risk_tag(risk: str) -> str:
    cls = {"high": "tag-high", "med": "tag-med", "low": "tag-low"}.get(risk, "tag-info")
    label = {"high": "高", "med": "中", "low": "低", "info": "提示"}.get(risk, risk)
    return f'<span class="tag {cls}">{label}</span>'


def _build_scan_form(biz_name: str) -> str:
    """对当前业务发起单点扫描的折叠表单。biz_name 为空则首页 / 全局视图不渲染。"""
    if not biz_name:
        return ""
    action = "/" + urllib.parse.quote(biz_name) + "/scan"
    return f"""
<details class="scan-form">
  <summary>扫描并入库（向 {_e(biz_name)} 提交新域名）</summary>
  <form method="post" action="{_e(action)}">
    <div class="hint">一行一个域名，&le;{SCAN_MAX_HOSTS} 条；自动去重 / 小写 / 忽略空行与井号注释；
    入库会同步触发 httpx 探测，结果刷新到本页面数据。</div>
    <textarea name="hosts" rows="4" required
placeholder="example.com&#10;api.example.com"></textarea>
    <div class="submit-row">
      <button type="submit" class="btn primary">扫描并入库</button>
      <span class="muted">同步等待（典型 5-30s）</span>
    </div>
  </form>
</details>
"""


def _build_scan_urls_form(biz_name: str) -> str:
    """URL 资产扫描折叠表单(ffuf / URLFinder / gau)。点击 POST /<biz>/scan-urls。

    设计目标:
      - 只扫描已存在的 active 子域(POST handler 校验),不会扩散到未知域
      - 默认 source 勾选 urlfinder(主动爬虫、无字典依赖、最稳)
      - ffuf / gau 留给用户按需勾选
      - wordlist 暂用 SCAN_URLS_DEFAULT_WORDLIST;高级用户可在表单里改路径
    """
    if not biz_name:
        return ""
    action = "/" + urllib.parse.quote(biz_name) + "/scan-urls"
    # checkbox 默认勾选:urlfinder。ffuf/gau 默认不勾
    return f"""
<details class="scan-form">
  <summary>URL 资产扫描（向 {_e(biz_name)} 已收录的子域扫 URL 路径）</summary>
  <form method="post" action="{_e(action)}">
    <div class="hint">每行一个 <b>已存在的子域</b>(从站点详情 tab 复制)，&le;{SCAN_URLS_MAX_HOSTS} 个;
    只对各 subdomain 下的 <code>web_subdomains.is_active=1</code> 行做扫描;
    多次扫描同一目标 = 同 URL 不会被重复插入(ON CONFLICT UPDATE last_seen)。
    <br>⏱ 大字典 ffuf 单 host 可能 5 分钟以上,整体超时 {SCAN_URLS_TIMEOUT}s。
    <br>⚠ 不会自动跑 — 仅手动提交触发;结果异步刷到「URL 详情」页面。</div>
    <textarea name="hosts" rows="4" required
placeholder="about.example.com&#10;www.example.com"></textarea>
    <div class="submit-row" style="flex-wrap:wrap;gap:10px;">
      <label><input type="checkbox" name="sources" value="ffuf">ffuf（主动爆破,需 wordlist）</label>
      <label><input type="checkbox" name="sources" value="urlfinder" checked>URLFinder（主动爬虫,推荐）</label>
      <label><input type="checkbox" name="sources" value="gau">gau（被动扫描,wayback/otx）</label>
    </div>
    <div class="hint" style="margin-top:6px;">wordlist(仅 ffuf 生效):
      <input type="text" name="wordlist" size="60" value="{_e(_scan_urls_default_wordlist_suggestion())}"
             style="font-family:monospace;font-size:12px;padding:2px 6px;">
    </div>
    <div class="submit-row">
      <button type="submit" class="btn primary">扫描 URL 并入库</button>
      <span class="muted">同步等待(单 host 60s ~ 600s)</span>
    </div>
  </form>
</details>
"""


def _build_overview(agg: dict, biz_list: bool = False, biz_name: str = "") -> str:
    rc = agg["row_counts"]
    cards = [
        ("业务",     rc.get("businesses", 0)),
        ("Scope",   rc.get("scopes", 0)),
        ("子公司",   rc.get("companies", 0)),
        ("小程序",   rc.get("mapp_records", 0)),
        ("公众号",   0),
        ("Web 子域",  rc.get("web_subdomains", 0)),
        ("TCP 端口",  rc.get("tcp_assets", 0)),
    ]

    # 业务 card: home page renders link list of non-empty businesses;
    # other pages keep the simple count.
    biz_card_parts = []
    for i, (n, v) in enumerate(cards):
        if i == 0 and biz_list:
            non_empty = [b for b in agg.get("biz_stats", []) if b["total"] > 0]
            if non_empty:
                links = "".join(
                    f'<a class="biz-link" href="{_e(b["href"])}" '
                    f'title="{b["total"]:,} 条资产">'
                    f'<span class="biz-name">{_e(b["name"])}</span>'
                    f'<span class="biz-count muted">{b["total"]:,}</span>'
                    f'</a>' for b in non_empty)
                value_html = f'<div class="biz-list">{links}</div>'
            else:
                value_html = '<div class="value">—</div>'
            biz_card_parts.append(
                f'<div class="card biz-card"><div class="label">{_e(n)}</div>'
                f'{value_html}</div>')
            continue
        biz_card_parts.append(
            f'<div class="card"><div class="label">{_e(n)}</div>'
            f'<div class="value">{v:,}</div></div>')
    cards_html = "".join(biz_card_parts)

    # ---- ICP 备案资产概览（按主体聚合子公司 + 小程序 + 公众号）----
    cc_rows_list = sorted(
        [c for c in agg["comp_counts"] if (c["mp"] + c["oa"]) > 0 or not agg["comp_counts"]],
        key=lambda x: -(x["mp"] + x["oa"]))
    cc_total = max(sum(c["mp"] + c["oa"] for c in cc_rows_list), 1)
    cc_rows = "".join(
        f'<tr><td>{_e(c["unit_name"])}</td>'
        f'<td class="muted">{_e(c["business"])}</td>'
        f'<td class="num">{c["mp"]}</td>'
        f'<td class="num">{c["oa"]}</td>'
        f'<td class="num">{c["mp"] + c["oa"]}</td>'
        f'<td><span class="bar-wrap"><span class="bar" '
        f'style="width:{(c["mp"]+c["oa"])/cc_total*100:.1f}%;background:#0969da"></span></span></td>'
        f'<td class="muted">{(c["mp"]+c["oa"])/cc_total*100:.1f}%</td></tr>'
        for c in cc_rows_list
    )

    # ---- Web analysis column ----
    sc = Counter(agg["web_status_codes"])
    sc_total = sum(sc.values()) or 1
    sc_rows = "".join(
        f'<tr><td>{k if k else "—"}</td><td>{v}</td>'
        f'<td><span class="bar-wrap"><span class="bar" style="width:{v/sc_total*100:.1f}%;background:#1a7f37"></span></span></td>'
        f'<td class="muted">{v/sc_total*100:.1f}%</td></tr>'
        for k, v in sc.most_common(10)
    )

    techs = Counter(agg["web_techs"])
    tech_total = sum(techs.values()) or 1
    tech_rows = "".join(
        f'<tr><td>{_e(k)}</td><td>{v}</td>'
        f'<td><span class="bar-wrap"><span class="bar" style="width:{v/tech_total*100:.1f}%;background:#8250df"></span></span></td>'
        f'<td class="muted">{v/tech_total*100:.1f}%</td></tr>'
        for k, v in techs.most_common(10)
    )

    roots = Counter(agg["web_root_domains"])
    roots_total = sum(roots.values()) or 1
    root_rows = "".join(
        f'<tr><td class="mono">{_e(k)}</td><td>{v}</td>'
        f'<td><span class="bar-wrap"><span class="bar" style="width:{v/roots_total*100:.1f}%;background:#0969da"></span></span></td>'
        f'<td class="muted">{v/roots_total*100:.1f}%</td></tr>'
        for k, v in roots.most_common(10)
    )

    bk = agg["web_fp_buckets"]
    bk_total = sum(bk.values()) or 1
    bucket_color = {"unique (1 子域)": "#1a7f37",
                    "2-10 子域": "#0969da",
                    "11-50 子域": "#9a6700",
                    "≥51 子域 (批量/默认页)": "#cf222e"}
    bucket_rows = "".join(
        f'<tr><td>{_e(k)}</td><td>{v}</td>'
        f'<td><span class="bar-wrap"><span class="bar" style="width:{v/bk_total*100:.1f}%;background:{bucket_color[k]}"></span></span></td>'
        f'<td class="muted">{v/bk_total*100:.1f}%</td></tr>'
        for k, v in bk.items()
    )

    # ---- TCP classification ----
    pc = agg["port_class"]
    pc_total = sum(pc.values()) or 1
    web_overlap = pc.get("web", 0)
    pc_rows = "".join(
        f'<tr><td>{_e(k)}</td><td>{v}</td>'
        f'<td><span class="bar-wrap"><span class="bar" style="width:{v/pc_total*100:.1f}%;background:#8250df"></span></span></td>'
        f'<td class="muted">{v/pc_total*100:.1f}%</td></tr>'
        for k, v in sorted(pc.items(), key=lambda x: -x[1]))
    tcp_note = ""
    if web_overlap:
        tcp_note = (f'<div class="note">⚠ 表中 <b>{web_overlap}</b> 条 '
                    f'web 端口（80/443 等）实际已在 '
                    f'<code>web_subdomains</code> 落库，本表保留用于交叉验证，'
                    f'做纯 TCP 分析时建议剔除。</div>')

    form_html = _build_scan_form(biz_name) if biz_name else ""
    form_urls_html = _build_scan_urls_form(biz_name) if biz_name else ""
    return f"""
{form_html}
{form_urls_html}
<section class="cards">{cards_html}</section>

<div class="section-title">ICP 备案资产概览</div>
<div class="note">按主体聚合子公司与小程序/公众号备案，便于发现主体侧资产集中度。</div>
<div class="scroll-x"><table>
<tr><th>主体</th><th>业务</th><th class="num">小程序</th>
<th class="num">公众号</th><th class="num">合计</th><th></th><th></th></tr>
{cc_rows or '<tr><td colspan="7" class="muted">无主体数据</td></tr>'}
</table></div>

<div class="two-col">
  <div>
    <div class="section-title">Web 资产分析</div>

    <div class="sub-title">状态码分布</div>
    <div class="scroll-x"><table>
    <tr><th>状态码</th><th>数量</th><th>占比</th><th></th></tr>
    {sc_rows or '<tr><td colspan="4" class="muted">无数据</td></tr>'}
    </table></div>

    <div class="sub-title">技术栈 (Top 10)</div>
    <div class="scroll-x"><table>
    <tr><th>技术</th><th>命中</th><th>占比</th><th></th></tr>
    {tech_rows or '<tr><td colspan="4" class="muted">无数据</td></tr>'}
    </table></div>

    <div class="sub-title">注册根域</div>
    <div class="scroll-x"><table>
    <tr><th>根域</th><th>子域数</th><th>占比</th><th></th></tr>
    {root_rows or '<tr><td colspan="4" class="muted">无数据</td></tr>'}
    </table></div>

    <div class="sub-title">指纹浓度桶 <span class="muted">(覆盖越广，价值越低)</span></div>
    <div class="scroll-x"><table>
    <tr><th>桶</th><th>指纹数</th><th>占比</th><th></th></tr>
    {bucket_rows or '<tr><td colspan="4" class="muted">无数据</td></tr>'}
    </table></div>
  </div>

  <div>
    <div class="section-title">TCP 端口分类</div>
    {tcp_note}
    <div class="scroll-x"><table>
    <tr><th>类别</th><th>数量</th><th>占比</th><th></th></tr>
    {pc_rows or '<tr><td colspan="4" class="muted">无数据</td></tr>'}
    </table></div>
  </div>
</div>
"""


def _score_cell(r: dict) -> str:
    """Render the score/description control cell for a sites-table row.

    Layout: <score input> <save> <status> | <desc input>

    Score input accepts:
      plain integer      → absolute (e.g. "75")
      prefixed +N / -N   → relative to current (e.g. "-20" subtracts 20,
                            "+10" adds 10)
    Empty / unparseable  → ignored (no save triggered).

    On save (Enter in input, or click "save"), JS POSTs to /api/hash/<id>/edit.
    The cell is rebuilt locally on success; the server endpoint also flushes
    _State.cached_snap so a reload won't regress.

    Sort key for the score column lives on data-num (set by JS too, but we
    seed it here from the raw value). NULL score → -1 so it sorts last in
    descending order.
    """
    hid = r.get("hash_id")
    score = r.get("hash_score")
    desc = r.get("hash_description") or ""
    sort_key = -1 if score is None else score
    score_txt = "—" if score is None else str(score)
    return (
        f'<td class="score-cell" data-hash-id="{hid}" data-num="{sort_key}">'
        f'<input type="number" class="score-input" data-hash-id="{hid}" '
        f'data-cur-score="{score_txt}" '
        f'value="{score_txt}" min="-1000" max="1000" step="1" '
        f'title="绝对值(0-100) 或 ±N 相对当前值" />'
        f'<button type="button" class="score-save" data-hash-id="{hid}" '
        f'title="保存 score + description">save</button>'
        f'<span class="desc-status" data-hash-id="{hid}"></span>'
        f'</td>'
        f'<td class="desc-cell" data-hash-id="{hid}">'
        f'<input type="text" class="desc-input" data-hash-id="{hid}" '
        f'value="{_e(desc)}" maxlength="500" placeholder="备注..." '
        f'title="回车或 save 保存" />'
        f'</td>'
    )


def _build_sites(agg: dict, diff_mode: bool = False, biz_name: str = "") -> str:
    """站点详情 — 隐藏无 TLD 的泛解析噪声（subdomain 不含 '.' 的全部剔除，
    治 'http://a/' / 'http://j/' 这类单字符 wildcard 命中），按 response_hash
    去重（每指纹一行，新增 count 列），点 hash 弹窗展示该 hash 下全部子域。

    行背景按 status_code 分级：5xx=红/4xx=黄/3xx=灰/2xx=绿/None=灰。
    URL 列按端口拼接（80→http / 443→https / 其它→http://host:port）。

    `diff_mode=True` (used by /diff and /<biz>/new): deduplication skipped,
    one row per affected subdomain. A leading category column ("新"/"hash 变")
    lets the user distinguish new entries from old (which the hash bucket alone
    hides — same hash can appear on both / and /new).

    每行末尾追加「URL 详情」按钮,点击跳到 /<biz>/urls/<hash_id>(新页面,
    按 hash_id 实时查 web_hash_urls 表 — 严格 lazy,不预加载)。
    """
    # URL 详情链接:仅在 biz_name 非空时生成(首页无业务上下文)
    if biz_name:
        biz_quoted = urllib.parse.quote(biz_name)
    else:
        biz_quoted = ""

    def _url_detail_cell(hash_id, subdomain):
        if not biz_quoted or hash_id is None:
            return ""
        href = f"/{biz_quoted}/urls/{hash_id}"
        return (f'<td><a class="btn small" href="{_e(href)}" target="_blank" '
                f'title="查看该 hash 下所有扫到的 URL 路径（按 {_e(subdomain or "")}）">open</a></td>')

    ws_raw = agg["tables"]["web_subdomains"]
    ws = [r for r in ws_raw if "." in (r.get("subdomain") or "")]
    hidden = len(ws_raw) - len(ws)

    def status_risk(code):
        if code is None:
            return "info"
        if 500 <= code < 600:
            return "high"
        if 400 <= code < 500:
            return "med"
        if 300 <= code < 400:
            return "info"
        return "low"

    def url_for(r) -> str:
        sub = r["subdomain"]
        port = r["port"]
        if port == 443:
            return f"https://{sub}/"
        if port == 80:
            return f"http://{sub}/"
        return f"http://{sub}:{port}/"

    def has_zh(s: str) -> int:
        return 1 if any("一" <= c <= "鿿" for c in s) else 0

    def cat_badge(cat: str) -> tuple[str, str]:
        return ({"added":   ("tag-new",     "新"),    # added + reactivated
                 "changed": ("tag-changed", "变")}    # any field diff
                .get(cat, ("tag-info", "?")))

    rows_html_parts: list[str] = []
    hash_subs_for_modal: dict[str, list[str]] = defaultdict(list)
    allnew_html: str = ""        # populated by diff_mode when every bucket is "added"

    if diff_mode:
        # Same hash-dedup logic as overview; per-bucket category badge
        # reflects whether ANY subdomain in the bucket is "added"/[新].
        # A bucket can contain subs from mixed categories; "added" wins
        # over "changed" since the user-visible impact (a brand-new
        # fingerprint appearing) is the more informative signal.
        hash_first: dict[str, dict] = {}
        hash_count: dict[str, int] = {}
        bucket_cat: dict[str, str] = {}
        # bucket_size_in_baseline: how many subs the same hash has in the
        # biz-filtered (or global) snap without the diff filter. Lets us
        # render "1 / 5" when only 1 of the 5 bucket subs changed.
        hash_total = agg.get("hash_total") or Counter()
        for r in ws:
            h = r.get("response_hash") or ""
            cat = r.get("diff_category") or "added"
            hash_count[h] = hash_count.get(h, 0) + 1
            hash_subs_for_modal[h].append(r["subdomain"])
            if h not in hash_first:
                hash_first[h] = r
                bucket_cat[h] = cat
            elif cat == "added":
                bucket_cat[h] = "added"   # promote bucket to [新]
        for h, r in hash_first.items():
            cnt = hash_count[h]
            total = hash_total.get(h, cnt) or cnt
            cls, label = cat_badge(bucket_cat[h])
            # count column: plain when the bucket is fully new, ratio
            # otherwise — makes /new rows visually distinguishable from /
            # (which always shows the plain total).
            count_txt = f"{cnt:,}" if cnt >= total else f"{cnt:,} / {total:,}"
            # Bare subdomains of every sibling in this hash bucket — used by
            # the client-side search so that a query like "oa.example.com"
            # finds rows where the visible URL is a sibling (e.g.
            # "0zc1zs.sibling.example.com") but the bucket also contains it.
            bucket_urls = " ".join(hash_subs_for_modal[h])
            rows_html_parts.append(
                f'<tr class="risk-{status_risk(r.get("status_code"))}" '
                f'data-hash="{_e(h)}" '
                f'data-port="{r["port"]}" '
                f'data-hash-count="{cnt}" '
                f'data-bucket-urls="{_e(bucket_urls)}" '
                f'data-zh="{has_zh(r.get("title") or "")}">'
                f'<td><span class="tag {cls}">{label}</span></td>'
                f'<td class="mono"><a href="{_e(url_for(r))}" target="_blank" '
                f'rel="noopener noreferrer">{_e(url_for(r))}</a></td>'
                f'<td>{r["port"]}</td>'
                f'<td>{r.get("status_code") if r.get("status_code") is not None else "—"}</td>'
                f'<td>{r.get("content_length") if r.get("content_length") is not None else "—"}</td>'
                f'<td>{_e(r.get("title") or "")}</td>'
                f'<td class="muted">{_e(r.get("technologies") or "")}</td>'
                f'<td><a class="hash-link" data-hash="{_e(h)}" '
                f'href="javascript:void(0)">{_e(h)}</a></td>'
                f'<td class="num">{count_txt}</td>'
                f'<td>{r.get("is_active", 0)}</td>'
                f'{_score_cell(r)}'
                f'{_url_detail_cell(r.get("hash_id"), r.get("subdomain"))}'
                f'</tr>'
            )
        rows_html = "".join(rows_html_parts)
        n_added = sum(1 for c in bucket_cat.values() if c == "added")
        n_changed = sum(1 for c in bucket_cat.values() if c == "changed")
        n_subs = sum(hash_count.values())
        meta_parts = [f'{len(hash_first):,} 条 · 按 hash 去重 · 原始 {n_subs:,}',
                      f'桶标 新 {n_added:,} · 变 {n_changed:,}']
        meta_parts.append('点 hash 看同指纹子域')
        # Banner: if every bucket is "added" (i.e. no bucket was merely
        # rotated — this biz had no prior baseline), surface that so the
        # user sees /new's purpose ("what changed") even when the answer
        # is "everything was new".
        if n_changed == 0 and n_added > 0:
            allnew_html = (
                f'<div class="note note-allnew">'
                f'⚠ 本业务下 <b>全部 {n_subs:,}</b> 条有效子域在本次运行中均为<b>新增</b>'
                f'（无既有基线对比，新增前可能未扫描过该业务）。如需关注内容变化，'
                f'需先建立该业务的扫描基线。'
                f'</div>'
            )
        else:
            allnew_html = ""
    else:
        hash_count: Counter = Counter()
        hash_subs: dict[str, list[str]] = defaultdict(list)
        hash_first: dict[str, dict] = {}
        for r in ws:
            h = r.get("response_hash") or ""
            hash_count[h] += 1
            hash_subs[h].append(r["subdomain"])
            if h not in hash_first:
                hash_first[h] = r
        for h, r in hash_first.items():
            cnt = hash_count[h]
            # Bare subdomains of every sibling in this hash bucket — used by
            # the client-side search so that a query like "oa.example.com"
            # finds rows where the visible URL is a sibling (e.g.
            # "0zc1zs.sibling.example.com") but the bucket also contains it.
            bucket_urls = " ".join(hash_subs[h])
            rows_html_parts.append(
                f'<tr class="risk-{status_risk(r.get("status_code"))}" '
                f'data-hash="{_e(h)}" '
                f'data-port="{r["port"]}" '
                f'data-hash-count="{cnt}" '
                f'data-bucket-urls="{_e(bucket_urls)}" '
                f'data-zh="{has_zh(r.get("title") or "")}">'
                f'<td class="mono"><a href="{_e(url_for(r))}" target="_blank" '
                f'rel="noopener noreferrer">{_e(url_for(r))}</a></td>'
                f'<td>{r["port"]}</td>'
                f'<td>{r.get("status_code") if r.get("status_code") is not None else "—"}</td>'
                f'<td>{r.get("content_length") if r.get("content_length") is not None else "—"}</td>'
                f'<td>{_e(r.get("title") or "")}</td>'
                f'<td class="muted">{_e(r.get("technologies") or "")}</td>'
                f'<td><a class="hash-link" data-hash="{_e(h)}" '
                f'href="javascript:void(0)">{_e(h)}</a></td>'
                f'<td class="num">{cnt:,}</td>'
                f'<td>{r.get("is_active", 0)}</td>'
                f'{_score_cell(r)}'
                f'{_url_detail_cell(r.get("hash_id"), r.get("subdomain"))}'
                f'</tr>'
            )
        rows_html = "".join(rows_html_parts)
        hash_subs_for_modal = dict(hash_subs)
        meta_parts = [f'{len(hash_first):,} 条 · 按 hash 去重 · 原始 {len(ws_raw):,}']
        if hidden:
            meta_parts.append(f'隐藏 {hidden:,} 条无 TLD 后缀')
        meta_parts.append('点 hash 看全部子域')

    meta = " · ".join(meta_parts)

    # Header columns differ: diff_mode adds a leading category column.
    if diff_mode:
        head_cells = (
            '<th>类别</th>'
            '<th class="sortable" data-type="str">URL</th>'
            '<th class="sortable" data-type="num">port</th>'
            '<th class="sortable" data-type="num">status</th>'
            '<th class="sortable" data-type="num">bytes</th>'
            '<th class="sortable" data-type="str">title</th>'
            '<th class="sortable" data-type="str">tech</th>'
            '<th class="sortable" data-type="str">hash</th>'
            '<th class="sortable" data-type="num">count</th>'
            '<th class="sortable" data-type="num">active</th>'
            '<th class="sortable" data-type="num">分数</th>'
            '<th>备注</th>'
            '<th>URL 详情</th>'
        )
        colspan = 13
        empty = '<tr><td colspan="13" class="muted">无新增站点</td></tr>'
    else:
        head_cells = (
            '<th class="sortable" data-type="str">URL</th>'
            '<th class="sortable" data-type="num">port</th>'
            '<th class="sortable" data-type="num">status</th>'
            '<th class="sortable" data-type="num">bytes</th>'
            '<th class="sortable" data-type="str">title</th>'
            '<th class="sortable" data-type="str">tech</th>'
            '<th class="sortable" data-type="str">hash</th>'
            '<th class="sortable" data-type="num">count</th>'
            '<th class="sortable" data-type="num">active</th>'
            '<th class="sortable" data-type="num">分数</th>'
            '<th>备注</th>'
            '<th>URL 详情</th>'
        )
        colspan = 12
        empty = '<tr><td colspan="12" class="muted">无 web 子域</td></tr>'

    hash_data_json = json.dumps(dict(hash_subs_for_modal), ensure_ascii=False)

    return f"""
{allnew_html}
<div class="filter">
  <input id="q-sites" placeholder="过滤 URL / title / tech / hash" />
  &nbsp;
  <button class="toggle" data-toggle="hide-http">隐藏 http</button>
  <button class="toggle" data-toggle="unique">仅 unique</button>
  <button class="toggle" data-toggle="zh">中文优先</button>
  &nbsp; <span class="muted">{meta}</span>
</div>
<div class="scroll-x"><table id="sites-table">
<thead><tr>
  {head_cells}
</tr></thead>
<tbody>
{rows_html or empty}
</tbody>
</table></div>

<div id="hash-modal" class="modal" style="display:none">
  <div class="modal-card">
    <div class="modal-header">
      <span id="hash-modal-title"></span>
      <button id="hash-modal-close" aria-label="关闭">×</button>
    </div>
    <div id="hash-modal-body"></div>
  </div>
</div>

<script id="hash-data" type="application/json">{hash_data_json}</script>
"""


def _build_ports(agg: dict) -> str:
    tcp = agg["tables"]["tcp_assets"]
    # IP comes straight from tcp_assets.ip (no host_ip_map fallback).
    rows = []
    for r in tcp:
        cls = classify_port(r["port"])
        rows.append({
            "biz": agg["biz"].get(r["business_id"], ""),
            "ip": r.get("ip") or "",
            "port": r["port"],
            "hosts": r.get("hosts") or "",
            "category": cls["category"],
            "service": cls["service"],
            "risk": cls["risk"],
            "is_active": r.get("is_active"),
        })
    rows.sort(key=lambda r: ({"high": 0, "med": 1, "low": 2, "info": 3}[r["risk"]],
                              r["ip"], r["port"]))

    rows_html = "".join(
        f'<tr class="risk-{_e(r["risk"])}">'
        f'<td>{_e(r["biz"])}</td>'
        f'<td class="mono">{_e(r["ip"]) if r["ip"] else "—"}</td>'
        f'<td>{r["port"]}</td>'
        f'<td class="mono">{_e(r["hosts"]) if r["hosts"] else "—"}</td>'
        f'<td>{_e(r["category"])}</td>'
        f'<td>{_e(r["service"])}</td>'
        f'<td>{_risk_tag(r["risk"])}</td>'
        f'</tr>' for r in rows
    )

    rc = agg["risk_counter"]
    rc_total = sum(rc.values()) or 1
    rc_html = "".join(
        f'<span class="tag tag-{k if k in ("high","med","low") else "info"}">'
        f'{ {"high":"高","med":"中","low":"低","info":"提示"}.get(k,k) } '
        f'{v} ({v/rc_total*100:.0f}%)</span> &nbsp; '
        for k, v in sorted(rc.items(), key=lambda x: -x[1]))

    return f"""
<div class="filter">
  <input id="q" placeholder="过滤 ip / hosts / service / category" />
  &nbsp; {rc_html}
</div>
<div class="scroll-x"><table id="tcp-table">
<thead><tr>
  <th class="sortable" data-type="str">业务</th>
  <th class="sortable" data-type="str">ip</th>
  <th class="sortable" data-type="num">port</th>
  <th class="sortable" data-type="str">hosts</th>
  <th class="sortable" data-type="str">category</th>
  <th class="sortable" data-type="str">service</th>
  <th>risk</th>
</tr></thead>
<tbody>
{rows_html or '<tr><td colspan="7" class="muted">无 tcp_assets</td></tr>'}
</tbody>
</table></div>
<div class="muted">{len(rows)} 条 ip:port · IP 直读自 tcp_assets.ip；hosts 来自 tcp_assets.hosts（多 host 以逗号分隔）。</div>
"""


def _build_mapp(agg: dict, kind: str) -> str:
    """小程序 / 公众号 详情页 — kind='mp' 取 mp_records，否则取 oa_records。"""
    records = agg["mp_records"] if kind == "mp" else agg["oa_records"]
    title = "小程序" if kind == "mp" else "公众号"
    empty_msg = "暂无小程序数据" if kind == "mp" else "暂无公众号数据"

    records = sorted(records, key=lambda r: (r.get("unit_name") or "",
                                             r.get("service_name") or ""))
    rows_html = "".join(
        f'<tr><td>{_e(r.get("unit_name") or "—")}</td>'
        f'<td>{_e(r.get("service_name") or "")}</td>'
        f'<td class="mono">{_e(r.get("service_licence") or "")}</td>'
        f'<td>{_e(r.get("content_type_name") or "")}</td>'
        f'<td>{_e(r.get("domain") or "")}</td>'
        f'<td>{_e(r.get("record_updated_at") or r.get("fetched_at") or "")}</td>'
        f'</tr>' for r in records
    )

    return f"""
<div class="filter">
  <input id="q-{kind}" placeholder="过滤 主体 / 名称 / 备案号 / 域名" />
  &nbsp; <span class="muted">{len(records)} 条 {title}备案</span>
</div>
<div class="scroll-x"><table id="{kind}-table">
<thead><tr>
  <th class="sortable" data-type="str">主体</th>
  <th class="sortable" data-type="str">名称</th>
  <th class="sortable" data-type="str">备案号</th>
  <th class="sortable" data-type="str">类型</th>
  <th class="sortable" data-type="str">域名</th>
  <th class="sortable" data-type="str">更新时间</th>
</tr></thead>
<tbody>
{rows_html or f'<tr><td colspan="6" class="muted">{empty_msg}</td></tr>'}
</tbody>
</table></div>
<script>
(function() {{
  const q = document.getElementById('q-{kind}');
  const tbody = document.querySelector('#{kind}-table tbody');
  if (!q || !tbody) return;
  q.addEventListener('input', () => {{
    const v = q.value.toLowerCase();
    tbody.querySelectorAll('tr').forEach(tr => {{
      tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';
    }});
  }});
}})();
</script>
"""


def _build_companies(agg: dict) -> str:
    """子公司详情 — 直接列 companies 表。"""
    rows = sorted(agg["companies"],
                  key=lambda c: (c.get("business_id") or 0,
                                 c.get("unit_name") or ""))
    counts = {c["unit_name"]: c for c in agg["comp_counts"]}
    rows_html = "".join(
        f'<tr><td>{_e(c.get("unit_name") or "")}</td>'
        f'<td class="muted">{_e(agg["biz"].get(c.get("business_id"), ""))}</td>'
        f'<td>{_e(c.get("nature_name") or "")}</td>'
        f'<td class="mono">{_e(c.get("main_licence") or "")}</td>'
        f'<td class="num">{counts.get(c.get("unit_name"), {}).get("mp", 0)}</td>'
        f'<td class="num">{counts.get(c.get("unit_name"), {}).get("oa", 0)}</td>'
        f'<td class="muted">{_e(c.get("updated_at") or "")}</td>'
        f'</tr>' for c in rows
    )

    return f"""
<div class="filter">
  <input id="q-comp" placeholder="过滤 主体 / 备案号 / 业务" />
  &nbsp; <span class="muted">{len(rows)} 家子公司</span>
</div>
<div class="scroll-x"><table id="comp-table">
<thead><tr>
  <th class="sortable" data-type="str">主体名称</th>
  <th class="sortable" data-type="str">业务</th>
  <th class="sortable" data-type="str">主体类型</th>
  <th class="sortable" data-type="str">主备案号</th>
  <th class="sortable" data-type="num">小程序</th>
  <th class="sortable" data-type="num">公众号</th>
  <th class="sortable" data-type="str">更新时间</th>
</tr></thead>
<tbody>
{rows_html or '<tr><td colspan="7" class="muted">无子公司数据</td></tr>'}
</tbody>
</table></div>
<script>
(function() {{
  const q = document.getElementById('q-comp');
  const tbody = document.querySelector('#comp-table tbody');
  if (!q || !tbody) return;
  q.addEventListener('input', () => {{
    const v = q.value.toLowerCase();
    tbody.querySelectorAll('tr').forEach(tr => {{
      tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';
    }});
  }});
}})();
</script>
"""


def _build_risk(agg: dict) -> str:
    parts = []

    # 1. Dangerous open ports
    risky = agg["risky_open"]
    risky_rows = "".join(
        f'<tr class="risk-high"><td>{_e(r["business"])}</td>'
        f'<td class="mono">{_e(r["host"])}</td>'
        f'<td>{r["port"]}</td>'
        f'<td>{_e(r["service"])}</td>'
        f'<td>{_e(r["category"])}</td></tr>'
        for r in risky
    )
    parts.append(f"""
<div class="section-title">① 暴露的高危端口 ({len(risky)})</div>
<div class="note">DB / 远程管理 / Memcached 等无认证服务暴露在公网。</div>
<div class="scroll-x"><table>
<tr><th>业务</th><th>host</th><th>port</th><th>service</th><th>category</th></tr>
{risky_rows or '<tr><td colspan="5" class="muted">无</td></tr>'}
</table></div>
""")

    # 2. Batch fingerprint alerts
    big_fps = [r for r in agg["fp_rows"] if r["count"] >= 50]
    fp_html = "".join(
        f'<tr class="risk-med"><td class="mono">{_e(r["hash"][:14]) if r["hash"] else "—"}</td>'
        f'<td>{r["count"]}</td>'
        f'<td class="mono">{_e(r["sample"])}</td>'
        f'<td>{_e(r["title"])}</td>'
        f'<td>{_e(r["tech"])}</td></tr>'
        for r in big_fps
    )
    parts.append(f"""
<div class="section-title">② 批量指纹告警 ({len(big_fps)})</div>
<div class="note">同一响应指纹覆盖 ≥50 个子域 — 多为泛解析/默认页/未配置独立站点。</div>
<div class="scroll-x"><table>
<tr><th>response_hash</th><th>子域数</th><th>示例</th><th>title</th><th>tech</th></tr>
{fp_html or '<tr><td colspan="5" class="muted">无</td></tr>'}
</table></div>
""")

    # 3. Fat hosts (many open ports)
    fh = agg["fat_hosts"]
    fh_rows = "".join(
        f'<tr class="risk-med"><td class="mono">{_e(r["host"])}</td>'
        f'<td>{r["port_count"]}</td>'
        f'<td class="muted">{_e(r["sample_ports"])}</td></tr>'
        for r in fh
    )
    parts.append(f"""
<div class="section-title">③ 端口展开过宽的主机 ({len(fh)})</div>
<div class="note">单 host 开放 ≥50 个端口 — 看起来像端口段扫描产物，web 视角可剔除。</div>
<div class="scroll-x"><table>
<tr><th>host</th><th>port 数</th><th>样本</th></tr>
{fh_rows or '<tr><td colspan="3" class="muted">无</td></tr>'}
</table></div>
""")

    return "".join(parts)


def render_html(agg: dict, *, mode: str = "overview", run_id: str = "",
                 biz_name: str = "", biz_nav: list[dict] = "",
                 home: bool = False) -> bytes:
    diff_mode = (mode == "diff")
    # mode: "overview" → /,  "diff" → /diff. Diff mode tweaks title/header
    # so it's clear the page is showing only assets that changed in this run,
    # but the body uses the exact same _build_* functions as overview.
    # biz_name != "" → /<业务名> route; biz_nav = [{name, href}] for the
    # quick-jump bar; always rendered when non-empty.
    biz_suffix = f" — {_e(biz_name)}" if biz_name else ""
    if mode == "diff":
        title = f"SRC Recon — 本次变动{biz_suffix} — {_e(run_id)}"
        h1 = f"SRC Recon — 本次变动{biz_suffix}"
        back_href = f"/{_e(biz_name)}" if biz_name else "/"
        back_label = f"← /{_e(biz_name)}" if biz_name else "← / 总览"
        back = f'<a style="color:#9da5b4" href="{back_href}">{back_label}</a> · '
    else:
        title = f"SRC Recon Dashboard{biz_suffix}"
        h1 = f"SRC Recon Dashboard{biz_suffix}"
        diff_href = f"/{_e(biz_name)}/new" if biz_name else "/diff"
        diff_label = "本次变动"
        back = f'<a style="color:#9da5b4" href="{diff_href}">{diff_label}</a> · '

    nav_html = ""
    if biz_nav:
        links = []
        for b in biz_nav:
            cls = ' class="active"' if b.get("name") == biz_name else ""
            links.append(
                f'<a{cls} style="color:#cfd7e3;text-decoration:none;padding:6px 10px;'
                f'border-radius:4px;font-size:13px" '
                f'href="{_e(b["href"])}">{_e(b["name"])}</a>'
            )
        links.append(
            f'<a style="color:#cfd7e3;text-decoration:none;padding:6px 10px;'
            f'border-radius:4px;font-size:13px" '
            f'href="/">全部</a>'
        )
        nav_html = (
            '<nav class="biz-bar" style="background:#2d333b;padding:8px 24px;'
            'display:flex;gap:6px;align-items:center;border-bottom:1px solid #1f2328">'
            '<span style="color:#9da5b4;font-size:12px;margin-right:4px">业务：</span>'
            + "".join(links) + '</nav>'
        )

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>{h1}</h1>
  <div class="meta">
    snapshot: {_e(agg['captured_at'])}
    · {back}<a style="color:#9da5b4" href="/api/snapshot">JSON</a>
  </div>
</header>
{nav_html}
<nav class="tabs">
  <button data-tab="status" class="active">任务状态</button>
  <button data-tab="screens">站点详情</button>
  <button data-tab="ports">端口服务</button>
  <button data-tab="risk">风险等级</button>
  <button data-tab="companies">子公司</button>
  <button data-tab="mp">小程序</button>
  <button data-tab="oa">公众号</button>
</nav>
<main>
  <section class="tab-pane active" id="tab-status">{_build_overview(agg, biz_list=home, biz_name=biz_name)}</section>
  <section class="tab-pane" id="tab-screens">{_build_sites(agg, diff_mode=diff_mode, biz_name=biz_name)}</section>
  <section class="tab-pane" id="tab-ports">{_build_ports(agg)}</section>
  <section class="tab-pane" id="tab-risk">{_build_risk(agg)}</section>
  <section class="tab-pane" id="tab-companies">{_build_companies(agg)}</section>
  <section class="tab-pane" id="tab-mp">{_build_mapp(agg, 'mp')}</section>
  <section class="tab-pane" id="tab-oa">{_build_mapp(agg, 'oa')}</section>
</main>
<script>
document.querySelectorAll('nav.tabs button').forEach(b => b.addEventListener('click', () => {{
  document.querySelectorAll('nav.tabs button').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  document.getElementById('tab-' + b.dataset.tab).classList.add('active');
}}));
const q = document.getElementById('q');
if (q) q.addEventListener('input', () => {{
  const v = q.value.toLowerCase();
  document.querySelectorAll('#tcp-table tr').forEach(tr => {{
    tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';
  }});
}});

// sites tab: filter (search + 3 toggles)
const sitesTbody = document.querySelector('#sites-table tbody');
const allSitesRows = Array.from(sitesTbody?.querySelectorAll('tr') || []);
const toggleStates = {{}};
const qSites = document.getElementById('q-sites');

function applySitesFilters() {{
  const v = (qSites?.value || '').toLowerCase();
  const hideHttp = toggleStates['hide-http'];
  const unique = toggleStates['unique'];
  allSitesRows.forEach(tr => {{
    let show = true;
    if (v && !tr.textContent.toLowerCase().includes(v)) {{
      // For buckets with >1 sibling subdomains, the visible row URL is just
      // one of them; fall back to data-bucket-urls so queries hit siblings
      // (e.g. searching "oa.example.com" finds the row whose visible URL is
      // a permutation sibling sharing the same fingerprint).
      const cnt = parseInt(tr.dataset.hashCount) || 1;
      const bucket = tr.dataset.bucketUrls || '';
      if (!(cnt > 1 && bucket.toLowerCase().includes(v))) show = false;
    }}
    if (show && hideHttp && tr.dataset.port === '80') show = false;
    if (show && unique && tr.dataset.hashCount !== '1') show = false;
    tr.style.display = show ? '' : 'none';
  }});
  // 中文优先: re-sort visible rows (Chinese-title first)
  if (toggleStates['zh']) {{
    const rows = allSitesRows.filter(tr => tr.style.display !== 'none');
    rows.sort((a, b) => (parseInt(b.dataset.zh) || 0) - (parseInt(a.dataset.zh) || 0));
    rows.forEach(r => sitesTbody.appendChild(r));
  }}
}}

document.querySelectorAll('button.toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    btn.classList.toggle('on');
    toggleStates[btn.dataset.toggle] = btn.classList.contains('on');
    applySitesFilters();
  }});
}});

if (qSites) qSites.addEventListener('input', applySitesFilters);

// generic column sort (works for sites-table, tcp-table, any th.sortable)
document.querySelectorAll('th.sortable').forEach(th => {{
  let asc = true;
  th.addEventListener('click', () => {{
    const table = th.closest('table');
    const tbody = table.querySelector('tbody') || table;
    const idx = Array.from(th.parentElement.children).indexOf(th);
    const type = th.dataset.type;
    const rows = Array.from(tbody.querySelectorAll('tr')).filter(
      r => !(r.children.length === 1 && r.children[0].hasAttribute('colspan')));
    if (!rows.length) return;
    const cmp = (a, b) => {{
      // 优先读 data-num（score-cell 上的排序键；input.textContent 是空字符串）
      const an = a.children[idx]?.dataset?.num;
      const bn = b.children[idx]?.dataset?.num;
      const av = (an !== undefined ? an : (a.children[idx]?.textContent || '')).trim();
      const bv = (bn !== undefined ? bn : (b.children[idx]?.textContent || '')).trim();
      if (type === 'num') {{
        return (parseFloat(av) || 0) - (parseFloat(bv) || 0);
      }}
      return av.localeCompare(bv, 'zh-Hans-CN');
    }};
    rows.sort((a, b) => asc ? cmp(a, b) : -cmp(a, b));
    rows.forEach(r => tbody.appendChild(r));
    th.parentElement.querySelectorAll('th').forEach(x => {{
      x.classList.remove('asc', 'desc');
    }});
    th.classList.add(asc ? 'asc' : 'desc');
    asc = !asc;
  }});
}});

// hash modal: click hash link → show all sites for that hash
const hashData = JSON.parse(document.getElementById('hash-data')?.textContent || '{{}}');
const modal = document.getElementById('hash-modal');
document.querySelectorAll('.hash-link').forEach(a => {{
  a.addEventListener('click', () => {{
    const h = a.dataset.hash;
    const subs = hashData[h] || [];
    document.getElementById('hash-modal-title').textContent =
      `hash ${{h || '(空)'}} · ${{subs.length}} 个站点`;
    const rowsHtml = subs.map(s =>
      `<tr><td class="mono"><a href="http://${{s}}/" target="_blank" rel="noopener noreferrer">${{s}}</a></td></tr>`
    ).join('');
    document.getElementById('hash-modal-body').innerHTML =
      '<table><tr><th>subdomain</th></tr>' + rowsHtml + '</table>';
    modal.style.display = 'flex';
  }});
}});
document.getElementById('hash-modal-close')?.addEventListener('click', () => {{
  modal.style.display = 'none';
}});
modal?.addEventListener('click', e => {{
  if (e.target === modal) modal.style.display = 'none';
}});
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape' && modal?.style.display === 'flex') {{
    modal.style.display = 'none';
  }}
}});

// --- score / description edit ---
// score 列换成 input：绝对值 (0-100) 或 ±N 相对当前；分数列点击排序由
// 通用 th.sortable 处理（data-num 已是排序键）。POST 成功时服务端会局部
// 更新 _State.cached_snap 并重算 cached_snap_json。
function parseScoreInput(raw, cur) {{
  if (raw === null) return null;
  const s = String(raw).trim();
  if (!s) return null;
  let n;
  if (s[0] === '+' || s[0] === '-') {{
    const delta = parseInt(s, 10);
    if (Number.isNaN(delta) || cur === null || cur === undefined) return null;
    n = cur + delta;
  }} else {{
    n = parseInt(s, 10);
    if (Number.isNaN(n)) return null;
  }}
  if (n < 0 || n > 100) return null;
  return n;
}}

async function editHash(hashId, newScore, newDesc) {{
  const status = document.querySelector('.desc-status[data-hash-id="' + hashId + '"]');
  if (status) status.textContent = '保存中…';
  try {{
    const body = new URLSearchParams();
    body.set('score', String(newScore));
    body.set('description', newDesc || '');
    const r = await fetch('/api/hash/' + hashId + '/edit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
      body: body.toString(),
    }});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'http ' + r.status);
    // 同步刷新所有 score-cell td 的 data-num（让排序立即生效）
    document.querySelectorAll('.score-cell[data-hash-id="' + hashId + '"]').forEach(td => {{
      td.setAttribute('data-num', String(j.score));
    }});
    document.querySelectorAll('.score-input[data-hash-id="' + hashId + '"]').forEach(el => {{
      el.value = String(j.score);
      el.setAttribute('data-cur-score', String(j.score));
    }});
    if (status) status.textContent = '✓ ' + j.score;
    setTimeout(() => {{ if (status && status.textContent.startsWith('✓')) status.textContent = ''; }}, 2000);
  }} catch (e) {{
    if (status) status.textContent = '✗ ' + e.message;
  }}
}}

function triggerSave(hid) {{
  const input = document.querySelector('.score-input[data-hash-id="' + hid + '"]');
  const descEl = document.querySelector('.desc-input[data-hash-id="' + hid + '"]');
  if (!input) return;
  const cur = parseInt(input.getAttribute('data-cur-score'), 10);
  const parsed = parseScoreInput(input.value, cur);
  if (parsed === null) {{
    const status = document.querySelector('.desc-status[data-hash-id="' + hid + '"]');
    if (status) status.textContent = '✗ 输入需是 0-100 整数或 ±N';
    setTimeout(() => {{ if (status && status.textContent.startsWith('✗')) status.textContent = ''; }}, 2500);
    return;
  }}
  editHash(hid, parsed, descEl ? descEl.value : '');
}}

document.addEventListener('click', e => {{
  const btn = e.target.closest('.score-save');
  if (btn) {{
    const hid = btn.getAttribute('data-hash-id');
    triggerSave(hid);
  }}
}});

document.addEventListener('keydown', e => {{
  if (e.key === 'Enter' && e.target.classList?.contains('score-input')) {{
    e.preventDefault();
    triggerSave(e.target.getAttribute('data-hash-id'));
  }}
  // 描述 input 按 Enter 触发保存；分数无效时只刷状态、不存描述
  if (e.key === 'Enter' && e.target.classList?.contains('desc-input')) {{
    e.preventDefault();
    const hid = e.target.getAttribute('data-hash-id');
    const input = document.querySelector('.score-input[data-hash-id="' + hid + '"]');
    if (!input) return;
    const cur = parseInt(input.getAttribute('data-cur-score'), 10);
    const parsed = parseScoreInput(input.value, cur);
    if (parsed === null) return;
    editHash(hid, parsed, e.target.value);
  }}
}});
</script>
</body></html>
"""
    return page.encode("utf-8")


# ---------------------------------------------------------------------------
# /diff page — same UI as /, but only assets that appear in the latest run's
# added/reactivated/changed CSVs. Reads from the report dir + the snapshot.
# ---------------------------------------------------------------------------

_DIFF_TABLES = ("scopes", "companies", "mapp_records",
                "web_hashes", "web_subdomains", "tcp_assets")


def _find_latest_report(reports_dir: Path = Path("reports")) -> Path | None:
    """Latest run dir under reports/ (named like 20260728-080001)."""
    if not reports_dir.exists():
        return None
    # run_id is 8 digits + '-' + 6 digits; sort by name works because of the
    # fixed width + lexicographic order matching chronological order.
    candidates = [d for d in reports_dir.iterdir()
                  if d.is_dir() and len(d.name) == 15 and d.name[8] == "-"]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.name, reverse=True)
    return candidates[0]


def _csv_row_key(table: str, row: dict,
                 biz_name_to_id: dict[str, int],
                 comp_name_to_id: dict[str, int]) -> tuple | None:
    """Build the same logical key tuple for a diff-CSV row that
    diff.py's _<table>_key produces for a snapshot row."""
    bid = biz_name_to_id.get(row.get("business", ""))
    if table == "scopes":
        return (bid, row.get("scope_name", ""), row.get("asset", ""))
    if table == "companies":
        return (bid, row.get("unit_name", ""))
    if table == "mapp_records":
        cid = comp_name_to_id.get(row.get("unit_name", ""))
        if row.get("service_licence"):
            return ("lic", cid, row["service_licence"])
        return ("nm", cid, row.get("service_name", ""), row.get("service_type"))
    if table == "web_hashes":
        return (bid, row.get("response_hash", ""))
    if table == "web_subdomains":
        port = int(row["port"]) if row.get("port") else 0
        return (bid, row.get("subdomain", ""), port)
    if table == "tcp_assets":
        # post-rename: CSV column is "host" but value is the IP
        port = int(row["port"]) if row.get("port") else 0
        return (bid, row.get("host", ""), port)
    return None


def _snapshot_row_key(table: str, r: dict) -> tuple | None:
    """Logical key for a snapshot row, matching diff.py's _<table>_key."""
    if table == "scopes":
        return (r.get("business_id"), r.get("scope_name"), r.get("asset"))
    if table == "companies":
        return (r.get("business_id"), r.get("unit_name"))
    if table == "mapp_records":
        if r.get("service_licence"):
            return ("lic", r.get("company_id"), r.get("service_licence"))
        return ("nm", r.get("company_id"),
                r.get("service_name"), r.get("service_type"))
    if table == "web_hashes":
        return (r.get("business_id"), r.get("response_hash"))
    if table == "web_subdomains":
        return (r.get("business_id"), r.get("subdomain"), r.get("port"))
    if table == "tcp_assets":
        return (r.get("business_id"), r.get("ip"), r.get("port"))
    return None


def _load_changed_keys(report_dir: Path, snap: dict) -> dict[str, dict[tuple, str]]:
    """Scan added/reactivated/changed CSVs; any difference vs the previous
    run qualifies. Returns {table: {key_tuple: category_label}}.

    Categories used:
        "added"      — row did not exist in the previous snapshot
        "added"      — row was inactive before, is active now (reactivated)
        "changed"    — row existed and was active in both runs, but some
                       field (hash / status / title / tech / etc.) differs

    Category is informational — the /new site tab badges rows as "新" or "变"
    purely for visual identification. Inclusion is uniform across all three
    CSVs: a row is on /new iff it shows up in any of them.
    """
    if not report_dir or not report_dir.exists():
        return {t: {} for t in _DIFF_TABLES}

    biz_name_to_id = {b.get("business_name"): b["id"]
                      for b in snap.get("tables", {}).get("businesses", [])
                      if b.get("business_name")}
    comp_name_to_id = {c.get("unit_name"): c["id"]
                       for c in snap.get("tables", {}).get("companies", [])
                       if c.get("unit_name")}

    tagged: dict[str, dict[tuple, str]] = {t: {} for t in _DIFF_TABLES}
    for cat in ("added", "reactivated", "changed"):
        for csv_path in report_dir.glob(f"{cat}_*.csv"):
            table = csv_path.stem[len(cat) + 1:]
            if table not in tagged:
                continue
            label = "added" if cat in ("added", "reactivated") else "changed"
            with csv_path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    key = _csv_row_key(table, row, biz_name_to_id, comp_name_to_id)
                    if key and all(k is not None for k in key):
                        tagged[table][key] = label
    return tagged


def _filter_snapshot(snap: dict, tagged: dict[str, dict[tuple, str]]) -> dict:
    """Filter snapshot to rows whose key is in `tagged`. Each kept row is
    annotated with its `diff_category` ("added" / "hash_changed" / "changed")
    so the UI can render badges.

    Rows are deduped by snapshot-key first — the source data accumulates
    duplicates from re-scans (a (bid, subdomain, port) may appear as
    multiple records). Without dedup, /<biz>/new ends up identical to
    /<biz> when every scan target is freshly "added" (e.g. DemoCorp's first
    full scan), since the filter just passes every duplicate row through."""
    if not tagged:
        return snap
    tables = snap.get("tables", {})
    out: dict[str, list] = {}
    for name, rows in tables.items():
        if name not in tagged:
            out[name] = rows              # not a diff-tracked table → keep
            continue
        keys = tagged[name]
        if not keys:
            out[name] = []                # tracked but no changes
            continue
        seen: set = set()
        kept: list = []
        for r in rows:
            k = _snapshot_row_key(name, r)
            if k is None or k not in keys or k in seen:
                continue
            seen.add(k)
            r2 = dict(r)
            r2["diff_category"] = keys[k]
            kept.append(r2)
        out[name] = kept
    new_snap = dict(snap)
    new_snap["tables"] = out
    new_snap["row_counts"] = {t: len(out.get(t, []))
                              for t in snap.get("row_counts", {})}
    return new_snap


def _filter_snap_by_business(snap: dict, bid: int) -> dict:
    """Return a new snapshot with every table limited to business_id == bid.

    mapp_records has no business_id (it's company_id -> companies.business_id),
    so it gets filtered through company_id membership.
    """
    tables = snap.get("tables", {})
    comp_id_set = {c["id"] for c in tables.get("companies", [])
                   if c.get("business_id") == bid}
    out: dict[str, list] = {}
    _BID_TABLES = ("scopes", "companies", "web_subdomains", "web_hashes",
                   "tcp_assets")
    for name, rows in tables.items():
        if name == "businesses":
            out[name] = [b for b in rows if b.get("id") == bid]
        elif name in _BID_TABLES:
            out[name] = [r for r in rows if r.get("business_id") == bid]
        elif name == "mapp_records":
            out[name] = [r for r in rows if r.get("company_id") in comp_id_set]
        else:
            out[name] = rows  # unknown table: pass through unchanged
    new_snap = dict(snap)
    new_snap["tables"] = out
    new_snap["row_counts"] = {t: len(out.get(t, []))
                              for t in snap.get("row_counts", {})}
    return new_snap


def _changed_keys_for_business(
    tagged: dict[str, dict[tuple, str]], bid: int, snap: dict,
) -> dict[str, dict[tuple, str]]:
    """Filter `tagged` (output of _load_changed_keys) to business_id == bid.

    mapp_records keys are ((lic|nm), company_id, ...) — those key-company_ids
    are mapped through companies.business_id to compare against `bid`.
    Category label is preserved.
    """
    comp_id_to_bid: dict[int, int] = {
        c["id"]: c.get("business_id")
        for c in snap.get("tables", {}).get("companies", [])
    }
    out: dict[str, dict[tuple, str]] = {}
    for table, kv in tagged.items():
        if not kv:
            out[table] = {}
            continue
        if table == "mapp_records":
            out[table] = {k: cat for k, cat in kv.items()
                          if comp_id_to_bid.get(k[1]) == bid}
        else:
            out[table] = {k: cat for k, cat in kv.items() if k[0] == bid}
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _State:
    # Default DB path: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[2] of this file).
    db_path: Path = Path(__file__).resolve().parents[2] / "db" / "recon.sqlite3"
    # Reload trigger: every `reload_interval` seconds (env DASHBOARD_RELOAD,
    # default 30). This replaces the old snapshot-file-mtime trigger — the
    # DB is the source of truth, no intermediate file.
    reload_interval: float = float(os.environ.get("DASHBOARD_RELOAD", "30"))
    last_reload_ts: float = 0.0
    # Two HTML variants are cached so the home page (/, biz-link card)
    # and any future overview-mode callers don't pay the per-request
    # _aggregate + render cost. _gz variants are pre-compressed at
    # reload time so / no longer spends ~150ms in gzip.compress() per hit.
    cached_html_home: bytes = b""        # render_html(agg, home=True)
    cached_html_overview: bytes = b""    # render_html(agg, home=False)
    cached_gz_home: bytes = b""          # gzip(cached_html_home)
    cached_gz_overview: bytes = b""      # gzip(cached_html_overview)
    cached_snap: dict = {}              # parsed snapshot (shared across routes)
    cached_snap_json: bytes = b""       # /api/snapshot body (pre-rendered)
    # Reload lock: _maybe_reload is called from every request handler; without
    # a lock, N concurrent reloads would N×hit SQLite. We serialize reloads.
    reload_lock: Any = None  # initialized in main()


class Handler(BaseHTTPRequestHandler):
    server_version = "ReconDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        accept_enc = self.headers.get("Accept-Encoding", "").encode()
        raw = self.path.split("?", 1)[0]
        path = urllib.parse.unquote(raw).rstrip("/") or "/"

        # Known global routes
        if path == "/":
            self._serve_page(accept_enc, home=True)
            return
        if path == "/diff":
            self._serve_diff(None, accept_enc)
            return
        if path == "/health":
            self._respond(200, "text/plain; charset=utf-8", b"ok", accept_enc)
            return
        if path == "/refresh":
            self._do_reload()
            self._respond(200, "text/plain; charset=utf-8",
                          b"reloaded\n", accept_enc)
            return
        if path.startswith("/api/snapshot"):
            self._serve_snapshot(accept_enc)
            return

        # Per-business routes: /<业务名> and /<业务名>/new and /<业务名>/urls/<hash_id>
        if path.startswith("/"):
            biz_part = path[1:]
            biz_name, is_new = biz_part, False
            if biz_part.endswith("/new"):
                biz_name, is_new = biz_part[:-4], True
            snap = self._load_snap()
            bid = next((b["id"] for b in snap.get("tables", {}).get("businesses", [])
                        if b.get("business_name") == biz_name), None)
            if bid is not None:
                self._serve_business(biz_name, is_new, accept_enc)
                return

            # URL 详情页 /<业务名>/urls/<hash_id>(用户决策:不预加载,打开时实时查)
            # biz_part 形如 "<业务名>/urls/<hash_id>"
            if "/urls/" in biz_part:
                maybe_biz, hash_part = biz_part.split("/urls/", 1)
                if hash_part and "/" not in hash_part:
                    self._serve_urls_detail(maybe_biz, hash_part, accept_enc)
                    return

        self._respond(404, "text/plain",
                      f"not found: {raw}".encode("utf-8"), accept_enc)

    def do_POST(self) -> None:  # noqa: N802
        """POST routes:
          /<业务名>/scan              — 提交 hosts 列表触发 scan-onesite 子流程
          /<业务名>/scan-urls         — 提交 hosts + sources + wordlist 触发 URL 资产扫描
          /api/hash/<id>/edit         — 改 score + description(纯 JSON 响应,无重定向)
          /<业务名>/schedule/toggle   — 用户 2026-08-26:toggle 子域加入/移除每日扫描
                                        (纯 JSON 响应,无重定向 — JS 处理跳转 / 提示)
        """
        accept_enc = self.headers.get("Accept-Encoding", "").encode()
        raw = self.path.split("?", 1)[0]
        path = urllib.parse.unquote(raw).rstrip("/") or "/"

        if path.startswith("/api/hash/") and path.endswith("/edit"):
            self._handle_hash_edit(path, accept_enc)
            return

        # scan-urls 必须在 scan 之前判(避免 /urls 被吞)
        if "/scan-urls" in path:
            if path.endswith("/scan-urls"):
                biz_name = path[: -len("/scan-urls")].lstrip("/")
                if biz_name:
                    self._dispatch_scan_urls(biz_name, accept_enc)
                    return
            self._respond(404, "text/plain",
                          f"bad scan-urls path: {raw}\n".encode("utf-8"),
                          accept_enc)
            return

        # 用户 2026-08-26:/<业务名>/schedule/toggle — 加入/移除每日扫描
        # 必须在 /scan 之前判,避免 /<biz>/schedule 被解释成 "<biz>"/scan
        if "/schedule/toggle" in path:
            if path.endswith("/schedule/toggle"):
                biz_name = path[: -len("/schedule/toggle")].lstrip("/")
                if biz_name:
                    self._handle_schedule_toggle(biz_name, accept_enc)
                    return
            self._respond(404, "text/plain",
                          f"bad schedule path: {raw}\n".encode("utf-8"),
                          accept_enc)
            return

        if not path.startswith("/") or not path.endswith("/scan"):
            self._respond(404, "text/plain",
                          f"not found: {raw}".encode("utf-8"), accept_enc)
            return
        biz_name = path[1:-len("/scan")]
        if not biz_name:
            self._respond(404, "text/plain", b"missing business name\n",
                          accept_enc)
            return

        # 业务存在性校验：优先读缓存的 snap（避免 _load_snap → _maybe_reload
        # 在 _do_reload 持有 reload_lock 的窗口里撞锁死锁）。冷启动 fallback。
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next(
            (b["id"] for b in snap.get("tables", {}).get("businesses", [])
             if b.get("business_name") == biz_name), None)
        if bid is None:
            self._respond(404, "text/plain",
                          f"unknown business: {biz_name}\n".encode("utf-8"),
                          accept_enc)
            return

        self._handle_scan(biz_name, accept_enc)

    def _handle_hash_edit(self, path: str, accept_enc: bytes) -> None:
        """POST /api/hash/<id>/edit — 改 score + description，纯 JSON 响应。

        Body: application/x-www-form-urlencoded
          score       = int 0..100   (required)
          description = str          (optional, defaults to '')

        响应：200 {"ok": true, "id": <id>, "score": ..., "description": "..."}
              400 参数错 / 404 hash 不存在

        写入后立即局部更新 _State.cached_snap，避免等5 分钟 reload 看到陈旧数据。
        cron 下次跑时（按 §"Web Hash 评分"）只会处理新增 hash + 未评分 hash，
        所以本次手动修改不会被 cron 覆盖。
        """
        # 1) 解析 hash_id
        prefix = "/api/hash/"
        suffix = "/edit"
        mid = path[len(prefix):-len(suffix)] if path.endswith(suffix) else ""
        try:
            hash_id = int(mid)
        except ValueError:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"bad hash id"}\n', accept_enc)
            return

        # 2) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"bad Content-Length"}\n', accept_enc)
            return
        if length <= 0 or length > 4096:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"body empty or too large"}\n',
                          accept_enc)
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            fields = urllib.parse.parse_qs(body, keep_blank_values=True)
        except Exception:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"bad form body"}\n', accept_enc)
            return

        # 3) 校验 score
        if "score" not in fields:
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"missing score field"}\n',
                          accept_enc)
            return
        try:
            new_score = int(fields["score"][0])
        except (ValueError, IndexError):
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"score must be int"}\n',
                          accept_enc)
            return
        if not (0 <= new_score <= 100):
            self._respond(400, "application/json",
                          b'{"ok":false,"error":"score must be 0..100"}\n',
                          accept_enc)
            return
        new_desc = fields.get("description", [""])[0][:500]  # cap to 500 chars

        # 4) 写 DB（直接连，与 dashboard 共用 DB 文件）
        db_path = _State.db_path
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                row = conn.execute(
                    "SELECT id, score, description FROM web_hashes WHERE id=?",
                    (hash_id,),
                ).fetchone()
                if row is None:
                    self._respond(404, "application/json",
                                  b'{"ok":false,"error":"hash not found"}\n',
                                  accept_enc)
                    return
                conn.execute(
                    "UPDATE web_hashes SET score=?, description=?, "
                    "score_initialized_at = COALESCE(score_initialized_at, datetime('now', 'localtime')) "
                    "WHERE id=?",
                    (new_score, new_desc, hash_id),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self._respond(500, "application/json",
                          f'{{"ok":false,"error":"db error: {_e(str(exc))}"}}\n'
                          .encode("utf-8"),
                          accept_enc)
            return

        # 5) 局部更新 cached_snap（避免等 reload）
        try:
            cached = _State.cached_snap
            if cached:
                for h in cached.get("tables", {}).get("web_hashes", []):
                    if h.get("id") == hash_id:
                        h["score"] = new_score
                        h["description"] = new_desc
                for ws in cached.get("tables", {}).get("web_subdomains", []):
                    if ws.get("hash_id") == hash_id:
                        ws["hash_score"] = new_score
                        ws["hash_description"] = new_desc
                # 同步重算 /api/snapshot 用的预渲染 JSON（POST 立刻能看到新值）
                _State.cached_snap_json = json.dumps(
                    cached, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
        except Exception:
            pass  # 缓存更新失败不影响写入；reload 会追上

        body_out = (f'{{"ok":true,"id":{hash_id},'
                    f'"score":{new_score},'
                    f'"description":{json.dumps(new_desc)}}}\n').encode("utf-8")
        self._respond(200, "application/json", body_out, accept_enc)
        log.info(f"hash edit id={hash_id} score={new_score} desc_len={len(new_desc)}")

    def _handle_scan(self, biz_name: str, accept_enc: bytes) -> None:
        """POST /<业务>/scan 的实际处理：解析 body → 预校验 → subprocess 调 scan-onesite。"""
        # 1) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, "text/plain", b"bad Content-Length\n",
                          accept_enc)
            return
        if length <= 0 or length > SCAN_MAX_BODY:
            self._respond(400, "text/plain",
                          f"body too large or empty ({length} bytes)\n"
                          .encode("utf-8"), accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"),
                                          keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond(400, "text/plain", b"invalid utf-8 body\n",
                          accept_enc)
            return
        raw_hosts = (fields.get("hosts", [""])[0] or "").strip()

        # 2) Tokenize：split / strip / 小写 / 去尾点 / 跳空行井号 / 去重保序
        seen: set[str] = set()
        hosts: list[str] = []
        for line in raw_hosts.splitlines():
            s = line.strip().lower().rstrip(".")
            if not s or s.startswith("#"):
                continue
            if s in seen:
                continue
            seen.add(s)
            hosts.append(s)
        if not hosts:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=["未提供有效域名（空行/井号会被忽略）"],
                accept_enc=accept_enc, status=400)
            return

        # 3) 预校验（节省 5-30s subprocess）
        if len(hosts) > SCAN_MAX_HOSTS:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=[f"域名数量 {len(hosts)} 超过上限 {SCAN_MAX_HOSTS}"],
                accept_enc=accept_enc, status=400)
            return
        invalid = [h for h in hosts if not SCAN_HOSTNAME_RE.match(h)]
        if invalid:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=[f"hostname 不合法：{', '.join(invalid[:5])}"],
                accept_enc=accept_enc, status=400)
            return

        # 4) 写 tmpfile + subprocess
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="scan-hosts-", suffix=".txt",
                                           dir="/tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write("\n".join(hosts) + "\n")

            pdtm_script = (_State.db_path.parent.parent / "pdtm"
                           / "import_scan_results.py")
            cmd = ["/usr/bin/python3", str(pdtm_script), "scan-onesite",
                   "--business", biz_name,
                   "--hosts-file", str(tmp_path),
                   "--db", str(_State.db_path)]
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=SCAN_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[f"subprocess 超时（{SCAN_TIMEOUT}s）"],
                    accept_enc=accept_enc, status=504)
                return
            except FileNotFoundError as e:
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[f"无法启动 python3：{e}"],
                    accept_enc=accept_enc, status=500)
                return

            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", "replace").strip() \
                      or f"exit={proc.returncode}"
                status = 400 if proc.returncode == 2 else 500
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[err[:500]], accept_enc=accept_enc, status=status)
                return

            # 5) 成功路径
            out = (proc.stdout or b"").decode("utf-8", "replace")
            m = SCAN_RES_OK_RE.search(out)
            n_total = int(m.group(1)) if m else 0
            # 不在 POST 里同步 _do_reload() —— reload 期间 RSS 涨到 ~1.4G，
            # 多 POST 并发会把整机内存冲爆。把 last_reload_ts 强制设为 0，
            # 下一次 GET 会自动触发 reload（_maybe_reload 检测到过期），
            # 用户点「返回 /<业务>」时拿到的是 5-10s 后 reload 完的新数据。
            _State.last_reload_ts = 0.0
            self._render_scan_result(biz_name, ok=True, n_total=n_total,
                                     error_lines=[out.strip()],
                                     accept_enc=accept_enc, status=200)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _render_scan_result(self, biz_name: str, *, ok: bool, n_total: int,
                            error_lines: list[str], accept_enc: bytes,
                            status: int) -> None:
        """POST /<业务>/scan 之后返回的小结果页（绿/红 panel + 返回链接）。"""
        biz_quoted = urllib.parse.quote(biz_name)
        back_href = "/" + biz_quoted
        cls = "ok" if ok else "err"
        if ok:
            stdout = error_lines[0] if error_lines else ""
            body_inner = (
                f'<h2>扫描完成</h2>'
                f'<p>写入 <b>{_e(biz_name)}</b>：<b>{n_total}</b> 条</p>'
                f'<pre>{_e(stdout)}</pre>'
            )
        else:
            body_inner = (
                f'<h2>扫描失败</h2>'
                + "".join(f'<pre>{_e(line)}</pre>' for line in error_lines)
            )
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>扫描结果 · {_e(biz_name)}</title>
<style>{_CSS}</style></head>
<body>
<header><h1>SRC Recon — 扫描结果</h1>
  <div class="meta">业务：{_e(biz_name)}</div></header>
<main>
  <div class="scan-result {cls}">
    {body_inner}
    <a class="btn primary" href="{_e(back_href)}">返回 /{_e(biz_name)}</a>
    <a class="btn" href="/" style="margin-left:6px">返回总览</a>
  </div>
</main>
</body></html>
"""
        self._respond(status, "text/html; charset=utf-8",
                      page.encode("utf-8"), accept_enc)

    # ============================================================
    # URL 资产扫描(/<biz>/scan-urls)— 用户决策:cron 不参与,仅手动
    # ============================================================

    def _dispatch_scan_urls(self, biz_name: str, accept_enc: bytes) -> None:
        """POST /<biz>/scan-urls 的入口校验 + dispatch 到 _handle_scan_urls。

        业务存在性:同 /scan,优先 _State.cached_snap,缺时 _load_snap 兜底。
        """
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next(
            (b["id"] for b in snap.get("tables", {}).get("businesses", [])
             if b.get("business_name") == biz_name), None)
        if bid is None:
            self._respond(404, "text/plain",
                          f"unknown business: {biz_name}\n".encode("utf-8"),
                          accept_enc)
            return
        self._handle_scan_urls(biz_name, accept_enc)

    def _handle_scan_urls(self, biz_name: str, accept_enc: bytes) -> None:
        """POST /<业务>/scan-urls 的实际处理。

        body 字段:
          hosts    = 多行子域(必填)
          sources  = 多值('ffuf' / 'urlfinder' / 'gau',至少 1 个)
          wordlist = ffuf 字典路径(可空,空则用 SCAN_URLS_DEFAULT_WORDLIST)
        """
        # 1) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond(400, "text/plain", b"bad Content-Length\n",
                          accept_enc)
            return
        if length <= 0 or length > SCAN_URLS_MAX_BODY:
            self._respond(400, "text/plain",
                          f"body too large or empty ({length} bytes)\n"
                          .encode("utf-8"), accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(
                body.decode("utf-8", "replace"),
                keep_blank_values=True,
            )
        except UnicodeDecodeError:
            self._respond(400, "text/plain", b"invalid utf-8 body\n",
                          accept_enc)
            return

        raw_hosts = (fields.get("hosts", [""])[0] or "").strip()
        sources = [s.strip() for s in fields.get("sources", [])
                   if s and s.strip()]
        wordlist_arg = (fields.get("wordlist", [""])[0] or "").strip()

        # 2) 校验 hosts
        seen: set[str] = set()
        hosts: list[str] = []
        for line in raw_hosts.splitlines():
            s = line.strip().lower().rstrip(".")
            if not s or s.startswith("#"):
                continue
            if s in seen:
                continue
            seen.add(s)
            hosts.append(s)
        if not hosts:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=["未提供有效域名（空行/井号会被忽略）"],
                accept_enc=accept_enc, status=400)
            return
        if len(hosts) > SCAN_URLS_MAX_HOSTS:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=[f"域名数量 {len(hosts)} 超过上限 {SCAN_URLS_MAX_HOSTS}"],
                accept_enc=accept_enc, status=400)
            return
        invalid = [h for h in hosts if not SCAN_HOSTNAME_RE.match(h)]
        if invalid:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=[f"hostname 不合法:{', '.join(invalid[:5])}"],
                accept_enc=accept_enc, status=400)
            return

        # 3) 校验 sources(至少 1 个,必须在 SCAN_URLS_VALID_SOURCES 内)
        if not sources:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=["至少勾选一个扫描阶段(ffuf/urlfinder/gau)"],
                accept_enc=accept_enc, status=400)
            return
        bad_src = [s for s in sources if s not in SCAN_URLS_VALID_SOURCES]
        if bad_src:
            self._render_scan_result(
                biz_name, ok=False, n_total=0,
                error_lines=[f"未知 source:{bad_src};合法: {list(SCAN_URLS_VALID_SOURCES)}"],
                accept_enc=accept_enc, status=400)
            return
        # 去重保序
        seen_s = set(); sources = [s for s in sources if not (s in seen_s or seen_s.add(s))]

        # 4) 写 tmpfile + subprocess
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix="scan-urls-hosts-", suffix=".txt", dir="/tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write("\n".join(hosts) + "\n")

            pdtm_script = (_State.db_path.parent.parent / "pdtm"
                           / "scan_urls.py")
            cmd = ["/usr/bin/python3", str(pdtm_script), "scan-urls",
                   "--business", biz_name,
                   "--hosts-file", str(tmp_path),
                   "--db", str(_State.db_path),
                   "--sources", ",".join(sources)]
            if wordlist_arg:
                cmd.extend(["--wordlist", wordlist_arg])
            try:
                proc = subprocess.run(cmd, capture_output=True,
                                      timeout=SCAN_URLS_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[f"subprocess 超时({SCAN_URLS_TIMEOUT}s)"],
                    accept_enc=accept_enc, status=504)
                return
            except FileNotFoundError as e:
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[f"无法启动 python3:{e}"],
                    accept_enc=accept_enc, status=500)
                return

            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", "replace").strip() \
                      or f"exit={proc.returncode}"
                status = 400 if proc.returncode == 2 else 500
                self._render_scan_result(
                    biz_name, ok=False, n_total=0,
                    error_lines=[err[:800]], accept_enc=accept_enc,
                    status=status)
                return

            # 5) 成功路径
            out = (proc.stdout or b"").decode("utf-8", "replace")
            # scan_urls.py 最后一行是 JSON;从里面抽 total_new_rows
            n_total = 0
            for line in out.splitlines():
                m = SCAN_URLS_RES_OK_RE.search(line)
                if m:
                    n_total = int(m.group(1))
                    break
            # 同 scan-onesite:不主动 _do_reload,把 last_reload_ts 置 0,
            # 下次 GET 自动 reload
            _State.last_reload_ts = 0.0
            self._render_scan_result(
                biz_name, ok=True, n_total=n_total,
                error_lines=[out.strip() + "\n\n(到「站点详情」点对应 hash 的「URL 详情」按钮查看新结果)"],
                accept_enc=accept_enc, status=200)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _handle_schedule_toggle(self, biz_name: str, accept_enc: bytes) -> None:
        """POST /<业务名>/schedule/toggle — 用户 2026-08-26 拍板。

        body 字段(x-www-form-urlencoded):
          subdomain = 子域名(必填,FQDN 格式,经 SCAN_HOSTNAME_RE 校验)
          action    = 'add' / 'remove'(必填)

        语义:
          - add    → INSERT INTO web_subdomain_scan_schedule(biz_id, sub, sources='urlfinder')
                     同 (biz_id, sub) 已存在 → 不动(幂等),返回 ok=1 / created=0
          - remove → DELETE FROM web_subdomain_scan_schedule WHERE biz_id & sub
                     不存在行也算 ok(幂等)

        返回纯 JSON(200 / 400 / 404),前端 JS 解析后提示用户 + 刷新页面。
        不刷新 cache:schedule 不在 _State.cached_snap 里(配置不在快照里),
        下次 _serve_urls_detail 查 schedule 表时即生效。
        """
        # 1) 读 body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._respond_json(400, {"error": "bad Content-Length"},
                               accept_enc)
            return
        if length <= 0 or length > SCAN_URLS_MAX_BODY:
            self._respond_json(400, {"error": f"body too large or empty ({length})"},
                               accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(
                body.decode("utf-8", "replace"), keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond_json(400, {"error": "invalid utf-8 body"}, accept_enc)
            return

        subdomain = (fields.get("subdomain", [""])[0] or "").strip().lower().rstrip(".")
        action = (fields.get("action", [""])[0] or "").strip().lower()

        # 2) 校验
        if not subdomain or not SCAN_HOSTNAME_RE.match(subdomain):
            self._respond_json(400, {"error": f"invalid subdomain: {subdomain!r}"},
                               accept_enc)
            return
        if action not in ("add", "remove"):
            self._respond_json(400,
                               {"error": f"action must be add|remove (got {action!r})"},
                               accept_enc)
            return

        # 3) 业务存在性 — 直接查 DB(schedule 表不走 cached_snap)
        conn = sqlite3.connect(str(_State.db_path), timeout=5)
        try:
            bid_row = conn.execute(
                "SELECT id FROM businesses WHERE TRIM(business_name)=?",
                (biz_name,)).fetchone()
            if not bid_row:
                self._respond_json(404,
                                   {"error": f"unknown business: {biz_name}"},
                                   accept_enc)
                return
            bid = int(bid_row[0])

            if action == "add":
                # sources 缺省 urlfinder(同 schedule 表列默认值)
                cur = conn.execute("""
                    INSERT OR IGNORE INTO web_subdomain_scan_schedule
                      (business_id, subdomain, sources, enabled)
                    VALUES (?, ?, 'urlfinder', 1)
                """, (bid, subdomain))
                created = 1 if cur.rowcount > 0 else 0
                self._respond_json(200,
                                   {"ok": True, "subdomain": subdomain,
                                    "action": "add", "created": created,
                                    "schedule_state": "enabled"},
                                   accept_enc)
            else:  # remove
                cur = conn.execute("""
                    DELETE FROM web_subdomain_scan_schedule
                     WHERE business_id = ? AND subdomain = ?
                """, (bid, subdomain))
                removed = cur.rowcount
                self._respond_json(200,
                                   {"ok": True, "subdomain": subdomain,
                                    "action": "remove", "removed": removed,
                                    "schedule_state": "absent"},
                                   accept_enc)
            conn.commit()
        except sqlite3.Error as e:
            self._respond_json(500, {"error": f"db error: {e}"}, accept_enc)
        finally:
            conn.close()

    def _respond_json(self, status: int, payload: dict,
                      accept_enc: bytes) -> None:
        """JSON 响应 helper,仅用于 _handle_hash_edit / _handle_schedule_toggle。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, "application/json; charset=utf-8", body,
                      accept_enc)

    def _serve_urls_detail(self, biz_name: str, hash_id_str: str,
                           accept_enc: bytes) -> None:
        """GET /<业务名>/urls/<hash_id> — 实时查 web_hash_urls 渲染 URL 详情页。

        用户决策:严格 lazy,这个页面打开时才查 SQL,不在 reload snapshot 里预加载。
        """
        # 1) 校验 hash_id
        try:
            hash_id = int(hash_id_str)
        except ValueError:
            self._respond(400, "text/plain", b"bad hash id\n", accept_enc)
            return
        if hash_id <= 0:
            self._respond(404, "text/plain", b"hash not found\n", accept_enc)
            return

        # 2) 业务存在性 + 拿 hash 元数据
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next(
            (b["id"] for b in snap.get("tables", {}).get("businesses", [])
             if b.get("business_name") == biz_name), None)
        if bid is None:
            self._respond(404, "text/plain",
                          f"unknown business: {biz_name}\n".encode("utf-8"),
                          accept_enc)
            return

        hash_meta = next(
            (h for h in snap.get("tables", {}).get("web_hashes", [])
             if h.get("id") == hash_id and h.get("business_id") == bid),
            None,
        )
        if hash_meta is None:
            self._respond(404, "text/plain",
                          f"hash #{hash_id} not in {biz_name}\n"
                          .encode("utf-8"), accept_enc)
            return

        # 3) 实时查 web_hash_urls(独立连接,5s timeout,只读)
        try:
            conn = sqlite3.connect(str(_State.db_path), timeout=5)
            try:
                # row_factory 让 fetchall/fetchone 返回 sqlite3.Row 支持列名访问
                conn.row_factory = sqlite3.Row
                # 子域:取该 hash 下任意一个 active subdomain 用于"来源子域"显示
                sub_row = conn.execute(
                    "SELECT subdomain, port FROM web_subdomains "
                    "WHERE hash_id = ? "
                    "ORDER BY is_active DESC, subdomain LIMIT 1",
                    (hash_id,),
                ).fetchone()
                sample_subdomain = sub_row["subdomain"] if sub_row else None
                sample_port = sub_row["port"] if sub_row else None

                # URL 列表:按 source 排序,path 字母序
                # 用户 2026-08-26:加 change_type(染色 + toggle 用)
                # + is_static(确认静态过滤用,替代前端 str.endswith 判定)
                rows = conn.execute(
                    "SELECT id, hash_id, subdomain, source, scheme, host, "
                    "       port, path, url, status_code, content_type, "
                    "       content_length, word_count, "
                    "       redirect, link_source, risk_flag, "
                    "       is_dangerous, danger_reason, "
                    "       first_seen, last_seen, fetched_at, is_active, "
                    "       is_static, change_type "
                    "  FROM web_hash_urls "
                    " WHERE hash_id = ? AND is_active = 1 "
                    " ORDER BY source, path, url",
                    (hash_id,),
                ).fetchall()

                # 用户 2026-08-26:读 sample_subdomain 的 schedule 信息
                # - schedule_state: 'enabled' / 'disabled' / 'absent'
                # - last_run_at: 上次成功扫描时间(toggle 染色基准;None = 没跑过)
                # schedule 在 diff.py 不参与 → 直接查表即可,不污染 snapshot
                if sample_subdomain:
                    sched_row = conn.execute(
                        "SELECT enabled, last_run_at FROM web_subdomain_scan_schedule "
                        "WHERE business_id = ? AND subdomain = ? LIMIT 1",
                        (bid, sample_subdomain),
                    ).fetchone()
                    if sched_row:
                        schedule_enabled = int(sched_row["enabled"] or 0)
                        schedule_state = "enabled" if schedule_enabled else "disabled"
                        last_scan_at = sched_row["last_run_at"]
                    else:
                        schedule_state = "absent"
                        last_scan_at = None
                else:
                    schedule_state = "absent"
                    last_scan_at = None

                # fallback:web_subdomains 没对应行(例如 URL 是 ffuf/gau 扫出但 web_subdomains
                # 已 deactive),用 web_hash_urls 的 subdomain — 该 hash 下所有 URL 通常都来自
                # 同一个 subdomain。取第一行即可。
                if not sample_subdomain and rows:
                    sample_subdomain = rows[0]["subdomain"] or None
                    sample_port = rows[0]["port"] if rows else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._respond(500, "text/plain",
                          f"db error: {e}\n".encode("utf-8"), accept_enc)
            return

        # 4) 按 source 分组
        from collections import defaultdict
        by_source: dict[str, list] = defaultdict(list)
        for r in rows:
            by_source[r["source"]].append(dict(r))
        per_source_count = {s: len(by_source.get(s, [])) for s in SCAN_URLS_VALID_SOURCES}

        # 5) 渲染(简单 HTML 表格;hash 子域数量展示用 url_count)
        url_count = hash_meta.get("url_count", len(rows))

        def _status_risk(code):
            if code is None:
                return "info"
            if 500 <= code < 600:
                return "high"
            if 400 <= code < 500:
                return "med"
            if 300 <= code < 400:
                return "info"
            return "low"

        # 用户 2026-08-26:schedule_state → 中文 label
        def _schedule_label(state):
            return {"enabled": "已开启", "disabled": "已暂停",
                    "absent": "未加入"}.get(state, "未知")

# 渲染顺序:urlfinder → ffuf → gau(用户决策)。
        # SCAN_URLS_VALID_SOURCES 的字母序("ffuf","gau","urlfinder")在 scan-urls 表单校验
        # 还要用,不能动;这里显式覆盖展示顺序。
        SOURCE_DISPLAY_ORDER = ("urlfinder", "ffuf", "gau")
        SOURCE_HEADING = {
            "urlfinder": "URLFinder 主动爬虫",
            "ffuf":       "ffuf 字典爆破",
            "gau":        "gau 被动收集 (wayback / otx / commoncrawl)",
        }
        # 列定义 (label, key, kind)。kind:
        #   'a'     — 链接(<a target=_blank>)
        #   'text'  — 纯文本
        #   'num'   — 数字列,右对齐 + em-dash 表示 None
        #   'risk'  — risk_flag 非空时染黄底
        #   'danger'— is_dangerous=1 红底+⚠
        #   'host'  — host[:非默认 port]
        #   'time'  — last_seen/fetched_at 原文
        COMMON_COLS = (
            ("URL",        "url",            "a"),
            ("path",       "path",           "text"),
            ("status",     "status_code",    "num"),
            ("bytes",      "content_length", "num"),
            ("host:port",  "_hostport",      "host"),
            ("risk",       "risk_flag",      "risk"),
            ("danger",     "_danger",        "danger"),
            ("last_seen",  "fetched_at",     "time"),
        )
        URLFINDER_EXTRA_COLS = (
            ("title",       "title",       "text"),
            ("words",       "word_count",  "num"),
            ("redirect",    "redirect",    "text"),
            ("link_source", "link_source", "text"),
        )
        FFUF_GAU_EXTRA_COLS = (
            ("words", "word_count", "num"),
        )

        def _trunc(html_inner, css_class=""):
            """把已渲染好的 cell 内容包进 <div class=\"trunc\">,触发可滚动行为。"""
            extra = f" {css_class}" if css_class else ""
            return f'<div class="trunc{extra}">{html_inner}</div>'

        def _render_cell(kind, key, e):
            """返回单 cell 的完整 <td>...</td>。

            kind 决定样式 (a/text/num/host/time/risk/danger);
            key 决定取哪一列的值 — 同一 kind (如 num) 在 status / bytes / words
            三个列各自取不同字段,所以参数化避免 bug。
            """
            url = e.get("url") or ""
            path = e.get("path") or ""
            host = e.get("host") or ""
            port = e.get("port")
            content_length = e.get("content_length")
            word_count = e.get("word_count")
            fetched_at = e.get("fetched_at") or ""
            redirect = e.get("redirect") or ""
            link_source = e.get("link_source") or ""
            risk_flag = e.get("risk_flag") or ""
            is_dangerous = e.get("is_dangerous") or 0
            danger_reason = e.get("danger_reason") or ""
            title = e.get("title") or ""

            # num 列:按 key 决定 status_code / content_length / word_count
            if kind == "num":
                if key == "status_code":
                    v = e.get("status_code")
                elif key == "content_length":
                    v = content_length
                elif key == "word_count":
                    v = word_count
                else:
                    v = None
                if v is None:
                    return f'<td class="num">{_trunc("—")}</td>'
                return f'<td class="num">{_trunc(str(v))}</td>'

            # text 列:按 key 决定 path / title / redirect / link_source
            if kind == "text":
                if key == "path":
                    txt = path
                elif key == "title":
                    txt = title
                elif key == "redirect":
                    txt = redirect
                elif key == "link_source":
                    txt = link_source
                else:
                    txt = ""
                if not txt:
                    return f'<td class="muted">{_trunc("—")}</td>'
                return f'<td>{_trunc(_e(txt))}</td>'

            if kind == "a":
                url_a = (
                    f'<a href="{_e(url)}" target="_blank" '
                    f'rel="noopener noreferrer">{_e(url)}</a>'
                )
                return f'<td>{_trunc(url_a)}</td>'
            if kind == "host":
                hp = _e(host) + (f":{port}" if port and port not in (80, 443) else "")
                return f'<td>{_trunc(hp)}</td>'
            if kind == "time":
                return f'<td>{_trunc(_e(fetched_at))}</td>'
            if kind == "risk":
                if risk_flag:
                    return f'<td class="risk-cell">{_trunc(_e(risk_flag))}</td>'
                return f'<td class="muted">{_trunc("—")}</td>'
            if kind == "danger":
                if is_dangerous:
                    inner = (
                        '<span title="' + _e(danger_reason) + '">'
                        '⚠ ' + (_e(danger_reason) if danger_reason else "danger") +
                        '</span>'
                    )
                    return f'<td class="danger-cell">{_trunc(inner, "danger-cell")}</td>'
                return f'<td class="muted">{_trunc("—")}</td>'
            return f'<td>{_trunc("")}</td>'

        def _render_row(e, cols, src):
            st = e.get("status_code")
            sub = e.get("subdomain") or ""
            host = e.get("host") or ""  # URL 实际请求的 host(可能 ≠ subdomain,如 large-assets 子域下访问了 test-internal)
            # 判断 path 是否为静态资源(.js / .css 任意大小写) — 用 path 部分,
            # query / fragment 已剥离在 _parse_url() 阶段,所以这里直接判 path。
            p = (e.get("path") or "").lower()
            # 用户 2026-08-26:优先用 DB 列 is_static(扫描时算好),
            # 旧行(None)回退到 str.endswith 判定 — 维持向后兼容
            raw_is_static = e.get("is_static")
            if raw_is_static is None:
                is_static = 1 if p.endswith(".js") or p.endswith(".css") else 0
            else:
                is_static = int(raw_is_static)
            # data-host → 「仅显示当前子域」(过滤 URL 实际 host = 当前 subdomain 的);
            # data-subdomain → 保留做参考,不影响过滤;
            # data-status → 「仅 200」;data-static → 「去除 .js / .css」
            # 用户 2026-08-26:加 data-change-type + data-last-scan-at
            # 触发器 trg_whu_* 维护 change_type(is_static=0 gate),diff.py 跑完 reset=0
            # last-scan-at 来自 schedule.last_run_at,所有行共用同一字符串
            ct = e.get("change_type") or 0
            lsa = last_scan_at or ""
            # 染色 class:仅对"满足当前子域 && 非静态 && change_type>0"行加 class
            # (符合用户 gate:只有满足条件才可能修改 change_type)
            row_class_extra = ""
            if not is_static and sub == sample_subdomain and ct > 0:
                if ct & 1:    # added
                    row_class_extra = " row-new"
                elif ct & 2:  # content changed
                    row_class_extra = " row-changed"
                elif ct & 4:  # reactivated
                    row_class_extra = " row-reactivated"
            return (
                f'<tr class="risk-{_status_risk(st)}{row_class_extra}" '
                f'data-source="{_e(src)}" data-subdomain="{_e(sub)}" '
                f'data-host="{_e(host)}" '
                f'data-status="{st if st is not None else ""}" '
                f'data-static="{is_static}" '
                f'data-change-type="{ct}" '
                f'data-last-scan-at="{_e(lsa)}">'
                + "".join(_render_cell(kind, key, e) for _label, key, kind in cols)
                + '</tr>'
            )

        # 每 source 的列:urlfinder 多 title/redirect/link + words;ffuf/gau 只有 words
        def _cols_for(src):
            if src == "urlfinder":
                return COMMON_COLS + URLFINDER_EXTRA_COLS
            return COMMON_COLS + FFUF_GAU_EXTRA_COLS

        def _colgroup(cols):
            """按列类型返回 col 元素(比例贴近 ant-table 默认 150px 最小宽度)。"""
            parts = []
            for label, _key, _kind in cols:
                if label == "URL":
                    parts.append('<col class="col-url">')
                elif label == "path":
                    parts.append('<col class="col-path">')
                elif label == "status":
                    parts.append('<col class="col-status">')
                elif label == "bytes":
                    parts.append('<col class="col-bytes">')
                elif label == "words":
                    parts.append('<col class="col-words">')
                elif label == "host:port":
                    parts.append('<col class="col-host">')
                elif label == "redirect":
                    parts.append('<col class="col-redirect">')
                elif label == "link_source":
                    parts.append('<col class="col-link">')
                elif label == "risk":
                    parts.append('<col class="col-risk">')
                elif label == "danger":
                    parts.append('<col class="col-danger">')
                elif label == "last_seen":
                    parts.append('<col class="col-last">')
                elif label == "title":
                    parts.append('<col class="col-title">')
                else:
                    parts.append('<col>')
            return "".join(parts)

        # 渲染每张表 — 按 SOURCE_DISPLAY_ORDER 顺序,空 source 跳过
        source_tables = []
        for src in SOURCE_DISPLAY_ORDER:
            entries = by_source.get(src, [])
            if not entries:
                continue
            cols = _cols_for(src)
            # 表头:仅 status / bytes 两列加 sortable(点击排序三态循环 asc → desc → none),
            # 其它列不可点击。
            def _th(label, key, kind):
                if key in ("status_code", "content_length"):
                    return (f'<th class="sortable" data-col="{_e(key)}" '
                            f'data-kind="{_e(kind)}">{_e(label)}</th>')
                return f'<th>{_e(label)}</th>'
            head = "".join(_th(label, key, kind) for label, key, kind in cols)
            rows_html = (
                "".join(_render_row(e, cols, src) for e in entries)
                if entries else
                f'<tr><td colspan="{len(cols)}" class="muted">(无)</td></tr>'
            )
            heading = SOURCE_HEADING.get(src, src)
            tbl_html = (
                f'<h3 class="src-heading">{_e(heading)} '
                f'<span class="muted">· {_e(src)} · {len(entries):,} 条</span></h3>'
                f'<div class="ant-table-wrapper">'
                f'<div class="ant-table">'
                f'<table class="urls-table">'
                f'<thead><tr>{head}</tr></thead>'
                f'<tbody>{rows_html}</tbody>'
                f'</table>'
                f'</div></div>'
            )
            source_tables.append(tbl_html)
        biz_quoted = urllib.parse.quote(biz_name)
        back_href = f"/{biz_quoted}"
        # 给 JS 用:把 sample_subdomain / last_scan_at / biz_quoted 序列化为 JSON 字面量
        # 避开双引号转义问题;用户 2026-08-26 新增 lastScanAt + bizName 给 JS 用
        _sample_sub_json = json.dumps(sample_subdomain)
        _last_scan_json = json.dumps(last_scan_at or "")
        _biz_quoted_json = json.dumps(biz_quoted)
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>URL 详情 · hash #{hash_id} · {_e(biz_name)}</title>
<style>{_CSS}</style>
#<style>
/* URL 详情页:URLFinder ant-table 视觉风格(提炼自 demo.html,~3KB,不引入 3.8MB Ant Design 全套 CSS) */
.ant-table-wrapper {{ zoom:1; }}
.ant-table-wrapper::before, .ant-table-wrapper::after {{ display:table; content:""; }}
.ant-table-wrapper::after {{ clear:both; }}
.ant-table {{
  -webkit-box-sizing:border-box; box-sizing:border-box; margin:0; padding:0;
  color:rgba(0,0,0,.65); font-size:14px; font-variant:tabular-nums;
  line-height:1.5; list-style:none; position:relative; clear:both;
}}
.urls-table {{
  width:100%; text-align:left; border-radius:4px 4px 0 0;
  border-collapse:separate; border-spacing:0;
  word-wrap:break-word; word-break:break-all;
  font-variant:tabular-nums; -webkit-font-feature-settings:"tnum";
  font-feature-settings:"tnum";
  /* 列宽由该列最长内容决定(自适应);数字列加 min-width 防过窄 */
  table-layout:auto;
}}
.urls-table th, .urls-table td {{
  border-bottom:1px solid #e8e8e8; padding:8px 12px;
  color:rgba(0,0,0,.65); font-size:13px;
  /* 长内容多行展示 — 不再 nowrap + 横向滚动;过长 URL/路径自动 wrap */
  white-space:normal; word-break:break-all; vertical-align:top;
}}
.urls-table thead > tr > th {{
  background:#fafafa; font-weight:500; text-align:left; color:rgba(0,0,0,.85);
  border-bottom:1px solid #e8e8e8;
  -webkit-transition:background .3s ease; transition:background .3s ease;
  position:relative; white-space:nowrap; /* 表头不换行,看着整齐 */
}}
/* ant-table 表头分隔线 */
.urls-table thead > tr > th:not(:last-child)::after {{
  content:""; position:absolute; right:0; top:8px; bottom:8px;
  width:1px; background:rgba(0,0,0,.06);
}}
.urls-table tbody > tr:not(:last-child) > td {{ border-bottom:1px solid #eaecef; }}
.urls-table tbody > tr:hover > td {{ background:#fafafa; }}
.urls-table tbody > tr > td .mono {{ font-family:monospace; }}
.urls-table tbody > tr > td.num,
.urls-table td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
/* 数字列 min-width 兜底 — table-layout:auto 时空字符串会被压到 0;
   用户拍板:status/bytes/words 列里数值都不会太长,40~70px 足够 */
.urls-table th.col-num, .urls-table td.num {{ min-width:48px; }}
/* .trunc 现在只是空容器 wrapper(为了 future 可选加 max-height),
   不再强制 nowrap + overflow;内容自然 wrap */
.urls-table .trunc {{
  display:block; max-width:100%;
  /* 不再 overflow-x:auto;让长 URL 多行 */
}}
/* 可点击表头(只 status / bytes 列) */
.urls-table th.sortable {{
  cursor:pointer; user-select:none;
  -webkit-user-select:none;
  transition:background .15s ease;
}}
.urls-table th.sortable:hover {{ background:#f0f0f0; }}
.urls-table th.sortable::after {{
  content:"⇅"; opacity:.35; margin-left:6px; font-size:11px;
  display:inline-block;
}}
.urls-table th.sortable.sort-asc::after  {{ content:"▲"; opacity:1; color:#0969da; }}
.urls-table th.sortable.sort-desc::after {{ content:"▼"; opacity:1; color:#0969da; }}
/* 风险行底色(贴近 ant-table token) */
.urls-table tbody > tr.risk-high > td {{ background:#fff1f0; }}
.urls-table tbody > tr.risk-high:hover > td {{ background:#ffccc7; }}
.urls-table tbody > tr.risk-med  > td {{ background:#fffbe6; }}
.urls-table tbody > tr.risk-med:hover  > td {{ background:#fff1b8; }}
.urls-table tbody > tr.risk-info > td {{ background:#fafafa; }}
.urls-table tbody > tr.risk-low  > td {{ background:#fcffe6; }}
.urls-table td.danger-cell {{
  background:#ffccc7; color:#cf222e; font-weight:600;
  border-left:3px solid #cf222e;
}}
.urls-table td.risk-cell {{
  background:#fffbe6; color:#d4380d; font-weight:500;
}}
h3.src-heading {{
  margin:18px 0 8px 0; font-size:15px; font-weight:500; color:rgba(0,0,0,.85);
}}
.urls-summary {{ display:flex; gap:14px; margin:8px 0 16px; flex-wrap:wrap;
                  align-items:center; }}
.urls-summary .chip {{
  background:#fafafa; border:1px solid #e8e8e8; border-radius:4px;
  padding:4px 10px; font-size:13px; color:rgba(0,0,0,.65);
}}
.urls-filter {{ display:flex; gap:14px; margin:0 0 16px 0; padding:8px 12px;
                background:#fafafa; border:1px solid #e8e8e8; border-radius:4px;
                align-items:center; font-size:13px; }}
.urls-filter label {{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }}
.urls-filter input[type=checkbox] {{ cursor:pointer; }}
.urls-filter .sample-sub {{ color:#57606a; margin-left:8px; }}

/* 用户 2026-08-26:默认染色 + schedule 按钮样式。
   row-new  = change_type & 1(新增)— 浅绿背景
   row-changed = change_type & 2(内容变化)— 浅蓝背景
   row-reactivated = change_type & 4 / 6(复活)— 浅橙背景
   这些 class 只在 _serve_urls_detail 给"满足当前子域 && 非静态 &&
   change_type>0"的行加;默认就对所有用户可见 */
.urls-table tbody > tr.row-new > td {{ background:#f6ffed; }}
.urls-table tbody > tr.row-new:hover > td {{ background:#d9f7be; }}
.urls-table tbody > tr.row-changed > td {{ background:#e6f7ff; }}
.urls-table tbody > tr.row-changed:hover > td {{ background:#bae7ff; }}
.urls-table tbody > tr.row-reactivated > td {{ background:#fff7e6; }}
.urls-table tbody > tr.row-reactivated:hover > td {{ background:#ffe7ba; }}

/* schedule chip + 按钮 */
.schedule-chip {{ font-weight:500; }}
.schedule-chip.schedule-enabled {{ background:#f6ffed; border-color:#b7eb8f;
                                     color:#389e0d; }}
.schedule-chip.schedule-disabled {{ background:#fffbe6; border-color:#ffe58f;
                                      color:#d48806; }}
.schedule-chip.schedule-absent {{ background:#fafafa; border-color:#d9d9d9;
                                    color:#8c8c8c; }}
.schedule-toggle-btn {{ margin-left:8px; }}
.schedule-toggle-btn:hover {{ background:#0969da; color:#fff; border-color:#0969da; }}
</style>
</head>
<body>
<header><h1>URL 资产详情 — hash #{hash_id}</h1>
  <div class="meta">业务:{_e(biz_name)} · 来源子域:{_e(sample_subdomain)}
    {f":{sample_port}" if sample_port and sample_port not in (80, 443) else ""}
    · response_hash: <code>{_e(str(hash_meta.get("response_hash")))}</code>
    · subdomain_count: {hash_meta.get("subdomain_count", "—")}
    · url_count(活跃): <b>{url_count:,}</b></div></header>
<main>
  <div class="urls-summary">
    <span class="chip">ffuf: {per_source_count.get("ffuf", 0):,}</span>
    <span class="chip">urlfinder: {per_source_count.get("urlfinder", 0):,}</span>
    <span class="chip">gau: {per_source_count.get("gau", 0):,}</span>
    <span class="chip">合计: {len(rows):,}</span>
    <span class="chip schedule-chip schedule-{schedule_state}">每日扫描:{_schedule_label(schedule_state)}</span>
    {f'<button class="btn schedule-toggle-btn" id="schedule-toggle-btn" data-action="{("remove" if schedule_state == "enabled" else "add")}">{("移除每日扫描" if schedule_state == "enabled" else "加入每日扫描")}</button>' if sample_subdomain else ''}
  </div>
  <div class="urls-filter">
    <label><input type="checkbox" id="only-this-sub"> 仅显示当前子域</label>
    <label><input type="checkbox" id="only-200"> 仅显示 status=200</label>
    <label><input type="checkbox" id="hide-static"> 去除 .js / .css</label>
    <label><input type="checkbox" id="only-new-changed"> 仅显示新增或改变</label>
    <span class="sample-sub">来源子域:<code>{_e(sample_subdomain)}</code></span>
  </div>
  {"".join(source_tables) if source_tables else '<p class="muted">该 hash 下暂无 URL 资产记录。回业务页用「URL 资产扫描」表单发起一次扫描。</p>'}
  <p style="margin-top:16px;">
    <a class="btn primary" href="{_e(back_href)}">返回 /{_e(biz_name)}</a>
    <a class="btn" href="/" style="margin-left:6px">返回总览</a>
  </p>
</main>
<script>
(function() {{
  // 客户端筛选 — 纯 DOM,无网络往返
  // 数据来源:每行 <tr data-subdomain data-status>;sample_subdomain 由页面渲染时 inline 注入
  var sampleSub = {_sample_sub_json};
  var lastScanAt = {_last_scan_json};
  var bizName = {_biz_quoted_json};
  var cbSub = document.getElementById('only-this-sub');
  var cb200 = document.getElementById('only-200');
  var cbStatic = document.getElementById('hide-static');
  var cbNewChanged = document.getElementById('only-new-changed');

  function applyFilters() {{
    var onlySub = cbSub.checked;
    var only200 = cb200.checked;
    var hideStatic = cbStatic && cbStatic.checked;
    var onlyNewChanged = cbNewChanged && cbNewChanged.checked;
    var rows = document.querySelectorAll('.urls-table tbody tr[data-subdomain]');
    rows.forEach(function(tr) {{
      var host = tr.getAttribute('data-host') || '';
      var st  = tr.getAttribute('data-status') || '';
      var isStatic = tr.getAttribute('data-static') === '1';
      var ct = parseInt(tr.getAttribute('data-change-type') || '0', 10);
      var lsa = tr.getAttribute('data-last-scan-at') || '';
      var ok = true;
      // 「仅显示当前子域」→ URL 实际 host === sample_subdomain(过滤掉 test-internal 这种杂 host)
      if (onlySub && host !== sampleSub) ok = false;
      if (only200 && st !== '200') ok = false;
      if (hideStatic && isStatic) ok = false;
      // 用户 2026-08-26:「仅显示新增或改变」(独立 toggle — 用户拍板分离,不联动
      // 「去除 .js / .css」)。语义:当前子域 + 非 js/css + (新增 / 内容变)。
      //   ct & 1 → INSERT 触发;ct & 2 → AU 触发;这两个 >0 表示本轮变了
      //   ls > lsa 兜底(若 schedule 缺失 last_run_at,永不命中 → 安全)
      if (onlyNewChanged) {{
        if (host !== sampleSub) ok = false;
        if (isStatic) ok = false;
        if (!((ct & 1) || (ct & 2))) ok = false;
      }}
      tr.style.display = ok ? '' : 'none';
    }});
    // 同步每张表的可见数到 h3 标题
    // DOM 结构:<h3 class="src-heading"> 是 <table>.parentElement.parentElement.previousElementSibling
    document.querySelectorAll('.urls-table').forEach(function(tbl) {{
      var wrapper = tbl.parentElement && tbl.parentElement.parentElement;
      var h = wrapper && wrapper.previousElementSibling;
      if (!h || !h.classList.contains('src-heading')) return;
      var n = 0, total = 0;
      tbl.querySelectorAll('tbody tr[data-subdomain]').forEach(function(tr) {{
        total++;
        if (tr.style.display !== 'none') n++;
      }});
      var chip = h.querySelector('.count-chip');
      if (!chip) {{
        chip = document.createElement('span');
        chip.className = 'count-chip muted';
        chip.style.marginLeft = '8px';
        h.appendChild(chip);
      }}
      chip.textContent = '· 可见 ' + n + '/' + total + ' 条';
    }});
  }}

  cbSub.addEventListener('change', applyFilters);
  cb200.addEventListener('change', applyFilters);
  if (cbStatic) cbStatic.addEventListener('change', applyFilters);
  if (cbNewChanged) cbNewChanged.addEventListener('change', applyFilters);

  // 用户 2026-08-26:schedule 按钮 → POST /<biz>/schedule/toggle
  var schedBtn = document.getElementById('schedule-toggle-btn');
  if (schedBtn) {{
    schedBtn.addEventListener('click', function() {{
      var action = schedBtn.getAttribute('data-action') || 'add';
      schedBtn.disabled = true;
      var origText = schedBtn.textContent;
      schedBtn.textContent = action === 'add' ? '加入中…' : '移除中…';
      var body = 'subdomain=' + encodeURIComponent(sampleSub) +
                 '&action=' + encodeURIComponent(action);
      fetch('/' + bizName + '/schedule/toggle', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
        body: body
      }}).then(function(r) {{
        return r.json().then(function(j) {{ return {{status: r.status, body: j}}; }});
      }}).then(function(res) {{
        if (res.status === 200 && res.body.ok) {{
          // 成功后提示并刷新页面,反映新 schedule 状态 + 按钮文案
          alert((action === 'add' ? '已加入每日扫描\\n' : '已移除每日扫描\\n') +
                '子域:' + sampleSub);
          location.reload();
        }} else {{
          alert('操作失败: ' + (res.body.error || JSON.stringify(res.body)));
          schedBtn.disabled = false;
          schedBtn.textContent = origText;
        }}
      }}).catch(function(e) {{
        alert('网络错误: ' + e);
        schedBtn.disabled = false;
        schedBtn.textContent = origText;
      }});
    }});
  }}

  // 列头排序:每张表独立维护 asc/desc/none 三态。点击触发 sortTable;
  // status/bytes 是数字列,直接数值比较;空值(None / —)排到末尾。
  function sortValue(tr, key) {{
    if (key === 'status_code') {{
      var s = tr.getAttribute('data-status');
      return s === '' || s == null ? null : Number(s);
    }}
    if (key === 'content_length') {{
      // bytes:从 tr 上没存 data-bytes,直接读第 4 个 td (按列序固定)的文本
      var tds = tr.querySelectorAll('td');
      // 列序:URL, path, status, bytes, host:port, ...
      var td = tds[3];
      var txt = td ? td.textContent.trim() : '';
      if (txt === '—' || txt === '') return null;
      var n = Number(txt.replace(/,/g, ''));
      return isNaN(n) ? null : n;
    }}
    return null;
  }}

  function sortTable(tbl, key, dir) {{
    // dir: 'asc' | 'desc' | null(回到原顺序)
    var tbody = tbl.tBodies[0];
    if (!tbody) return;
    var trs = Array.from(tbody.querySelectorAll('tr[data-host]'));
    if (dir === null) {{
      // 用 tr 在 DOM 里的初始顺序排序(原顺序) — 我们没有原始 index,
      // 用 dataset._idx 写在初次渲染时打的标
      trs.sort(function(a, b) {{
        return (Number(a.dataset._idx) || 0) - (Number(b.dataset._idx) || 0);
      }});
    }} else {{
      trs.sort(function(a, b) {{
        var va = sortValue(a, key);
        var vb = sortValue(b, key);
        // null 排到末尾(无论 asc/desc)
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        return dir === 'asc' ? va - vb : vb - va;
      }});
    }}
    // 重排 DOM
    var frag = document.createDocumentFragment();
    trs.forEach(function(tr) {{ frag.appendChild(tr); }});
    tbody.appendChild(frag);
    // 同步 th 视觉
    tbl.querySelectorAll('th.sortable').forEach(function(th) {{
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.getAttribute('data-col') === key && dir) {{
        th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
      }}
    }});
  }}

  document.querySelectorAll('.urls-table').forEach(function(tbl) {{
    // 给每行打 idx 标签(用於 sort=null 还原顺序)
    Array.from(tbl.tBodies[0].querySelectorAll('tr[data-host]')).forEach(function(tr, i) {{
      tr.dataset._idx = String(i);
    }});
    tbl.querySelectorAll('th.sortable').forEach(function(th) {{
      th.addEventListener('click', function() {{
        var key = th.getAttribute('data-col');
        var cur = th.classList.contains('sort-asc') ? 'asc'
                 : th.classList.contains('sort-desc') ? 'desc' : null;
        var next = (cur === 'asc') ? 'desc' : (cur === 'desc') ? null : 'asc';
        sortTable(tbl, key, next);
      }});
    }});
  }});

  applyFilters();
}})();
</script>
</body></html>
"""
        self._respond(200, "text/html; charset=utf-8",
                      page.encode("utf-8"), accept_enc)
        log.info(f"urls detail biz={biz_name} hash_id={hash_id} "
                 f"rows={len(rows)} per_source={per_source_count}")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info(f"dashboard {self.address_string()} {fmt % args}")

    def _serve_page(self, accept_enc: bytes = b"", *, home: bool = True) -> None:
        # / is always home=True (biz-link card); cached HTML is built once
        # in _maybe_reload() — no per-request aggregation or rendering.
        self._maybe_reload()
        if home:
            body, gz = _State.cached_html_home, _State.cached_gz_home
        else:
            body, gz = _State.cached_html_overview, _State.cached_gz_overview
        self._respond(200, "text/html; charset=utf-8", body, accept_enc,
                      cached_gz=gz)

    def _serve_snapshot(self, accept_enc: bytes = b"") -> None:
        # Serve the cached snapshot (same shape as the old JSON dump). The
        # cached bytes are produced at reload time so this endpoint is O(1).
        self._maybe_reload()
        if not _State.cached_snap_json:
            self._respond(503, "text/plain", b"snapshot not loaded", accept_enc)
            return
        self._respond(200, "application/json; charset=utf-8",
                      _State.cached_snap_json, accept_enc)

    def _serve_diff(self, biz_name: str | None,
                    accept_enc: bytes = b"") -> None:
        report_dir = _find_latest_report()
        if report_dir is None:
            self._respond(200, "text/html; charset=utf-8",
                          b"<html><body><h1>No reports yet</h1>"
                          b"<p>Run <code>daily_monitor.sh</code> first.</p></body></html>",
                          accept_enc)
            return
        snap = self._load_snap()
        biz_nav = self._build_biz_nav()
        changed_keys = _load_changed_keys(report_dir, snap)
        # Snapshot used as the "total" baseline for sites count column:
        # biz-scoped if asked, otherwise global — i.e. the same view /<biz>
        # or / would compute, before the diff filter narrows it down.
        baseline_snap = snap
        if biz_name:
            bid = next((b["id"] for b in snap["tables"].get("businesses", [])
                        if b.get("business_name") == biz_name), None)
            baseline_snap = _filter_snap_by_business(snap, bid)
            changed_keys = _changed_keys_for_business(changed_keys, bid, baseline_snap)
        filtered = _filter_snapshot(baseline_snap, changed_keys)
        agg = _aggregate(filtered)
        agg["hash_total"] = Counter(
            r.get("response_hash") or ""
            for r in baseline_snap.get("tables", {}).get("web_subdomains", [])
        )
        body = render_html(agg, mode="diff", run_id=report_dir.name,
                           biz_name=biz_name or "", biz_nav=biz_nav)
        self._respond(200, "text/html; charset=utf-8", body, accept_enc)

    def _build_biz_nav(self) -> list[dict]:
        """List of {name, href} for every business in the snapshot."""
        snap = self._load_snap()
        nav = []
        for b in snap.get("tables", {}).get("businesses", []):
            name = b.get("business_name") or ""
            if not name:
                continue
            nav.append({"name": name, "href": f"/{urllib.parse.quote(name)}"})
        nav.sort(key=lambda x: x["name"])
        return nav

    def _serve_business(self, biz_name: str, is_new: bool,
                         accept_enc: bytes = b"") -> None:
        snap = self._load_snap()
        bid = next((b["id"] for b in snap["tables"].get("businesses", [])
                    if b.get("business_name") == biz_name), None)
        if bid is None:
            self._respond(404, "text/plain",
                          f"unknown business: {biz_name}".encode("utf-8"),
                          accept_enc)
            return
        biz_nav = self._build_biz_nav()
        biz_snap = _filter_snap_by_business(snap, bid)
        # biz_snap is the baseline for /<biz>/new diff view; for non-diff
        # /<biz> page, it's also where K == T (full biz, no diff filter).
        hash_total = Counter(
            r.get("response_hash") or ""
            for r in biz_snap.get("tables", {}).get("web_subdomains", [])
        )

        if is_new:
            report_dir = _find_latest_report()
            run_id = report_dir.name if report_dir else ""
            filtered = biz_snap
            if report_dir is not None:
                changed_keys = _load_changed_keys(report_dir, snap)
                changed_keys = _changed_keys_for_business(changed_keys, bid,
                                                         snap)
                filtered = _filter_snapshot(biz_snap, changed_keys)
            agg = _aggregate(filtered)
            agg["hash_total"] = hash_total
            body = render_html(agg, mode="diff", run_id=run_id,
                               biz_name=biz_name, biz_nav=biz_nav)
        else:
            agg = _aggregate(biz_snap)
            agg["hash_total"] = hash_total   # K == T here; counts render normally
            body = render_html(agg, mode="overview",
                               biz_name=biz_name, biz_nav=biz_nav)
        self._respond(200, "text/html; charset=utf-8", body, accept_enc)

    def _maybe_reload(self) -> None:
        # Time-based reload trigger: every `reload_interval` seconds, or on
        # explicit /refresh request (which calls _do_reload directly).
        now = time.time()
        if (now - _State.last_reload_ts) < _State.reload_interval:
            return
        self._do_reload()

    def _do_reload(self) -> None:
        # Serialize reloads — concurrent requests must not double-load.
        if _State.reload_lock is None:
            return  # not initialized yet (very first call before main())
        with _State.reload_lock:
            try:
                # Build new snapshot in local vars first; only assign to
                # _State caches once they're fully built. Drop refs to
                # previous snap dict by reassigning, then gc.collect()
                # so RSS drops back to baseline before the next reload.
                snap = _load_snap_from_db(_State.db_path)
                agg = _aggregate(snap)
                html_home = render_html(agg, home=True, biz_name="")
                html_overview = render_html(agg, home=False, biz_name="")
                gz_home = gzip.compress(html_home, compresslevel=6) if len(html_home) > 1024 else b""
                gz_overview = gzip.compress(html_overview, compresslevel=6) if len(html_overview) > 1024 else b""
                snap_json = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                row_total = sum(agg['row_counts'].values())
                # Swap atomically. Old refs (held by previous reload) are
                # dropped here; gc.collect() ensures prompt free.
                _State.cached_html_home = html_home
                _State.cached_html_overview = html_overview
                _State.cached_gz_home = gz_home
                _State.cached_gz_overview = gz_overview
                _State.cached_snap = snap
                _State.cached_snap_json = snap_json
                _State.last_reload_ts = time.time()
                del snap, snap_json, html_home, html_overview, gz_home, gz_overview
                import gc
                gc.collect()
                log.info(f"dashboard reloaded: {_State.db_path} "
                         f"({row_total} rows; "
                         f"home={len(_State.cached_html_home):,}/{len(_State.cached_gz_home):,} gz, "
                         f"overview={len(_State.cached_html_overview):,}/{len(_State.cached_gz_overview):,} gz)")
            except Exception as e:  # noqa: BLE001
                log.error(f"dashboard reload failed: {e}")
                if not _State.cached_html_home:
                    err = (f"<html><body><h1>Snapshot error</h1><pre>{_e(e)}</pre></body></html>"
                           ).encode("utf-8")
                    _State.cached_html_home = err
                    _State.cached_html_overview = err

    def _load_snap(self) -> dict:
        """Cached parse of the active snapshot; reads + caches on first call,
        returns the cached copy on subsequent calls (mtime-bumped refresh
        goes through _maybe_reload)."""
        self._maybe_reload()
        return _State.cached_snap

    def _respond(self, status: int, ctype: str, body: bytes,
                 accept_enc: bytes = b"", *, cached_gz: bytes = b"") -> None:
        # gzip when client advertised it AND body is large enough that the
        # per-request CPU cost is amortized. Cached responses (the overview
        # dashboard) supply `cached_gz` so we don't pay gzip.compress() on
        # every hit — it's already pre-computed at reload time.
        # Status line MUST go first — sending headers before send_response
        # makes clients reject the response as malformed HTTP/0.9.
        self.send_response(status)
        if cached_gz and b"gzip" in accept_enc:
            body = cached_gz
            self.send_header("Content-Encoding", "gzip")
        elif b"gzip" in accept_enc and len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def _resolve_db(path_arg: str | None) -> Path:
    """Backward-compatible: old --snapshot arg now points to a SQLite DB.

    Falls back to the recon DB if no arg given. DB existence is enforced.
    """
    if path_arg:
        p = Path(path_arg)
    else:
        # Default: <repo>/db/recon.sqlite3 (REPO_ROOT = parents[2] of this file).
        p = Path(__file__).resolve().parents[2] / "db" / "recon.sqlite3"
    if not p.exists():
        log.error(f"db not found: {p}")
        raise SystemExit(2)
    return p.resolve()


# Queries that build the snapshot-shaped dict from the live DB.
# Schema mirrors snapshot.py / diff.py; kept inline here so the dashboard
# has zero dependency on snapshot.py (which is being deprecated).
_DB_SNAPSHOT_QUERIES: dict[str, str] = {
    # Mirror daily_monitor.sh's recon_business_config.enabled=1 gate across
    # every table that carries (or joins through to) business_id. The
    # INNER JOIN on recon_business_config means a row missing from config is
    # also dropped (opt-out, matches cron: missing config = all-zero = skip).
    # mapp_records has no business_id of its own — it joins via companies.
    "businesses":
        "SELECT b.id, b.business_name "
        "FROM businesses b "
        "INNER JOIN recon_business_config c ON c.business_id = b.id "
        "WHERE c.enabled = 1",
    "scopes": "SELECT s.id, s.business_id, s.scope_name, s.asset, s.is_wildcard, "
              "s.created_at, s.updated_at, s.fetched_at "
              "FROM scopes s "
              "INNER JOIN businesses b ON b.id = s.business_id "
              "INNER JOIN recon_business_config c ON c.business_id = b.id "
              "WHERE c.enabled = 1",
    "companies": "SELECT co.id, co.business_id, co.unit_name, co.nature_name, "
                 "co.main_licence, co.created_at, co.updated_at "
                 "FROM companies co "
                 "INNER JOIN businesses b ON b.id = co.business_id "
                 "INNER JOIN recon_business_config c ON c.business_id = b.id "
                 "WHERE c.enabled = 1",
    "mapp_records": "SELECT m.id, m.company_id, m.source_data_id, m.service_name, "
                    "m.service_licence, m.service_type, m.content_type_name, "
                    "m.domain, m.record_updated_at, m.fetched_at "
                    "FROM mapp_records m "
                    "INNER JOIN companies co ON co.id = m.company_id "
                    "INNER JOIN businesses b ON b.id = co.business_id "
                    "INNER JOIN recon_business_config c ON c.business_id = b.id "
                    "WHERE c.enabled = 1",
    "web_hashes": "SELECT wh.id, wh.business_id, wh.response_hash, wh.subdomain_count, "
                  "wh.score, wh.description, wh.score_initialized_at, "
                  "wh.url_count, "
                  "wh.created_at, wh.updated_at, wh.fetched_at "
                  "FROM web_hashes wh "
                  "INNER JOIN businesses b ON b.id = wh.business_id "
                  "INNER JOIN recon_business_config c ON c.business_id = b.id "
                  "WHERE c.enabled = 1",
    "web_subdomains":
        "SELECT ws.id, ws.hash_id, ws.subdomain, ws.port, ws.url, "
        "ws.status_code, ws.content_length, ws.title, ws.technologies, "
        "ws.first_seen, ws.last_seen, ws.fetched_at, ws.is_active, "
        "wh.business_id AS business_id, "
        "wh.response_hash AS response_hash, "
        "wh.score AS hash_score, "
        "wh.description AS hash_description "
        "FROM web_subdomains ws "
        "JOIN web_hashes wh ON wh.id = ws.hash_id "
        "INNER JOIN businesses b ON b.id = wh.business_id "
        "INNER JOIN recon_business_config c ON c.business_id = b.id "
        "WHERE c.enabled = 1",
    "tcp_assets": "SELECT t.id, t.business_id, t.ip, t.port, t.hosts, "
                  "t.first_seen, t.last_seen, t.fetched_at, t.is_active, t.raw_value "
                  "FROM tcp_assets t "
                  "INNER JOIN businesses b ON b.id = t.business_id "
                  "INNER JOIN recon_business_config c ON c.business_id = b.id "
                  "WHERE c.enabled = 1",
}


def _load_snap_from_db(db_path: Path) -> dict:
    """Load the dashboard's snapshot-shaped dict directly from SQLite.

    Shape is identical to the old JSON dump so all downstream consumers
    (_aggregate, _filter_snapshot, render_html, etc.) work unchanged.
    WAL mode allows concurrent reads; this function holds the connection
    only for the duration of the load (~5-10s for 374k web_subdomains rows).
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables: dict[str, list[dict]] = {}
        row_counts: dict[str, int] = {}
        for name, sql in _DB_SNAPSHOT_QUERIES.items():
            rows = conn.execute(sql).fetchall()
            tables[name] = [dict(r) for r in rows]
            row_counts[name] = len(rows)
        # host_ip_map — same derivation as snapshot.py.
        host_ip_map: dict[str, str] = {}
        try:
            for r in conn.execute(
                "SELECT subdomain, json_extract(raw_json, '$.host_ip') AS ip "
                "FROM web_subdomains "
                "WHERE raw_json IS NOT NULL "
                "  AND json_extract(raw_json, '$.host_ip') IS NOT NULL"
            ):
                host_ip_map.setdefault(r[0], r[1])
        except sqlite3.OperationalError:
            pass
        return {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "db_path": str(db_path.resolve()),
            "tables": tables,
            "row_counts": row_counts,
            "host_ip_map": host_ip_map,
        }
    finally:
        conn.close()


def main() -> int:
    import threading
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", dest="db",
                   help="path to recon.sqlite3 (default: <repo>/db/recon.sqlite3)")
    p.add_argument("--snapshot", dest="db",
                   help=argparse.SUPPRESS)  # deprecated alias for --db
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address. KEEP 127.0.0.1; expose via SSH port-forward.")
    args = p.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warn(f"BINDING TO {args.host} — recon data is now reachable beyond localhost!")

    _State.db_path = _resolve_db(args.db)
    _State.reload_lock = threading.Lock()
    # Eager initial load so the first request doesn't pay 5-10s of cold-start.
    try:
        snap = _load_snap_from_db(_State.db_path)
        agg = _aggregate(snap)
        html_home = render_html(agg, home=True, biz_name="")
        html_overview = render_html(agg, home=False, biz_name="")
        _State.cached_html_home = html_home
        _State.cached_html_overview = html_overview
        if len(html_home) > 1024:
            _State.cached_gz_home = gzip.compress(html_home, compresslevel=6)
        if len(html_overview) > 1024:
            _State.cached_gz_overview = gzip.compress(html_overview, compresslevel=6)
        _State.cached_snap = snap
        _State.cached_snap_json = json.dumps(
            snap, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        _State.last_reload_ts = time.time()
        log.info(f"initial load: {_State.db_path} "
                 f"({sum(agg['row_counts'].values())} rows; "
                 f"home={len(html_home):,}/{len(_State.cached_gz_home):,} gz)")
    except Exception as e:  # noqa: BLE001
        log.error(f"initial load failed: {e}")

    # allow_reuse_address lets a freshly-restarted process bind to the same
    # port while the previous socket is in TIME_WAIT (typical after OOM
    # kill + systemd Restart=). Without this, every restart race-loses and
    # the service stays dead.
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info(f"dashboard listening on http://{args.host}:{args.port}")
    log.info(f"reading db: {_State.db_path}")
    log.info(f"reload interval: {_State.reload_interval}s (env DASHBOARD_RELOAD)")
    log.info(f"ssh -L {args.port}:127.0.0.1:{args.port} user@recon-host")
    log.info("Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("dashboard stopped")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())