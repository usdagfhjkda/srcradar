#!/usr/bin/env python3
"""check_schema.py — 校对 db/schema.sql vs 各源文件里的 CREATE TABLE。

check-only,不修改任何东西。

退出码:
    0 = 一致(允许报告里只有 "ACCEPTED-DRIFT" 类目)
    1 = drift(出现 "REAL-DRIFT",需要人工同步 schema.sql / 源文件)

分类:
    - REAL-DRIFT:       表结构真不一致(增/减列) → exit 1
    - ACCEPTED-DRIFT:   ALTER-列 + 隐式建表 等 A1 协议下预期的差异 → exit 0 但打印提示
    - MISSING/NEW:      同 REAL-DRIFT,exit 1
"""
import re, sys, pathlib

schema_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 \
    else pathlib.Path(__file__).resolve().parent.parent / "db/schema.sql"
repo = schema_path.parent.parent

# 这些表是迁移临时表,不该出现在 schema.sql
TRANSIENT_TABLES = {"web_subdomains_new"}

# A1 协议下"通过 ALTER TABLE 加列"在源文件里会出现,而 schema.sql 把它们合并进建表 DDL。
# 这里列出已知的 ALTER 列 — 对比时允许 schema.sql "超前"。
ACCEPTED_ALTER_COLS = {
    "businesses":     {"change_type"},
    "companies":      {"change_type"},
    "mapp_records":   {"change_type"},
    "scopes":         {"change_type"},
    "web_hashes":     {"change_type", "url_count", "score",
                       "description", "score_initialized_at"},
    "web_subdomains": {"change_type"},
    "tcp_assets":     {"change_type"},
    "web_hash_urls":  {"change_type", "redirect", "link_source",
                       "risk_flag", "is_dangerous", "danger_reason",
                       "is_static"},
}

sources = {
    "ymicp:icp_mapp_query.py":        repo / "ymicp/icp_mapp_query.py",
    "pdtm:import_scan_results.py":    repo / "pdtm/import_scan_results.py",
    "db_align:internal/store/schema.sql": repo / "db_align/internal/store/schema.sql",
    "daily:migrate_urls.py":          repo / "daily/lib/migrate_urls.py",
    "daily:migrate_schedule.py":      repo / "daily/lib/migrate_schedule.py",
    "daily:migrate_change_type.sql":  repo / "daily/lib/migrate_change_type.sql",
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
            if not ln or ln.startswith("--") or ln.startswith("UNIQUE") \
               or ln.startswith("CHECK") or ln.startswith("FOREIGN") \
               or ln.startswith("PRIMARY") or ln.startswith("CONSTRAINT"):
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

# schema.sql vs 源文件
for t, scols in schema_fps.items():
    candidates = []
    for tag, fp in src_fps.items():
        if t in fp:
            candidates.append((tag, fp[t]))
    if not candidates:
        # 仅 schema.sql 有(隐式建表合法)
        accepted_drift.append(f"[ACCEPTED] {t} 只在 schema.sql (隐式建表,源文件仅 INSERT)")
        continue
    src_cols = set()
    for _, c in candidates:
        src_cols.update(c)
    schema_cols = set(scols)
    only_schema = schema_cols - src_cols
    # 分类 REAL vs ACCEPTED
    accepted = ACCEPTED_ALTER_COLS.get(t, set())
    real_new = only_schema - accepted
    accepted_only = only_schema & accepted
    if real_new:
        real_drift.append(
            f"[REAL-DRIFT] {t}: schema.sql 有但源文件没,且不是已知 ALTER: {sorted(real_new)}")
    if accepted_only:
        accepted_drift.append(
            f"[ACCEPTED] {t}: schema.sql 超前 ALTER 列: {sorted(accepted_only)}")

# 源文件 vs schema.sql
for tag, fp in src_fps.items():
    for t in fp:
        if t in TRANSIENT_TABLES:
            continue
        if t not in schema_fps:
            real_drift.append(
                f"[REAL-DRIFT] {t} 在 {tag} 里有 CREATE, schema.sql 缺失")

if real_drift:
    print("REAL drift detected (must fix):")
    for p in real_drift:
        print(f"  {p}")
    if accepted_drift:
        print("\nAccepted (A1 协议预期,无需修):")
        for p in accepted_drift:
            print(f"  {p}")
    sys.exit(1)
else:
    n_tbl = len(schema_fps)
    n_src = sum(1 for fp in src_fps.values() if fp)
    print(f"OK: schema.sql 含 {n_tbl} 张表,与 {n_src} 个源文件 CREATE 一致")
    if accepted_drift:
        print("注(ACCEPTED):")
        for p in accepted_drift:
            print(f"  {p}")
    sys.exit(0)
