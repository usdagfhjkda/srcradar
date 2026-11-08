#!/usr/bin/env python3
"""Write the Web/TCP results produced by scanner.sh into recon.sqlite3."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t as _t


DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "recon.sqlite3"
DEFAULT_SCAN_DIR = Path(__file__).resolve().parent / "scan_results"

# Constants used by the single-site scan mode (the scan-onesite
# subcommand).
# 2026-10-03: HTTPX_BIN path resolution priority - the HTTPX_BIN
# environment variable overrides > httpx_bin in config/pdtm.conf > the
# default bin/httpx. Kept in step with the bash helper in
# pdtm/lib/resolve_tools.sh so that there is a single source of truth
# for the config.

def _resolve_httpx_bin() -> str:
    """Resolve the httpx binary path. Priority:
    1. the HTTPX_BIN environment variable (caller override)
    2. httpx_bin in config/pdtm.conf (written by install.sh / patches/install.sh)
       - the config path itself is overridden by the SRCRADAR_CONF environment
         variable, defaulting to <repo>/config/pdtm.conf
    3. the default <script_dir>/bin/httpx (same directory as this script)

    Returns the first path that exists; when none exist it returns the
    default path and the caller checks it.
    """
    # 1. environment variable
    env_val = os.environ.get("HTTPX_BIN")
    if env_val and Path(env_val).exists():
        return env_val

    # 2. config/pdtm.conf
    cfg_path = _find_pdtm_conf()
    if cfg_path and cfg_path.exists():
        for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line_no_comment = line.split("#", 1)[0]
            if line_no_comment.startswith("httpx_bin="):
                v = line_no_comment.split("=", 1)[1].strip().strip("'\"")
                if v and Path(v).exists():
                    return v

    # 3. default
    return str(Path(__file__).resolve().parent / "bin" / "httpx")


def _find_pdtm_conf() -> "Path | None":
    """Locate the config/pdtm.conf path.
    Priority: the SRCRADAR_CONF environment variable > <repo>/config/pdtm.conf
    inferred from this script's location.
    Returns None when it cannot be inferred.
    """
    env_val = os.environ.get("SRCRADAR_CONF")
    if env_val:
        return Path(env_val)
    # import_scan_results.py lives in modules/main/pdtm/, three levels up = <repo>/
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[2]  # pdtm -> main -> <repo>
    return repo_root / "config" / "pdtm.conf"


HTTPX_BIN = _resolve_httpx_bin()
HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")
MAX_HOSTS = 200            # per-invocation scan-onesite limit
HTTPX_TIMEOUT = 120        # httpx subprocess timeout in seconds


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _s(v: Any) -> Any:
    """Bind-safe coercion: None stays None; str/int/float pass through;
    dict/list/other containers become str().

    httpx JSON fields occasionally carry title=None / technologies=dict /
    status_code=int; handing those straight to the sqlite3 binding raises
    "Error binding parameter N - probably unsupported type". After coercing
    to str the SQLite binding is safe, and an empty container becomes ''
    which is valid TEXT.
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float)):
        return v
    return str(v)


def _normalize_tech(v: Any) -> Any:
    """Normalise technologies into a stable string form.

    The httpx tech field is always list[str]. It used to go through the
    generic _s() coercion, and _s() applies str(v) to a list, so Python's
    list literal representation ended up stored as a string:
        str(['Alibaba Cloud CDN'])  ->  "['Alibaba Cloud CDN']"

    For one and the same technology stack, the stored form jumps between
    "['X']" / "X" / NULL depending on whether the scan took the httpx JSONL
    main path or the raw text fallback — the STEP 1 technologies fast-field
    comparison then matches and reports a change, even though the body is
    byte-for-byte identical.

    The format follows the convention already used elsewhere in the repo:
        list -> ",".join(str(t) for t in v)      # ["A","B"] -> "A,B"
        str  -> unchanged
        None -> None

    Deliberately not reusing _s(): _s is the generic bind-safe coercion
    shared by title / content_type and others, so changing its behaviour
    there would affect unrelated fields.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=_t("main.import_scan_results.description"))
    parser.add_argument("--business", required=True, help=_t("main.import_scan_results.arg_business"))
    parser.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or str(DEFAULT_DB), help=_t("main.import_scan_results.arg_db"))
    parser.add_argument("--scan-dir", default=str(DEFAULT_SCAN_DIR), help=_t("main.import_scan_results.arg_scan_dir"))
    parser.add_argument("--target-file", default="target.txt", help=_t("main.import_scan_results.arg_measurable"))
    parser.add_argument("--exclude-file", default="exclude.txt", help=_t("main.import_scan_results.arg_not_measurable"))
    parser.add_argument("--wildcard-file", default="wildcard.txt", help=_t("main.import_scan_results.arg_wildcard"))
    parser.add_argument("--dry-run", action="store_true", help=_t("main.import_scan_results.arg_dry_run"))
    return parser.parse_args()


def clean_line(line: str) -> str | None:
    line = line.strip().lstrip("﻿")
    if not line or line.startswith("#"):
        return None
    return line.split()[0].strip().lower().rstrip(".")


def read_scope_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(_t("main.import_scan_results.scope_file_missing", {"path": path}))
    result: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = clean_line(line)
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def host_from_value(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if "://" in value:
        value = urlparse(value).hostname or value
    value = value.split("/", 1)[0]
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def parse_host_port(value: str, default_port: int | None = None) -> tuple[str, int | None]:
    value = value.strip().rstrip("/")
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port or default_port
    return host, port


def matches_scope(host: str, scope: str) -> bool:
    """Glob matching (same semantics as the ERE path in scan.sh, see
    target_glob.matches_glob)."""
    from target_glob import matches_glob
    return matches_glob(host, scope)


def split_excludes(excludes: list[str]) -> tuple[list[str], list[str]]:
    """Backwards-compatible call: returns (empty list, cleaned exclude list).

    The old version had two groups (domains / keywords); the new version
    compiles everything as globs and has no keyword branch (decision B).
    The function is kept so that other callers do not break; the actual
    filtering goes through matches_exclude().
    """
    cleaned = []
    for item in excludes:
        value = item.strip().lower().rstrip(".")
        if not value or value.startswith("#"):
            continue
        cleaned.append(value)
    return ([], cleaned)


def matches_exclude(host: str, excludes: list[str]) -> bool:
    """Glob compilation + anchored re.search (via target_glob, no keyword
    branch)."""
    from target_glob import matches_exclude_glob
    return matches_exclude_glob(host, excludes)


def classify_scope(host: str, targets: list[str], excludes: list[str]) -> str | None:
    if matches_exclude(host, excludes):
        return "非可测资产"
    if any(matches_scope(host, item) for item in targets):
        return "可测资产"
    return None


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def response_hash(data: dict[str, Any]) -> str | None:
    value = data.get("hash")
    if isinstance(value, dict):
        # httpx JSON: hash.body_mmh3 / hash.header_mmh3
        value = (
            value.get("body_mmh3") or value.get("header_mmh3")
            or value.get("body") or value.get("header")
        )
    if value is None:
        value = data.get("body_hash")
    if isinstance(value, str):
        value = value.strip()
    return value or None


def body_simhash(data: dict[str, Any]) -> int | None:
    """Extract body_simhash from httpx JSON hash dict.

    httpx outputs hash.body_simhash as a decimal string (64-bit unsigned int).
    Returns the int value, or None if not present / unparseable.

    Only the structured httpx hash dict path is supported - text fallback
    paths (parse_text_web) won't carry simhash.
    """
    value = data.get("hash")
    if not isinstance(value, dict):
        return None
    raw = value.get("body_simhash")
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


# Hamming distance threshold for "near-duplicate body" classification.
# When web_subdomains.hash_id changes between runs but the two body_simhash
# values are within this many bit flips, treat the change as noise
# ("near-duplicate") and downgrade change_type=2 (real change) to 0.
# Threshold picked empirically - see deploy notes; tune in
# modules/main/db/recon_business_config if a business needs stricter/looser.
SIMHASH_HAMMING_THRESHOLD = 3


def hamming_simhash(a: int, b: int) -> int:
    """Bit-level hamming distance for two 64-bit simhash ints.

    Python 3.10+ int.bit_count() is the standard idiom; works for any width
    so threshold logic doesn't care whether httpx actually fills all 64 bits
    (it doesn't - empirically ~60 bits used, see deploy notes).
    """
    return (a ^ b).bit_count()


def _real_sub_from_input(raw_input: Any) -> str | None:
    """Extract the real subdomain from the `input` field of an httpx jsonl line.

    The stage 8 input format is "<sub>,<ip>:<port>" (built by stage 8 of
    scanner.sh). httpx connects to the IP, so the `url` in the jsonl holds
    only the IP literal and no subdomain. `input` keeps the original string
    and is the only reliable source of the asset identity.

    Returns the normalised subdomain (lowercased, trailing dot removed),
    or None when it cannot be parsed, in which case the caller falls back
    to the IP reverse-lookup path. A bare IP is deliberately not accepted
    as real_sub — that means the input is not in the stage 8 format, and
    letting the downstream fallback handle it is the honest choice.
    """
    if not isinstance(raw_input, str):
        return None
    value = raw_input.strip()
    if not value or "," not in value:
        return None
    candidate = value.split(",", 1)[0].strip().lower().rstrip(".")
    if not candidate:
        return None
    # A bare IP is not a subdomain (it carries no Host header semantics),
    # so it is left to the downstream IP reverse lookup.
    if _looks_like_ip(candidate):
        return None
    return candidate


def parse_json_web(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(_t("main.import_scan_results.invalid_jsonl", {"path": path, "line": number, "error": exc})) from exc
        if not isinstance(item, dict):
            continue
        url = first_value(item, "url", "input")
        if not url:
            continue
        host, port = parse_host_port(str(url), 443 if str(url).startswith("https://") else 80)
        if host and port:
            records.append({
                "host": host,
                "port": int(port),
                "url": str(url),
                # 2026-10-21: the real subdomain. The stage 8 input format is
                # "<sub>,<ip>:<port>". httpx connects to the IP, so the `url` above holds
                # only the IP and no sub, while `input` keeps the original
                # "<sub>,<ip>:<port>". The downstream load_web_records() uses this field
                # to determine the asset identity instead of an IP reverse lookup to
                # guess the domain (see the fanout comment on that function). When
                # real_sub is None the downstream fallback runs.
                "real_sub": _real_sub_from_input(item.get("input")),
                "status_code": first_value(item, "status-code", "status_code", "statusCode"),
                "content_length": first_value(item, "content-length", "content_length", "contentLength"),
                "title": first_value(item, "title"),
                # Normalised into a stable form (see _normalize_tech): the httpx tech
                # is list[str], and a direct coercion lands Python's list literal
                # representation in the database, so the form jumps day to day and the
                # fast-field comparison misreads that as a change.
                "technologies": _normalize_tech(first_value(item, "tech", "technologies")),
                "response_hash": response_hash(item),
                "body_simhash": body_simhash(item),
                "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                # 2026-10-03: extract host_ip + a[] into the ips column (the
                # downstream _upsert_web_records writes it to the DB). The httpx JSONL
                # top level carries host_ip (string) and a (list[str]), merged and
                # deduplicated while keeping the order. Parsing raw_json is the fallback
                # path (older data / renamed httpx fields).
                "ips": _extract_ips_from_item(item),
                "content_type": first_value(item, "content_type", "contentType") or "",
                # PATCH-FINAL-URL-2026-10-28: the final landing point after httpx
                # follows -follow-redirects. The field is absent from a 2xx response ->
                # empty string (skipped automatically during comparison); in a 3xx
                # response it holds a stable domain form that does not change with
                # whether the scan stage connects to the IP or to the domain. The url
                # column cannot stand in for it: the url form flips day to day (domain
                # <-> IP), so adding it to the fast fields would misread the form
                # jitter as a change.
                "final_url": first_value(item, "final_url") or "",
                # inline algo_diff block in _upsert_web_records reads record["body"]
                # to compare prev vs curr in the same UPSERT transaction; without
                # this field _curr_body falls back to b"" and change_type stays
                # at the trigger-set insert=1 instead of upgrading to 0/2.
                # httpx -json (no -sr-base64) returns body as str; encode to bytes
                # so the existing isinstance(..., (bytes, bytearray)) guard passes.
                "body": (item["body"].encode("utf-8", errors="replace")
                         if isinstance(item.get("body"), str)
                         else (item["body"] if isinstance(item.get("body"), (bytes, bytearray))
                               else b"")),
            })
    return records


def _extract_ips_from_item(item: dict) -> str:
    """Extract and merge host_ip + a[] from the top level of an httpx JSONL
    item, as a JSON array string.

    The return value is always '["ip1","ip2",...]' or '[]'. The downstream
    _upsert_web_records prefers record['ips']; when it is empty it falls
    back to parsing raw_json."""
    ips: list[str] = []
    host_ip = item.get("host_ip")
    if isinstance(host_ip, str) and host_ip:
        ips.append(host_ip)
    a = item.get("a")
    if isinstance(a, list):
        for x in a:
            if isinstance(x, str) and x and x not in ips:
                ips.append(x)
    return json.dumps(ips, ensure_ascii=False) if ips else "[]"


URL_RE = re.compile(r"(?:https?://)?(?:\[[^]]+\]|[^\s\[\]]+?)(?::\d+)?(?:/[^\s]*)?")
STATUS_RE = re.compile(r"\[(\d{3})\]")
LENGTH_RE = re.compile(r"\[(\d+)\s*(?:B|bytes)?\]")
HASH_RE = re.compile(
    r"(?:hash|body_hash)\s*[:=]\s*([^\s\]]+)|"
    r"\[(?:mmh3|md5|sha1|sha256|simhash):([^\]]+)\]",
    re.IGNORECASE,
)


def parse_text_web(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        match = URL_RE.search(line)
        if not match:
            continue
        raw_url = match.group(0)
        host, port = parse_host_port(raw_url, 443 if raw_url.startswith("https://") else 80)
        if not host or not port:
            continue
        status_match = STATUS_RE.search(line)
        length_match = LENGTH_RE.search(line)
        hash_match = HASH_RE.search(line)
        hash_value = None
        if hash_match:
            hash_value = hash_match.group(1) or hash_match.group(2)
        records.append({
            "host": host,
            "port": int(port),
            "url": raw_url if raw_url.startswith("http") else f"http://{raw_url}",
            "status_code": int(status_match.group(1)) if status_match else None,
            "content_length": int(length_match.group(1)) if length_match else None,
            "title": None,
            "technologies": None,
            "response_hash": hash_value,
            "raw_json": json.dumps({"raw": line}, ensure_ascii=False),
        })
    return records


def load_ip_domains(scan_dir: Path) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for name in ("tmp_domain_ip_pairs.txt", "dnsx_raw_output.txt"):
        path = scan_dir / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            if name == "tmp_domain_ip_pairs.txt":
                parts = line.split()
                if len(parts) < 2:
                    continue
                ip, domain = parts[0], parts[1].lower().rstrip(".")
                if ip and domain:
                    mapping.setdefault(ip, [])
                    if domain not in mapping[ip]:
                        mapping[ip].append(domain)
            else:
                if "[A]" not in line:
                    continue
                try:
                    left, right = line.split("[A]", 1)
                except ValueError:
                    continue
                domain = left.strip().lower().rstrip(".")
                value = right.strip()
                value = value.removeprefix("[")
                ip = value.split("]")[0].strip()
                if ip and domain and _looks_like_ip(ip):
                    mapping.setdefault(ip, [])
                    if domain not in mapping[ip]:
                        mapping[ip].append(domain)
    return mapping


def _looks_like_ip(value: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _resolve_host(value: str, ip_domains: dict[str, list[str]]) -> list[str]:
    if not value:
        return []
    value = value.strip()
    if _looks_like_ip(value):
        return list(ip_domains.get(value, []))
    return [value.lower().rstrip(".")]


def load_web_records(scan_dir: Path, ip_domains: dict[str, list[str]]) -> list[dict[str, Any]]:
    paths = [
        scan_dir / "non_cdn_web_summary.json",
        scan_dir / "cdn_lb_web_summary.json",
        scan_dir / "non_cdn_web_summary.txt",
        scan_dir / "cdn_lb_web_summary.txt",
        scan_dir / "raw_httpx_active_ips.json",
    ]
    raw: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        raw.extend(parse_json_web(path) if path.suffix == ".json" else parse_text_web(path))

    # 2026-10-21: dropped the IP -> multi-domain fanout, and dropped
    # response_hash from the key.
    #
    # Why the fanout was removed:
    #   Stage 8 already produces one probe per domain per input (stage 8 of
    #   scanner.sh emits one line "<sub>,<ip>:<port>" for every sub under that
    #   IP), and each result carries a different Host header, so the responses
    #   can be completely different. _resolve_host() used to reverse-lookup
    #   the host (IP) into every domain under that IP and then copy the same
    #   record (body/status_code/content_length/raw_json) to each of them
    #   indiscriminately — when an IP behind a shared CDN entry point serves
    #   several vhosts, responses meant for another Host header were filed
    #   under this domain. Measured: one IP with 10 domains, where the 3 ports
    #   of one of them all served the page of a different site (the redirect
    #   landing point across Host headers); content_length then fluctuated by
    #   roughly a kilobyte per day and diff reported changed every day. The
    #   premise was verified: each domain has a single form (no mixing), which
    #   shows every domain already had its own probe result, so removing the
    #   copy loses no data.
    #
    # Why response_hash was dropped from the key:
    #   The unique key of web_subdomains is (business_id, subdomain, port) and
    #   does not include the hash. With the hash in the key, an asset that
    #   shows two hashes in one round produced two records, both of which were
    #   written by _upsert_web_records to the same row, where whoever wrote
    #   last won — order became the truth. Removing it leaves one record per
    #   asset and hands the decision back to the database unique key.
    #
    # The role of the hash is unchanged: it is still passed to
    # _upsert_web_records as part of the payload, and the
    # UNIQUE(business_id, response_hash) content addressing on web_hashes
    # decides whether hash_id is new or reuses a historical cluster (a
    # cluster is reused when the hash content regresses; the existing
    # ON CONFLICT plus the second SELECT already cover that, so nothing has
    # to be decided here).
    records: dict[tuple[str, int], dict[str, Any]] = {}
    fallback_ip_count = 0
    for record in raw:
        hash_value = record.get("response_hash") or f"{record['status_code']}|{record['content_length']}"
        record["response_hash"] = hash_value

        # The asset identity prefers the real subdomain from the input;
        # the IP reverse lookup is only the fallback.
        domain = record.get("real_sub")
        if not domain:
            domains = _resolve_host(record["host"], ip_domains)
            domain = domains[0] if domains else record["host"]
            fallback_ip_count += 1
        record["domain"] = domain

        # No cross-domain copying. For one (domain, port), a later record
        # overwrites an earlier one, which matches the
        # ON CONFLICT(business_id, subdomain, port) semantics of web_subdomains.
        records[(domain, record["port"])] = record

    if fallback_ip_count:
        # The response fields of a fallback-path record come from the IP
        # reverse lookup and are less trustworthy than the input path. Only
        # the counts are recorded, never swallowed: a change in the upstream
        # format shows up here explicitly.
        print(
            _t("main.import_scan_results.warn_no_subdomain", {
                "count": fallback_ip_count, "total": len(raw),
            }),
            file=sys.stderr,
        )
    return list(records.values())


def load_tcp_records(scan_dir: Path, ip_domains: dict[str, list[str]]) -> list[dict[str, Any]]:
    # non_cdn_tcp_ports.txt now holds the non-web ports that scanner keeps
    # after dropping the web ports at the IP:port level; each line is one
    # IP:port. The asset identity is (ip, port), and hosts records the
    # domains that resolve to that IP.
    path = scan_dir / "non_cdn_tcp_ports.txt"
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    if not path.is_file():
        return []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or ":" not in line:
            continue
        ip, port = parse_host_port(line)
        if not ip or not port or not _looks_like_ip(ip):
            continue
        key = (ip, int(port))
        grouped.setdefault(key, {"ip": ip, "port": int(port), "hosts": [], "_raw_lines": []})
        if line.strip() not in grouped[key]["_raw_lines"]:
            grouped[key]["_raw_lines"].append(line.strip())
    for record in grouped.values():
        for domain in ip_domains.get(record["ip"], []):
            if domain not in record["hosts"]:
                record["hosts"].append(domain)
        record["raw_value"] = ",".join(record.pop("_raw_lines"))
    return list(grouped.values())


def create_tables(conn: sqlite3.Connection) -> None:
    # tcp_assets old schema (host primary key) migration: the asset identity
    # becomes (business_id, ip, port). The data in the database does not
    # matter, so an old column layout is detected and the table is rebuilt
    # without migrating history.
    tcp_cols = {row[1] for row in conn.execute("PRAGMA table_info(tcp_assets)").fetchall()}
    if tcp_cols and "ip" not in tcp_cols:
        conn.execute("DROP TABLE tcp_assets")
    # web_subdomains old schema (UNIQUE (hash_id, subdomain, port), no
    # business_id) migration: the asset identity becomes
    # (business_id, subdomain, port). SQLite cannot DROP a UNIQUE constraint
    # directly, so the whole table is rebuilt; during the migration rows are
    # deduplicated by (business_id, subdomain, port), keeping the row with
    # is_active=1, the newest last_seen and the largest id in each group.
    ws_cols = {row[1] for row in conn.execute("PRAGMA table_info(web_subdomains)").fetchall()}
    if ws_cols and "business_id" not in ws_cols:
        conn.executescript("""
        CREATE TABLE web_subdomains_new (
            id INTEGER PRIMARY KEY,
            business_id INTEGER NOT NULL REFERENCES businesses(id),
            hash_id INTEGER NOT NULL REFERENCES web_hashes(id),
            subdomain TEXT NOT NULL,
            port INTEGER NOT NULL,
            url TEXT,
            status_code INTEGER,
            content_length INTEGER,
            title TEXT,
            technologies TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            raw_json TEXT,
            UNIQUE (business_id, subdomain, port)
        );
        INSERT INTO web_subdomains_new
            (id, business_id, hash_id, subdomain, port, url, status_code,
             content_length, title, technologies, first_seen, last_seen,
             fetched_at, is_active, raw_json)
        SELECT ws.id, wh.business_id, ws.hash_id, ws.subdomain, ws.port,
               ws.url, ws.status_code, ws.content_length, ws.title,
               ws.technologies, ws.first_seen, ws.last_seen, ws.fetched_at,
               ws.is_active, ws.raw_json
        FROM web_subdomains ws
        JOIN web_hashes wh ON ws.hash_id = wh.id
        WHERE ws.id IN (
            SELECT keep_id FROM (
                SELECT MAX(ws2.id) AS keep_id
                FROM web_subdomains ws2
                JOIN web_hashes wh2 ON ws2.hash_id = wh2.id
                GROUP BY wh2.business_id, ws2.subdomain, ws2.port
            )
        );
        DROP TABLE web_subdomains;
        ALTER TABLE web_subdomains_new RENAME TO web_subdomains;
        CREATE INDEX idx_web_subdomains_hash_id ON web_subdomains(hash_id);
        CREATE INDEX idx_web_subdomains_subdomain ON web_subdomains(subdomain);
        """)
    # web_subdomains gains the ips column (2026-10-04):
    # import_scan_results.py:798-813 writes the host_ip + a[] array extracted
    # from the httpx JSONL. schema.sql already has it; older databases need
    # an ALTER upgrade.
    if ws_cols and "ips" not in ws_cols:
        conn.execute("ALTER TABLE web_subdomains ADD COLUMN ips TEXT DEFAULT '[]'")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS scopes (
        id INTEGER PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        scope_name TEXT NOT NULL CHECK (scope_name IN ('可测资产', '非可测资产')),
        asset TEXT NOT NULL,
        is_wildcard INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        UNIQUE (business_id, scope_name, asset)
    );
    CREATE TABLE IF NOT EXISTS web_hashes (
        id INTEGER PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        response_hash TEXT NOT NULL,
        subdomain_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        UNIQUE (business_id, response_hash)
    );
    CREATE TABLE IF NOT EXISTS web_subdomains (
        id INTEGER PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        hash_id INTEGER NOT NULL REFERENCES web_hashes(id),
        subdomain TEXT NOT NULL,
        port INTEGER NOT NULL,
        url TEXT,
        status_code INTEGER,
        content_length INTEGER,
        title TEXT,
        technologies TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        raw_json TEXT,
        UNIQUE (business_id, subdomain, port)
    );
    CREATE TABLE IF NOT EXISTS tcp_assets (
        id INTEGER PRIMARY KEY,
        business_id INTEGER NOT NULL REFERENCES businesses(id),
        ip TEXT NOT NULL,
        port INTEGER NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        raw_value TEXT,
        hosts TEXT,
        UNIQUE (business_id, ip, port)
    );
    CREATE TABLE IF NOT EXISTS permutation_state (
        business_id     INTEGER NOT NULL REFERENCES businesses(id),
        base_domain     TEXT NOT NULL,
        permutation     TEXT NOT NULL,
        status          TEXT NOT NULL CHECK (status IN
                            ('resolved','nxdomain','timeout','stale','wildcard_hit')),
        resolved_ip     TEXT,
        wordlist_hash   TEXT NOT NULL,
        last_attempt_at TEXT NOT NULL,
        next_attempt_at TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        source          TEXT NOT NULL DEFAULT 'alterx',
        PRIMARY KEY (business_id, base_domain, permutation)
    );
    CREATE INDEX IF NOT EXISTS idx_perm_due
        ON permutation_state(next_attempt_at)
        WHERE status IN ('nxdomain', 'timeout');
    CREATE TABLE IF NOT EXISTS alterx_runs (
        business_id    INTEGER PRIMARY KEY REFERENCES businesses(id),
        last_ran_at    TEXT NOT NULL,
        wordlist_hash  TEXT NOT NULL,
        candidates     INTEGER,
        resolved       INTEGER
    );
    -- brute_runs: shuffledns 字典爆破节奏表 (对齐 alterx_runs schema)
    -- 2026-09-15: 新增,与 alterx_runs 同结构,只是 source 不同
    CREATE TABLE IF NOT EXISTS brute_runs (
        business_id    INTEGER PRIMARY KEY REFERENCES businesses(id),
        last_ran_at    TEXT NOT NULL,
        wordlist_hash  TEXT NOT NULL,
        candidates     INTEGER,
        resolved       INTEGER
    );
    -- hash_simhash_map: (mmh3 response_hash -> body_simhash) lookup table.
    -- See modules/main/db/migrate_hash_simhash_map.sql for full rationale.
    -- Mirrored here so in-process import (cmd_scan_onesite) can build a
    -- fresh db without depending on init_db having been run first.
    CREATE TABLE IF NOT EXISTS hash_simhash_map (
        response_hash TEXT    NOT NULL,
        body_simhash  INTEGER NOT NULL,
        PRIMARY KEY (response_hash)
    );
    """)
    # Old database migration: the scopes table gains the is_wildcard column and
    # the permutation_state table is created.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(scopes)").fetchall()}
    if "is_wildcard" not in cols:
        conn.execute("ALTER TABLE scopes ADD COLUMN is_wildcard INTEGER NOT NULL DEFAULT 0")
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "permutation_state" not in tables:
        conn.executescript("""
        CREATE TABLE permutation_state (
            business_id     INTEGER NOT NULL REFERENCES businesses(id),
            base_domain     TEXT NOT NULL,
            permutation     TEXT NOT NULL,
            status          TEXT NOT NULL CHECK (status IN
                                ('resolved','nxdomain','timeout','stale','wildcard_hit')),
            resolved_ip     TEXT,
            wordlist_hash   TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            next_attempt_at TEXT,
            attempts        INTEGER NOT NULL DEFAULT 0,
            source          TEXT NOT NULL DEFAULT 'alterx',
            PRIMARY KEY (business_id, base_domain, permutation)
        );
        CREATE INDEX idx_perm_due ON permutation_state(next_attempt_at)
            WHERE status IN ('nxdomain', 'timeout');
        """)

    # brute_runs (added 2026-09-15): same structure as alterx_runs, added to
    # existing databases.
    if "brute_runs" not in tables:
        conn.executescript("""
        CREATE TABLE brute_runs (
            business_id    INTEGER PRIMARY KEY REFERENCES businesses(id),
            last_ran_at    TEXT NOT NULL,
            wordlist_hash  TEXT NOT NULL,
            candidates     INTEGER,
            resolved       INTEGER
        );
        """)


def upsert_scope(conn: sqlite3.Connection, business_id: int, scope_name: str, asset: str,
                  fetched_at: str, is_wildcard: int = 0) -> None:
    conn.execute(
        """INSERT INTO scopes (business_id, scope_name, asset, is_wildcard, created_at, updated_at, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(business_id, scope_name, asset) DO UPDATE SET
             updated_at = excluded.updated_at, fetched_at = excluded.fetched_at,
             is_wildcard = excluded.is_wildcard""",
        (business_id, scope_name, asset, is_wildcard, fetched_at, fetched_at, fetched_at),
    )


def _upsert_web_records(conn: sqlite3.Connection, business_id: int,
                         fetched_at: str, web: list[dict[str, Any]]
                        ) -> tuple[dict[str, int], int, list[int]]:
    """Insert / refresh the web_hashes + web_subdomains rows; other is_active=1
    rows of the same business are left alone.

    Returns:
      (hash_ids, web_count, new_hash_ids)
        hash_ids       — every hash seen in this scan (response_hash -> hash_id)
        web_count      — the number of web_subdomains rows written / updated
        new_hash_ids   — the web_hashes row ids **newly** added by this scan
                         (first appearance)

    Used by two kinds of caller:
      1. inside persist() (the cron full path). persist() itself runs the
         UPDATE ... is_active=0 sweep first.
      2. cmd_scan_onesite (the lightweight "add one row and look" path from
         the dashboard). The caller does not deactivate anything.

    Keys each record needs: response_hash, host, port, domain (optional, falls
    back to host); optional: url / status_code / content_length / title /
    technologies / raw_json. A record without response_hash is silently
    dropped (the same as the original persist() behaviour).

    The caller owns the commit. This function neither opens nor commits a
    transaction.

    Cross-package dependency:
      persist() passes `new_hash_ids` to
        ../daily/lib/score.py score-new --db <db> --ids <ids>
      to give the new hashes an initial score (cron only). cmd_scan_onesite()
      does not call it (a manually added hash is not scored automatically; an
      operator can set the score by hand in the dashboard). See README
      "Web Hash scoring".
    """
    hash_ids: dict[str, int] = {}
    seen_hash_counts: dict[int, set[tuple[str, int]]] = {}
    new_hash_ids_list: list[int] = []
    web_count = 0
    for record in web:
        value = record.get("response_hash")
        if not value:
            continue
        response_hash = str(value)
        subdomain = record.get("domain") or record["host"]
        # Existence pre-check: the only reliable way to tell INSERT from UPDATE
        # (SQLite ON CONFLICT DO UPDATE does not expose that signal)
        existing_hash = conn.execute(
            "SELECT id FROM web_hashes WHERE business_id=? AND response_hash=?",
            (business_id, response_hash),
        ).fetchone()
        is_new_hash = existing_hash is None
        conn.execute(
            """INSERT INTO web_hashes (business_id, response_hash, subdomain_count, created_at, updated_at, fetched_at)
               VALUES (?, ?, 0, ?, ?, ?)
               ON CONFLICT(business_id, response_hash) DO UPDATE SET
                 updated_at = excluded.updated_at, fetched_at = excluded.fetched_at""",
            (business_id, response_hash, fetched_at, fetched_at, fetched_at),
        )
        row = conn.execute(
            "SELECT id FROM web_hashes WHERE business_id = ? AND response_hash = ?",
            (business_id, response_hash),
        ).fetchone()
        hash_id = int(row[0])
        hash_ids[response_hash] = hash_id
        if is_new_hash:
            new_hash_ids_list.append(hash_id)
        seen_hash_counts.setdefault(hash_id, set()).add((subdomain, record["port"]))
        # Persist body_simhash into the (mmh3 -> simhash) lookup table so
        # future UPSERTs of the same response_hash (across runs / businesses)
        # can use it for near-duplicate body detection. INSERT OR IGNORE
        # because the same mmh3 may reappear before simhash changed
        # (deterministic algorithm => same body => same simhash, so the
        # existing row is already correct).
        body_sim_value = record.get("body_simhash")
        if body_sim_value is not None:
            # B-fix (2026-09-26, B-skip pre-check): httpx body_simhash is unsigned
            # 64-bit (range 0..2**64-1). SQLite INTEGER is signed 64-bit
            # (-2**63..2**63-1). The original `int(...)` + INSERT would raise
            # OverflowError at sqlite3 binding time when value >= 2**63, aborting
            # _upsert_web_records mid-transaction and rolling back every web
            # record of that batch. INSERT OR IGNORE cannot rescue it: the
            # OverflowError is raised in the Python sqlite3 binding layer, before
            # SQLite sees the SQL.
            #
            # Real fix: range pre-check. Only INSERT when the value fits in
            # signed 64-bit; otherwise leave hash_simhash_map untouched and
            # continue with downstream UPSERTs (web_hashes / web_subdomains),
            # which are unaffected because nothing here raises. No continue,
            # no try/except, no exception swallowing in user-visible flow.
            #
            # hash_simhash_map currently has no live readers (the simhash
            # branch at line 712+ is dead code behind `if False` since
            # 2026-09-16, see comment at line 712). Dropping an overflow row is
            # functionally a no-op. If the simhash branch is ever revived, its
            # SELECT-side must NOT be confused by missing rows for these
            # records (treat as "unknown", same as row absence).
            try:
                _bsim = int(body_sim_value)
            except (TypeError, ValueError):
                _bsim = None
            if _bsim is not None and 0 <= _bsim < (1 << 63):
                conn.execute(
                    """INSERT OR IGNORE INTO hash_simhash_map (response_hash, body_simhash)
                       VALUES (?, ?)""",
                    (response_hash, _bsim),
                )
        # Capture OLD web_subdomains state BEFORE the UPSERT to detect
        # hash_id changes in the live-active path. trg_ws_au (DEPRECATED 2026-10-07) on hash_id
        # change will set change_type=2; we may downgrade it to 0 below
        # if the new and old body_simhash are within the hamming threshold.
        # We need OLD.response_hash (via JOIN web_hashes) so we can look up
        # the OLD body_simhash in hash_simhash_map later.
        prior_ws = conn.execute(
            """SELECT ws.hash_id, ws.is_active, wh.response_hash
               FROM web_subdomains ws
               JOIN web_hashes wh ON wh.id = ws.hash_id
               WHERE ws.business_id = ? AND ws.subdomain = ? AND ws.port = ?""",
            (business_id, subdomain, record["port"]),
        ).fetchone()
        prior_hash_id = prior_ws[0] if prior_ws else None
        prior_was_active = bool(prior_ws[1]) if prior_ws else False
        prior_response_hash = prior_ws[2] if prior_ws else None
        existing = conn.execute(
            "SELECT first_seen FROM web_subdomains WHERE business_id = ? AND subdomain = ? AND port = ?",
            (business_id, subdomain, record["port"]),
        ).fetchone()
        first_seen = existing[0] if existing else fetched_at
        # 2026-10-03: write the ips column (the host_ip + a[] array extracted
        # from the httpx JSONL). record['ips'] is preferred (parse_json_web
        # already extracts it from the item top level); when it is empty it falls
        # back to parsing raw_json (older data / renamed httpx fields).
        ips_value = record.get("ips") or "[]"
        if ips_value == "[]":
            raw = record.get("raw_json")
            if raw:
                try:
                    _j = json.loads(raw)
                    _ips: list[str] = []
                    _hi = _j.get("host_ip")
                    if isinstance(_hi, str) and _hi:
                        _ips.append(_hi)
                    _a = _j.get("a")
                    if isinstance(_a, list):
                        for _x in _a:
                            if isinstance(_x, str) and _x and _x not in _ips:
                                _ips.append(_x)
                    if _ips:
                        ips_value = json.dumps(_ips, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    pass
        # 2026-10-07: write curr_body (round 1) and roll prev_body <- curr_body
        # (round 2+) so the inline algo_diff block has prev_body != NULL on the
        # second cycle and compare() emits a real verdict. Without this,
        # prev_body is NULL on round 1 (no previous cycle), the inline block
        # skips via `_prev_row[4] is not None`, and change_type stays at
        # whatever the trigger wrote — never upgrades to 0/2 from the
        # round-2 fixture swap. The `prev_body = curr_body` self-ref reads
        # the row's CURRENT value (round 1 body, NOT excluded.*) and writes
        # it to prev_body in the same statement; `curr_body = excluded.curr_body`
        # then writes round 2 body. Both run atomically in one UPSERT.
        # CASE WHEN guards round 1 (curr_body IS NULL on first INSERT) so
        # prev_body stays NULL on the first cycle instead of being clobbered
        # by NULL = NULL.
        _curr_body_bytes = record.get("body")
        if isinstance(_curr_body_bytes, (bytes, bytearray)) and _curr_body_bytes:
            _curr_body_blob = zlib.compress(bytes(_curr_body_bytes), level=6)
        else:
            _curr_body_blob = None
        _curr_content_type = _s(record.get("content_type"))
        conn.execute(
            """INSERT INTO web_subdomains
               (business_id, hash_id, subdomain, port, url, status_code, content_length, title, technologies,
                first_seen, last_seen, fetched_at, is_active, raw_json, ips,
                curr_body, content_type, final_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?,
                       ?, ?, ?)
               ON CONFLICT(business_id, subdomain, port) DO UPDATE SET
                 hash_id = excluded.hash_id, url = excluded.url,
                 status_code = excluded.status_code, content_length = excluded.content_length,
                 title = excluded.title, technologies = excluded.technologies,
                 last_seen = excluded.last_seen, fetched_at = excluded.fetched_at,
                 is_active = 1, raw_json = excluded.raw_json,
                 ips = excluded.ips,
                 prev_body = CASE WHEN curr_body IS NOT NULL THEN curr_body ELSE prev_body END,
                 curr_body = excluded.curr_body,
                 content_type = excluded.content_type,
                 final_url = excluded.final_url""",
            (business_id, hash_id, subdomain, record["port"], _s(record.get("url")), record.get("status_code"),
             record.get("content_length"), _s(record.get("title")), _s(record.get("technologies")),
             first_seen, fetched_at, fetched_at, _s(record.get("raw_json")), ips_value,
             _curr_body_blob, _curr_content_type, _s(record.get("final_url"))),
        )
        # Near-duplicate downgrade (live-active path only):
        # If hash_id actually changed AND the previous row was active,
        # 2026-10-07 refactor: trg_ws_au was made a no-op and no longer sets
        # change_type=2. The initial value of the reactivated bit is now written
        # explicitly by Python (commit 2).
        # modules/main/daily/lib/algo_diff/README.md for the rationale.
        # 2026-10-07 refactor: DEPRECATED, the simhash neighbour downgrade
        # writing change_type=0 (follow-up to commit 1). Case E already returns
        # early through `if False`; this comment only exists to match the style
        # of case B so that it can be found by grep. Reason: the import stage
        # must not write change_type=0, per the division of labour "diff writes
        # 0/2". The original simhash downgrade logic (to roll back, change
        # `if False` to `if True`):
        #   - take the body_simhash of prior_response_hash and of the new
        #     response_hash
        #   - if hamming_simhash(...) <= SIMHASH_HAMMING_THRESHOLD then write
        #     change_type = 0
        if False and (prior_hash_id is not None
                and prior_hash_id != hash_id
                and prior_was_active):
            rows = conn.execute(
                """SELECT
                       (SELECT body_simhash FROM hash_simhash_map WHERE response_hash = ?),
                       (SELECT body_simhash FROM hash_simhash_map WHERE response_hash = ?)""",
                (prior_response_hash, response_hash),
            ).fetchone()
            old_sim, new_sim = rows[0], rows[1]
            if old_sim is not None and new_sim is not None:
                if hamming_simhash(int(old_sim), int(new_sim)) <= SIMHASH_HAMMING_THRESHOLD:
                    conn.execute(
                        """UPDATE web_subdomains SET change_type = 0
                           WHERE business_id = ? AND subdomain = ? AND port = ?""",
                        (business_id, subdomain, record["port"]),
                    )
        # The similarity verdict used to be computed here by importing the
        # retired Python diff package in-process. That engine now lives in
        # its own repository and is reached through the command line, so
        # this path no longer computes a verdict. The columns that used to
        # record it stay in the schema and are filled by that engine when
        # it is installed; the daily snapshot copies them without
        # interpreting them. Nothing in this repository depends on the
        # values, and the block was best-effort by construction, so the
        # import outcome is unchanged.

        # =====================================================================
        web_count += 1
    for hash_id, subdomains in seen_hash_counts.items():
        conn.execute(
            "UPDATE web_hashes SET subdomain_count = ?, updated_at = ?, fetched_at = ? WHERE id = ?",
            (len(subdomains), fetched_at, fetched_at, hash_id),
        )
    return hash_ids, web_count, new_hash_ids_list


def persist(conn: sqlite3.Connection, business: str, targets: list[str], excludes: list[str],
              web: list[dict[str, Any]], tcp: list[dict[str, Any]],
              wildcards: list[str] | None = None) -> tuple[int, int]:
    fetched_at = now()
    business_row = conn.execute("SELECT id FROM businesses WHERE business_name = ?", (business,)).fetchone()
    if business_row is None:
        conn.execute("INSERT INTO businesses (business_name) VALUES (?)", (business,))
        business_row = conn.execute("SELECT id FROM businesses WHERE business_name = ?", (business,)).fetchone()
    business_id = int(business_row[0])

    wildcard_set = {host_from_value(w) for w in (wildcards or [])}
    for asset in targets:
        upsert_scope(conn, business_id, "可测资产", asset, fetched_at,
                     is_wildcard=1 if host_from_value(asset) in wildcard_set else 0)
    for asset in excludes:
        upsert_scope(conn, business_id, "非可测资产", asset, fetched_at,
                     is_wildcard=1 if host_from_value(asset) in wildcard_set else 0)

    conn.execute("UPDATE web_subdomains SET is_active = 0 WHERE business_id = ?", (business_id,))
    conn.execute("UPDATE tcp_assets SET is_active = 0 WHERE business_id = ?", (business_id,))
    # 2026-10-08: reset ct for this business. With the trigger plus the
    # diff.py and algo_diff.write paths temporarily disabled, this is the
    # only reset entry point for web_subdomains.change_type. The later diff
    # stage (when enabled) accumulates 1/2/4/8 starting from a baseline of
    # 0.
    _cur = conn.execute(
        "UPDATE web_subdomains SET change_type = 0 WHERE business_id = ?",
        (business_id,),
    )
    print(f"[change_type] import_scan_results.py:1083  reset change_type=0  table=web_subdomains  business_id={business_id}  rows={_cur.rowcount}")

    _hash_ids, web_count, _new_hash_ids = _upsert_web_records(conn, business_id, fetched_at, web)

    tcp_count = 0
    for record in tcp:
        existing = conn.execute("SELECT first_seen FROM tcp_assets WHERE business_id = ? AND ip = ? AND port = ?", (business_id, record["ip"], record["port"])).fetchone()
        first_seen = existing[0] if existing else fetched_at
        hosts = ",".join(record.get("hosts") or [])
        conn.execute(
            """INSERT INTO tcp_assets (business_id, ip, port, first_seen, last_seen, fetched_at, is_active, raw_value, hosts)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(business_id, ip, port) DO UPDATE SET
                 last_seen = excluded.last_seen, fetched_at = excluded.fetched_at,
                 is_active = 1, raw_value = excluded.raw_value, hosts = excluded.hosts""",
            (business_id, record["ip"], record["port"], first_seen, fetched_at, fetched_at, _s(record.get("raw_value")), _s(hosts)),
        )
        tcp_count += 1

    # --- cron-only: give a newly added hash an initial score ---
    # See README "Web Hash scoring". Failures are swallowed (best-effort);
    # the import itself has already succeeded. Calling ../daily/lib/score.py
    # as a subprocess keeps the two packages decoupled: the daily package can
    # be upgraded on its own, and this side only cares whether a new hash
    # got scored.
    if _new_hash_ids:
        try:
            score_script = (Path(__file__).resolve().parent.parent
                            / "daily" / "lib" / "score.py")
            if score_script.exists():
                subprocess.run(
                    ["python3", str(score_script), "score-new",
                     "--db", str(conn.execute("PRAGMA database_list").fetchone()[2]),
                     "--ids", ",".join(str(i) for i in _new_hash_ids)],
                    check=False, timeout=30, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[score-new] subprocess failed (non-fatal): {exc}\n")

    return web_count, tcp_count


def main() -> int:
    args = parse_args()
    scan_dir = Path(args.scan_dir).expanduser().resolve()
    target_file = Path(args.target_file).expanduser().resolve()
    exclude_file = Path(args.exclude_file).expanduser().resolve()
    wildcard_file = Path(args.wildcard_file).expanduser().resolve()
    targets = read_scope_file(target_file)
    excludes = read_scope_file(exclude_file)
    wildcards = read_scope_file(wildcard_file) if wildcard_file.exists() else []
    ip_domains = load_ip_domains(scan_dir)
    web = load_web_records(scan_dir, ip_domains)
    tcp = load_tcp_records(scan_dir, ip_domains)
    missing_hash = sum(1 for item in web if not item.get("response_hash"))

    print(_t("main.import_scan_results.business", {"business": args.business}))
    print(_t("main.import_scan_results.scope_counts", {
        "targets": len(targets), "excludes": len(excludes), "wildcards": len(wildcards),
    }))
    print(_t("main.import_scan_results.record_counts", {
        "web": len(web), "tcp": len(tcp), "missing_hash": missing_hash,
    }))
    if not web and not tcp:
        print(_t("main.import_scan_results.no_results"), file=sys.stderr)
        return 1
    if missing_hash:
        print(_t("main.import_scan_results.missing_hash"), file=sys.stderr)
        return 1
    if args.dry_run:
        print(_t("main.import_scan_results.dry_run_note"))
        return 0

    db_path = Path(args.db).expanduser().resolve()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            create_tables(conn)
            web_count, tcp_count = persist(conn, args.business, targets, excludes, web, tcp, wildcards)
    except (sqlite3.Error, OSError, ValueError, KeyError) as exc:
        conn.rollback()
        print(_t("main.import_scan_results.store_failed", {"exc": exc}), file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(_t("main.import_scan_results.store_ok", {"web_count": web_count, "tcp_count": tcp_count}))
    return 0


# ---------------------------------------------------------------------------
# the scan-onesite subcommand (lightweight mode)
# ---------------------------------------------------------------------------
# It skips the dnsx / cdnmatch / naabu prerequisite stages and runs
# httpx as a subprocess directly; the result is written to the two
# tables by _upsert_web_records(). It deliberately does NOT trigger the
# is_active=0 cleanup in persist(); the caller wraps the web_records
# write in a single BEGIN/COMMIT and the process ends at the commit.

def _parse_scan_onesite_args(argv: list[str]) -> argparse.Namespace:
    """The argparse setup for the scan-onesite subcommand. Not shared with the parse_args() in main()."""
    parser = argparse.ArgumentParser(
        prog="import_scan_results.py scan-onesite",
        description=_t("main.import_scan_results.onesite_desc"),
    )
    parser.add_argument("--business", required=True, help=_t("main.import_scan_results.onesite_arg_business"))
    parser.add_argument("--hosts-file", required=True, type=Path,
                        help=_t("main.import_scan_results.onesite_arg_hosts"))
    parser.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or str(DEFAULT_DB), help=_t("main.import_scan_results.onesite_arg_db"))
    return parser.parse_args(argv)


def _read_hosts_file(path: Path) -> list[str]:
    """Read the hosts file: skip blank lines and # comments; deduplicate keeping the order; strip and lowercase; drop the trailing dot."""
    seen: set[str] = set()
    result: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip().lower().rstrip(".")
        if not s or s.startswith("#"):
            continue
        if s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def _run_httpx(hosts: list[str]) -> list[dict[str, Any]]:
    """Run httpx and parse the JSONL in one go, returning the web_records.

    Failure modes: FileNotFoundError / TimeoutExpired / a non-zero exit with
    empty stdout all raise RuntimeError, which the caller maps onto an exit
    code.
    """
    if not Path(HTTPX_BIN).exists():
        raise RuntimeError(_t("main.import_scan_results.httpx_not_found", {"bin": HTTPX_BIN}))

    stdin_blob = ("\n".join(hosts) + "\n").encode("utf-8")
    # Without -l, httpx reads the host list from stdin by default.
    # scanner.sh uses -l <file> plus < /dev/null; here the "no temporary
    # file" route is taken and stdin is fed entirely through input=.
    cmd = [HTTPX_BIN,
           "-sc", "-cl", "-title", "-hash", "mmh3",
           "-random-agent", "-follow-redirects", "-max-redirects", "3",
           "-td", "-rate-limit", "50", "-timeout", "5",
           "-silent", "-no-color", "-json"]
    try:
        proc = subprocess.run(
            cmd, input=stdin_blob,
            capture_output=True, timeout=HTTPX_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(_t("main.import_scan_results.httpx_start_failed", {"error": exc})) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(_t("main.import_scan_results.httpx_timeout", {"timeout": HTTPX_TIMEOUT})) from exc

    if proc.returncode != 0 and not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip() or f"exit={proc.returncode}"
        raise RuntimeError(_t("main.import_scan_results.httpx_failed", {"error": err[:300]}))

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".jsonl") as f:
            f.write(proc.stdout)
            tmp_path = Path(f.name)
        try:
            return parse_json_web(tmp_path)
        except (ValueError, OSError) as exc:
            raise RuntimeError(_t("main.import_scan_results.httpx_output_failed", {"error": exc})) from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def cmd_scan_onesite(args: argparse.Namespace) -> int:
    """Scan only the given hosts with httpx -> web_hashes + web_subdomains; the other is_active rows are left alone."""
    db_path = Path(args.db).expanduser().resolve()
    business = args.business

    # 1) read and validate the hosts
    hosts_file = args.hosts_file.expanduser().resolve()
    if not hosts_file.exists():
        sys.stderr.write(_t("main.import_scan_results.hosts_file_missing", {"file": hosts_file}) + "\n")
        return 2
    hosts = _read_hosts_file(hosts_file)
    if not hosts:
        sys.stderr.write(_t("main.import_scan_results.hosts_file_empty") + "\n")
        return 2
    if len(hosts) > MAX_HOSTS:
        sys.stderr.write(_t("main.import_scan_results.too_many_domains", {
            "count": len(hosts), "max": MAX_HOSTS,
        }) + "\n")
        return 2
    invalid = [h for h in hosts if not HOSTNAME_RE.match(h)]
    if invalid:
        sys.stderr.write(
            _t("main.import_scan_results.invalid_hostname", {"hosts": ", ".join(invalid[:5])}) + "\n"
        )
        return 2

    # 2) run httpx
    try:
        web_records = _run_httpx(hosts)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3

    # 3) write to the database (PRAGMA plus `with conn:` wraps the
    # transaction; no unlock and no refresh of tcp_assets)
    fetched_at = now()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            create_tables(conn)
            # The businesses table is normally owned by ymicp and maintained by the
            # shared side; scan-onesite is an entry point that may run at any time,
            # so the table is created as a fallback (created when missing, otherwise
            # nothing happens) to avoid changing another project's DDL.
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(businesses)").fetchall()}
            if "business_name" not in cols:
                conn.execute(
                    "CREATE TABLE businesses "
                    "(id INTEGER PRIMARY KEY, business_name TEXT NOT NULL UNIQUE)"
                )
            bid = conn.execute(
                "SELECT id FROM businesses WHERE business_name = ?",
                (business,),
            ).fetchone()
            if bid is None:
                conn.execute(
                    "INSERT INTO businesses (business_name) VALUES (?)",
                    (business,),
                )
                bid = conn.execute(
                    "SELECT id FROM businesses WHERE business_name = ?",
                    (business,),
                ).fetchone()
            business_id = int(bid[0])
            # scan-onesite: does not call score.py (a manually added hash is left
            # for the operator to score by hand in the dashboard)
            _upsert_web_records(conn, business_id, fetched_at, web_records)
    except sqlite3.Error as exc:
        sys.stderr.write(_t("main.import_scan_results.write_failed", {"error": exc}) + "\n")
        return 1
    finally:
        conn.close()

    sys.stdout.write(
        _t("main.import_scan_results.scan_done", {
            "count": len(web_records), "business": business, "business_id": business_id,
        }) + "\n"
    )
    return 0


def _dispatch() -> int:
    """When the first argv element is 'scan-onesite' take the lightweight path; otherwise (no argument / --business / --scan-dir) keep the original main() behaviour."""
    if len(sys.argv) > 1 and sys.argv[1] == "scan-onesite":
        sub_args = _parse_scan_onesite_args(sys.argv[2:])
        return cmd_scan_onesite(sub_args)
    return main()


if __name__ == "__main__":
    raise SystemExit(_dispatch())
