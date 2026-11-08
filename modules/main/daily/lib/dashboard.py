#!/usr/bin/env python3
"""Recon-DB dashboard — the ARL 4-tab style, served read-only on localhost.

Reads recon.sqlite3 directly through SQLite (there is no JSON snapshot layer). The cached
HTML is rebuilt every `reload_interval` seconds (30 by default, override with the
DASHBOARD_RELOAD env), or on demand through GET /refresh.

Usage:
  dashboard.py [--db PATH] [--port PORT] [--host HOST]

The defaults:
  --db    <repo>/db/recon.sqlite3
  --port  8765
  --host  127.0.0.1   <-- intentionally; see the SECURITY section below

SECURITY
========
The server binds to 127.0.0.1 ONLY. To reach it from your laptop, forward
the port over SSH:

    ssh -L 8765:127.0.0.1:8765 user@recon-host
    open http://localhost:8765

No auth is enforced - the assumption is that the SSH tunnel authenticates the
caller. If you change --host to 0.0.0.0 you expose the recon data to
everyone on the network; do not.

Endpoints
=========
  GET /                the HTML page (rebuilt on the reload interval / /refresh)
  GET /api/snapshot    the raw JSON dump of the current DB state (the same shape
                        the old snapshot.py produced, for the downstream scripts)
  GET /refresh         forces an immediate reload from the DB; returns "reloaded"
  GET /<business name>        the per-business view (e.g. /ExampleCo, /DemoCorp)
  GET /<business name>/new    the per-business "new since the last diff" view
  GET /<business name>/body/raw?url=<domain-url>&side=pre|cur
                        the raw HTTP body of one subdomain, as captured by the
                        daily snapshot (side=pre) or by the latest import
                        (side=cur); the body bytes are returned verbatim and
                        rendered by the browser under a locked-down CSP
  GET /diff            the global "new since the last diff" view
  GET /health          "ok"

The page is read-only - only GET is implemented.
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
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
sys.path.insert(0, str(Path(__file__).resolve().parent))
import log

# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import KNOWN_LANGS, get_lang, set_lang
from i18n import t as _t
_DB_PORTS = {3306: 'MySQL', 5432: 'PostgreSQL', 27017: 'MongoDB', 6379: 'Redis', 1433: 'MSSQL', 9200: 'Elasticsearch', 11211: 'Memcached', 8529: 'RethinkDB', 5984: 'CouchDB', 28017: 'MongoDB-HTTP', 1521: 'OracleDB', 50000: 'SAP'}
_REMOTE_PORTS = {22: 'SSH', 23: 'Telnet', 3389: 'RDP', 5900: 'VNC', 5901: 'VNC-1'}
_MAIL_PORTS = {25: 'SMTP', 110: 'POP3', 143: 'IMAP', 465: 'SMTPS', 587: 'SMTP-submission', 993: 'IMAPS', 995: 'POP3S'}
_WEB_ALWAYS_LOW = {80, 443, 8080, 8443, 8000, 8888, 9090}
_DB_HIGH = {1433, 3306, 5432, 6379, 9200, 11211, 27017, 28017, 5984, 8529, 1521, 50000}
_REMOTE_HIGH = {23, 3389, 5900, 5901}

def classify_port(port: int) -> dict:
    """Return {category, service, risk} for a TCP port."""
    if port in _WEB_ALWAYS_LOW or 8000 <= port <= 8999:
        return {'category': 'web', 'service': 'http', 'risk': 'low'}
    if port in _DB_PORTS:
        risk = 'high' if port in _DB_HIGH else 'med'
        return {'category': 'db', 'service': _DB_PORTS[port], 'risk': risk}
    if port in _REMOTE_PORTS:
        risk = 'high' if port in _REMOTE_HIGH else 'med'
        return {'category': 'remote', 'service': _REMOTE_PORTS[port], 'risk': risk}
    if port in _MAIL_PORTS:
        return {'category': 'mail', 'service': _MAIL_PORTS[port], 'risk': 'med'}
    if port in (161, 162):
        return {'category': 'snmp', 'service': 'SNMP', 'risk': 'med'}
    if port == 69:
        return {'category': 'tftp', 'service': 'TFTP', 'risk': 'med'}
    if port > 10000:
        return {'category': 'high-port', 'service': 'unknown', 'risk': 'med'}
    return {'category': 'other', 'service': 'unknown', 'risk': 'info'}

def _biz_label(businesses: list[dict]) -> dict[int, str]:
    return {b['id']: b.get('business_name') or f"#{b['id']}" for b in businesses}

def _host_of(r: dict) -> str:
    """Derive a single 'host' key from a tcp_assets row.

    New schema has `hosts` (comma-separated multi-domain string) and no
    `host` column; we take the first entry. Older snapshots may still carry
    a bare `host` field — prefer it when present.
    """
    if r.get('host'):
        return r['host']
    hn = (r.get('hosts') or '').split(',')
    return hn[0].strip() if hn and hn[0].strip() else '(unknown)'

def _aggregate(snap: dict) -> dict:
    tables = snap.get('tables', {})
    biz = _biz_label(tables.get('businesses', []))
    tcp = tables.get('tcp_assets', [])
    ws = tables.get('web_subdomains', [])
    wh = tables.get('web_hashes', [])
    scopes = tables.get('scopes', [])
    by_host: dict[str, list[dict]] = defaultdict(list)
    risk_by_host: dict[str, Counter] = defaultdict(Counter)
    port_class_counter: Counter = Counter()
    risk_counter: Counter = Counter()
    risky_open: list[dict] = []
    for r in tcp:
        h = _host_of(r)
        cls = classify_port(r['port'])
        by_host[h].append({**r, **cls})
        port_class_counter[cls['category']] += 1
        risk_counter[cls['risk']] += 1
        if cls['risk'] == 'high':
            risky_open.append({'host': h, 'port': r['port'], 'service': cls['service'], 'category': cls['category'], 'business': biz.get(r['business_id'], '')})
    fp_count = Counter()
    fp_first_sub: dict[str, str] = {}
    fp_first_title: dict[str, str] = {}
    fp_first_status: dict[str, int] = {}
    fp_first_tech: dict[str, str] = {}
    for r in ws:
        h = r.get('response_hash') or ''
        fp_count[h] += 1
        if h not in fp_first_sub:
            fp_first_sub[h] = r['subdomain']
            fp_first_title[h] = r.get('title') or ''
            fp_first_status[h] = r.get('status_code') or 0
            fp_first_tech[h] = r.get('technologies') or ''
    fp_rows = sorted([{'hash': h, 'count': c, 'sample': fp_first_sub.get(h, ''), 'title': fp_first_title.get(h, ''), 'status': fp_first_status.get(h, 0), 'tech': fp_first_tech.get(h, '')} for (h, c) in fp_count.items()], key=lambda x: -x['count'])
    fat_hosts = sorted([{'host': h, 'port_count': len(rows), 'sample_ports': ', '.join((str(r['port']) for r in rows[:8]))} for (h, rows) in by_host.items() if len(rows) >= 50], key=lambda x: -x['port_count'])
    web_status_codes: Counter = Counter()
    web_techs: Counter = Counter()
    web_root_domains: Counter = Counter()
    for r in ws:
        web_status_codes[r.get('status_code') or 0] += 1
        techs_raw = r.get('technologies') or ''
        if techs_raw:
            try:
                techs = ast.literal_eval(techs_raw)
            except (ValueError, SyntaxError):
                techs = []
            for t in techs:
                web_techs[t] += 1
        parts = r['subdomain'].split('.')
        if len(parts) >= 2:
            web_root_domains['.'.join(parts[-2:])] += 1
    fp_buckets = {'unique (1 子域)': 0, '2-10 子域': 0, '11-50 子域': 0, '≥51 子域 (批量/默认页)': 0}
    for c in fp_count.values():
        if c == 1:
            fp_buckets['unique (1 子域)'] += 1
        elif c <= 10:
            fp_buckets['2-10 子域'] += 1
        elif c <= 50:
            fp_buckets['11-50 子域'] += 1
        else:
            fp_buckets['≥51 子域 (批量/默认页)'] += 1
    companies = tables.get('companies', [])
    mapp = tables.get('mapp_records', [])
    comp_name = {c['id']: c.get('unit_name') or '' for c in companies}
    mp_records: list[dict] = []
    oa_records: list[dict] = []
    comp_counts = {c['id']: {'unit_name': c.get('unit_name') or '', 'business': biz.get(c.get('business_id'), ''), 'mp': 0, 'oa': 0} for c in companies}
    for r in mapp:
        rec = {**r, 'unit_name': comp_name.get(r.get('company_id'), '')}
        is_mp = r.get('service_type') == 7
        (mp_records if is_mp else oa_records).append(rec)
        cc = comp_counts.get(r.get('company_id'))
        if cc is not None:
            cc['mp' if is_mp else 'oa'] += 1
    return {'captured_at': snap.get('captured_at', ''), 'row_counts': snap.get('row_counts', {}), 'tables': {'scopes': scopes, 'tcp_assets': tcp, 'web_subdomains': ws, 'web_hashes': wh}, 'biz': biz, 'by_host': {h: rows for (h, rows) in by_host.items()}, 'risk_by_host': dict(risk_by_host), 'port_class': dict(port_class_counter), 'risk_counter': dict(risk_counter), 'risky_open': risky_open, 'fp_rows': fp_rows, 'fat_hosts': fat_hosts, 'web_status_codes': dict(web_status_codes), 'web_techs': dict(web_techs), 'web_root_domains': dict(web_root_domains), 'web_fp_buckets': fp_buckets, 'companies': companies, 'mp_records': mp_records, 'oa_records': oa_records, 'comp_counts': list(comp_counts.values()), 'biz_stats': _per_biz_stats(tables, biz)}

def _per_biz_stats(tables: dict, biz: dict[int, str]) -> list[dict]:
    """The per-business asset counts — used by the business card link list on the
    home page.
    The empty rows (total == 0) are included so that the callers can decide to
    hide them.
    The mapp_records are counted through the companies.company_id -> business_id
    join."""
    _BID_TABS = ('scopes', 'companies', 'web_subdomains', 'web_hashes', 'tcp_assets')
    bid_counter: dict[str, Counter] = {t: Counter((r.get('business_id') for r in tables.get(t, []) if r.get('business_id') is not None)) for t in _BID_TABS}
    comp_to_bid = {c['id']: c.get('business_id') for c in tables.get('companies', []) if c.get('business_id') is not None}
    mapp_by_bid = Counter((comp_to_bid.get(r.get('company_id')) for r in tables.get('mapp_records', []) if comp_to_bid.get(r.get('company_id')) is not None))
    out = []
    for b in tables.get('businesses', []):
        bid = b['id']
        name = b.get('business_name') or f'#{bid}'
        if not b.get('business_name'):
            continue
        counts = {t: bid_counter[t].get(bid, 0) for t in _BID_TABS}
        counts['mapp_records'] = mapp_by_bid.get(bid, 0)
        out.append({'id': bid, 'name': name, 'href': '/' + urllib.parse.quote(name), **counts, 'total': sum(counts.values())})
    out.sort(key=lambda x: x['name'])
    return out
_CSS = '\n* { box-sizing: border-box; }\nbody { margin: 0; font: 14px/1.5 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;\n       background: #f4f5f7; color: #1f2328; }\nheader { background: #1f2328; color: #fff; padding: 14px 24px;\n         display: flex; justify-content: space-between; align-items: center; }\nheader h1 { margin: 0; font-size: 18px; font-weight: 600; }\nheader .meta { font-size: 12px; opacity: 0.7; }\nnav.tabs { background: #fff; border-bottom: 1px solid #d0d7de; padding: 0 24px;\n           display: flex; gap: 0; position: sticky; top: 0; z-index: 10; }\nnav.tabs button { background: none; border: none; padding: 14px 18px;\n                  font-size: 14px; color: #57606a; cursor: pointer;\n                  border-bottom: 2px solid transparent; }\nnav.tabs button:hover { color: #1f2328; }\nnav.tabs button.active { color: #0969da; border-bottom-color: #0969da; }\nmain { padding: 24px; max-width: 1400px; margin: 0 auto; }\n.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));\n         gap: 16px; margin-bottom: 24px; }\n.card { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;\n        padding: 16px; }\n.card .label { font-size: 12px; color: #57606a; text-transform: uppercase;\n               letter-spacing: 0.5px; }\n.card .value { font-size: 28px; font-weight: 600; margin-top: 4px; }\n.tab-pane { display: none; }\n.tab-pane.active { display: block; }\ntable { width: 100%; border-collapse: collapse; background: #fff;\n        border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden;\n        font-size: 13px; }\nth, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eaecef; }\nth { background: #f6f8fa; font-weight: 600; color: #57606a; }\nth.sortable { cursor: pointer; user-select: none; }\nth.sortable:hover { background: #eaeef2; color: #1f2328; }\nth.sortable.asc::after  { content: " ▲"; color: #57606a; font-size: 11px; }\nth.sortable.desc::after { content: " ▼"; color: #57606a; font-size: 11px; }\ntr:last-child td { border-bottom: none; }\ntr:hover td { background: #f6f8fa; }\n.risk-high { background: #ffebe9; }\n.risk-high td:first-child { border-left: 3px solid #cf222e; }\n.risk-med  { background: #fff8c5; }\n.risk-med  td:first-child { border-left: 3px solid #9a6700; }\n.risk-low  td:first-child { border-left: 3px solid #1a7f37; }\n.risk-info td:first-child { border-left: 3px solid #6e7781; }\n.bar { display: inline-block; height: 14px; background: #0969da;\n       border-radius: 2px; vertical-align: middle; }\n.bar-wrap { background: #eaecef; border-radius: 2px;\n            display: inline-block; width: 220px; height: 14px; }\n.tag { display: inline-block; padding: 2px 8px; border-radius: 12px;\n       font-size: 11px; font-weight: 500; }\n.tag-high  { background: #ffebe9; color: #cf222e; }\n.tag-med   { background: #fff8c5; color: #9a6700; }\n.tag-low   { background: #dafbe1; color: #1a7f37; }\n.tag-info  { background: #eaeef2; color: #57606a; }\n.tag-new     { background: #0969da; color: #fff; }   /* /new: the new ones (added or reactivated) */\n.tag-changed { background: #8250df; color: #fff; }   /* /new: the changed ones (any field diff) */\n.note { background: #fff8c5; border-left: 3px solid #9a6700;\n        padding: 10px 14px; border-radius: 4px; margin-bottom: 16px;\n        font-size: 13px; }\n.note-allnew { background: #dafbe1; border-left: 3px solid #1a7f37; }\n.section-title { font-size: 16px; font-weight: 600; margin: 24px 0 12px; }\n.sub-title   { font-size: 13px; font-weight: 600; color: #57606a;\n               margin: 16px 0 8px; text-transform: uppercase; letter-spacing: 0.5px; }\n.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }\n@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }\n.muted { color: #57606a; font-size: 12px; }\ncode { background: #eaeef2; padding: 1px 6px; border-radius: 3px;\n       font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }\n.filter { margin-bottom: 12px; }\n.filter input { padding: 6px 10px; border: 1px solid #d0d7de;\n                border-radius: 4px; width: 280px; font-size: 13px; }\n.fp-warn { background: #fff8c5; padding: 12px; border-radius: 6px;\n           border-left: 3px solid #9a6700; margin-bottom: 12px; }\n.fp-warn b { color: #9a6700; }\n.scroll-x { overflow-x: auto; }\n.mono { font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; }\n.toggle { padding: 5px 12px; border: 1px solid #d0d7de;\n          background: #fff; border-radius: 4px; cursor: pointer;\n          font-size: 13px; color: #1f2328; }\n.toggle:hover { background: #f6f8fa; }\n.toggle.on { background: #0969da; color: #fff; border-color: #0969da; }\n.hash-link { color: #0969da; text-decoration: none; cursor: pointer; }\n.hash-link:hover { text-decoration: underline; }\n.hash-count { color: #57606a; font-size: 11px; margin-left: 2px; }\n.num { text-align: right; font-variant-numeric: tabular-nums; }\n.modal { position: fixed; inset: 0; background: rgba(31,35,40,0.5);\n         z-index: 100; display: flex; align-items: center;\n         justify-content: center; }\n.modal-card { background: #fff; border-radius: 8px;\n              max-width: 800px; max-height: 80vh; width: 90%;\n              display: flex; flex-direction: column; overflow: hidden; }\n.modal-header { padding: 12px 16px; border-bottom: 1px solid #d0d7de;\n                display: flex; justify-content: space-between;\n                align-items: center; font-weight: 600; }\n#hash-modal-close { background: none; border: none; font-size: 24px;\n                    cursor: pointer; padding: 0 8px; line-height: 1;\n                    color: #57606a; }\n#hash-modal-close:hover { color: #1f2328; }\n#hash-modal-body { padding: 12px 16px; overflow: auto; font-size: 13px; }\n#hash-modal-body table { font-size: 13px; }\n#hash-modal-body a { color: #0969da; text-decoration: none; }\n#hash-modal-body a:hover { text-decoration: underline; }\n.biz-bar a.active { background:#0969da;color:#fff !important; }\n.biz-bar a:hover { background:#444c56; }\n.biz-card .value { font-size: 13px; font-weight: 400; margin-top: 6px; }\n.biz-list { display: flex; flex-direction: column; gap: 4px; margin-top: 4px; }\n.biz-link { display: flex; justify-content: space-between; align-items: center;\n            padding: 5px 8px; border-radius: 4px; color: #0969da;\n            text-decoration: none; font-size: 13px; }\n.biz-link:hover { background: #f6f8fa; text-decoration: none; }\n.biz-link .biz-name { font-weight: 500; }\n.biz-link .biz-count { font-size: 11px; font-variant-numeric: tabular-nums; }\n\n/* the scan-onesite form and the result page */\n.scan-form { background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px;\n             padding:10px 12px; margin:0 0 14px 0; font-size:13px; }\n.scan-form summary { cursor:pointer; font-weight:600; color:#1f2328;\n                     user-select:none; }\n.scan-form textarea { width:100%; min-height:80px; padding:6px 10px;\n                      border:1px solid #d0d7de; border-radius:4px;\n                      font-family: ui-monospace, "SF Mono", monospace;\n                      font-size:13px; box-sizing:border-box; }\n.scan-form .hint { color:#57606a; font-size:12px; margin:6px 0 6px 0; }\n.scan-form .submit-row { margin-top:8px; display:flex; align-items:center;\n                         gap:10px; }\n.scan-form .muted { color:#57606a; font-size:12px; }\n\n.btn { padding:6px 14px; border:1px solid #d0d7de; background:#fff;\n       border-radius:4px; cursor:pointer; font-size:13px; color:#1f2328;\n       text-decoration:none; display:inline-block; }\n.btn:hover { background:#f6f8fa; }\n.btn.primary { background:#0969da; color:#fff; border-color:#0969da; }\n.btn.primary:hover { background:#0550ae; border-color:#0550ae; }\n\n.scan-result { background:#fff; border:1px solid #d0d7de; border-radius:6px;\n               padding:18px 22px; margin:14px 0; max-width:720px; }\n.scan-result.ok { border-left:4px solid #1a7f37; }\n.scan-result.err { border-left:4px solid #cf222e; background:#fff5f5; }\n.scan-result pre { background:#fff; border:1px solid #d0d7de; padding:8px;\n                   border-radius:4px; max-height:320px; overflow:auto;\n                   font-size:12px; white-space:pre-wrap; word-break:break-all; }\n.scan-result h2 { margin:0 0 10px 0; font-size:18px; }\n\n.score-cell { white-space:nowrap; }\n.score-cell .score-input { width:4em; padding:2px 4px; border:1px solid #d0d7de;\n                          border-radius:3px; font-size:12px; text-align:center;\n                          font-variant-numeric: tabular-nums; }\n.score-cell .score-input:focus { outline:none; border-color:#0969da; }\n.score-cell .score-save { padding:0 6px; cursor:pointer; border:1px solid #d0d7de;\n                          background:#f6f8fa; border-radius:3px; font-size:11px;\n                          margin-left:2px; }\n.score-cell .score-save:hover { background:#eaeef2; }\n.desc-cell { min-width:240px; }\n.desc-cell .desc-input { width:80%; padding:2px 4px; border:1px solid #d0d7de;\n                          border-radius:3px; font-size:12px; }\n.desc-cell .desc-status { display:inline-block; margin-left:6px; font-size:11px;\n                           color:#57606a; min-width:6em; }\n'
SCAN_HOSTNAME_RE = re.compile('^([a-zA-Z0-9][-a-zA-Z0-9]*\\.)+[a-zA-Z]{2,}$')
SCAN_MAX_HOSTS = 200
SCAN_MAX_BODY = 64 * 1024
SCAN_TIMEOUT = 180
SCAN_RES_OK_RE = re.compile('扫描完成：(\\d+) 条')
SCAN_URLS_MAX_HOSTS = 50
SCAN_URLS_MAX_BODY = 64 * 1024
SCAN_URLS_TIMEOUT = 600
SCAN_URLS_RES_OK_RE = re.compile('"total_new_rows":\\s*(\\d+)')
SCAN_URLS_VALID_SOURCES = ('ffuf', 'urlfinder', 'gau')
KATANA_ENRICH_MAX_HOSTS = 30
KATANA_ENRICH_TIMEOUT = 600
SCAN_URLS_DEFAULT_WORDLIST = os.environ.get('SCAN_URLS_WORDLIST', '')

def _scan_urls_default_wordlist_suggestion() -> str:
    """The form suggestion value — the same probing as
    scan_urls.py._resolve_default_wordlist_path.

    When env is set explicitly it is used directly; otherwise the two candidate
    paths are probed and the first hit is used;
    when neither can be found it returns an empty string (the form renders an
    empty input and scan_urls.py reports the error after the submit).
    """
    env = os.environ.get('SCAN_URLS_WORDLIST')
    if env:
        return env
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates = [project_root / 'tools' / 'wordlists' / 'SecLists-master' / 'Discovery' / 'Web-Content' / 'common.txt', Path.home() / 'tools' / 'wordlists' / 'SecLists-master' / 'Discovery' / 'Web-Content' / 'common.txt']
    for c in candidates:
        if c.is_file():
            return str(c)
    return ''

def _e(s: Any) -> str:
    return html.escape(str(s)) if s is not None else ''

def _risk_tag(risk: str) -> str:
    cls = {'high': 'tag-high', 'med': 'tag-med', 'low': 'tag-low'}.get(risk, 'tag-info')
    label = {
        'high': _t("dashboard.risk.high"),
        'med': _t("dashboard.risk.med"),
        'low': _t("dashboard.risk.low"),
        'info': _t("dashboard.risk.info"),
    }.get(risk, risk)
    return f'<span class="tag {cls}">{label}</span>'

def _build_scan_form(biz_name: str) -> str:
    """The collapsible form that starts a single point scan of the current business. When biz_name is empty the home page / the global view does not render it."""
    if not biz_name:
        return ''
    action = '/' + urllib.parse.quote(biz_name) + '/scan'
    return f'\n<details class="scan-form">\n  <summary>{_t("dashboard.scan_form.summary", {"biz": _e(biz_name)})}</summary>\n  <form method="post" action="{_e(action)}">\n    <div class="hint">{_t("dashboard.scan_form.hint_input", {"max": SCAN_MAX_HOSTS})}\n    {_t("dashboard.scan_form.hint_effect")}</div>\n    <textarea name="hosts" rows="4" required\nplaceholder="example.com&#10;api.example.com"></textarea>\n    <div class="submit-row">\n      <button type="submit" class="btn primary">{_t("dashboard.scan_form.submit")}</button>\n      <span class="muted">{_t("dashboard.scan_form.wait_hint")}</span>\n    </div>\n  </form>\n</details>\n'

def _build_scan_urls_form(biz_name: str) -> str:
    """The URL asset scan collapsible form (ffuf / URLFinder / gau). It posts to
    /<biz>/scan-urls.

    The design goals:
      - only the active subdomains that already exist are scanned (the POST handler
        validates that), so it never spreads to unknown domains
      - urlfinder is checked by default (the active crawler, no dictionary
        dependency, the most reliable one)
      - ffuf / gau are left for the user to tick when they want them
      - the wordlist uses SCAN_URLS_DEFAULT_WORDLIST for now; an advanced user
        can change the path in the form
    """
    if not biz_name:
        return ''
    action = '/' + urllib.parse.quote(biz_name) + '/scan-urls'
    return f'\n<details class="scan-form">\n  <summary>{_t("dashboard.scan_urls_form.summary", {"biz": _e(biz_name)})}</summary>\n  <form method="post" action="{_e(action)}">\n    <div class="hint">{_t("dashboard.common.each_line")} <b>{_t("dashboard.common.existing_subdomain")}</b>{_t("dashboard.scan_urls_form.hint_copy", {"max": SCAN_URLS_MAX_HOSTS})}\n    {_t("dashboard.common.scan_active_prefix")} <code>web_subdomains.is_active=1</code> {_t("dashboard.common.scan_active_suffix")}\n    {_t("dashboard.common.no_duplicate_insert")}\n    <br>{_t("dashboard.scan_urls_form.hint_timeout", {"timeout": SCAN_URLS_TIMEOUT})}\n    <br>{_t("dashboard.scan_urls_form.hint_manual")}</div>\n    <textarea name="hosts" rows="4" required\nplaceholder="about.example.com&#10;www.example.com"></textarea>\n    <div class="submit-row" style="flex-wrap:wrap;gap:10px;">\n      <label><input type="checkbox" name="sources" value="ffuf">{_t("dashboard.scan_urls_form.source_ffuf")}</label>\n      <label><input type="checkbox" name="sources" value="urlfinder" checked>{_t("dashboard.scan_urls_form.source_urlfinder")}</label>\n      <label><input type="checkbox" name="sources" value="gau">{_t("dashboard.scan_urls_form.source_gau")}</label>\n    </div>\n    <div class="hint" style="margin-top:6px;">{_t("dashboard.scan_urls_form.wordlist_label")}\n      <input type="text" name="wordlist" size="60" value="{_e(_scan_urls_default_wordlist_suggestion())}"\n             style="font-family:monospace;font-size:12px;padding:2px 6px;">\n    </div>\n    <div class="submit-row">\n      <button type="submit" class="btn primary">{_t("dashboard.scan_urls_form.submit")}</button>\n      <span class="muted">{_t("dashboard.scan_urls_form.wait_hint")}</span>\n    </div>\n  </form>\n</details>\n'

def _build_overview(agg: dict, biz_list: bool=False, biz_name: str='') -> str:
    rc = agg['row_counts']
    cards = [(_t('dashboard.overview.card_business'), rc.get('businesses', 0)), ('Scope', rc.get('scopes', 0)), (_t('dashboard.overview.card_companies'), rc.get('companies', 0)), (_t('dashboard.overview.card_mp'), rc.get('mapp_records', 0)), (_t('dashboard.overview.card_oa'), 0), (_t('dashboard.overview.card_web_sub'), rc.get('web_subdomains', 0)), (_t('dashboard.overview.card_tcp_ports'), rc.get('tcp_assets', 0))]
    biz_card_parts = []
    for (i, (n, v)) in enumerate(cards):
        if i == 0 and biz_list:
            non_empty = [b for b in agg.get('biz_stats', []) if b['total'] > 0]
            if non_empty:
                links = ''.join((f'''<a class="biz-link" href="{_e(b['href'])}" title="{_t('dashboard.overview.biz_assets', {'count': format(b['total'], ',')})}"><span class="biz-name">{_e(b['name'])}</span><span class="biz-count muted">{b['total']:,}</span></a>''' for b in non_empty))
                value_html = f'<div class="biz-list">{links}</div>'
            else:
                value_html = '<div class="value">—</div>'
            biz_card_parts.append(f'<div class="card biz-card"><div class="label">{_e(n)}</div>{value_html}</div>')
            continue
        biz_card_parts.append(f'<div class="card"><div class="label">{_e(n)}</div><div class="value">{v:,}</div></div>')
    cards_html = ''.join(biz_card_parts)
    cc_rows_list = sorted([c for c in agg['comp_counts'] if c['mp'] + c['oa'] > 0 or not agg['comp_counts']], key=lambda x: -(x['mp'] + x['oa']))
    cc_total = max(sum((c['mp'] + c['oa'] for c in cc_rows_list)), 1)
    cc_rows = ''.join((f"""<tr><td>{_e(c['unit_name'])}</td><td class="muted">{_e(c['business'])}</td><td class="num">{c['mp']}</td><td class="num">{c['oa']}</td><td class="num">{c['mp'] + c['oa']}</td><td><span class="bar-wrap"><span class="bar" style="width:{(c['mp'] + c['oa']) / cc_total * 100:.1f}%;background:#0969da"></span></span></td><td class="muted">{(c['mp'] + c['oa']) / cc_total * 100:.1f}%</td></tr>""" for c in cc_rows_list))
    sc = Counter(agg['web_status_codes'])
    sc_total = sum(sc.values()) or 1
    sc_rows = ''.join((f"""<tr><td>{(k if k else '—')}</td><td>{v}</td><td><span class="bar-wrap"><span class="bar" style="width:{v / sc_total * 100:.1f}%;background:#1a7f37"></span></span></td><td class="muted">{v / sc_total * 100:.1f}%</td></tr>""" for (k, v) in sc.most_common(10)))
    techs = Counter(agg['web_techs'])
    tech_total = sum(techs.values()) or 1
    tech_rows = ''.join((f'<tr><td>{_e(k)}</td><td>{v}</td><td><span class="bar-wrap"><span class="bar" style="width:{v / tech_total * 100:.1f}%;background:#8250df"></span></span></td><td class="muted">{v / tech_total * 100:.1f}%</td></tr>' for (k, v) in techs.most_common(10)))
    roots = Counter(agg['web_root_domains'])
    roots_total = sum(roots.values()) or 1
    root_rows = ''.join((f'<tr><td class="mono">{_e(k)}</td><td>{v}</td><td><span class="bar-wrap"><span class="bar" style="width:{v / roots_total * 100:.1f}%;background:#0969da"></span></span></td><td class="muted">{v / roots_total * 100:.1f}%</td></tr>' for (k, v) in roots.most_common(10)))
    bk = agg['web_fp_buckets']
    bk_total = sum(bk.values()) or 1
    bucket_color = {'unique (1 子域)': '#1a7f37', '2-10 子域': '#0969da', '11-50 子域': '#9a6700', '≥51 子域 (批量/默认页)': '#cf222e'}
    # the bucket names are data keys (the aggregate counts them by key);
    # the label the reader sees comes from the message table.
    bucket_label_key = {'unique (1 子域)': 'dashboard.overview.bucket_unique', '2-10 子域': 'dashboard.overview.bucket_2_10', '11-50 子域': 'dashboard.overview.bucket_11_50', '≥51 子域 (批量/默认页)': 'dashboard.overview.bucket_51_plus'}
    bucket_rows = ''.join((f'<tr><td>{_e(_t(bucket_label_key.get(k, k)))}</td><td>{v}</td><td><span class="bar-wrap"><span class="bar" style="width:{v / bk_total * 100:.1f}%;background:{bucket_color[k]}"></span></span></td><td class="muted">{v / bk_total * 100:.1f}%</td></tr>' for (k, v) in bk.items()))
    pc = agg['port_class']
    pc_total = sum(pc.values()) or 1
    web_overlap = pc.get('web', 0)
    pc_rows = ''.join((f'<tr><td>{_e(k)}</td><td>{v}</td><td><span class="bar-wrap"><span class="bar" style="width:{v / pc_total * 100:.1f}%;background:#8250df"></span></span></td><td class="muted">{v / pc_total * 100:.1f}%</td></tr>' for (k, v) in sorted(pc.items(), key=lambda x: -x[1])))
    tcp_note = ''
    if web_overlap:
        tcp_note = ('<div class="note">' + _t('dashboard.overview.note_tcp_overlap', {'count': f'<b>{web_overlap}</b>', 'table': '<code>web_subdomains</code>'}) + '</div>')
    form_html = _build_scan_form(biz_name) if biz_name else ''
    form_urls_html = _build_scan_urls_form(biz_name) if biz_name else ''
    form_katana_html = _build_katana_enrich_form(biz_name) if biz_name else ''
    return f"""\n{form_html}\n{form_urls_html}\n{form_katana_html}\n<section class="cards">{cards_html}</section>\n\n<div class="section-title">{_t('dashboard.overview.section_icp')}</div>\n<div class="note">{_t('dashboard.overview.note_icp')}</div>\n<div class="scroll-x"><table>\n<tr><th>{_t('dashboard.overview.table_entity')}</th><th>{_t('dashboard.overview.table_biz')}</th><th class="num">{_t('dashboard.overview.table_mp')}</th>\n<th class="num">{_t('dashboard.overview.table_oa')}</th><th class="num">{_t('dashboard.overview.table_total')}</th><th></th><th></th></tr>\n{cc_rows or ('<tr><td colspan="7" class="muted">' + _t('dashboard.overview.no_entity_data') + '</td></tr>')}\n</table></div>\n\n<div class="two-col">\n  <div>\n    <div class="section-title">{_t('dashboard.overview.section_web')}</div>\n\n    <div class="sub-title">{_t('dashboard.overview.sub_status_codes')}</div>\n    <div class="scroll-x"><table>\n    <tr><th>{_t('dashboard.overview.table_status_code')}</th><th>{_t('dashboard.common.count')}</th><th>{_t('dashboard.common.share')}</th><th></th></tr>\n    {sc_rows or ('<tr><td colspan="4" class="muted">' + _t('dashboard.common.no_data') + '</td></tr>')}\n    </table></div>\n\n    <div class="sub-title">{_t('dashboard.overview.sub_tech_stack')}</div>\n    <div class="scroll-x"><table>\n    <tr><th>{_t('dashboard.overview.table_tech')}</th><th>{_t('dashboard.overview.table_hits')}</th><th>{_t('dashboard.common.share')}</th><th></th></tr>\n    {tech_rows or ('<tr><td colspan="4" class="muted">' + _t('dashboard.common.no_data') + '</td></tr>')}\n    </table></div>\n\n    <div class="sub-title">{_t('dashboard.overview.sub_root_domains')}</div>\n    <div class="scroll-x"><table>\n    <tr><th>{_t('dashboard.overview.table_root_domain')}</th><th>{_t('dashboard.overview.table_subdomain_count')}</th><th>{_t('dashboard.common.share')}</th><th></th></tr>\n    {root_rows or ('<tr><td colspan="4" class="muted">' + _t('dashboard.common.no_data') + '</td></tr>')}\n    </table></div>\n\n    <div class="sub-title">{_t('dashboard.overview.sub_fp_buckets')} <span class="muted">{_t('dashboard.overview.note_fp_buckets')}</span></div>\n    <div class="scroll-x"><table>\n    <tr><th>{_t('dashboard.overview.table_bucket')}</th><th>{_t('dashboard.overview.table_fingerprint_count')}</th><th>{_t('dashboard.common.share')}</th><th></th></tr>\n    {bucket_rows or ('<tr><td colspan="4" class="muted">' + _t('dashboard.common.no_data') + '</td></tr>')}\n    </table></div>\n  </div>\n\n  <div>\n    <div class="section-title">{_t('dashboard.overview.section_tcp')}</div>\n    {tcp_note}\n    <div class="scroll-x"><table>\n    <tr><th>{_t('dashboard.overview.table_category')}</th><th>{_t('dashboard.common.count')}</th><th>{_t('dashboard.common.share')}</th><th></th></tr>\n    {pc_rows or ('<tr><td colspan="4" class="muted">' + _t('dashboard.common.no_data') + '</td></tr>')}\n    </table></div>\n  </div>\n</div>\n"""

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
    hid = r.get('hash_id')
    score = r.get('hash_score')
    desc = r.get('hash_description') or ''
    sort_key = -1 if score is None else score
    score_txt = '—' if score is None else str(score)
    return f'<td class="score-cell" data-hash-id="{hid}" data-num="{sort_key}"><input type="number" class="score-input" data-hash-id="{hid}" data-cur-score="{score_txt}" value="{score_txt}" min="-1000" max="1000" step="1" title="{_t("dashboard.sites.score_input_title")}" /><button type="button" class="score-save" data-hash-id="{hid}" title="{_t("dashboard.sites.score_save_title")}">save</button><span class="desc-status" data-hash-id="{hid}"></span></td><td class="desc-cell" data-hash-id="{hid}"><input type="text" class="desc-input" data-hash-id="{hid}" value="{_e(desc)}" maxlength="500" placeholder="{_t("dashboard.sites.desc_placeholder")}" title="{_t("dashboard.sites.desc_save_title")}" /></td>'

def _build_sites(agg: dict, diff_mode: bool=False, biz_name: str='') -> str:
    """The site details — the wildcard noise without a TLD is hidden (every
    subdomain that does not contain a "." is dropped,
    which takes care of the single character wildcard hits such as "http://a/" /
    "http://j/"), deduplicated by response_hash
    (one row per fingerprint, with a new count column), and clicking a hash opens
    a modal that shows every subdomain under that hash.

    The row background is graded by status_code: 5xx=red / 4xx=yellow / 3xx=grey
    / 2xx=green / None=grey.
    The URL column is joined by port (80->http / 443->https / the others ->
    http://host:port).

    `diff_mode=True` (used by /diff and /<biz>/new): the deduplication is skipped,
    there is one row per affected subdomain. A leading category column ("new" /
    "hash changed")
    lets the user tell the new entries from the old ones (which the hash bucket
    alone hides — the same hash can appear on both / and /new).

    A "URL details" button is appended to the end of every row; clicking it goes
    to /<biz>/urls/<hash_id> (a new page
    that queries the web_hash_urls table live by hash_id - strictly lazy, nothing
    is preloaded).
    """
    if biz_name:
        biz_quoted = urllib.parse.quote(biz_name)
    else:
        biz_quoted = ''

    def _url_detail_cell(hash_id, subdomain):
        if not biz_quoted or hash_id is None:
            return ''
        href = f'/{biz_quoted}/urls/{hash_id}'
        detail_title = _t('dashboard.sites.url_detail_title', {'subdomain': _e(subdomain or '')})
        return f'''<td><a class="btn small" href="{_e(href)}" target="_blank" title="{detail_title}">open</a></td>'''
    ws_raw = agg['tables']['web_subdomains']
    ws = [r for r in ws_raw if '.' in (r.get('subdomain') or '')]
    hidden = len(ws_raw) - len(ws)

    def status_risk(code):
        if code is None:
            return 'info'
        if 500 <= code < 600:
            return 'high'
        if 400 <= code < 500:
            return 'med'
        if 300 <= code < 400:
            return 'info'
        return 'low'

    def url_for(r) -> str:
        sub = r['subdomain']
        port = r['port']
        if port == 443:
            return f'https://{sub}/'
        if port == 80:
            return f'http://{sub}/'
        return f'http://{sub}:{port}/'

    def has_zh(s: str) -> int:
        return 1 if any(('一' <= c <= '\u9fff' for c in s)) else 0

    def cat_badge(cat: str) -> tuple[str, str]:
        # a reactivated row (change_type & 4) counts as "new": the CSS comment of
        # .tag-new is
        # "/new: new (added or reactivated)". The reactivated case used to have no
        # mapping, so the badge
        # fell back to ?, and the user could not tell that it was a revival.
        return {
            'added':       ('tag-new', _t('dashboard.sites.tag_new')),
            'reactivated': ('tag-new', _t('dashboard.sites.tag_new')),
            'changed':     ('tag-changed', _t('dashboard.sites.tag_changed')),
        }.get(cat, ('tag-info', '?'))
    rows_html_parts: list[str] = []
    hash_subs_for_modal: dict[str, list[str]] = defaultdict(list)
    allnew_html: str = ''
    if diff_mode:
        hash_first: dict[str, dict] = {}
        hash_count: dict[str, int] = {}
        bucket_cat: dict[str, str] = {}
        hash_total = agg.get('hash_total') or Counter()
        for r in ws:
            h = r.get('response_hash') or ''
            cat = r.get('diff_category') or 'added'
            hash_count[h] = hash_count.get(h, 0) + 1
            hash_subs_for_modal[h].append(r['subdomain'])
            if h not in hash_first:
                hash_first[h] = r
                bucket_cat[h] = cat
            elif cat == 'added':
                bucket_cat[h] = 'added'
        for (h, r) in hash_first.items():
            cnt = hash_count[h]
            total = hash_total.get(h, cnt) or cnt
            (cls, label) = cat_badge(bucket_cat[h])
            count_txt = f'{cnt:,}' if cnt >= total else f'{cnt:,} / {total:,}'
            bucket_urls = ' '.join(hash_subs_for_modal[h])
            rows_html_parts.append(f'''<tr class="risk-{status_risk(r.get('status_code'))}" data-hash="{_e(h)}" data-port="{r['port']}" data-hash-count="{cnt}" data-bucket-urls="{_e(bucket_urls)}" data-zh="{has_zh(r.get('title') or '')}"><td><span class="tag {cls}">{label}</span></td><td class="mono"><a href="{_e(url_for(r))}" target="_blank" rel="noopener noreferrer">{_e(url_for(r))}</a></td><td>{r['port']}</td><td>{(r.get('status_code') if r.get('status_code') is not None else '—')}</td><td>{(r.get('content_length') if r.get('content_length') is not None else '—')}</td><td>{_e(r.get('title') or '')}</td><td class="muted">{_e(r.get('technologies') or '')}</td><td><a class="hash-link" data-hash="{_e(h)}" href="javascript:void(0)">{_e(h)}</a></td><td class="num">{count_txt}</td><td>{r.get('is_active', 0)}</td>{_score_cell(r)}{_url_detail_cell(r.get('hash_id'), r.get('subdomain'))}</tr>''')
        rows_html = ''.join(rows_html_parts)
        n_added = sum((1 for c in bucket_cat.values() if c == 'added'))
        n_changed = sum((1 for c in bucket_cat.values() if c == 'changed'))
        n_subs = sum(hash_count.values())
        meta_parts = [
            _t('dashboard.sites.meta_dedup', {'count': f'{len(hash_first):,}', 'total': f'{n_subs:,}'}),
            _t('dashboard.sites.meta_bucket_tags', {'added': f'{n_added:,}', 'changed': f'{n_changed:,}'}),
        ]
        meta_parts.append(_t('dashboard.sites.meta_click_hash_same'))
        if n_changed == 0 and n_added > 0:
            allnew_html = (
                '<div class="note note-allnew">'
                + _t('dashboard.sites.note_all_new', {
                    'total': '<b>' + _t('dashboard.sites.note_all_new.total', {'count': f'{n_subs:,}'}) + '</b>',
                    'added': '<b>' + _t('dashboard.sites.note_all_new.added') + '</b>',
                })
                + '</div>'
            )
        else:
            allnew_html = ''
    else:
        hash_count: Counter = Counter()
        hash_subs: dict[str, list[str]] = defaultdict(list)
        hash_first: dict[str, dict] = {}
        for r in ws:
            h = r.get('response_hash') or ''
            hash_count[h] += 1
            hash_subs[h].append(r['subdomain'])
            if h not in hash_first:
                hash_first[h] = r
        for (h, r) in hash_first.items():
            cnt = hash_count[h]
            bucket_urls = ' '.join(hash_subs[h])
            rows_html_parts.append(f'''<tr class="risk-{status_risk(r.get('status_code'))}" data-hash="{_e(h)}" data-port="{r['port']}" data-hash-count="{cnt}" data-bucket-urls="{_e(bucket_urls)}" data-zh="{has_zh(r.get('title') or '')}"><td class="mono"><a href="{_e(url_for(r))}" target="_blank" rel="noopener noreferrer">{_e(url_for(r))}</a></td><td>{r['port']}</td><td>{(r.get('status_code') if r.get('status_code') is not None else '—')}</td><td>{(r.get('content_length') if r.get('content_length') is not None else '—')}</td><td>{_e(r.get('title') or '')}</td><td class="muted">{_e(r.get('technologies') or '')}</td><td><a class="hash-link" data-hash="{_e(h)}" href="javascript:void(0)">{_e(h)}</a></td><td class="num">{cnt:,}</td><td>{r.get('is_active', 0)}</td>{_score_cell(r)}{_url_detail_cell(r.get('hash_id'), r.get('subdomain'))}</tr>''')
        rows_html = ''.join(rows_html_parts)
        hash_subs_for_modal = dict(hash_subs)
        meta_parts = [_t('dashboard.sites.meta_dedup', {'count': f'{len(hash_first):,}', 'total': f'{len(ws_raw):,}'})]
        if hidden:
            meta_parts.append(_t('dashboard.sites.meta_hidden_no_tld', {'count': f'{hidden:,}'}))
        meta_parts.append(_t('dashboard.sites.meta_click_hash_all'))
    meta = ' · '.join(meta_parts)
    if diff_mode:
        head_cells = f'<th>{_t("dashboard.sites.table_category")}</th><th class="sortable" data-type="str">URL</th><th class="sortable" data-type="num">port</th><th class="sortable" data-type="num">status</th><th class="sortable" data-type="num">bytes</th><th class="sortable" data-type="str">title</th><th class="sortable" data-type="str">tech</th><th class="sortable" data-type="str">hash</th><th class="sortable" data-type="num">count</th><th class="sortable" data-type="num">active</th><th class="sortable" data-type="num">{_t("dashboard.sites.table_score")}</th><th>{_t("dashboard.sites.table_note")}</th><th>{_t("dashboard.sites.table_url_detail")}</th>'
        empty = f'<tr><td colspan="13" class="muted">{_t("dashboard.sites.empty_new")}</td></tr>'
    else:
        head_cells = f'<th class="sortable" data-type="str">URL</th><th class="sortable" data-type="num">port</th><th class="sortable" data-type="num">status</th><th class="sortable" data-type="num">bytes</th><th class="sortable" data-type="str">title</th><th class="sortable" data-type="str">tech</th><th class="sortable" data-type="str">hash</th><th class="sortable" data-type="num">count</th><th class="sortable" data-type="num">active</th><th class="sortable" data-type="num">{_t("dashboard.sites.table_score")}</th><th>{_t("dashboard.sites.table_note")}</th><th>{_t("dashboard.sites.table_url_detail")}</th>'
        empty = f'<tr><td colspan="12" class="muted">{_t("dashboard.sites.empty_web")}</td></tr>'
    hash_data_json = json.dumps(dict(hash_subs_for_modal), ensure_ascii=False)
    return f'\n{allnew_html}\n<div class="filter">\n  <input id="q-sites" placeholder="{_t("dashboard.sites.filter_placeholder")}" />\n  &nbsp;\n  <button class="toggle" data-toggle="hide-http">{_t("dashboard.sites.filter_hide_http")}</button>\n  <button class="toggle" data-toggle="unique">{_t("dashboard.sites.filter_unique_only")}</button>\n  <button class="toggle" data-toggle="zh">{_t("dashboard.sites.filter_zh_first")}</button>\n  &nbsp; <span class="muted">{meta}</span>\n</div>\n<div class="scroll-x"><table id="sites-table">\n<thead><tr>\n  {head_cells}\n</tr></thead>\n<tbody>\n{rows_html or empty}\n</tbody>\n</table></div>\n\n<div id="hash-modal" class="modal" style="display:none">\n  <div class="modal-card">\n    <div class="modal-header">\n      <span id="hash-modal-title"></span>\n      <button id="hash-modal-close" aria-label="{_t("dashboard.sites.modal_close")}">×</button>\n    </div>\n    <div id="hash-modal-body"></div>\n  </div>\n</div>\n\n<script id="hash-data" type="application/json">{hash_data_json}</script>\n'

def _build_ports(agg: dict) -> str:
    tcp = agg['tables']['tcp_assets']
    rows = []
    for r in tcp:
        cls = classify_port(r['port'])
        rows.append({'biz': agg['biz'].get(r['business_id'], ''), 'ip': r.get('ip') or '', 'port': r['port'], 'hosts': r.get('hosts') or '', 'category': cls['category'], 'service': cls['service'], 'risk': cls['risk'], 'is_active': r.get('is_active')})
    rows.sort(key=lambda r: ({'high': 0, 'med': 1, 'low': 2, 'info': 3}[r['risk']], r['ip'], r['port']))
    rows_html = ''.join((f"""<tr class="risk-{_e(r['risk'])}"><td>{_e(r['biz'])}</td><td class="mono">{(_e(r['ip']) if r['ip'] else '—')}</td><td>{r['port']}</td><td class="mono">{(_e(r['hosts']) if r['hosts'] else '—')}</td><td>{_e(r['category'])}</td><td>{_e(r['service'])}</td><td>{_risk_tag(r['risk'])}</td></tr>""" for r in rows))
    rc = agg['risk_counter']
    rc_total = sum(rc.values()) or 1
    rc_labels = {'high': _t('dashboard.risk.high'), 'med': _t('dashboard.risk.med'),
                 'low': _t('dashboard.risk.low'), 'info': _t('dashboard.risk.info')}
    rc_html = ''.join((f"""<span class="tag tag-{(k if k in ('high', 'med', 'low') else 'info')}">{rc_labels.get(k, k)} {v} ({v / rc_total * 100:.0f}%)</span> &nbsp; """ for (k, v) in sorted(rc.items(), key=lambda x: -x[1])))
    return f"""\n<div class="filter">\n  <input id="q" placeholder="{_t('dashboard.ports.filter_placeholder')}" />\n  &nbsp; {rc_html}\n</div>\n<div class="scroll-x"><table id="tcp-table">\n<thead><tr>\n  <th class="sortable" data-type="str">{_t('dashboard.common.biz')}</th>\n  <th class="sortable" data-type="str">ip</th>\n  <th class="sortable" data-type="num">port</th>\n  <th class="sortable" data-type="str">hosts</th>\n  <th class="sortable" data-type="str">category</th>\n  <th class="sortable" data-type="str">service</th>\n  <th>risk</th>\n</tr></thead>\n<tbody>\n{rows_html or ('<tr><td colspan="7" class="muted">' + _t('dashboard.ports.no_data') + '</td></tr>')}\n</tbody>\n</table></div>\n<div class="muted">{_t('dashboard.ports.note', {'count': len(rows)})}</div>\n"""

def _build_mapp(agg: dict, kind: str) -> str:
    """The mini program / WeChat details page — kind="mp" takes mp_records, otherwise it takes oa_records."""
    records = agg['mp_records'] if kind == 'mp' else agg['oa_records']
    title = _t('dashboard.mapp.tab.mp') if kind == 'mp' else _t('dashboard.mapp.tab.oa')
    empty_msg = _t('dashboard.mapp.no_data.mp') if kind == 'mp' else _t('dashboard.mapp.no_data.oa')
    records = sorted(records, key=lambda r: (r.get('unit_name') or '', r.get('service_name') or ''))
    rows_html = ''.join((f"""<tr><td>{_e(r.get('unit_name') or '—')}</td><td>{_e(r.get('service_name') or '')}</td><td class="mono">{_e(r.get('service_licence') or '')}</td><td>{_e(r.get('content_type_name') or '')}</td><td>{_e(r.get('domain') or '')}</td><td>{_e(r.get('record_updated_at') or r.get('fetched_at') or '')}</td></tr>""" for r in records))
    return f'''\n<div class="filter">\n  <input id="q-{kind}" placeholder="{_t('dashboard.mapp.filter_placeholder')}" />\n  &nbsp; <span class="muted">{_t('dashboard.mapp.count_note', {'count': len(records), 'kind': title})}</span>\n</div>\n<div class="scroll-x"><table id="{kind}-table">\n<thead><tr>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.entity')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.name')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.licence')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.type')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.domain')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.updated_at')}</th>\n</tr></thead>\n<tbody>\n{rows_html or f'<tr><td colspan="6" class="muted">{empty_msg}</td></tr>'}\n</tbody>\n</table></div>\n<script>\n(function() {{\n  const q = document.getElementById('q-{kind}');\n  const tbody = document.querySelector('#{kind}-table tbody');\n  if (!q || !tbody) return;\n  q.addEventListener('input', () => {{\n    const v = q.value.toLowerCase();\n    tbody.querySelectorAll('tr').forEach(tr => {{\n      tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';\n    }});\n  }});\n}})();\n</script>\n'''

def _build_companies(agg: dict) -> str:
    """The subsidiary details — it lists the companies table directly."""
    rows = sorted(agg['companies'], key=lambda c: (c.get('business_id') or 0, c.get('unit_name') or ''))
    counts = {c['unit_name']: c for c in agg['comp_counts']}
    rows_html = ''.join((f"""<tr><td>{_e(c.get('unit_name') or '')}</td><td class="muted">{_e(agg['biz'].get(c.get('business_id'), ''))}</td><td>{_e(c.get('nature_name') or '')}</td><td class="mono">{_e(c.get('main_licence') or '')}</td><td class="num">{counts.get(c.get('unit_name'), {}).get('mp', 0)}</td><td class="num">{counts.get(c.get('unit_name'), {}).get('oa', 0)}</td><td class="muted">{_e(c.get('updated_at') or '')}</td></tr>""" for c in rows))
    return f"""\n<div class="filter">\n  <input id="q-comp" placeholder="{_t('dashboard.companies.filter_placeholder')}" />\n  &nbsp; <span class="muted">{_t('dashboard.companies.count_note', {'count': len(rows)})}</span>\n</div>\n<div class="scroll-x"><table id="comp-table">\n<thead><tr>\n  <th class="sortable" data-type="str">{_t('dashboard.companies.table.entity_name')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.common.biz')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.companies.table.entity_type')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.companies.table.main_licence')}</th>\n  <th class="sortable" data-type="num">{_t('dashboard.mapp.tab.mp')}</th>\n  <th class="sortable" data-type="num">{_t('dashboard.mapp.tab.oa')}</th>\n  <th class="sortable" data-type="str">{_t('dashboard.mapp.table.updated_at')}</th>\n</tr></thead>\n<tbody>\n{rows_html or ('<tr><td colspan="7" class="muted">' + _t('dashboard.companies.no_data') + '</td></tr>')}\n</tbody>\n</table></div>\n<script>\n(function() {{\n  const q = document.getElementById('q-comp');\n  const tbody = document.querySelector('#comp-table tbody');\n  if (!q || !tbody) return;\n  q.addEventListener('input', () => {{\n    const v = q.value.toLowerCase();\n    tbody.querySelectorAll('tr').forEach(tr => {{\n      tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';\n    }});\n  }});\n}})();\n</script>\n"""

def _build_risk(agg: dict) -> str:
    parts = []
    risky = agg['risky_open']
    risky_rows = ''.join((f"""<tr class="risk-high"><td>{_e(r['business'])}</td><td class="mono">{_e(r['host'])}</td><td>{r['port']}</td><td>{_e(r['service'])}</td><td>{_e(r['category'])}</td></tr>""" for r in risky))
    parts.append(f"""\n<div class="section-title">{_t('dashboard.risk.section.exposed_ports', {'count': len(risky)})}</div>\n<div class="note">{_t('dashboard.risk.note.exposed_ports')}</div>\n<div class="scroll-x"><table>\n<tr><th>{_t('dashboard.common.biz')}</th><th>host</th><th>port</th><th>service</th><th>category</th></tr>\n{risky_rows or ('<tr><td colspan="5" class="muted">' + _t('dashboard.common.none') + '</td></tr>')}\n</table></div>\n""")
    big_fps = [r for r in agg['fp_rows'] if r['count'] >= 50]
    fp_html = ''.join((f"""<tr class="risk-med"><td class="mono">{(_e(r['hash'][:14]) if r['hash'] else '—')}</td><td>{r['count']}</td><td class="mono">{_e(r['sample'])}</td><td>{_e(r['title'])}</td><td>{_e(r['tech'])}</td></tr>""" for r in big_fps))
    parts.append(f"""\n<div class="section-title">{_t('dashboard.risk.section.batch_fingerprint', {'count': len(big_fps)})}</div>\n<div class="note">{_t('dashboard.risk.note.batch_fingerprint')}</div>\n<div class="scroll-x"><table>\n<tr><th>response_hash</th><th>{_t('dashboard.risk.table.subdomain_count')}</th><th>{_t('dashboard.risk.table.sample_fingerprint')}</th><th>title</th><th>tech</th></tr>\n{fp_html or ('<tr><td colspan="5" class="muted">' + _t('dashboard.common.none') + '</td></tr>')}\n</table></div>\n""")
    fh = agg['fat_hosts']
    fh_rows = ''.join((f"""<tr class="risk-med"><td class="mono">{_e(r['host'])}</td><td>{r['port_count']}</td><td class="muted">{_e(r['sample_ports'])}</td></tr>""" for r in fh))
    parts.append(f"""\n<div class="section-title">{_t('dashboard.risk.section.wide_ports', {'count': len(fh)})}</div>\n<div class="note">{_t('dashboard.risk.note.wide_ports')}</div>\n<div class="scroll-x"><table>\n<tr><th>host</th><th>{_t('dashboard.risk.table.port_count')}</th><th>{_t('dashboard.risk.table.sample_port')}</th></tr>\n{fh_rows or ('<tr><td colspan="3" class="muted">' + _t('dashboard.common.none') + '</td></tr>')}\n</table></div>\n""")
    return ''.join(parts)

def _js_messages_json() -> str:
    """The messages the script of the page needs, as a JSON object.

    The script runs in the browser and cannot reach the message table
    of the server, so the messages it shows are injected into the page
    as JSON. Only the keys the script uses are included.
    """
    keys = (
        'dashboard.sites.score.saving',
        'dashboard.sites.score.invalid',
        'dashboard.sites.score.saved',
        'dashboard.sites.score.save_error',
        'dashboard.sites.hash_modal.empty_hash',
        'dashboard.sites.hash_modal.site_count',
        'dashboard.sites.desc.placeholder',
    )
    out = {}
    for key in keys:
        value = _t(key)
        if value != key:
            out[key] = value
    return json.dumps(out, ensure_ascii=False)


def _lang_switcher(current: str) -> str:
    """The language switcher of the header.

    It is a small form that posts the language back to the same page, so
    it works without JavaScript. The current language is marked, and the
    links of the page keep the language of the request.
    """
    labels = {'zh': '中文', 'en': 'English'}
    parts = []
    for lang in KNOWN_LANGS:
        cls = ' style="color:#cfd7e3;font-weight:600"' if lang == current else ' style="color:#9da5b4"'
        parts.append(
            '<a%s href="?lang=%s">%s</a>' % (cls, lang, labels.get(lang, lang))
        )
    return '<span style="font-size:12px">· ' + ' | '.join(parts) + '</span>'


def render_html(agg: dict, *, mode: str='overview', run_id: str='', biz_name: str='', biz_nav: list[dict]='', home: bool=False) -> bytes:
    diff_mode = mode == 'diff'
    _lang = get_lang()
    _html_lang = 'zh-CN' if _lang == 'zh' else 'en'
    _switcher_html = _lang_switcher(_lang)
    biz_suffix = f' — {_e(biz_name)}' if biz_name else ''
    if mode == 'diff':
        title = f'{_t("dashboard.title.diff", {"biz": biz_suffix, "run_id": _e(run_id)})}'
        h1 = f'{_t("dashboard.title.diff_short", {"biz": biz_suffix})}'
        back_href = f'/{_e(biz_name)}' if biz_name else '/'
        back_label = f'← /{_e(biz_name)}' if biz_name else _t('dashboard.back_to_overview')
        back = f'<a style="color:#9da5b4" href="{back_href}">{back_label}</a> · '
    else:
        title = f'SRC Recon Dashboard{biz_suffix}'
        h1 = f'SRC Recon Dashboard{biz_suffix}'
        diff_href = f'/{_e(biz_name)}/new' if biz_name else '/diff'
        diff_label = _t('dashboard.diff_label')
        back = f'<a style="color:#9da5b4" href="{diff_href}">{diff_label}</a> · '
    nav_html = ''
    if biz_nav:
        links = []
        for b in biz_nav:
            cls = ' class="active"' if b.get('name') == biz_name else ''
            links.append(f'''<a{cls} style="color:#cfd7e3;text-decoration:none;padding:6px 10px;border-radius:4px;font-size:13px" href="{_e(b['href'])}">{_e(b['name'])}</a>''')
        links.append('<a style="color:#cfd7e3;text-decoration:none;padding:6px 10px;border-radius:4px;font-size:13px" href="/">' + _t('dashboard.all_businesses') + '</a>')
        nav_html = '<nav class="biz-bar" style="background:#2d333b;padding:8px 24px;display:flex;gap:6px;align-items:center;border-bottom:1px solid #1f2328"><span style="color:#9da5b4;font-size:12px;margin-right:4px">' + _t('dashboard.business_label') + '</span>' + ''.join(links) + '</nav>'
    page = f"""<!doctype html>\n<html lang="{_html_lang}"><head><meta charset="utf-8">\n<title>{title}</title>\n<style>{_CSS}</style></head>\n<body>\n<script>window.I18N = {_js_messages_json()};</script>\n<header>\n  <h1>{h1}</h1>\n  <div class="meta">\n    snapshot: {_e(agg['captured_at'])}\n    · {back}<a style="color:#9da5b4" href="/api/snapshot">JSON</a>\n    · {_switcher_html}\n  </div>\n</header>\n{nav_html}\n<nav class="tabs">\n  <button data-tab="status" class="active">{_t('dashboard.tab.status')}</button>\n  <button data-tab="screens">{_t('dashboard.tab.screens')}</button>\n  <button data-tab="ports">{_t('dashboard.tab.ports')}</button>\n  <button data-tab="risk">{_t('dashboard.tab.risk')}</button>\n  <button data-tab="companies">{_t('dashboard.tab.companies')}</button>\n  <button data-tab="mp">{_t('dashboard.mapp.tab.mp')}</button>\n  <button data-tab="oa">{_t('dashboard.mapp.tab.oa')}</button>\n</nav>\n<main>\n  <section class="tab-pane active" id="tab-status">{_build_overview(agg, biz_list=home, biz_name=biz_name)}</section>\n  <section class="tab-pane" id="tab-screens">{_build_sites(agg, diff_mode=diff_mode, biz_name=biz_name)}</section>\n  <section class="tab-pane" id="tab-ports">{_build_ports(agg)}</section>\n  <section class="tab-pane" id="tab-risk">{_build_risk(agg)}</section>\n  <section class="tab-pane" id="tab-companies">{_build_companies(agg)}</section>\n  <section class="tab-pane" id="tab-mp">{_build_mapp(agg, 'mp')}</section>\n  <section class="tab-pane" id="tab-oa">{_build_mapp(agg, 'oa')}</section>\n</main>\n<script>\ndocument.querySelectorAll('nav.tabs button').forEach(b => b.addEventListener('click', () => {{\n  document.querySelectorAll('nav.tabs button').forEach(x => x.classList.remove('active'));\n  document.querySelectorAll('.tab-pane').forEach(x => x.classList.remove('active'));\n  b.classList.add('active');\n  document.getElementById('tab-' + b.dataset.tab).classList.add('active');\n}}));\nconst q = document.getElementById('q');\nif (q) q.addEventListener('input', () => {{\n  const v = q.value.toLowerCase();\n  document.querySelectorAll('#tcp-table tr').forEach(tr => {{\n    tr.style.display = tr.textContent.toLowerCase().includes(v) ? '' : 'none';\n  }});\n}});\n\n// sites tab: filter (search + 3 toggles)\nconst sitesTbody = document.querySelector('#sites-table tbody');\nconst allSitesRows = Array.from(sitesTbody?.querySelectorAll('tr') || []);\nconst toggleStates = {{}};\nconst qSites = document.getElementById('q-sites');\n\nfunction applySitesFilters() {{\n  const v = (qSites?.value || '').toLowerCase();\n  const hideHttp = toggleStates['hide-http'];\n  const unique = toggleStates['unique'];\n  allSitesRows.forEach(tr => {{\n    let show = true;\n    if (v && !tr.textContent.toLowerCase().includes(v)) {{\n      // For buckets with >1 sibling subdomains, the visible row URL is just\n      // one of them; fall back to data-bucket-urls so queries hit siblings\n      // (e.g. searching "oa.example.com" finds the row whose visible URL is\n      // a permutation sibling sharing the same fingerprint).\n      const cnt = parseInt(tr.dataset.hashCount) || 1;\n      const bucket = tr.dataset.bucketUrls || '';\n      if (!(cnt > 1 && bucket.toLowerCase().includes(v))) show = false;\n    }}\n    if (show && hideHttp && tr.dataset.port === '80') show = false;\n    if (show && unique && tr.dataset.hashCount !== '1') show = false;\n    tr.style.display = show ? '' : 'none';\n  }});\n  // Chinese first: re-sort the visible rows (the Chinese title first)\n  if (toggleStates['zh']) {{\n    const rows = allSitesRows.filter(tr => tr.style.display !== 'none');\n    rows.sort((a, b) => (parseInt(b.dataset.zh) || 0) - (parseInt(a.dataset.zh) || 0));\n    rows.forEach(r => sitesTbody.appendChild(r));\n  }}\n}}\n\ndocument.querySelectorAll('button.toggle').forEach(btn => {{\n  btn.addEventListener('click', () => {{\n    btn.classList.toggle('on');\n    toggleStates[btn.dataset.toggle] = btn.classList.contains('on');\n    applySitesFilters();\n  }});\n}});\n\nif (qSites) qSites.addEventListener('input', applySitesFilters);\n\n// generic column sort (works for sites-table, tcp-table, any th.sortable)\ndocument.querySelectorAll('th.sortable').forEach(th => {{\n  let asc = true;\n  th.addEventListener('click', () => {{\n    const table = th.closest('table');\n    const tbody = table.querySelector('tbody') || table;\n    const idx = Array.from(th.parentElement.children).indexOf(th);\n    const type = th.dataset.type;\n    const rows = Array.from(tbody.querySelectorAll('tr')).filter(\n      r => !(r.children.length === 1 && r.children[0].hasAttribute('colspan')));\n    if (!rows.length) return;\n    const cmp = (a, b) => {{\n      // read data-num first (the sort key of the score cell; the textContent of the input is an empty string)\n      const an = a.children[idx]?.dataset?.num;\n      const bn = b.children[idx]?.dataset?.num;\n      const av = (an !== undefined ? an : (a.children[idx]?.textContent || '')).trim();\n      const bv = (bn !== undefined ? bn : (b.children[idx]?.textContent || '')).trim();\n      if (type === 'num') {{\n        return (parseFloat(av) || 0) - (parseFloat(bv) || 0);\n      }}\n      return av.localeCompare(bv, 'zh-Hans-CN');\n    }};\n    rows.sort((a, b) => asc ? cmp(a, b) : -cmp(a, b));\n    rows.forEach(r => tbody.appendChild(r));\n    th.parentElement.querySelectorAll('th').forEach(x => {{\n      x.classList.remove('asc', 'desc');\n    }});\n    th.classList.add(asc ? 'asc' : 'desc');\n    asc = !asc;\n  }});\n}});\n\n// hash modal: click hash link → show all sites for that hash\nconst hashData = JSON.parse(document.getElementById('hash-data')?.textContent || '{{}}');\nconst modal = document.getElementById('hash-modal');\ndocument.querySelectorAll('.hash-link').forEach(a => {{\n  a.addEventListener('click', () => {{\n    const h = a.dataset.hash;\n    const subs = hashData[h] || [];\n    document.getElementById('hash-modal-title').textContent =\n      `hash ${{h || window.I18N['dashboard.sites.hash_modal.empty_hash']}} · ${{subs.length}} ${{window.I18N['dashboard.sites.hash_modal.site_count']}}`;\n    const rowsHtml = subs.map(s =>\n      `<tr><td class="mono"><a href="http://${{s}}/" target="_blank" rel="noopener noreferrer">${{s}}</a></td></tr>`\n    ).join('');\n    document.getElementById('hash-modal-body').innerHTML =\n      '<table><tr><th>subdomain</th></tr>' + rowsHtml + '</table>';\n    modal.style.display = 'flex';\n  }});\n}});\ndocument.getElementById('hash-modal-close')?.addEventListener('click', () => {{\n  modal.style.display = 'none';\n}});\nmodal?.addEventListener('click', e => {{\n  if (e.target === modal) modal.style.display = 'none';\n}});\ndocument.addEventListener('keydown', e => {{\n  if (e.key === 'Escape' && modal?.style.display === 'flex') {{\n    modal.style.display = 'none';\n  }}\n}});\n\n// --- score / description edit ---\n// the score column becomes an input: an absolute value (0-100) or +/-N relative to the current one; the sorting of the score column is done by\n// the generic th.sortable handler (data-num is already the sort key). When the POST succeeds the server updates the\n// _State.cached_snap in place and recalculates cached_snap_json.\nfunction parseScoreInput(raw, cur) {{\n  if (raw === null) return null;\n  const s = String(raw).trim();\n  if (!s) return null;\n  let n;\n  if (s[0] === '+' || s[0] === '-') {{\n    const delta = parseInt(s, 10);\n    if (Number.isNaN(delta) || cur === null || cur === undefined) return null;\n    n = cur + delta;\n  }} else {{\n    n = parseInt(s, 10);\n    if (Number.isNaN(n)) return null;\n  }}\n  if (n < 0 || n > 100) return null;\n  return n;\n}}\n\nasync function editHash(hashId, newScore, newDesc) {{\n  const status = document.querySelector('.desc-status[data-hash-id="' + hashId + '"]');\n  if (status) status.textContent = window.I18N['dashboard.sites.score.saving'];\n  try {{\n    const body = new URLSearchParams();\n    body.set('score', String(newScore));\n    body.set('description', newDesc || '');\n    const r = await fetch('/api/hash/' + hashId + '/edit', {{\n      method: 'POST',\n      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},\n      body: body.toString(),\n    }});\n    const j = await r.json();\n    if (!j.ok) throw new Error(j.error || 'http ' + r.status);\n    // refresh the data-num of every score-cell td at once (the sorting works right away)\n    document.querySelectorAll('.score-cell[data-hash-id="' + hashId + '"]').forEach(td => {{\n      td.setAttribute('data-num', String(j.score));\n    }});\n    document.querySelectorAll('.score-input[data-hash-id="' + hashId + '"]').forEach(el => {{\n      el.value = String(j.score);\n      el.setAttribute('data-cur-score', String(j.score));\n    }});\n    if (status) status.textContent = window.I18N['dashboard.sites.score.saved'] + ' ' + j.score;\n    setTimeout(() => {{ if (status && status.textContent.startsWith('✓')) status.textContent = ''; }}, 2000);\n  }} catch (e) {{\n    if (status) status.textContent = window.I18N['dashboard.sites.score.save_error'] + ' ' + e.message;\n  }}\n}}\n\nfunction triggerSave(hid) {{\n  const input = document.querySelector('.score-input[data-hash-id="' + hid + '"]');\n  const descEl = document.querySelector('.desc-input[data-hash-id="' + hid + '"]');\n  if (!input) return;\n  const cur = parseInt(input.getAttribute('data-cur-score'), 10);\n  const parsed = parseScoreInput(input.value, cur);\n  if (parsed === null) {{\n    const status = document.querySelector('.desc-status[data-hash-id="' + hid + '"]');\n    if (status) status.textContent = window.I18N['dashboard.sites.score.invalid'];\n    setTimeout(() => {{ if (status && status.textContent.startsWith('✗')) status.textContent = ''; }}, 2500);\n    return;\n  }}\n  editHash(hid, parsed, descEl ? descEl.value : '');\n}}\n\ndocument.addEventListener('click', e => {{\n  const btn = e.target.closest('.score-save');\n  if (btn) {{\n    const hid = btn.getAttribute('data-hash-id');\n    triggerSave(hid);\n  }}\n}});\n\ndocument.addEventListener('keydown', e => {{\n  if (e.key === 'Enter' && e.target.classList?.contains('score-input')) {{\n    e.preventDefault();\n    triggerSave(e.target.getAttribute('data-hash-id'));\n  }}\n  // the description input saves on Enter; an invalid score only refreshes the status, it does not store the description\n  if (e.key === 'Enter' && e.target.classList?.contains('desc-input')) {{\n    e.preventDefault();\n    const hid = e.target.getAttribute('data-hash-id');\n    const input = document.querySelector('.score-input[data-hash-id="' + hid + '"]');\n    if (!input) return;\n    const cur = parseInt(input.getAttribute('data-cur-score'), 10);\n    const parsed = parseScoreInput(input.value, cur);\n    if (parsed === null) return;\n    editHash(hid, parsed, e.target.value);\n  }}\n}});\n</script>\n</body></html>\n"""
    return page.encode('utf-8')
_DIFF_TABLES = ('scopes', 'companies', 'mapp_records', 'web_hashes', 'web_subdomains', 'tcp_assets')

def _find_latest_report(reports_dir: Path=Path('reports')) -> Path | None:
    """Latest run dir under reports/ (named like 20260728-080001)."""
    if not reports_dir.exists():
        return None
    candidates = [d for d in reports_dir.iterdir() if d.is_dir() and len(d.name) == 15 and (d.name[8] == '-')]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.name, reverse=True)
    return candidates[0]

def _csv_row_key(table: str, row: dict, biz_name_to_id: dict[str, int], comp_name_to_id: dict[str, int]) -> tuple | None:
    """Build the same logical key tuple for a diff-CSV row that
    diff.py's _<table>_key produces for a snapshot row."""
    bid = biz_name_to_id.get(row.get('business', ''))
    if table == 'scopes':
        return (bid, row.get('scope_name', ''), row.get('asset', ''))
    if table == 'companies':
        return (bid, row.get('unit_name', ''))
    if table == 'mapp_records':
        cid = comp_name_to_id.get(row.get('unit_name', ''))
        if row.get('service_licence'):
            return ('lic', cid, row['service_licence'])
        return ('nm', cid, row.get('service_name', ''), row.get('service_type'))
    if table == 'web_hashes':
        return (bid, row.get('response_hash', ''))
    if table == 'web_subdomains':
        port = int(row['port']) if row.get('port') else 0
        return (bid, row.get('subdomain', ''), port)
    if table == 'tcp_assets':
        port = int(row['port']) if row.get('port') else 0
        return (bid, row.get('host', ''), port)
    return None

def _snapshot_row_key(table: str, r: dict) -> tuple | None:
    """Logical key for a snapshot row, matching diff.py's _<table>_key."""
    if table == 'scopes':
        return (r.get('business_id'), r.get('scope_name'), r.get('asset'))
    if table == 'companies':
        return (r.get('business_id'), r.get('unit_name'))
    if table == 'mapp_records':
        if r.get('service_licence'):
            return ('lic', r.get('company_id'), r.get('service_licence'))
        return ('nm', r.get('company_id'), r.get('service_name'), r.get('service_type'))
    if table == 'web_hashes':
        return (r.get('business_id'), r.get('response_hash'))
    if table == 'web_subdomains':
        return (r.get('business_id'), r.get('subdomain'), r.get('port'))
    if table == 'tcp_assets':
        return (r.get('business_id'), r.get('ip'), r.get('port'))
    return None

def _load_changed_keys(report_dir: Path, snap: dict) -> dict[str, dict[tuple, str]]:
    """Scan the added/reactivated/changed CSVs; any difference against the
    previous
    run qualifies. Returns {table: {key_tuple: category_label}}.

    The categories used:
        "added"      — the row did not exist in the previous snapshot
        "added"      — the row was inactive before and is active now (reactivated)
        "changed"    — the row existed and was active in both runs, but a
                       field (hash / status / title / tech / etc.) differs

    The category is informational — the /new site tab badges the rows as "new" or
    "changed"
    purely for the visual identification. The inclusion is uniform across all
    three CSVs:
    a row is on /new if and only if it shows up in any of them.
    """
    if not report_dir or not report_dir.exists():
        return {t: {} for t in _DIFF_TABLES}
    biz_name_to_id = {b.get('business_name'): b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name')}
    comp_name_to_id = {c.get('unit_name'): c['id'] for c in snap.get('tables', {}).get('companies', []) if c.get('unit_name')}
    tagged: dict[str, dict[tuple, str]] = {t: {} for t in _DIFF_TABLES}
    for cat in ('added', 'reactivated', 'changed'):
        for csv_path in report_dir.glob(f'{cat}_*.csv'):
            table = csv_path.stem[len(cat) + 1:]
            if table not in tagged:
                continue
            label = 'added' if cat in ('added', 'reactivated') else 'changed'
            with csv_path.open(encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    key = _csv_row_key(table, row, biz_name_to_id, comp_name_to_id)
                    if key and all((k is not None for k in key)):
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
    tables = snap.get('tables', {})
    out: dict[str, list] = {}
    for (name, rows) in tables.items():
        if name not in tagged:
            out[name] = rows
            continue
        keys = tagged[name]
        if not keys:
            out[name] = []
            continue
        seen: set = set()
        kept: list = []
        for r in rows:
            k = _snapshot_row_key(name, r)
            if k is None or k not in keys or k in seen:
                continue
            seen.add(k)
            r2 = dict(r)
            r2['diff_category'] = keys[k]
            kept.append(r2)
        out[name] = kept
    new_snap = dict(snap)
    new_snap['tables'] = out
    new_snap['row_counts'] = {t: len(out.get(t, [])) for t in snap.get('row_counts', {})}
    return new_snap

def _filter_snap_by_business(snap: dict, bid: int) -> dict:
    """Return a new snapshot with every table limited to business_id == bid.

    mapp_records has no business_id (it's company_id -> companies.business_id),
    so it gets filtered through company_id membership.
    """
    tables = snap.get('tables', {})
    comp_id_set = {c['id'] for c in tables.get('companies', []) if c.get('business_id') == bid}
    out: dict[str, list] = {}
    _BID_TABLES = ('scopes', 'companies', 'web_subdomains', 'web_hashes', 'tcp_assets')
    for (name, rows) in tables.items():
        if name == 'businesses':
            out[name] = [b for b in rows if b.get('id') == bid]
        elif name in _BID_TABLES:
            out[name] = [r for r in rows if r.get('business_id') == bid]
        elif name == 'mapp_records':
            out[name] = [r for r in rows if r.get('company_id') in comp_id_set]
        else:
            out[name] = rows
    new_snap = dict(snap)
    new_snap['tables'] = out
    new_snap['row_counts'] = {t: len(out.get(t, [])) for t in snap.get('row_counts', {})}
    return new_snap

def _changed_keys_for_business(tagged: dict[str, dict[tuple, str]], bid: int, snap: dict) -> dict[str, dict[tuple, str]]:
    """Filter `tagged` (output of _load_changed_keys) to business_id == bid.

    mapp_records keys are ((lic|nm), company_id, ...) — those key-company_ids
    are mapped through companies.business_id to compare against `bid`.
    Category label is preserved.
    """
    comp_id_to_bid: dict[int, int] = {c['id']: c.get('business_id') for c in snap.get('tables', {}).get('companies', [])}
    out: dict[str, dict[tuple, str]] = {}
    for (table, kv) in tagged.items():
        if not kv:
            out[table] = {}
            continue
        if table == 'mapp_records':
            out[table] = {k: cat for (k, cat) in kv.items() if comp_id_to_bid.get(k[1]) == bid}
        else:
            out[table] = {k: cat for (k, cat) in kv.items() if k[0] == bid}
    return out

class _State:
    # The rendered pages are cached per language: the language of a page
    # is a request setting, so one cache entry cannot serve both. The
    # key is the language, the value is (home, overview) of the rendered
    # pages and of their gzip form.
    cached_pages: dict = {}

    db_path: Path = Path(__file__).resolve().parents[2] / 'db' / 'recon.sqlite3'
    reload_interval: float = float(os.environ.get('DASHBOARD_RELOAD', '30'))
    last_reload_ts: float = 0.0
    cached_snap: dict = {}
    cached_snap_json: bytes = b''
    reload_lock: Any = None

def _ct_category(table: str, ct: int) -> str | None:
    """Classify a row by its change_type bitmask, mirroring diff.py's rules.

    added       - change_type & 1
    reactivated - change_type & 4   (web_subdomains / tcp_assets only)
    changed     - change_type & 2

    Deactivated (change_type & 8) is deliberately excluded: the report kept
    deactivated_* CSVs as a separate category and the /new page badges rows
    only as "new" / "changed", so the same exclusion applies here.

    Returns None for rows that should not appear on /new.
    """
    if not ct or ct <= 0:
        return None
    if ct & 1:
        return 'added'
    if table in ('web_subdomains', 'tcp_assets') and (ct & 4):
        return 'reactivated'
    if ct & 2:
        return 'changed'
    # Only the deactivated bit (8) is left -> not part of /new.
    return None


def _filter_snapshot_by_ct(snap: dict) -> dict:
    """Filter a snapshot down to rows whose change_type > 0.

    Replaces the report-CSV based filter (_filter_snapshot + _load_changed_keys)
    so /new reflects the live database instead of the last daily report.

    Rows are deduped by snapshot key: the source data accumulates duplicates
    from re-scans (a (bid, subdomain, port) may appear as multiple records),
    and without dedup /new would list every duplicate.
    """
    tables = snap.get('tables', {})
    out: dict[str, list] = {}
    for (name, rows) in tables.items():
        kept: list = []
        seen: set = set()
        for r in rows:
            try:
                ct = int(r.get('change_type') or 0)
            except (TypeError, ValueError):
                ct = 0
            cat = _ct_category(name, ct)
            if cat is None:
                continue
            k = _snapshot_row_key(name, r)
            if k is None or k in seen:
                continue
            seen.add(k)
            r2 = dict(r)
            r2['diff_category'] = cat
            kept.append(r2)
        out[name] = kept
    new_snap = dict(snap)
    new_snap['tables'] = out
    new_snap['row_counts'] = {t: len(out.get(t, [])) for t in snap.get('row_counts', {})}
    return new_snap


def _latest_run_id() -> str:
    """Most recent finished run id from run_markers (empty string if none).

    Used only for display in the /new page header; the data itself now comes
    straight from the database, so a missing/stale run marker is cosmetic.
    """
    try:
        conn = sqlite3.connect(str(_State.db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT run_id FROM run_markers WHERE finished_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else ''
        finally:
            conn.close()
    except sqlite3.Error:
        return ''


# ---------------------------------------------------------------------------
# /<business name>/body/raw?url=<domain-url>&side=pre|cur
# ---------------------------------------------------------------------------
# Take the raw HTTP body of one subdomain at the "before / after" moments and hand
# it to the browser to render.
# side=pre -> web_subdomains_pre.curr_body (the version in the step 0 snapshot of
# daily_monitor)
# side=cur -> web_subdomains.curr_body       (the version the latest import
# wrote)
#
# Three hard constraints:
# 1) The data is queried from SQLite every time, it does not go into the snapshot
#    cache. The body column is not in
#    _DB_SNAPSHOT_QUERIES to begin with, and side=pre depends on the semantics of
#    the step 0 snapshot, so the cache would mix things up.
# 2) The pre side joins on the four conditions (id + business_id + subdomain +
#    port) being equal, not
#    only on the id — the judgement period (algo_diff_cpp) uses pre.id = cur.id,
#    but after an id is recycled
#    it may point at a different logical row; only with the three business columns
#    added are the two really equivalent.
#    It does not take ws.prev_body either: the UPSERT of import_scan_results
#    rolls prev_body over,
#    and it differs from the snapshot table in 56/272 rows in the measurements.
# 3) The response has to carry a CSP sandbox. The body is the third party content
#    of the scanned target, and this endpoint is
#    same-origin with the dashboard — without the sandbox a <script> in the body
#    could call
#    the POST of the dashboard (trigger a scan / change the score) and GET
#    /api/snapshot to read all the business data.

_MIME_TOKEN_RE = re.compile(r'^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$')
_CHARSET_TOKEN_RE = re.compile(r'^[A-Za-z0-9_.:+-]{1,40}$')
_SCHEME_TOKEN_RE = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*$')
_META_CHARSET_RE = re.compile(rb'<meta[^>]{0,200}?charset\s*=\s*["\']?\s*([A-Za-z0-9_.:+-]{1,40})', re.I)
_XML_ENCODING_RE = re.compile(rb'<\?xml[^>]{0,200}?encoding\s*=\s*["\']?\s*([A-Za-z0-9_.:+-]{1,40})', re.I)

# The third party body is parsed under the origin of the dashboard itself, so the
# CSP keeps the active surface clean:
#   sandbox (no allow-scripts / allow-same-origin) -> the scripts in the body do
#     not run, the
#     origin is opaque and they cannot call the POST of the dashboard or read
#     /api/snapshot;
#   default-src 'none' -> no subresource is loaded, so the browser does not call
#     back to the target site and leak the IP;
#   style-src 'unsafe-inline' -> the inline styles of the page are kept, otherwise
#     the page has no layout at all;
#   img-src data: blob: -> only the inline images are allowed, which blocks the
#     remote image beacons;
#   base-uri 'none' -> a <base href> in the body cannot rewrite the relative links
#     to point at the dashboard;
#   form-action 'none' -> a captured phishing page cannot submit a form (one more
#     layer on top of the sandbox);
#   frame-ancestors 'self' -> only the dashboard itself may embed it.
_BODY_RAW_CSP = (
    "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
    "frame-ancestors 'self'"
)


def _parse_body_url(raw: str) -> tuple[str, int | None, int] | None:
    """Parse the ?url= parameter -> (host, the explicit port or None, the default
    port inferred from the scheme).

    Only host[:port] is taken, the path / query / fragment are dropped — the
    unique key of web_subdomains
    is (business_id, subdomain, port), it has no path dimension.
    It returns None when it cannot be parsed.
    """
    s = (raw or '').strip()
    if not s:
        return None
    scheme = ''
    if '://' in s:
        scheme, _, s = s.partition('://')
        scheme = scheme.strip().lower()
        # the scheme has to be a valid token, otherwise treating the whole string as
        # the host would give a misleading
        # "no such subdomain" (404) instead of "cannot parse url" (400).
        if not scheme or not _SCHEME_TOKEN_RE.match(scheme):
            return None
    s = s.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    if '@' in s:
        s = s.rsplit('@', 1)[1]
    if s.startswith('['):
        end = s.find(']')
        if end < 0:
            return None
        host = s[1:end]
        rest = s[end + 1:]
        if rest.startswith(':'):
            port_s = rest[1:]
        elif rest:
            return None
        else:
            port_s = ''
    else:
        host, _, port_s = s.partition(':')
    host = host.strip().lower()
    if not host or any(c.isspace() for c in host):
        return None
    port: int | None = None
    if port_s:
        if not port_s.isdigit():
            return None
        port = int(port_s)
        if not (1 <= port <= 65535):
            return None
    return host, port, (80 if scheme == 'http' else 443)


def _snapshot_at_iso(raw: str) -> str:
    """The _snapshot_at of web_subdomains_pre holds SQLite datetime('now') = UTC.

    The other timestamps in the repo (fetched_at / last_seen) are all +08:00 ISO,
    so they are unified into the same
    reading here, to avoid two headers that are one UTC and one +08:00 being
    misread as the same moment.
    """
    s = (raw or '').strip()
    if not s:
        return ''
    try:
        dt = datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return s
    return dt.astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _declared_body_charset(data: bytes) -> str:
    """Read the charset the body declares for itself (the HTML <meta charset> or
    the XML declaration).

    Only the first 4KB are looked at — the browser only looks for the meta
    charset in the first 1KB either.
    """
    head = data[:4096]
    for rx in (_META_CHARSET_RE, _XML_ENCODING_RE):
        m = rx.search(head)
        if m:
            tok = m.group(1).decode('ascii', 'replace')
            if _CHARSET_TOKEN_RE.match(tok):
                return tok
    return ''


def _pick_body_charset(data: bytes, declared: str) -> str:
    """Pick a charset that can really decode this byte string.

    utf-8 comes first: the whole string decoding cleanly as utf-8 is a strong
    signal, so the bytes should be
    trusted even when the body declares another charset; when it does not decode
    it falls back to the declared value, then to gb18030 (a superset of GBK),
    and utf-8 is the last fallback.
    """
    order = ['utf-8']
    if declared and declared.lower() not in order:
        order.append(declared)
    if 'gb18030' not in order:
        order.append('gb18030')
    for enc in order:
        try:
            data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
        return enc
    return 'utf-8'


def _sniff_body_mime(data: bytes) -> str:
    """When the stored content_type is empty, guess a MIME from the bytes."""
    head = data[:512].lstrip().lower()
    if head.startswith((b'<!doctype', b'<html')):
        return 'text/html'
    if head.startswith(b'<?xml'):
        return 'application/xml'
    if head.startswith(b'{'):
        return 'application/json'
    return 'text/plain'


def _body_content_type(stored: str | None, data: bytes) -> str:
    """Compute the Content-Type (with the charset) that goes back to the browser.

    The stored value comes first (that is the type the other side really sent
    during the scan), but it has to pass a token check — it comes
    from a third party response header, so it cannot be pasted into a response
    header directly. When the stored value is empty or invalid it is sniffed
    from the bytes.
    """
    mime = ''
    if stored:
        cand = stored.split(';', 1)[0].strip().lower()
        if _MIME_TOKEN_RE.match(cand):
            mime = cand
    if not mime:
        mime = _sniff_body_mime(data)
    if mime.startswith('text/') or mime in ('application/xml', 'application/json'):
        return f'{mime}; charset={_pick_body_charset(data, _declared_body_charset(data))}'
    return mime


class Handler(BaseHTTPRequestHandler):
    server_version = 'ReconDashboard/1.0'

    def do_GET(self) -> None:
        accept_enc = self.headers.get('Accept-Encoding', '').encode()
        raw, _, raw_query = self.path.partition('?')
        path = urllib.parse.unquote(raw).rstrip('/') or '/'
        self._apply_request_lang(raw_query)
        if path == '/':
            self._serve_page(accept_enc, home=True)
            return
        if path == '/diff':
            self._serve_diff(None, accept_enc)
            return
        if path == '/health':
            self._respond(200, 'text/plain; charset=utf-8', b'ok', accept_enc)
            return
        if path == '/refresh':
            self._do_reload()
            self._respond(200, 'text/plain; charset=utf-8', b'reloaded\n', accept_enc)
            return
        if path.startswith('/api/snapshot'):
            self._serve_snapshot(accept_enc)
            return
        if path.startswith('/'):
            biz_part = path[1:]
            # /<business name>/body/raw?url=<domain-url>&side=pre|cur
            # it comes before the snapshot: this endpoint always queries SQLite
            # live, it does not use the snapshot cache.
            if biz_part.endswith('/body/raw'):
                self._serve_body_raw(biz_part[:-len('/body/raw')], raw_query, accept_enc)
                return
            (biz_name, is_new) = (biz_part, False)
            if biz_part.endswith('/new'):
                (biz_name, is_new) = (biz_part[:-4], True)
            snap = self._load_snap()
            bid = next((b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name') == biz_name), None)
            if bid is not None:
                self._serve_business(biz_name, is_new, accept_enc)
                return
            if '/urls/' in biz_part:
                (maybe_biz, hash_part) = biz_part.split('/urls/', 1)
                if hash_part and '/' not in hash_part:
                    self._serve_urls_detail(maybe_biz, hash_part, accept_enc)
                    return
        self._respond(404, 'text/plain', f'not found: {raw}'.encode(), accept_enc)

    def do_POST(self) -> None:
        # The thread serves one request after the other, so the language
        # of the request is released when it is done.
        self._apply_request_lang(self.path.partition('?')[2])
        """The POST routes:
          /<business name>/scan              — post the hosts list to trigger the
                                          scan-onesite sub flow
          /<business name>/scan-urls         — post hosts + sources + wordlist to
                                          trigger the URL asset scan
          /api/hash/<id>/edit         — change the score + the description (a
                                     plain JSON response, no redirect)
          /<business name>/schedule/toggle   — the user decided on 2026-08-26:
                                         toggle a subdomain in and out of the
                                         daily scan
                                         (a plain JSON response, no redirect —
                                          the JS handles the redirect / the hint)
        """
        accept_enc = self.headers.get('Accept-Encoding', '').encode()
        raw = self.path.split('?', 1)[0]
        path = urllib.parse.unquote(raw).rstrip('/') or '/'
        if path.startswith('/api/hash/') and path.endswith('/edit'):
            self._handle_hash_edit(path, accept_enc)
            return
        if '/scan-urls' in path:
            if path.endswith('/scan-urls'):
                biz_name = path[:-len('/scan-urls')].lstrip('/')
                if biz_name:
                    self._dispatch_scan_urls(biz_name, accept_enc)
                    return
            self._respond(404, 'text/plain', f'bad scan-urls path: {raw}\n'.encode(), accept_enc)
            return
        if '/katana-enrich' in path:
            if path.endswith('/katana-enrich'):
                biz_name = path[:-len('/katana-enrich')].lstrip('/')
                if biz_name:
                    self._dispatch_katana_enrich(biz_name, accept_enc)
                    return
            self._respond(404, 'text/plain', f'bad katana-enrich path: {raw}\n'.encode(), accept_enc)
            return
        if '/schedule/toggle' in path:
            if path.endswith('/schedule/toggle'):
                biz_name = path[:-len('/schedule/toggle')].lstrip('/')
                if biz_name:
                    self._handle_schedule_toggle(biz_name, accept_enc)
                    return
            self._respond(404, 'text/plain', f'bad schedule path: {raw}\n'.encode(), accept_enc)
            return
        if not path.startswith('/') or not path.endswith('/scan'):
            self._respond(404, 'text/plain', f'not found: {raw}'.encode(), accept_enc)
            return
        biz_name = path[1:-len('/scan')]
        if not biz_name:
            self._respond(404, 'text/plain', b'missing business name\n', accept_enc)
            return
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next((b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name') == biz_name), None)
        if bid is None:
            self._respond(404, 'text/plain', f'unknown business: {biz_name}\n'.encode(), accept_enc)
            return
        self._handle_scan(biz_name, accept_enc)

    def _handle_hash_edit(self, path: str, accept_enc: bytes) -> None:
        """POST /api/hash/<id>/edit — changes the score + the description, the
        response is plain JSON.

        Body: application/x-www-form-urlencoded
          score       = int 0..100   (required)
          description = str          (optional, it defaults to '')

        The response: 200 {"ok": true, "id": <id>, "score": ..., "description": "..."}
                    400 bad arguments / 404 the hash does not exist

        The _State.cached_snap is updated in place right after the write, so that
        the stale data is not shown for the 5 minutes until the reload.
        On the next cron run (per the "Web Hash scoring" section) only the new
        hashes + the unscored hashes are
        processed, so this manual change is not overwritten by the cron.
        """
        prefix = '/api/hash/'
        suffix = '/edit'
        mid = path[len(prefix):-len(suffix)] if path.endswith(suffix) else ''
        try:
            hash_id = int(mid)
        except ValueError:
            self._respond(400, 'application/json', b'{"ok":false,"error":"bad hash id"}\n', accept_enc)
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._respond(400, 'application/json', b'{"ok":false,"error":"bad Content-Length"}\n', accept_enc)
            return
        if length <= 0 or length > 4096:
            self._respond(400, 'application/json', b'{"ok":false,"error":"body empty or too large"}\n', accept_enc)
            return
        body = self.rfile.read(length).decode('utf-8', 'replace')
        try:
            fields = urllib.parse.parse_qs(body, keep_blank_values=True)
        except Exception:
            self._respond(400, 'application/json', b'{"ok":false,"error":"bad form body"}\n', accept_enc)
            return
        if 'score' not in fields:
            self._respond(400, 'application/json', b'{"ok":false,"error":"missing score field"}\n', accept_enc)
            return
        try:
            new_score = int(fields['score'][0])
        except (ValueError, IndexError):
            self._respond(400, 'application/json', b'{"ok":false,"error":"score must be int"}\n', accept_enc)
            return
        if not 0 <= new_score <= 100:
            self._respond(400, 'application/json', b'{"ok":false,"error":"score must be 0..100"}\n', accept_enc)
            return
        new_desc = fields.get('description', [''])[0][:500]
        db_path = _State.db_path
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                row = conn.execute('SELECT id, score, description FROM web_hashes WHERE id=?', (hash_id,)).fetchone()
                if row is None:
                    self._respond(404, 'application/json', b'{"ok":false,"error":"hash not found"}\n', accept_enc)
                    return
                conn.execute("UPDATE web_hashes SET score=?, description=?, score_initialized_at = COALESCE(score_initialized_at, datetime('now', 'localtime')) WHERE id=?", (new_score, new_desc, hash_id))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self._respond(500, 'application/json', f'{{"ok":false,"error":"db error: {_e(str(exc))}"}}\n'.encode(), accept_enc)
            return
        try:
            cached = _State.cached_snap
            if cached:
                for h in cached.get('tables', {}).get('web_hashes', []):
                    if h.get('id') == hash_id:
                        h['score'] = new_score
                        h['description'] = new_desc
                for ws in cached.get('tables', {}).get('web_subdomains', []):
                    if ws.get('hash_id') == hash_id:
                        ws['hash_score'] = new_score
                        ws['hash_description'] = new_desc
                _State.cached_snap_json = json.dumps(cached, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except Exception as exc:
            print(f'[dashboard] cache update failed (reload will retry): {exc}', file=sys.stderr)
        body_out = f'{{"ok":true,"id":{hash_id},"score":{new_score},"description":{json.dumps(new_desc)}}}\n'.encode()
        self._respond(200, 'application/json', body_out, accept_enc)
        log.info(f'hash edit id={hash_id} score={new_score} desc_len={len(new_desc)}')

    def _handle_scan(self, biz_name: str, accept_enc: bytes) -> None:
        """The actual handling of POST /<business>/scan: parse the body -> pre-validate -> subprocess call scan-onesite."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._respond(400, 'text/plain', b'bad Content-Length\n', accept_enc)
            return
        if length <= 0 or length > SCAN_MAX_BODY:
            self._respond(400, 'text/plain', f'body too large or empty ({length} bytes)\n'.encode(), accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond(400, 'text/plain', b'invalid utf-8 body\n', accept_enc)
            return
        raw_hosts = (fields.get('hosts', [''])[0] or '').strip()
        seen: set[str] = set()
        hosts: list[str] = []
        for line in raw_hosts.splitlines():
            s = line.strip().lower().rstrip('.')
            if not s or s.startswith('#'):
                continue
            if s in seen:
                continue
            seen.add(s)
            hosts.append(s)
        if not hosts:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.no_valid_domain")], accept_enc=accept_enc, status=400)
            return
        if len(hosts) > SCAN_MAX_HOSTS:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.too_many_domains", {"count": len(hosts), "max": SCAN_MAX_HOSTS})], accept_enc=accept_enc, status=400)
            return
        invalid = [h for h in hosts if not SCAN_HOSTNAME_RE.match(h)]
        if invalid:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.invalid_hostname", {"hosts": ', '.join(invalid[:5])})], accept_enc=accept_enc, status=400)
            return
        (tmp_fd, tmp_name) = tempfile.mkstemp(prefix='scan-hosts-', suffix='.txt', dir='/tmp')
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                f.write('\n'.join(hosts) + '\n')
            pdtm_script = _State.db_path.parent.parent / 'pdtm' / 'import_scan_results.py'
            cmd = ['/usr/bin/python3', str(pdtm_script), 'scan-onesite', '--business', biz_name, '--hosts-file', str(tmp_path), '--db', str(_State.db_path)]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=SCAN_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.subprocess_timeout", {"timeout": SCAN_TIMEOUT})], accept_enc=accept_enc, status=504)
                return
            except FileNotFoundError as e:
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.python_start_failed", {"error": e})], accept_enc=accept_enc, status=500)
                return
            if proc.returncode != 0:
                err = (proc.stderr or b'').decode('utf-8', 'replace').strip() or f'exit={proc.returncode}'
                status = 400 if proc.returncode == 2 else 500
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[err[:500]], accept_enc=accept_enc, status=status)
                return
            out = (proc.stdout or b'').decode('utf-8', 'replace')
            m = SCAN_RES_OK_RE.search(out)
            n_total = int(m.group(1)) if m else 0
            _State.last_reload_ts = 0.0
            self._render_scan_result(biz_name, ok=True, n_total=n_total, error_lines=[out.strip()], accept_enc=accept_enc, status=200)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _render_scan_result(self, biz_name: str, *, ok: bool, n_total: int, error_lines: list[str], accept_enc: bytes, status: int) -> None:
        """The small result page returned after POST /<business>/scan (a green/red panel + a back link)."""
        biz_quoted = urllib.parse.quote(biz_name)
        back_href = '/' + biz_quoted
        cls = 'ok' if ok else 'err'
        if ok:
            stdout = error_lines[0] if error_lines else ''
            biz_html = f'<b>{_e(biz_name)}</b>'
            count_html = f'<b>{n_total}</b>'
            body_inner = f'<h2>{_t("dashboard.scan_done")}</h2><p>{_t("dashboard.scan_result.written", {"biz": biz_html, "count": count_html})}</p><pre>{_e(stdout)}</pre>'
        else:
            body_inner = f'<h2>{_t("dashboard.scan_failed")}</h2>' + ''.join((f'<pre>{_e(line)}</pre>' for line in error_lines))
        _lang = get_lang()
        _html_lang = 'zh-CN' if _lang == 'zh' else 'en'
        page = f'<!doctype html>\n<html lang="{_html_lang}"><head><meta charset="utf-8">\n<title>{_t("dashboard.scan_result.page_title", {"biz": _e(biz_name)})}</title>\n<style>{_CSS}</style></head>\n<body>\n<header><h1>{_t("dashboard.scan_result.page_heading")}</h1>\n  <div class="meta">{_t("dashboard.scan_result.business_label", {"biz": _e(biz_name)})}</div></header>\n<main>\n  <div class="scan-result {cls}">\n    {body_inner}\n    <a class="btn primary" href="{_e(back_href)}">{_t("dashboard.common.back_to_business", {"biz": _e(biz_name)})}</a>\n    <a class="btn" href="/" style="margin-left:6px">{_t("dashboard.common.back_to_overview")}</a>\n  </div>\n</main>\n</body></html>\n'
        self._respond(status, 'text/html; charset=utf-8', page.encode('utf-8'), accept_enc)

    def _dispatch_scan_urls(self, biz_name: str, accept_enc: bytes) -> None:
        """The entry validation of POST /<biz>/scan-urls + the dispatch to
        _handle_scan_urls.

        The business existence: the same as /scan, _State.cached_snap comes
        first and _load_snap is the fallback.
        """
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next((b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name') == biz_name), None)
        if bid is None:
            self._respond(404, 'text/plain', f'unknown business: {biz_name}\n'.encode(), accept_enc)
            return
        self._handle_scan_urls(biz_name, accept_enc)

    def _handle_scan_urls(self, biz_name: str, accept_enc: bytes) -> None:
        """The actual handling of POST /<business>/scan-urls.

        The body fields:
          hosts    = the subdomains, one per line (required)
          sources  = the multiple values ('ffuf' / 'urlfinder' / 'gau', at least 1)
          wordlist = the path of the ffuf dictionary (it may be empty, then
                     SCAN_URLS_DEFAULT_WORDLIST is used)
        """
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._respond(400, 'text/plain', b'bad Content-Length\n', accept_enc)
            return
        if length <= 0 or length > SCAN_URLS_MAX_BODY:
            self._respond(400, 'text/plain', f'body too large or empty ({length} bytes)\n'.encode(), accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond(400, 'text/plain', b'invalid utf-8 body\n', accept_enc)
            return
        raw_hosts = (fields.get('hosts', [''])[0] or '').strip()
        sources = [s.strip() for s in fields.get('sources', []) if s and s.strip()]
        wordlist_arg = (fields.get('wordlist', [''])[0] or '').strip()
        seen: set[str] = set()
        hosts: list[str] = []
        for line in raw_hosts.splitlines():
            s = line.strip().lower().rstrip('.')
            if not s or s.startswith('#'):
                continue
            if s in seen:
                continue
            seen.add(s)
            hosts.append(s)
        if not hosts:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.no_valid_domain")], accept_enc=accept_enc, status=400)
            return
        if len(hosts) > SCAN_URLS_MAX_HOSTS:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.too_many_domains", {"count": len(hosts), "max": SCAN_URLS_MAX_HOSTS})], accept_enc=accept_enc, status=400)
            return
        invalid = [h for h in hosts if not SCAN_HOSTNAME_RE.match(h)]
        if invalid:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.invalid_hostname_urls", {"hosts": ', '.join(invalid[:5])})], accept_enc=accept_enc, status=400)
            return
        if not sources:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.no_source")], accept_enc=accept_enc, status=400)
            return
        bad_src = [s for s in sources if s not in SCAN_URLS_VALID_SOURCES]
        if bad_src:
            self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.unknown_source", {"source": bad_src, "valid": list(SCAN_URLS_VALID_SOURCES)})], accept_enc=accept_enc, status=400)
            return
        seen_s = set()
        sources = [s for s in sources if not (s in seen_s or seen_s.add(s))]
        (tmp_fd, tmp_name) = tempfile.mkstemp(prefix='scan-urls-hosts-', suffix='.txt', dir='/tmp')
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                f.write('\n'.join(hosts) + '\n')
            pdtm_script = _State.db_path.parent.parent / 'pdtm' / 'scan_urls.py'
            cmd = ['/usr/bin/python3', str(pdtm_script), 'scan-urls', '--business', biz_name, '--hosts-file', str(tmp_path), '--db', str(_State.db_path), '--sources', ','.join(sources)]
            if wordlist_arg:
                cmd.extend(['--wordlist', wordlist_arg])
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=SCAN_URLS_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.subprocess_timeout_urls", {"timeout": SCAN_URLS_TIMEOUT})], accept_enc=accept_enc, status=504)
                return
            except FileNotFoundError as e:
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[_t("dashboard.scan_result.python_start_failed_urls", {"error": e})], accept_enc=accept_enc, status=500)
                return
            if proc.returncode != 0:
                err = (proc.stderr or b'').decode('utf-8', 'replace').strip() or f'exit={proc.returncode}'
                status = 400 if proc.returncode == 2 else 500
                self._render_scan_result(biz_name, ok=False, n_total=0, error_lines=[err[:800]], accept_enc=accept_enc, status=status)
                return
            out = (proc.stdout or b'').decode('utf-8', 'replace')
            n_total = 0
            for line in out.splitlines():
                m = SCAN_URLS_RES_OK_RE.search(line)
                if m:
                    n_total = int(m.group(1))
                    break
            _State.last_reload_ts = 0.0
            self._render_scan_result(biz_name, ok=True, n_total=n_total, error_lines=[out.strip() + '\n\n' + _t("dashboard.scan_result.hint_details")], accept_enc=accept_enc, status=200)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _handle_schedule_toggle(self, biz_name: str, accept_enc: bytes) -> None:
        """POST /<business name>/schedule/toggle — the user decided on
        2026-08-26.

        The body fields (x-www-form-urlencoded):
          subdomain = the subdomain (required, the FQDN format, validated by
                      SCAN_HOSTNAME_RE)
          action    = 'add' / 'remove' (required)

        The semantics:
          - add    -> INSERT INTO web_subdomain_scan_schedule(biz_id, sub,
                     sources='urlfinder')
                     when the same (biz_id, sub) already exists it is left alone
                     (idempotent) and it returns ok=1 / created=0
          - remove -> DELETE FROM web_subdomain_scan_schedule WHERE biz_id & sub
                     a missing row also counts as ok (idempotent)

        It returns plain JSON (200 / 400 / 404) and the front end JS parses it,
        hints the user and reloads the page.
        The cache is not reloaded: the schedule is not in _State.cached_snap
        (the configuration is not in the snapshot),
        so it takes effect the next time _serve_urls_detail queries the schedule
        table.
        """
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._respond_json(400, {'error': 'bad Content-Length'}, accept_enc)
            return
        if length <= 0 or length > SCAN_URLS_MAX_BODY:
            self._respond_json(400, {'error': f'body too large or empty ({length})'}, accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond_json(400, {'error': 'invalid utf-8 body'}, accept_enc)
            return
        subdomain = (fields.get('subdomain', [''])[0] or '').strip().lower().rstrip('.')
        action = (fields.get('action', [''])[0] or '').strip().lower()
        if not subdomain or not SCAN_HOSTNAME_RE.match(subdomain):
            self._respond_json(400, {'error': f'invalid subdomain: {subdomain!r}'}, accept_enc)
            return
        if action not in ('add', 'remove'):
            self._respond_json(400, {'error': f'action must be add|remove (got {action!r})'}, accept_enc)
            return
        conn = sqlite3.connect(str(_State.db_path), timeout=5)
        try:
            bid_row = conn.execute('SELECT id FROM businesses WHERE TRIM(business_name)=?', (biz_name,)).fetchone()
            if not bid_row:
                self._respond_json(404, {'error': f'unknown business: {biz_name}'}, accept_enc)
                return
            bid = int(bid_row[0])
            if action == 'add':
                cur = conn.execute("\n                    INSERT OR IGNORE INTO web_subdomain_scan_schedule\n                      (business_id, subdomain, sources, enabled)\n                    VALUES (?, ?, 'urlfinder', 1)\n                ", (bid, subdomain))
                created = 1 if cur.rowcount > 0 else 0
                self._respond_json(200, {'ok': True, 'subdomain': subdomain, 'action': 'add', 'created': created, 'schedule_state': 'enabled'}, accept_enc)
            else:
                cur = conn.execute('\n                    DELETE FROM web_subdomain_scan_schedule\n                     WHERE business_id = ? AND subdomain = ?\n                ', (bid, subdomain))
                removed = cur.rowcount
                self._respond_json(200, {'ok': True, 'subdomain': subdomain, 'action': 'remove', 'removed': removed, 'schedule_state': 'absent'}, accept_enc)
            conn.commit()
        except sqlite3.Error as e:
            self._respond_json(500, {'error': f'db error: {e}'}, accept_enc)
        finally:
            conn.close()

    def _respond_json(self, status: int, payload: dict, accept_enc: bytes) -> None:
        """The JSON response helper, only used by _handle_hash_edit / _handle_schedule_toggle."""
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self._respond(status, 'application/json; charset=utf-8', body, accept_enc)

    def _serve_urls_detail(self, biz_name: str, hash_id_str: str, accept_enc: bytes) -> None:
        """GET /<business name>/urls/<hash_id> — queries web_hash_urls live and
        renders the URL details page.

        The user decided: strictly lazy, the SQL is only queried when this page
        is opened, it is not preloaded in the reload snapshot.
        """
        try:
            hash_id = int(hash_id_str)
        except ValueError:
            self._respond(400, 'text/plain', b'bad hash id\n', accept_enc)
            return
        if hash_id <= 0:
            self._respond(404, 'text/plain', b'hash not found\n', accept_enc)
            return
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next((b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name') == biz_name), None)
        if bid is None:
            self._respond(404, 'text/plain', f'unknown business: {biz_name}\n'.encode(), accept_enc)
            return
        hash_meta = next((h for h in snap.get('tables', {}).get('web_hashes', []) if h.get('id') == hash_id and h.get('business_id') == bid), None)
        if hash_meta is None:
            self._respond(404, 'text/plain', f'hash #{hash_id} not in {biz_name}\n'.encode(), accept_enc)
            return
        try:
            conn = sqlite3.connect(str(_State.db_path), timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                sub_row = conn.execute('SELECT subdomain, port FROM web_subdomains WHERE hash_id = ? ORDER BY is_active DESC, subdomain LIMIT 1', (hash_id,)).fetchone()
                sample_subdomain = sub_row['subdomain'] if sub_row else None
                sample_port = sub_row['port'] if sub_row else None
                rows = conn.execute('SELECT id, hash_id, subdomain, source, scheme, host,        port, path, url, status_code, content_type,        content_length, word_count,        redirect, link_source, risk_flag,        is_dangerous, danger_reason,        first_seen, last_seen, fetched_at, is_active,        is_static, change_type   FROM web_hash_urls  WHERE hash_id = ? AND is_active = 1  ORDER BY source, path, url', (hash_id,)).fetchall()
                if sample_subdomain:
                    sched_row = conn.execute('SELECT enabled, last_run_at FROM web_subdomain_scan_schedule WHERE business_id = ? AND subdomain = ? LIMIT 1', (bid, sample_subdomain)).fetchone()
                    if sched_row:
                        schedule_enabled = int(sched_row['enabled'] or 0)
                        schedule_state = 'enabled' if schedule_enabled else 'disabled'
                        last_scan_at = sched_row['last_run_at']
                    else:
                        schedule_state = 'absent'
                        last_scan_at = None
                else:
                    schedule_state = 'absent'
                    last_scan_at = None
                if not sample_subdomain and rows:
                    sample_subdomain = rows[0]['subdomain'] or None
                    sample_port = rows[0]['port'] if rows else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._respond(500, 'text/plain', f'db error: {e}\n'.encode(), accept_enc)
            return
        from collections import defaultdict
        by_source: dict[str, list] = defaultdict(list)
        for r in rows:
            by_source[r['source']].append(dict(r))
        per_source_count = {s: len(by_source.get(s, [])) for s in SCAN_URLS_VALID_SOURCES}
        url_count = hash_meta.get('url_count', len(rows))

        def _status_risk(code):
            if code is None:
                return 'info'
            if 500 <= code < 600:
                return 'high'
            if 400 <= code < 500:
                return 'med'
            if 300 <= code < 400:
                return 'info'
            return 'low'

        def _schedule_label(state):
            labels = {
                'enabled': _t("dashboard.urls.schedule_enabled"),
                'disabled': _t("dashboard.urls.schedule_disabled"),
                'absent': _t("dashboard.urls.schedule_absent"),
            }
            return labels.get(state, _t("dashboard.urls.schedule_unknown"))
        SOURCE_DISPLAY_ORDER = ('urlfinder', 'ffuf', 'gau')
        SOURCE_HEADING = {
            'urlfinder': _t("dashboard.urls.source_heading_urlfinder"),
            'ffuf': _t("dashboard.urls.source_heading_ffuf"),
            'gau': _t("dashboard.urls.source_heading_gau"),
        }
        COMMON_COLS = (('URL', 'url', 'a'), ('path', 'path', 'text'), ('status', 'status_code', 'num'), ('bytes', 'content_length', 'num'), ('host:port', '_hostport', 'host'), ('risk', 'risk_flag', 'risk'), ('danger', '_danger', 'danger'), ('last_seen', 'fetched_at', 'time'))
        URLFINDER_EXTRA_COLS = (('title', 'title', 'text'), ('words', 'word_count', 'num'), ('redirect', 'redirect', 'text'), ('link_source', 'link_source', 'text'))
        FFUF_GAU_EXTRA_COLS = (('words', 'word_count', 'num'),)

        def _trunc(html_inner, css_class=''):
            """Wrap the already rendered cell content in <div class="trunc">, which triggers the scrollable behaviour."""
            extra = f' {css_class}' if css_class else ''
            return f'<div class="trunc{extra}">{html_inner}</div>'

        def _render_cell(kind, key, e):
            """Return the full <td>...</td> of a single cell.

            kind decides the style (a/text/num/host/time/risk/danger);
            key decides which column the value comes from — the same kind (such as
            num) takes a different field in the status / bytes / words
            columns, so the parameterisation avoids the bug.
            """
            url = e.get('url') or ''
            path = e.get('path') or ''
            host = e.get('host') or ''
            port = e.get('port')
            content_length = e.get('content_length')
            word_count = e.get('word_count')
            fetched_at = e.get('fetched_at') or ''
            redirect = e.get('redirect') or ''
            link_source = e.get('link_source') or ''
            risk_flag = e.get('risk_flag') or ''
            is_dangerous = e.get('is_dangerous') or 0
            danger_reason = e.get('danger_reason') or ''
            title = e.get('title') or ''
            if kind == 'num':
                if key == 'status_code':
                    v = e.get('status_code')
                elif key == 'content_length':
                    v = content_length
                elif key == 'word_count':
                    v = word_count
                else:
                    v = None
                if v is None:
                    return f"""<td class="num">{_trunc('—')}</td>"""
                return f'<td class="num">{_trunc(str(v))}</td>'
            if kind == 'text':
                if key == 'path':
                    txt = path
                elif key == 'title':
                    txt = title
                elif key == 'redirect':
                    txt = redirect
                elif key == 'link_source':
                    txt = link_source
                else:
                    txt = ''
                if not txt:
                    return f"""<td class="muted">{_trunc('—')}</td>"""
                return f'<td>{_trunc(_e(txt))}</td>'
            if kind == 'a':
                url_a = f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(url)}</a>'
                return f'<td>{_trunc(url_a)}</td>'
            if kind == 'host':
                hp = _e(host) + (f':{port}' if port and port not in (80, 443) else '')
                return f'<td>{_trunc(hp)}</td>'
            if kind == 'time':
                return f'<td>{_trunc(_e(fetched_at))}</td>'
            if kind == 'risk':
                if risk_flag:
                    return f'<td class="risk-cell">{_trunc(_e(risk_flag))}</td>'
                return f"""<td class="muted">{_trunc('—')}</td>"""
            if kind == 'danger':
                if is_dangerous:
                    inner = '<span title="' + _e(danger_reason) + '">⚠ ' + (_e(danger_reason) if danger_reason else 'danger') + '</span>'
                    return f"""<td class="danger-cell">{_trunc(inner, 'danger-cell')}</td>"""
                return f"""<td class="muted">{_trunc('—')}</td>"""
            return f"<td>{_trunc('')}</td>"

        def _render_row(e, cols, src):
            st = e.get('status_code')
            sub = e.get('subdomain') or ''
            host = e.get('host') or ''
            p = (e.get('path') or '').lower()
            raw_is_static = e.get('is_static')
            if raw_is_static is None:
                is_static = 1 if p.endswith(('.js', '.css')) else 0
            else:
                is_static = int(raw_is_static)
            ct = e.get('change_type') or 0
            lsa = last_scan_at or ''
            row_class_extra = ''
            if not is_static and sub == sample_subdomain and (ct > 0):
                if ct & 1:
                    row_class_extra = ' row-new'
                elif ct & 2:
                    row_class_extra = ' row-changed'
                elif ct & 4:
                    row_class_extra = ' row-reactivated'
            return f'''<tr class="risk-{_status_risk(st)}{row_class_extra}" data-source="{_e(src)}" data-subdomain="{_e(sub)}" data-host="{_e(host)}" data-status="{(st if st is not None else '')}" data-static="{is_static}" data-change-type="{ct}" data-last-scan-at="{_e(lsa)}">''' + ''.join((_render_cell(kind, key, e) for (_label, key, kind) in cols)) + '</tr>'

        def _cols_for(src):
            if src == 'urlfinder':
                return COMMON_COLS + URLFINDER_EXTRA_COLS
            return COMMON_COLS + FFUF_GAU_EXTRA_COLS

        def _colgroup(cols):
            """Return the col element for a column type (the width is close to the 150px minimum of the ant-table default)."""
            parts = []
            for (label, _key, _kind) in cols:
                if label == 'URL':
                    parts.append('<col class="col-url">')
                elif label == 'path':
                    parts.append('<col class="col-path">')
                elif label == 'status':
                    parts.append('<col class="col-status">')
                elif label == 'bytes':
                    parts.append('<col class="col-bytes">')
                elif label == 'words':
                    parts.append('<col class="col-words">')
                elif label == 'host:port':
                    parts.append('<col class="col-host">')
                elif label == 'redirect':
                    parts.append('<col class="col-redirect">')
                elif label == 'link_source':
                    parts.append('<col class="col-link">')
                elif label == 'risk':
                    parts.append('<col class="col-risk">')
                elif label == 'danger':
                    parts.append('<col class="col-danger">')
                elif label == 'last_seen':
                    parts.append('<col class="col-last">')
                elif label == 'title':
                    parts.append('<col class="col-title">')
                else:
                    parts.append('<col>')
            return ''.join(parts)
        source_tables = []
        for src in SOURCE_DISPLAY_ORDER:
            entries = by_source.get(src, [])
            if not entries:
                continue
            cols = _cols_for(src)

            def _th(label, key, kind):
                if key in ('status_code', 'content_length'):
                    return f'<th class="sortable" data-col="{_e(key)}" data-kind="{_e(kind)}">{_e(label)}</th>'
                return f'<th>{_e(label)}</th>'
            head = ''.join((_th(label, key, kind) for (label, key, kind) in cols))
            rows_html = ''.join((_render_row(e, cols, src) for e in entries)) if entries else f'<tr><td colspan="{len(cols)}" class="muted">{_t("dashboard.urls.no_rows")}</td></tr>'
            heading = SOURCE_HEADING.get(src, src)
            tbl_html = f'<h3 class="src-heading">{_e(heading)} <span class="muted">· {_e(src)} · {len(entries):,} {_t("dashboard.urls.count_unit")}</span></h3><div class="ant-table-wrapper"><div class="ant-table"><table class="urls-table"><thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table></div></div>'
            source_tables.append(tbl_html)
        biz_quoted = urllib.parse.quote(biz_name)
        back_href = f'/{biz_quoted}'
        _sample_sub_json = json.dumps(sample_subdomain)
        _last_scan_json = json.dumps(last_scan_at or '')
        _biz_quoted_json = json.dumps(biz_quoted)
        _lang = get_lang()
        _html_lang = 'zh-CN' if _lang == 'zh' else 'en'
        page = f'''<!doctype html>\n<html lang="{_html_lang}"><head><meta charset="utf-8">\n<title>{_t("dashboard.urls.page_title", {"hash_id": hash_id, "biz": _e(biz_name)})}</title>\n<style>{_CSS}</style>\n#<style>\n/* URL detail page: the URLFinder ant-table look (distilled from demo.html, ~3KB, without the full 3.8MB Ant Design CSS) */\n.ant-table-wrapper {{ zoom:1; }}\n.ant-table-wrapper::before, .ant-table-wrapper::after {{ display:table; content:""; }}\n.ant-table-wrapper::after {{ clear:both; }}\n.ant-table {{\n  -webkit-box-sizing:border-box; box-sizing:border-box; margin:0; padding:0;\n  color:rgba(0,0,0,.65); font-size:14px; font-variant:tabular-nums;\n  line-height:1.5; list-style:none; position:relative; clear:both;\n}}\n.urls-table {{\n  width:100%; text-align:left; border-radius:4px 4px 0 0;\n  border-collapse:separate; border-spacing:0;\n  word-wrap:break-word; word-break:break-all;\n  font-variant:tabular-nums; -webkit-font-feature-settings:"tnum";\n  font-feature-settings:"tnum";\n  /* the column width follows its longest cell (adaptive); the numeric columns get a min-width so they cannot get too narrow */\n  table-layout:auto;\n}}\n.urls-table th, .urls-table td {{\n  border-bottom:1px solid #e8e8e8; padding:8px 12px;\n  color:rgba(0,0,0,.65); font-size:13px;\n  /* long content is shown over several lines - no nowrap and no horizontal scrolling; a long URL/path wraps on its own */\n  white-space:normal; word-break:break-all; vertical-align:top;\n}}\n.urls-table thead > tr > th {{\n  background:#fafafa; font-weight:500; text-align:left; color:rgba(0,0,0,.85);\n  border-bottom:1px solid #e8e8e8;\n  -webkit-transition:background .3s ease; transition:background .3s ease;\n  position:relative; white-space:nowrap; /* the header does not wrap, it looks tidy */\n}}\n/* the ant-table header separator */\n.urls-table thead > tr > th:not(:last-child)::after {{\n  content:""; position:absolute; right:0; top:8px; bottom:8px;\n  width:1px; background:rgba(0,0,0,.06);\n}}\n.urls-table tbody > tr:not(:last-child) > td {{ border-bottom:1px solid #eaecef; }}\n.urls-table tbody > tr:hover > td {{ background:#fafafa; }}\n.urls-table tbody > tr > td .mono {{ font-family:monospace; }}\n.urls-table tbody > tr > td.num,\n.urls-table td.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}\n/* the numeric column min-width is the safety net - with table-layout:auto an empty string is squeezed to 0;\n   the user decided: the values of the status/bytes/words columns are never long, 40~70px is enough */\n.urls-table th.col-num, .urls-table td.num {{ min-width:48px; }}\n/* .trunc is now only an empty container wrapper (a max-height can be added later if needed),\n   it no longer forces nowrap + overflow; the content wraps on its own */\n.urls-table .trunc {{\n  display:block; max-width:100%;\n  /* no more overflow-x:auto; a long URL spans several lines */\n}}\n/* the clickable header (the status / bytes columns only) */\n.urls-table th.sortable {{\n  cursor:pointer; user-select:none;\n  -webkit-user-select:none;\n  transition:background .15s ease;\n}}\n.urls-table th.sortable:hover {{ background:#f0f0f0; }}\n.urls-table th.sortable::after {{\n  content:"⇅"; opacity:.35; margin-left:6px; font-size:11px;\n  display:inline-block;\n}}\n.urls-table th.sortable.sort-asc::after  {{ content:"▲"; opacity:1; color:#0969da; }}\n.urls-table th.sortable.sort-desc::after {{ content:"▼"; opacity:1; color:#0969da; }}\n/* the row background of the risk levels (close to the ant-table token) */\n.urls-table tbody > tr.risk-high > td {{ background:#fff1f0; }}\n.urls-table tbody > tr.risk-high:hover > td {{ background:#ffccc7; }}\n.urls-table tbody > tr.risk-med  > td {{ background:#fffbe6; }}\n.urls-table tbody > tr.risk-med:hover  > td {{ background:#fff1b8; }}\n.urls-table tbody > tr.risk-info > td {{ background:#fafafa; }}\n.urls-table tbody > tr.risk-low  > td {{ background:#fcffe6; }}\n.urls-table td.danger-cell {{\n  background:#ffccc7; color:#cf222e; font-weight:600;\n  border-left:3px solid #cf222e;\n}}\n.urls-table td.risk-cell {{\n  background:#fffbe6; color:#d4380d; font-weight:500;\n}}\nh3.src-heading {{\n  margin:18px 0 8px 0; font-size:15px; font-weight:500; color:rgba(0,0,0,.85);\n}}\n.urls-summary {{ display:flex; gap:14px; margin:8px 0 16px; flex-wrap:wrap;\n                  align-items:center; }}\n.urls-summary .chip {{\n  background:#fafafa; border:1px solid #e8e8e8; border-radius:4px;\n  padding:4px 10px; font-size:13px; color:rgba(0,0,0,.65);\n}}\n.urls-filter {{ display:flex; gap:14px; margin:0 0 16px 0; padding:8px 12px;\n                background:#fafafa; border:1px solid #e8e8e8; border-radius:4px;\n                align-items:center; font-size:13px; }}\n.urls-filter label {{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }}\n.urls-filter input[type=checkbox] {{ cursor:pointer; }}\n.urls-filter .sample-sub {{ color:#57606a; margin-left:8px; }}\n\n/* decided by the user on 2026-08-26: the default colouring and the schedule button style.\n   row-new  = change_type & 1 (new) - light green background\n   row-changed = change_type & 2 (content changed) - light blue background\n   row-reactivated = change_type & 4 / 6 (reactivated) - light orange background\n   these classes are only added by _serve_urls_detail to the rows that match\n   the current subdomain && are not static && have change_type>0; by default\n   every user sees them */\n.urls-table tbody > tr.row-new > td {{ background:#f6ffed; }}\n.urls-table tbody > tr.row-new:hover > td {{ background:#d9f7be; }}\n.urls-table tbody > tr.row-changed > td {{ background:#e6f7ff; }}\n.urls-table tbody > tr.row-changed:hover > td {{ background:#bae7ff; }}\n.urls-table tbody > tr.row-reactivated > td {{ background:#fff7e6; }}\n.urls-table tbody > tr.row-reactivated:hover > td {{ background:#ffe7ba; }}\n\n/* the schedule chip and its button */\n.schedule-chip {{ font-weight:500; }}\n.schedule-chip.schedule-enabled {{ background:#f6ffed; border-color:#b7eb8f;\n                                     color:#389e0d; }}\n.schedule-chip.schedule-disabled {{ background:#fffbe6; border-color:#ffe58f;\n                                      color:#d48806; }}\n.schedule-chip.schedule-absent {{ background:#fafafa; border-color:#d9d9d9;\n                                    color:#8c8c8c; }}\n.schedule-toggle-btn {{ margin-left:8px; }}\n.schedule-toggle-btn:hover {{ background:#0969da; color:#fff; border-color:#0969da; }}\n</style>\n</head>\n<body>\n<header><h1>{_t("dashboard.urls.page_heading", {"hash_id": hash_id})}</h1>\n  <div class="meta">{_t("dashboard.urls.meta_business", {"biz": _e(biz_name)})} · {_t("dashboard.urls.meta_source_subdomain", {"sub": _e(sample_subdomain)})}\n    {(f':{sample_port}' if sample_port and sample_port not in (80, 443) else '')}\n    · response_hash: <code>{_e(str(hash_meta.get('response_hash')))}</code>\n    · subdomain_count: {hash_meta.get('subdomain_count', '—')}\n    · url_count({_t("dashboard.urls.label_active")}): <b>{url_count:,}</b></div></header>\n<main>\n  <div class="urls-summary">\n    <span class="chip">ffuf: {per_source_count.get('ffuf', 0):,}</span>\n    <span class="chip">urlfinder: {per_source_count.get('urlfinder', 0):,}</span>\n    <span class="chip">gau: {per_source_count.get('gau', 0):,}</span>\n    <span class="chip">{_t("dashboard.urls.chip_total")} {len(rows):,}</span>\n    <span class="chip schedule-chip schedule-{schedule_state}">{_t("dashboard.urls.chip_daily_scan", {"state": _schedule_label(schedule_state)})}</span>\n    {(f"""<button class="btn schedule-toggle-btn" id="schedule-toggle-btn" data-action="{('remove' if schedule_state == 'enabled' else 'add')}">{(_t('dashboard.urls.schedule_remove') if schedule_state == 'enabled' else _t('dashboard.urls.schedule_add'))}</button>""" if sample_subdomain else '')}\n  </div>\n  <div class="urls-filter">\n    <label><input type="checkbox" id="only-this-sub"> {_t("dashboard.urls.filter_only_this_subdomain")}</label>\n    <label><input type="checkbox" id="only-200"> {_t("dashboard.urls.filter_only_200")}</label>\n    <label><input type="checkbox" id="hide-static"> {_t("dashboard.urls.filter_hide_static")}</label>\n    <label><input type="checkbox" id="only-new-changed"> {_t("dashboard.urls.filter_only_new_changed")}</label>\n    <span class="sample-sub">{_t("dashboard.urls.filter_source_subdomain")} <code>{_e(sample_subdomain)}</code></span>\n  </div>\n  {(''.join(source_tables) if source_tables else '<p class="muted">' + _t("dashboard.urls.no_records") + '</p>')}\n  <p style="margin-top:16px;">\n    <a class="btn primary" href="{_e(back_href)}">{_t("dashboard.common.back_to_business", {"biz": _e(biz_name)})}</a>\n    <a class="btn" href="/" style="margin-left:6px">{_t("dashboard.common.back_to_overview")}</a>\n  </p>\n</main>\n<script>\n(function() {{\n  // client side filtering - pure DOM, no network round trip\n  // the data source: every row <tr data-subdomain data-status>; sample_subdomain is injected inline while the page is rendered\n  var sampleSub = {_sample_sub_json};\n  var lastScanAt = {_last_scan_json};\n  var bizName = {_biz_quoted_json};\n  var cbSub = document.getElementById('only-this-sub');\n  var cb200 = document.getElementById('only-200');\n  var cbStatic = document.getElementById('hide-static');\n  var cbNewChanged = document.getElementById('only-new-changed');\n\n  function applyFilters() {{\n    var onlySub = cbSub.checked;\n    var only200 = cb200.checked;\n    var hideStatic = cbStatic && cbStatic.checked;\n    var onlyNewChanged = cbNewChanged && cbNewChanged.checked;\n    var rows = document.querySelectorAll('.urls-table tbody tr[data-subdomain]');\n    rows.forEach(function(tr) {{\n      var host = tr.getAttribute('data-host') || '';\n      var st  = tr.getAttribute('data-status') || '';\n      var isStatic = tr.getAttribute('data-static') === '1';\n      var ct = parseInt(tr.getAttribute('data-change-type') || '0', 10);\n      var lsa = tr.getAttribute('data-last-scan-at') || '';\n      var ok = true;\n      // "only this subdomain" -> the real host of the URL === sample_subdomain (drops the junk hosts such as test-internal)\n      if (onlySub && host !== sampleSub) ok = false;\n      if (only200 && st !== '200') ok = false;\n      if (hideStatic && isStatic) ok = false;\n      // decided by the user on 2026-08-26: "only the new or the changed ones" (a separate toggle -\n    // the user decided to split it, it does not follow "exclude .js / .css"). The meaning: the\n    // current subdomain + not js/css + (new / content changed).\n    //   ct & 1 -> triggered by an INSERT; ct & 2 -> triggered by an AU; both >0 mean that this round changed something\n    //   ls > lsa is the fallback (if the schedule has no last_run_at it never matches -> safe)\n      if (onlyNewChanged) {{\n        if (host !== sampleSub) ok = false;\n        if (isStatic) ok = false;\n        if (!((ct & 1) || (ct & 2))) ok = false;\n      }}\n      tr.style.display = ok ? '' : 'none';\n    }});\n    // sync the visible row count of every table into its h3 title\n    // the DOM structure: the <h3 class="src-heading"> is the previousElementSibling of <table>.parentElement.parentElement\n    document.querySelectorAll('.urls-table').forEach(function(tbl) {{\n      var wrapper = tbl.parentElement && tbl.parentElement.parentElement;\n      var h = wrapper && wrapper.previousElementSibling;\n      if (!h || !h.classList.contains('src-heading')) return;\n      var n = 0, total = 0;\n      tbl.querySelectorAll('tbody tr[data-subdomain]').forEach(function(tr) {{\n        total++;\n        if (tr.style.display !== 'none') n++;\n      }});\n      var chip = h.querySelector('.count-chip');\n      if (!chip) {{\n        chip = document.createElement('span');\n        chip.className = 'count-chip muted';\n        chip.style.marginLeft = '8px';\n        h.appendChild(chip);\n      }}\n      chip.textContent = '{_t("dashboard.urls.visible_count_prefix")} ' + n + '/' + total + ' {_t("dashboard.urls.count_unit")}';\n    }});\n  }}\n\n  cbSub.addEventListener('change', applyFilters);\n  cb200.addEventListener('change', applyFilters);\n  if (cbStatic) cbStatic.addEventListener('change', applyFilters);\n  if (cbNewChanged) cbNewChanged.addEventListener('change', applyFilters);\n\n  // decided by the user on 2026-08-26: the schedule button -> POST /<biz>/schedule/toggle\n  var schedBtn = document.getElementById('schedule-toggle-btn');\n  if (schedBtn) {{\n    schedBtn.addEventListener('click', function() {{\n      var action = schedBtn.getAttribute('data-action') || 'add';\n      schedBtn.disabled = true;\n      var origText = schedBtn.textContent;\n      schedBtn.textContent = action === 'add' ? '{_t("dashboard.urls.schedule_adding")}' : '{_t("dashboard.urls.schedule_removing")}';\n      var body = 'subdomain=' + encodeURIComponent(sampleSub) +\n                 '&action=' + encodeURIComponent(action);\n      fetch('/' + bizName + '/schedule/toggle', {{\n        method: 'POST',\n        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},\n        body: body\n      }}).then(function(r) {{\n        return r.json().then(function(j) {{ return {{status: r.status, body: j}}; }});\n      }}).then(function(res) {{\n        if (res.status === 200 && res.body.ok) {{\n          // after the success it tells the user and reloads the page, showing the new schedule state and the button text\n          alert((action === 'add' ? '{_t("dashboard.urls.schedule_added")}\\n' : '{_t("dashboard.urls.schedule_removed")}\\n') +\n                '{_t("dashboard.urls.label_subdomain")}' + sampleSub);\n          location.reload();\n        }} else {{\n          alert('{_t("dashboard.urls.operation_failed")} ' + (res.body.error || JSON.stringify(res.body)));\n          schedBtn.disabled = false;\n          schedBtn.textContent = origText;\n        }}\n      }}).catch(function(e) {{\n        alert('{_t("dashboard.urls.network_error")} ' + e);\n        schedBtn.disabled = false;\n        schedBtn.textContent = origText;\n      }});\n    }});\n  }}\n\n  // header sorting: every table keeps its own asc/desc/none state. A click calls sortTable;\n  // status/bytes are numeric columns, they are compared as numbers; the empty values (None / -) go last.\n  function sortValue(tr, key) {{\n    if (key === 'status_code') {{\n      var s = tr.getAttribute('data-status');\n      return s === '' || s == null ? null : Number(s);\n    }}\n    if (key === 'content_length') {{\n      // bytes: the tr does not store data-bytes, the text of the 4th td is read (the column order is fixed)\n      var tds = tr.querySelectorAll('td');\n      // column order: URL, path, status, bytes, host:port, ...\n      var td = tds[3];\n      var txt = td ? td.textContent.trim() : '';\n      if (txt === '—' || txt === '') return null;\n      var n = Number(txt.replace(/,/g, ''));\n      return isNaN(n) ? null : n;\n    }}\n    return null;\n  }}\n\n  function sortTable(tbl, key, dir) {{\n    // dir: 'asc' | 'desc' | null (back to the original order)\n    var tbody = tbl.tBodies[0];\n    if (!tbody) return;\n    var trs = Array.from(tbody.querySelectorAll('tr[data-host]'));\n    if (dir === null) {{\n      // sort by the initial order of the tr in the DOM (the original order) - there is no original index,\n      // the dataset._idx written during the first render is used instead\n      trs.sort(function(a, b) {{\n        return (Number(a.dataset._idx) || 0) - (Number(b.dataset._idx) || 0);\n      }});\n    }} else {{\n      trs.sort(function(a, b) {{\n        var va = sortValue(a, key);\n        var vb = sortValue(b, key);\n        // the null values go last (whatever asc/desc is)\n        if (va == null && vb == null) return 0;\n        if (va == null) return 1;\n        if (vb == null) return -1;\n        return dir === 'asc' ? va - vb : vb - va;\n      }});\n    }}\n    // reorder the DOM\n    var frag = document.createDocumentFragment();\n    trs.forEach(function(tr) {{ frag.appendChild(tr); }});\n    tbody.appendChild(frag);\n    // sync the th look\n    tbl.querySelectorAll('th.sortable').forEach(function(th) {{\n      th.classList.remove('sort-asc', 'sort-desc');\n      if (th.getAttribute('data-col') === key && dir) {{\n        th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');\n      }}\n    }});\n  }}\n\n  document.querySelectorAll('.urls-table').forEach(function(tbl) {{\n    // tag every row with idx (used to restore the original order for sort=null)\n    Array.from(tbl.tBodies[0].querySelectorAll('tr[data-host]')).forEach(function(tr, i) {{\n      tr.dataset._idx = String(i);\n    }});\n    tbl.querySelectorAll('th.sortable').forEach(function(th) {{\n      th.addEventListener('click', function() {{\n        var key = th.getAttribute('data-col');\n        var cur = th.classList.contains('sort-asc') ? 'asc'\n                 : th.classList.contains('sort-desc') ? 'desc' : null;\n        var next = (cur === 'asc') ? 'desc' : (cur === 'desc') ? null : 'asc';\n        sortTable(tbl, key, next);\n      }});\n    }});\n  }});\n\n  applyFilters();\n}})();\n</script>\n</body></html>\n'''
        self._respond(200, 'text/html; charset=utf-8', page.encode('utf-8'), accept_enc)
        log.info(f'urls detail biz={biz_name} hash_id={hash_id} rows={len(rows)} per_source={per_source_count}')

    def _serve_body_raw(self, biz_name: str, raw_query: str, accept_enc: bytes) -> None:
        """GET /<business name>/body/raw?url=<domain-url>&side=pre|cur

        It returns the raw HTTP body that was stored for one side of that
        subdomain (after the zlib decompression), and the browser
        renders it according to the Content-Type. side=pre takes the version in
        the daily run snapshot,
        side=cur takes the version the latest import wrote.
        """
        def _fail(status: int, msg: str) -> None:
            self._respond(status, 'text/plain; charset=utf-8', msg.encode('utf-8'), accept_enc)

        params = urllib.parse.parse_qs(raw_query)
        side = (params.get('side') or [''])[0].strip().lower()
        url_raw = (params.get('url') or [''])[0].strip()
        if side not in ('pre', 'cur'):
            _fail(400, 'bad side: expected "pre" or "cur"\n')
            return
        if not url_raw:
            _fail(400, 'missing url parameter\n')
            return
        parsed = _parse_body_url(url_raw)
        if parsed is None:
            _fail(400, 'cannot parse url parameter\n')
            return
        host, explicit_port, default_port = parsed

        try:
            conn = sqlite3.connect(str(_State.db_path), timeout=5)
        except sqlite3.Error as e:
            _fail(500, f'db error: {e}\n')
            return
        try:
            conn.row_factory = sqlite3.Row
            # the business visibility follows the judgement rule: enabled=1 or there is no config row at all.
            biz = conn.execute(
                "SELECT b.id FROM businesses b "
                "LEFT JOIN recon_business_config c ON c.business_id = b.id "
                "WHERE b.business_name = ? AND (c.enabled = 1 OR c.business_id IS NULL)",
                (biz_name,),
            ).fetchone()
            if biz is None:
                _fail(404, 'unknown or disabled business\n')
                return
            bid = biz['id']
            rows = conn.execute(
                "SELECT ws.id, ws.subdomain, ws.port, ws.content_type, ws.curr_body, ws.fetched_at, "
                "       pre.curr_body AS pre_body, pre.content_type AS pre_content_type, "
                "       pre.fetched_at AS pre_fetched_at, pre._snapshot_at AS pre_snapshot_at "
                "FROM web_subdomains ws "
                "LEFT JOIN web_subdomains_pre pre "
                "  ON pre.id = ws.id AND pre.business_id = ws.business_id "
                " AND pre.subdomain = ws.subdomain AND pre.port = ws.port "
                "WHERE ws.business_id = ? AND ws.subdomain = ?",
                (bid, host),
            ).fetchall()
        except sqlite3.Error as e:
            _fail(500, f'db error: {e}\n')
            return
        finally:
            conn.close()

        if not rows:
            _fail(404, 'no such subdomain in this business\n')
            return

        # the port fallback chain: an explicit port matches exactly -> an inferred
        # port tries the scheme default first and then the other
        # default port -> when the subdomain has only one row under this business
        # that row is used directly.
        match: list[Any] = []
        if explicit_port is not None:
            match = [r for r in rows if r['port'] == explicit_port]
        else:
            for cand in (default_port, 443 if default_port == 80 else 80):
                match = [r for r in rows if r['port'] == cand]
                if match:
                    break
        if not match and len(rows) == 1:
            match = rows
        if not match:
            _fail(404, 'no row for that subdomain and port\n')
            return
        row = match[0]

        if side == 'cur':
            blob = row['curr_body']
            stored_ct = row['content_type']
            fetched_at = row['fetched_at']
            snapshot_at = ''
        else:
            blob = row['pre_body']
            stored_ct = row['pre_content_type']
            fetched_at = row['pre_fetched_at']
            snapshot_at = row['pre_snapshot_at'] or ''
        if blob is None:
            if side == 'pre':
                _fail(404, 'no pre body for this row (not in the last daily snapshot)\n')
            else:
                _fail(404, 'no current body for this row\n')
            return
        try:
            data = zlib.decompress(bytes(blob))
        except zlib.error:
            # the same convention as algo_diff/compute.py::_decompress: when it
            # cannot be decompressed it is treated as not compressed
            # raw text (the blob may be uncompressed / corrupted). The bytes are
            # returned as they are when it is rendered.
            data = bytes(blob)

        extra = {
            'Content-Security-Policy': _BODY_RAW_CSP,
            'Referrer-Policy': 'no-referrer',
            'X-Src-Side': side,
            'X-Src-Port': str(row['port']),
            'X-Src-Fetched-At': fetched_at or '',
        }
        if snapshot_at:
            extra['X-Src-Snapshot-At'] = _snapshot_at_iso(snapshot_at)
        self._respond(200, _body_content_type(stored_ct, data), data, accept_enc, extra_headers=extra)
        log.info(f'body raw biz={biz_name} side={side} port={row["port"]} bytes={len(data)}')

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info(f'dashboard {self.address_string()} {fmt % args}')

    def _render_pages(self, lang: str) -> dict:
        """Render the pages of one language and cache them.

        The pages are built with the language of the request, so they
        hold the text of that language. The cache holds one entry per
        language, and the entry is replaced when the data is reloaded.
        """
        agg = _aggregate(_State.cached_snap)
        html_home = render_html(agg, home=True, biz_name='')
        html_overview = render_html(agg, home=False, biz_name='')
        pages = {
            'home': html_home,
            'overview': html_overview,
            'gz_home': gzip.compress(html_home, compresslevel=6) if len(html_home) > 1024 else b'',
            'gz_overview': gzip.compress(html_overview, compresslevel=6) if len(html_overview) > 1024 else b'',
        }
        _State.cached_pages[lang] = pages
        return pages

    def _serve_page(self, accept_enc: bytes=b'', *, home: bool=True) -> None:
        self._maybe_reload()
        lang = get_lang()
        pages = _State.cached_pages.get(lang)
        if pages is None:
            pages = self._render_pages(lang)
        if home:
            (body, gz) = (pages['home'], pages['gz_home'])
        else:
            (body, gz) = (pages['overview'], pages['gz_overview'])
        self._respond(200, 'text/html; charset=utf-8', body, accept_enc, cached_gz=gz)

    def _apply_request_lang(self, raw_query: str) -> None:
        """Take the language of the request out of the query string.

        The language is a per-request setting: the dashboard serves the
        requests concurrently, so it belongs to the thread that serves
        the request and not to the process. The configuration stays the
        default when the query does not name a language.
        """
        want = ''
        if raw_query:
            params = urllib.parse.parse_qs(raw_query)
            values = params.get('lang') or []
            if values:
                want = values[0]
        if want:
            set_lang(want)

    def _serve_snapshot(self, accept_enc: bytes=b'') -> None:
        self._maybe_reload()
        if not _State.cached_snap_json:
            self._respond(503, 'text/plain', b'snapshot not loaded', accept_enc)
            return
        self._respond(200, 'application/json; charset=utf-8', _State.cached_snap_json, accept_enc)

    def _serve_diff(self, biz_name: str | None, accept_enc: bytes=b'') -> None:
        report_dir = _find_latest_report()
        if report_dir is None:
            self._respond(200, 'text/html; charset=utf-8', b'<html><body><h1>No reports yet</h1><p>Run <code>daily_monitor.sh</code> first.</p></body></html>', accept_enc)
            return
        snap = self._load_snap()
        biz_nav = self._build_biz_nav()
        changed_keys = _load_changed_keys(report_dir, snap)
        baseline_snap = snap
        if biz_name:
            bid = next((b['id'] for b in snap['tables'].get('businesses', []) if b.get('business_name') == biz_name), None)
            baseline_snap = _filter_snap_by_business(snap, bid)
            changed_keys = _changed_keys_for_business(changed_keys, bid, baseline_snap)
        filtered = _filter_snapshot(baseline_snap, changed_keys)
        agg = _aggregate(filtered)
        agg['hash_total'] = Counter((r.get('response_hash') or '' for r in baseline_snap.get('tables', {}).get('web_subdomains', [])))
        body = render_html(agg, mode='diff', run_id=report_dir.name, biz_name=biz_name or '', biz_nav=biz_nav)
        self._respond(200, 'text/html; charset=utf-8', body, accept_enc)

    def _build_biz_nav(self) -> list[dict]:
        """List of {name, href} for every business in the snapshot."""
        snap = self._load_snap()
        nav = []
        for b in snap.get('tables', {}).get('businesses', []):
            name = b.get('business_name') or ''
            if not name:
                continue
            nav.append({'name': name, 'href': f'/{urllib.parse.quote(name)}'})
        nav.sort(key=lambda x: x['name'])
        return nav

    def _serve_business(self, biz_name: str, is_new: bool, accept_enc: bytes=b'') -> None:
        snap = self._load_snap()
        bid = next((b['id'] for b in snap['tables'].get('businesses', []) if b.get('business_name') == biz_name), None)
        if bid is None:
            self._respond(404, 'text/plain', f'unknown business: {biz_name}'.encode(), accept_enc)
            return
        biz_nav = self._build_biz_nav()
        biz_snap = _filter_snap_by_business(snap, bid)
        hash_total = Counter((r.get('response_hash') or '' for r in biz_snap.get('tables', {}).get('web_subdomains', [])))
        if is_new:
            # 2026-10-24: /new filters directly on the change_type > 0 of the DB, it
            # does not depend on
            # the diff CSV under the reports/ directory any more. The reason:
            #   - the freshness: the report is only updated when daily_monitor has
            #     finished (once a day), so after a manual
            #     run of algo_diff_cpp the DB changed but the report did not ->
            #     /new did not reflect the newest data.
            #   - the resistance to poisoning: _find_latest_report() takes the
            #     first one by the reverse order of the directory names, so any
            #     action that pushes a directory into
            #     reports/ hijacks /new (an empty directory -> changed_keys
            #     is empty -> not a single row). The smoke test of 2026-10-24 left
            #     two directories under the production reports/
            #     directory, which made /<business>/new completely empty.
            # The classification follows the bitmask semantics of diff.py
            # (added/reactivated/changed).
            run_id = _latest_run_id()
            filtered = _filter_snapshot_by_ct(biz_snap)
            agg = _aggregate(filtered)
            agg['hash_total'] = hash_total
            body = render_html(agg, mode='diff', run_id=run_id, biz_name=biz_name, biz_nav=biz_nav)
        else:
            agg = _aggregate(biz_snap)
            agg['hash_total'] = hash_total
            body = render_html(agg, mode='overview', biz_name=biz_name, biz_nav=biz_nav)
        self._respond(200, 'text/html; charset=utf-8', body, accept_enc)

    def _maybe_reload(self) -> None:
        now = time.time()
        if now - _State.last_reload_ts < _State.reload_interval:
            return
        self._do_reload()

    def _do_reload(self) -> None:
        if _State.reload_lock is None:
            return
        with _State.reload_lock:
            try:
                snap = _load_snap_from_db(_State.db_path)
                agg = _aggregate(snap)
                snap_json = json.dumps(snap, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                row_total = sum(agg['row_counts'].values())
                # the pages are cached per language, so the reload drops
                # the cache of every language and each one is built again
                # on the next request
                _State.cached_pages = {}
                _State.cached_snap = snap
                _State.cached_snap_json = snap_json
                _State.last_reload_ts = time.time()
                del snap, snap_json
                import gc
                gc.collect()
                log.info(f'dashboard reloaded: {_State.db_path} ({row_total} rows; the pages are rendered per language on the next request)')
            except Exception as e:
                log.error(f'dashboard reload failed: {e}')
                if not _State.cached_pages:
                    err = f'<html><body><h1>Snapshot error</h1><pre>{_e(e)}</pre></body></html>'.encode()
                    _State.cached_pages = {
                        lang: {'home': err, 'overview': err, 'gz_home': b'', 'gz_overview': b''}
                        for lang in KNOWN_LANGS
                    }

    def _load_snap(self) -> dict:
        """Cached parse of the active snapshot; reads + caches on first call,
        returns the cached copy on subsequent calls (mtime-bumped refresh
        goes through _maybe_reload)."""
        self._maybe_reload()
        return _State.cached_snap

    def _respond(self, status: int, ctype: str, body: bytes, accept_enc: bytes=b'', *, cached_gz: bytes=b'', extra_headers: dict | None = None) -> None:
        self.send_response(status)
        if cached_gz and b'gzip' in accept_enc:
            body = cached_gz
            self.send_header('Content-Encoding', 'gzip')
        elif b'gzip' in accept_enc and len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for hdr_name, hdr_value in (extra_headers or {}).items():
            self.send_header(hdr_name, hdr_value)
        self.end_headers()
        self.wfile.write(body)

def _build_katana_enrich_form(biz_name: str) -> str:
    """The katana enrich collapsible form (added 2026-09-22). It posts to
    /<biz>/katana-enrich.

    It is at the same level as _build_scan_urls_form, but it only has one textbox
(the hosts), no sources / wordlist:
      - katana turns on js-crawl / form-extraction / xhr-extraction on its own
      - no dictionary is needed (katana is an active crawler)
      - the fields are aligned with httpx (see modules/public/katana/README.md
        section 3)

    The design goals:
      - only the active subdomains that already exist are scanned (the POST
        handler validates that)
      - the per-invocation host limit is tightened to 30 (katana is heavier than
        URLFinder and it times out with a big dictionary)
      - the 5 word risk blacklist is the one of URLFinder, katana gates it at
        the source with -crawl-out-scope and a second gate before the write
    """
    if not biz_name:
        return ''
    action = '/' + urllib.parse.quote(biz_name) + '/katana-enrich'
    return f'\n<details class="scan-form">\n  <summary>{_t("dashboard.katana_form.summary", {"biz": _e(biz_name)})}</summary>\n  <form method="post" action="{_e(action)}">\n    <div class="hint">{_t("dashboard.common.each_line")} <b>{_t("dashboard.common.existing_subdomain")}</b>{_t("dashboard.katana_form.hint_copy", {"max": KATANA_ENRICH_MAX_HOSTS})}\n    {_t("dashboard.common.scan_active_prefix")} <code>web_subdomains.is_active=1</code> {_t("dashboard.common.scan_active_suffix")}\n    {_t("dashboard.katana_form.hint_features")}\n    {_t("dashboard.katana_form.hint_danger")}\n    {_t("dashboard.common.no_duplicate_insert")}</div>\n    <textarea name="hosts" rows="6" cols="60" placeholder="a.example.com&#10;b.example.com"></textarea><br>\n    <button type="submit">Run katana</button>\n  </form>\n</details>\n'

    def _dispatch_katana_enrich(self, biz_name: str, accept_enc: bytes) -> None:
        """The entry validation of POST /<biz>/katana-enrich + the dispatch to
        _handle_katana_enrich.

        The katana module does not exist / the binary is not installed -> 404 + a
        hint, do not fall back silently.
        """
        snap = _State.cached_snap
        if not snap:
            snap = self._load_snap()
        bid = next((b['id'] for b in snap.get('tables', {}).get('businesses', []) if b.get('business_name') == biz_name), None)
        if bid is None:
            self._respond(404, 'text/plain', f'unknown business: {biz_name}\n'.encode(), accept_enc)
            return
        self._handle_katana_enrich(biz_name, accept_enc)

    def _handle_katana_enrich(self, biz_name: str, accept_enc: bytes) -> None:
        """The actual handling of POST /<business>/katana-enrich.

        The body fields:
          hosts = the subdomains, one per line (required, <=
                 KATANA_ENRICH_MAX_HOSTS)

        The behaviour:
          - katana_enrich.sh does not exist (the module is not installed) -> 503 +
            a hint on how to install it
          - subprocess.run + KATANA_ENRICH_TIMEOUT as the global timeout
          - when it finishes the stdout is a JSON summary; a non-zero exit -> 502
            + a summary of the stderr
        """
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self._respond(400, 'text/plain', b'bad Content-Length\n', accept_enc)
            return
        if length <= 0 or length > SCAN_URLS_MAX_BODY:
            self._respond(400, 'text/plain', f'body too large or empty ({length} bytes)\n'.encode(), accept_enc)
            return
        body = self.rfile.read(length)
        try:
            fields = urllib.parse.parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        except UnicodeDecodeError:
            self._respond(400, 'text/plain', b'invalid utf-8 body\n', accept_enc)
            return
        raw_hosts = (fields.get('hosts', [''])[0] or '').strip()
        seen: set[str] = set()
        hosts: list[str] = []
        for line in raw_hosts.splitlines():
            h = line.strip().lower().rstrip('.')
            if not h or h.startswith('#'):
                continue
            if h in seen:
                continue
            seen.add(h)
            hosts.append(h)
        if not hosts:
            self._respond(400, 'text/plain', b'no hosts\n', accept_enc)
            return
        if len(hosts) > KATANA_ENRICH_MAX_HOSTS:
            self._respond(400, 'text/plain', f'too many hosts ({len(hosts)} > {KATANA_ENRICH_MAX_HOSTS})\n'.encode(), accept_enc)
            return
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        katana_enrich = repo_root / 'modules' / 'public' / 'katana' / 'katana_enrich.sh'
        if not katana_enrich.exists() or not os.access(katana_enrich, os.X_OK):
            self._respond(503, 'text/plain', f'katana module not installed: {katana_enrich}\n  hint: bash {katana_enrich.parent}/install.sh\n'.encode(), accept_enc)
            return
        with tempfile.NamedTemporaryFile(mode='w', prefix='katana-hosts-', suffix='.txt', delete=False, dir='/tmp', encoding='utf-8') as f:
            for h in hosts:
                f.write(h + '\n')
            tmp_hosts = f.name
        recon_db = getattr(_State, 'recon_db', None) or str(repo_root / 'modules' / 'main' / 'db' / 'recon.sqlite3')
        try:
            proc = subprocess.run(['/usr/bin/env', 'bash', str(katana_enrich), '--db', recon_db, '--business', biz_name, '--hosts-file', tmp_hosts], capture_output=True, text=True, timeout=KATANA_ENRICH_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            self._respond(504, 'text/plain', f'katana enrich timeout after {KATANA_ENRICH_TIMEOUT}s\n'.encode(), accept_enc)
            try:
                os.unlink(tmp_hosts)
            except OSError:
                pass
            return
        finally:
            try:
                os.unlink(tmp_hosts)
            except OSError:
                pass
        if proc.returncode == 0 and proc.stdout.strip():
            summary = proc.stdout.strip().splitlines()[-1]
            try:
                obj = json.loads(summary)
                pretty = json.dumps(obj, ensure_ascii=False, indent=2)
                self._respond(200, 'application/json', pretty.encode('utf-8'), accept_enc)
            except (ValueError, json.JSONDecodeError):
                self._respond(200, 'text/plain', proc.stdout.encode('utf-8'), accept_enc)
        else:
            stderr_tail = (proc.stderr or '')[-800:].strip() or '(no stderr)'
            self._respond(502, 'text/plain', f'katana enrich failed (rc={proc.returncode}):\n{stderr_tail}\n'.encode('utf-8'), accept_enc)

def _resolve_db(path_arg: str | None) -> Path:
    """Backward-compatible: old --snapshot arg now points to a SQLite DB.

    Falls back to the recon DB if no arg given. DB existence is enforced.
    """
    if path_arg:
        p = Path(path_arg)
    else:
        p = Path(__file__).resolve().parents[2] / 'db' / 'recon.sqlite3'
    if not p.exists():
        log.error(f'db not found: {p}')
        raise SystemExit(2)
    return p.resolve()
_DB_SNAPSHOT_QUERIES: dict[str, str] = {'businesses': 'SELECT b.id, b.business_name FROM businesses b INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'scopes': 'SELECT s.id, s.business_id, s.scope_name, s.asset, s.is_wildcard, s.created_at, s.updated_at, s.fetched_at, s.change_type FROM scopes s INNER JOIN businesses b ON b.id = s.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'companies': 'SELECT co.id, co.business_id, co.unit_name, co.nature_name, co.main_licence, co.created_at, co.updated_at, co.change_type FROM companies co INNER JOIN businesses b ON b.id = co.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'mapp_records': 'SELECT m.id, m.company_id, m.source_data_id, m.service_name, m.service_licence, m.service_type, m.content_type_name, m.domain, m.record_updated_at, m.fetched_at, m.change_type FROM mapp_records m INNER JOIN companies co ON co.id = m.company_id INNER JOIN businesses b ON b.id = co.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'web_hashes': 'SELECT wh.id, wh.business_id, wh.response_hash, wh.subdomain_count, wh.score, wh.description, wh.score_initialized_at, wh.url_count, wh.created_at, wh.updated_at, wh.fetched_at, wh.change_type FROM web_hashes wh INNER JOIN businesses b ON b.id = wh.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'web_subdomains': 'SELECT ws.id, ws.hash_id, ws.subdomain, ws.port, ws.url, ws.status_code, ws.content_length, ws.title, ws.technologies, ws.first_seen, ws.last_seen, ws.fetched_at, ws.is_active, ws.change_type, wh.business_id AS business_id, wh.response_hash AS response_hash, wh.score AS hash_score, wh.description AS hash_description FROM web_subdomains ws JOIN web_hashes wh ON wh.id = ws.hash_id INNER JOIN businesses b ON b.id = wh.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1', 'tcp_assets': 'SELECT t.id, t.business_id, t.ip, t.port, t.hosts, t.first_seen, t.last_seen, t.fetched_at, t.is_active, t.raw_value, t.change_type FROM tcp_assets t INNER JOIN businesses b ON b.id = t.business_id INNER JOIN recon_business_config c ON c.business_id = b.id WHERE c.enabled = 1'}

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
        for (name, sql) in _DB_SNAPSHOT_QUERIES.items():
            rows = conn.execute(sql).fetchall()
            tables[name] = [dict(r) for r in rows]
            row_counts[name] = len(rows)
        host_ip_map: dict[str, str] = {}
        try:
            for r in conn.execute("SELECT subdomain, json_extract(raw_json, '$.host_ip') AS ip FROM web_subdomains WHERE raw_json IS NOT NULL   AND json_extract(raw_json, '$.host_ip') IS NOT NULL"):
                host_ip_map.setdefault(r[0], r[1])
        except sqlite3.OperationalError:
            pass
        return {'schema_version': 1, 'captured_at': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), 'db_path': str(db_path.resolve()), 'tables': tables, 'row_counts': row_counts, 'host_ip_map': host_ip_map}
    finally:
        conn.close()

def main() -> int:
    import threading
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--db', dest='db', help='path to recon.sqlite3 (default: <repo>/db/recon.sqlite3)')
    p.add_argument('--snapshot', dest='db', help=argparse.SUPPRESS)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--host', default='127.0.0.1', help='bind address. KEEP 127.0.0.1; expose via SSH port-forward.')
    args = p.parse_args()
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        log.warn(f'BINDING TO {args.host} — recon data is now reachable beyond localhost!')
    _State.db_path = _resolve_db(args.db or os.environ.get('RECON_DB') or os.environ.get('DB'))
    _State.reload_lock = threading.Lock()
    try:
        snap = _load_snap_from_db(_State.db_path)
        _State.cached_snap = snap
        _State.cached_snap_json = json.dumps(snap, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        _State.last_reload_ts = time.time()
        log.info(f"initial load: {_State.db_path} ({sum(len(v) for v in snap.get('tables', {}).values())} rows; the pages are rendered per language on the first request)")
    except Exception as e:
        log.error(f'initial load failed: {e}')
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info(f'dashboard listening on http://{args.host}:{args.port}')
    log.info(f'reading db: {_State.db_path}')
    log.info(f'reload interval: {_State.reload_interval}s (env DASHBOARD_RELOAD)')
    log.info(f'ssh -L {args.port}:127.0.0.1:{args.port} user@recon-host')
    log.info('Ctrl-C to stop')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info('dashboard stopped')
    finally:
        srv.server_close()
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
