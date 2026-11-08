#!/usr/bin/env python3
"""check_schema.py — cross-check db/schema.sql against the CREATE TABLE statements in the source files.

check-only, it does not modify anything.

Exit codes:
    0 = consistent (the report may contain only "ACCEPTED-DRIFT" categories)
    1 = drift (a "REAL-DRIFT" appears, schema.sql / the source files must be synced by hand)

Categories:
    - REAL-DRIFT:       the table structure really differs (column added/removed) → exit 1
    - ACCEPTED-DRIFT:   differences expected under the A1 protocol, such as ALTER columns + implicit table creation → exit 0 but prints a notice
    - MISSING/NEW:      same as REAL-DRIFT, exit 1
"""
import pathlib
import re
import sys
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t as _t

schema_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 \
    else pathlib.Path(__file__).resolve().parent.parent / "db/schema.sql"
repo = schema_path.parent.parent.parent

# these tables are migration temporaries, they must not appear in schema.sql
TRANSIENT_TABLES = {"web_subdomains_new"}

# under the A1 protocol "adding a column via ALTER TABLE" shows up in the source files, while schema.sql merges them into the CREATE TABLE DDL.
# the known ALTER columns are listed here — during the comparison schema.sql is allowed to be "ahead".
ACCEPTED_ALTER_COLS = {
    "companies":      {"change_type"},
    "mapp_records":   {"change_type"},
    "scopes":         {"change_type"},
    "businesses":     {"change_type", "scan_count"},
    "web_hashes":     {"change_type", "url_count", "score",
                       "description", "score_initialized_at",
                       "learned_anchors"},
    "web_subdomains": {"change_type", "ips", "prev_body", "curr_body",
                       "content_type", "final_url",
                       "last_comparison_ratio", "last_comparison_kind",
                       "last_comparison_at"},
    "tcp_assets":     {"change_type"},
    "web_hash_urls":  {"change_type", "redirect", "link_source",
                       "risk_flag", "is_dangerous", "danger_reason",
                       "is_static", "body", "prev_body", "curr_body",
                       "baseline_at"},
}

sources = {
    "ymicp:icp_mapp_query.py":        repo / "public/ymicp/icp_mapp_query.py",
    "pdtm:import_scan_results.py":    repo / "main/pdtm/import_scan_results.py",
    "db_align:internal/store/schema.sql": repo / "public/db_align/internal/store/schema.sql",
    "daily:migrate_urls.py":          repo / "main/daily/lib/migrate_urls.py",
    "daily:migrate_schedule.py":      repo / "main/daily/lib/migrate_schedule.py",
    "daily:migrate_change_type.sql":  repo / "main/daily/lib/migrate_change_type.sql",
}


def extract_create_tables(text):
    out = {}
    for m in re.finditer(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s*\((.*?)\)\s*;",
        text, re.DOTALL | re.IGNORECASE,
    ):
        name = m.group(1)
        body = m.group(2)
        cols = []
        for ln in body.split("\n"):
            ln = ln.strip().rstrip(",")
            if not ln or ln.startswith(("--", "UNIQUE", "CHECK", "FOREIGN", "PRIMARY", "CONSTRAINT")):
                continue
            mm = re.match(r"(\w+)\s", ln)
            if mm:
                cols.append(mm.group(1))
        out[name] = cols
    return out


src_fps = {}
for tag, p in sources.items():
    if not p.exists():
        continue
    text = p.read_text(encoding="utf-8", errors="replace")
    src_fps[tag] = extract_create_tables(text)

schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
schema_fps = extract_create_tables(schema_text)

real_drift = []
accepted_drift = []

# schema.sql vs source files
for t, scols in schema_fps.items():
    candidates = []
    for tag, fp in src_fps.items():
        if t in fp:
            candidates.append((tag, fp[t]))
    if not candidates:
        # only in schema.sql (implicit table creation is legal)
        accepted_drift.append(_t("main.check_schema.drift.accepted_implicit", {"table": t}))
        continue
    src_cols = set()
    for _, c in candidates:
        src_cols.update(c)
    schema_cols = set(scols)
    only_schema = schema_cols - src_cols
    # classify REAL vs ACCEPTED
    accepted = ACCEPTED_ALTER_COLS.get(t, set())
    real_new = only_schema - accepted
    accepted_only = only_schema & accepted
    if real_new:
        real_drift.append(
            _t("main.check_schema.drift.real_new", {"table": t, "cols": sorted(real_new)}))
    if accepted_only:
        accepted_drift.append(
            _t("main.check_schema.drift.accepted_alter", {"table": t, "cols": sorted(accepted_only)}))

# source files vs schema.sql
for tag, fp in src_fps.items():
    for t in fp:
        if t in TRANSIENT_TABLES:
            continue
        if t not in schema_fps:
            real_drift.append(
                _t("main.check_schema.drift.missing_in_schema", {"table": t, "tag": tag}))

if real_drift:
    print("REAL drift detected (must fix):")
    for p in real_drift:
        print(f"  {p}")
    if accepted_drift:
        print(_t("main.check_schema.accepted"))
        for p in accepted_drift:
            print(f"  {p}")
    sys.exit(1)
else:
    n_tbl = len(schema_fps)
    n_src = sum(1 for fp in src_fps.values() if fp)
    print(_t("main.check_schema.consistent", {"n_tbl": n_tbl, "n_src": n_src}))
    if accepted_drift:
        print(_t("main.check_schema.accepted_note"))
        for p in accepted_drift:
            print(f"  {p}")
    sys.exit(0)
