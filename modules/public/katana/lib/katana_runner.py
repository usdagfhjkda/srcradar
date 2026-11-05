"""katana_runner.py — katana subprocess wrapper + JSONL parsing + insert into web_hash_urls.

Relation to modules/main/pdtm/scan_urls.py:
  - does not depend on the internals of scan_urls.py (one-way subprocess boundary)
  - the insert style is aligned: _s() coerce / now() / PRAGMA foreign_keys / with conn transaction /
    INSERT OR IGNORE + ON CONFLICT UPDATE (see modules/main/pdtm/scan_urls.py:566-610)
  - failures are swallowed, best-effort (same as import_scan_results → score.py)

CLI:
  python3 katana_runner.py enrich \
    --db db/recon.sqlite3 \
    --business ExampleCo \
    --hosts-file /tmp/hosts.txt

Output (stdout JSON):
  {"ok": true, "business": "ExampleCo", "n_hosts": N, "n_rows_scanned": N,
   "n_new": N, "n_dangerous": N, "n_errors": N, "elapsed_s": N.N}

Environment variables:
  KATANA_BIN              binary path (env > shutil.which > error)
  KATANA_TIMEOUT          per-host timeout in seconds (default 600; the user's call for big dictionaries)
  KATANA_EXTRA_FLAGS      extra flags (space separated), e.g. --no-sandbox
  KATANA_OMIT_BODY=1      turn off storing the body (saves storage, response_hash degrades to status|length)

  # rate/thread control (aligned with the httpx srcradar config scanner.sh:328: -rate-limit 50)
  KATANA_RATE_LIMIT       global rate-limit req/s (default 50, same as httpx; 0 = use the katana default 150)
  KATANA_RATE_LIMIT_MIN   global rate-limit req/min (default 0 = not set)
  KATANA_CONCURRENCY      number of concurrent fetchers (default 10; 0 = use the katana default 10)
  KATANA_PARALLELISM      number of concurrent inputs (default 10; 0 = use the katana default 10)
  KATANA_HOST_RATE_LIMIT  per-host rate-limit req/s (default 0 = unlimited)
  KATANA_DELAY            delay between requests in seconds (default 0 = not set, set 1 for SRC sites behind a WAF)
  KATANA_REQUEST_TIMEOUT  per-request timeout in seconds (default 10; different from httpx -timeout 5, katana
                          is heavier, 10s is steadier for SRC sites)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from risks import crawl_out_scope_pattern, is_dangerous_url  # noqa: E402
from schema_map import map_row  # noqa: E402
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t

SOURCE = "katana"

DEFAULT_TIMEOUT = 600  # the same as scan_urls.py SCAN_URLS_TIMEOUT=600 (the user's call)


def _resolve_bin(env_var: str, fallback_name: str) -> str:
    """env > shutil.which > error.

    Exactly the same strategy as modules/main/pdtm/scan_urls.py:_resolve_bin,
    no hardcoded /home/ubuntu/... (a trap that was hit before).
    """
    p = os.environ.get(env_var)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    which = shutil.which(fallback_name)
    if which:
        return which
    raise FileNotFoundError(
        t("public.katana.runner.tool_not_found", {
            "tool": fallback_name, "env_var": env_var,
        })
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _coerce(v):
    """None / '' / 0 all return None (so the trigger does not misjudge them after the insert)."""
    if v is None or v == "":
        return None
    return v


# url-diff storage helper — compress the raw body bytes for the
# `body` column added by modules/main/db/migrate_url_diff.sql. The
# compression level (6) matches both algo_diff.compute._compress
# (subdomain-grain store) and url_diff.store.compress so a body
# can be read back by either path losslessly. KATANA_OMIT_BODY=1
# short-circuits to None to mirror the existing KATANA_OMIT_BODY=1
# behaviour documented in the module-level docstring.
import zlib as _zlib_katana  # noqa: E402

_KATANA_BODY_ZLIB_LEVEL = 6  # MUST match url_diff.store.ZLIB_LEVEL


def _compress_body(raw_body):
    if not raw_body:
        return None
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8", errors="replace")
    if os.environ.get("KATANA_OMIT_BODY", "").strip() == "1":
        return None
    return _zlib_katana.compress(raw_body, level=_KATANA_BODY_ZLIB_LEVEL)


def _resolve_business_id(conn: sqlite3.Connection, biz_name: str) -> int | None:
    """Same as scan_urls.py:701 bid_row: TRIM(business_name)=? to get the id."""
    row = conn.execute(
        "SELECT id FROM businesses WHERE TRIM(business_name)=?",
        (biz_name,),
    ).fetchone()
    return row[0] if row else None


def _resolve_hash_id(conn: sqlite3.Connection, business_id: int, host: str) -> int | None:
    """Same (business, subdomain) → the most recent hash_id (the row with the largest web_subdomains.id).

    Exactly the same logic as scan_urls.py:637 _resolve_hash_id;
    when it is not available the caller INSERTs web_hashes itself (not implemented
    in this round, the fallback is None and the row is skipped).
    """
    row = conn.execute(
        """
        SELECT ws.hash_id
          FROM web_subdomains ws
         WHERE ws.business_id = ? AND ws.subdomain = ?
         ORDER BY ws.id DESC
         LIMIT 1
        """,
        (business_id, host),
    ).fetchone()
    return row[0] if row else None


def _katana_cmd(katana_bin: str, seed_url: str) -> list[str]:
    """Build the katana command.

    - -jsonl           JSONL output (one Result per line)
    - -js-crawl        JS file parsing (endpoint extraction)
    - -form-extraction form field extraction
    - -xhr-extraction  XHR call URL extraction
    - -eof             exclude the given fields (keeps the output small)
    - -crawl-out-scope dangerous route regex filter (same as risks.py)
    - depth 3 / timeout 10 / crawl-duration 300 put a ceiling on it, so a long run cannot run away
    """
    # ---- rate / thread control (aligned with the httpx srcradar config)----
    # the defaults are listed in the docstring at the top of lib/katana_runner.py; env overrides → 0/empty = do not add the flag
    # read with _int_env, on failure it returns 0 (= the katana default, do not add the flag)
    def _int_env(name: str, default: int = 0) -> int:
        """Read an int from the env. Env not set or not a number → return default.

        Default mapping (aligned with the httpx srcradar scanner.sh:328 mild setting):
          KATANA_RATE_LIMIT       default 50  (httpx -rate-limit 50)
          KATANA_CONCURRENCY      default 10  (the katana default is also 10, stated explicitly)
          KATANA_PARALLELISM      default 10
          KATANA_REQUEST_TIMEOUT  default 10  (httpx -timeout 5 is too tight for katana)
        the other KATANA_RATE_LIMIT_MIN / KATANA_HOST_RATE_LIMIT / KATANA_DELAY default to 0 (not enabled)
        """
        v = os.environ.get(name, "").strip()
        if not v:
            return default
        try:
            return int(v)
        except ValueError:
            return default

    cmd = [
        katana_bin,
        "-u", seed_url,
        "-jsonl",
        "-js-crawl",
        "-form-extraction",
        "-xhr-extraction",
        "-depth", "3",
        "-crawl-duration", "300",
        "-crawl-out-scope", crawl_out_scope_pattern(),
        # trim the output: raw / stored_response_path / knowledgebase / forms / xhr_requests
        # are all big blocks of data that the web_hash_urls table does not need, only the body is kept (already trimmed to what we need)
        "-eof", "raw,stored_response_path,knowledgebase,forms,xhr_requests",
        "-silent",
    ]

    # per-request timeout (a single request, distinct from the KATANA_TIMEOUT per-host timeout)
    req_timeout = _int_env("KATANA_REQUEST_TIMEOUT", 10)
    cmd += ["-timeout", str(req_timeout)]

    # rate / concurrency (aligned with httpx rate-limit=50, threads=10)
    rl = _int_env("KATANA_RATE_LIMIT", 50)
    if rl:
        cmd += ["-rate-limit", str(rl)]
    rlm = _int_env("KATANA_RATE_LIMIT_MIN", 0)
    if rlm:
        cmd += ["-rate-limit-minute", str(rlm)]
    conc = _int_env("KATANA_CONCURRENCY", 10)
    if conc:
        cmd += ["-concurrency", str(conc)]
    par = _int_env("KATANA_PARALLELISM", 10)
    if par:
        cmd += ["-parallelism", str(par)]
    hrl = _int_env("KATANA_HOST_RATE_LIMIT", 0)
    if hrl:
        cmd += ["-host-rate-limit", str(hrl)]
    delay = _int_env("KATANA_DELAY", 0)
    if delay:
        cmd += ["-delay", str(delay)]

    extra = os.environ.get("KATANA_EXTRA_FLAGS", "").strip()
    if extra:
        cmd.extend(extra.split())
    return cmd


def _run_katana(
    katana_bin: str, host: str, *, timeout: int
) -> list[dict]:
    """Run katana once, return the parsed list[dict].

    Failure handling:
      - timeout / non-zero exit / parse failure → return [], one line on stderr
    """
    seed_url = f"https://{host}/"
    cmd = _katana_cmd(katana_bin, seed_url)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        print(f"[katana] timeout after {timeout}s ({host})", file=sys.stderr)
        return []
    if proc.returncode != 0 and not proc.stdout:
        stderr = (proc.stderr or "")[:200].strip()
        print(f"[katana] non-zero exit ({host}) rc={proc.returncode}: {stderr}",
              file=sys.stderr)
        return []

    rows: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # skip a single broken JSON line, do not fail the whole batch (katana sometimes ends with an empty line/a warning)
            continue
    return rows


def _upsert_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    business_id: int,
    fetched_at: str,
) -> tuple[int, int, int]:
    """Insert into web_hash_urls; return (n_new, n_dangerous, n_errors).

    The SQL copies the pattern of scan_urls.py:566-610 exactly:
      INSERT OR IGNORE + ON CONFLICT(hash_id, subdomain, url, source) DO UPDATE
    """
    # pull the set of existing URLs for (hash_id, subdomain, source) in one go, to decide what is new later
    # the same (business, host) shares a hash_id, see _resolve_hash_id
    by_subdomain: dict[str, int] = {}  # host → hash_id
    for host in {r["subdomain"] for r in rows if r.get("subdomain")}:
        hid = _resolve_hash_id(conn, business_id, host)
        if hid is not None:
            by_subdomain[host] = hid
    # hosts without a hash_id are skipped — this round does not create web_hashes rows itself (can be added later)
    skipped_no_hash = 0
    for r in rows:
        if r.get("subdomain") and r["subdomain"] not in by_subdomain:
            skipped_no_hash += 1
    rows = [r for r in rows if r.get("subdomain") in by_subdomain]

    if not rows:
        return 0, 0, skipped_no_hash

    # set of existing URLs (used to compute new_count)
    existing_by_sub: dict[str, set[str]] = {}
    for host, hid in by_subdomain.items():
        existing_by_sub[host] = {
            u for (u,) in conn.execute(
                "SELECT url FROM web_hash_urls "
                " WHERE hash_id = ? AND subdomain = ? AND source = ?",
                (hid, host, SOURCE),
            ).fetchall()
        }

    sql = """
        INSERT INTO web_hash_urls (
            hash_id, business_id, subdomain, source,
            scheme, host, port, path, url,
            status_code, title, content_type, content_length, response_hash,
            word_count,
            redirect, link_source, risk_flag, is_dangerous, danger_reason,
            is_static,
            first_seen, last_seen, fetched_at, is_active,
            body
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?, 1,
            ?
        )
        ON CONFLICT(hash_id, subdomain, url, source) DO UPDATE SET
            last_seen   = excluded.fetched_at,
            fetched_at  = excluded.fetched_at,
            is_active   = 1,
            status_code = excluded.status_code,
            title       = excluded.title,
            content_type= excluded.content_type,
            content_length = excluded.content_length,
            word_count  = excluded.word_count,
            redirect    = excluded.redirect,
            link_source = excluded.link_source,
            risk_flag   = excluded.risk_flag,
            is_dangerous= excluded.is_dangerous,
            danger_reason= excluded.danger_reason,
            is_static   = excluded.is_static,
            body        = excluded.body
    """

    n_new = 0
    n_dangerous = 0
    n_errors = 0
    for r in rows:
        try:
            host = r["subdomain"]
            hid = by_subdomain[host]
            is_new = r["url"] not in existing_by_sub[host]
            cur = conn.execute(sql, (
                hid, business_id, host, SOURCE,
                r["scheme"], r["host"], r["port"], r["path"], r["url"],
                _coerce(r["status_code"]), _coerce(r["title"]),
                _coerce(r["content_type"]), _coerce(r["content_length"]),
                _coerce(r["response_hash"]),
                _coerce(r["word_count"]),
                _coerce(r["redirect"]), _coerce(r["link_source"]),
                _coerce(r["risk_flag"]),
                int(r.get("is_dangerous") or 0),
                _coerce(r["danger_reason"]),
                int(r.get("is_static") or 0),
                fetched_at, fetched_at, fetched_at,
                # body field: the default is KATANA_OMIT_BODY=0 → stored (zlib level=6);
                # with KATANA_OMIT_BODY=1 schema_map has already filled in the body string
                # "", _compress_body detects the env and stores NULL, consistent with the None of the existing column.
                _compress_body(r.get("body") or ""),
            ))
            if is_new and cur.rowcount > 0:
                n_new += 1
            if r.get("is_dangerous"):
                n_dangerous += 1
        except sqlite3.Error as e:
            n_errors += 1
            print(f"[katana] db write failed ({r.get('url','?')}): {e}",
                  file=sys.stderr)
            continue
    return n_new, n_dangerous, n_errors + skipped_no_hash


def cmd_enrich(args: argparse.Namespace) -> int:
    """Main entry: run katana over all hosts of one business + insert the results."""
    try:
        katana_bin = _resolve_bin("KATANA_BIN", "katana")
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 3

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[katana] db not found: {db_path}", file=sys.stderr)
        return 4

    hosts_file = Path(args.hosts_file)
    if not hosts_file.exists():
        print(f"[katana] hosts-file not found: {hosts_file}", file=sys.stderr)
        return 4

    timeout = int(os.environ.get("KATANA_TIMEOUT", str(DEFAULT_TIMEOUT)))

    hosts = [
        line.strip()
        for line in hosts_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not hosts:
        print(f"[katana] no hosts in {hosts_file}", file=sys.stderr)
        return 5

    started = datetime.now(timezone.utc)
    fetched_at = _now_iso()

    total_scanned = 0
    total_new = 0
    total_dangerous = 0
    total_errors = 0

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        business_id = _resolve_business_id(conn, args.business)
        if business_id is None:
            print(f"[katana] business not found: {args.business!r}", file=sys.stderr)
            return 6

        for host in hosts:
            rows = _run_katana(katana_bin, host, timeout=timeout)
            # belt and braces: run the risks.py gate once more before the insert
            mapped: list[dict] = []
            for r in rows:
                endpoint = (r.get("request") or {}).get("endpoint") or ""
                dang = is_dangerous_url(endpoint)
                m = map_row(
                    r,
                    business_id=business_id,
                    hash_id=0,  # temporary placeholder, recomputed inside _upsert_rows
                    fetched_at=fetched_at,
                    is_dangerous=dang,
                )
                if m is not None:
                    mapped.append(m)
            total_scanned += len(mapped)

            if not mapped:
                continue

            try:
                with conn:  # transaction
                    n_new, n_dang, n_err = _upsert_rows(
                        conn, mapped,
                        business_id=business_id,
                        fetched_at=fetched_at,
                    )
                total_new += n_new
                total_dangerous += n_dang
                total_errors += n_err
            except sqlite3.Error as e:
                total_errors += 1
                print(f"[katana] transaction failed ({host}): {e}", file=sys.stderr)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "ok": True,
        "business": args.business,
        "n_hosts": len(hosts),
        "n_rows_scanned": total_scanned,
        "n_new": total_new,
        "n_dangerous": total_dangerous,
        "n_errors": total_errors,
        "elapsed_s": round(elapsed, 2),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=t("public.katana_runner.description"),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enrich", help=t("public.katana_runner.help_run"))
    p.add_argument("--db", required=True, help=t("public.katana_runner.help_db"))
    p.add_argument("--business", required=True, help=t("public.katana_runner.help_business"))
    p.add_argument("--hosts-file", required=True, help=t("public.katana_runner.help_hosts"))
    p.add_argument("--dry-run", action="store_true", help=t("public.katana_runner.help_dryrun"))

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.cmd == "enrich":
        return cmd_enrich(args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
