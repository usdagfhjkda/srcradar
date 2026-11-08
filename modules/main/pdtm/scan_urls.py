#!/usr/bin/env python3
"""scan_urls.py — the URL level asset scan (ffuf / URLFinder / gau) -> the web_hash_urls table.

How it relates to import_scan_results.py:
  - it is a standalone module and does not depend on the internal functions of
    import_scan_results.py
  - the way it writes to the database is aligned (_s() coerce / now() / PRAGMA
    foreign_keys / the with conn transaction)
  - the caller: the dashboard subprocess calls `python3 scan_urls.py scan-urls --...`

The cross-project dependency (a user constraint):
  - daily(dashboard.py) -> pdtm(scan_urls.py): a one-way subprocess call
  - a daily upgrade does not affect pdtm; a failure is swallowed (best-effort, the
    same as import_scan_results -> score.py)

It does not join diff.py / the cron:
  - it does not write the change_type triggers (Q3: the dashboard only, by hand)
  - it does not refresh web_hashes.url_count (the dashboard does a single UPDATE
    before the reload)

The field semantics follow web_subdomains (the user asked for "fields similar to
the subdomain assets"),
with the URL specific fields (host/scheme/path/url/source/word_count) as the
addition.

The tool binary paths (probed at runtime):
  - ffuf / URLFinder / gau are all outside the standard PATH, so the environment
    variables FFUF_BIN /
    URLFINDER_BIN / GAU_BIN are read first; when they are not set shutil.which is
    used; when every probe fails it raises at startup.
  - URLFinder is the Chinese community edition (pingc0y), there is no official
    package with the same name on GitHub, so it has to be prepared by hand.

The default wordlist: SecLists Discovery/Web-Content/common.txt
  - the SCAN_URLS_WORDLIST environment variable is read first; when it is not set
    the following order is probed:
      1. $RECON_ROOT/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt
      2. ~/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt
    when both fail it raises (so that the operator knows where to install it,
    instead of silently running with the wrong dictionary).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
from i18n import t

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "recon.sqlite3"

# the tool binaries: env > PATH > raise (the hardcoded /home/ubuntu/... is gone)


def _resolve_bin(env_var: str, fallback_name: str) -> str:
    """env > shutil.which > raise. Returns the absolute path as a string."""
    p = os.environ.get(env_var)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    which = shutil.which(fallback_name)
    if which:
        return which
    raise FileNotFoundError(
        t("main.scan_urls.tool_not_found", {
            "tool": fallback_name, "env_var": env_var,
        })
    )


def _resolve_default_wordlist_path() -> Path:
    """The default wordlist path (env > $RECON_ROOT/tools/wordlists > ~/tools/wordlists).

    The file is not required to exist here - the downstream _resolve_wordlist(arg)
    only raises when every
    probe failed, which leaves the operator room to configure it.
    """
    env = os.environ.get("SCAN_URLS_WORDLIST")
    if env:
        return Path(env).expanduser()
    project_root = Path(__file__).resolve().parent.parent
    candidate = (
        project_root / "tools" / "wordlists" / "SecLists-master"
        / "Discovery" / "Web-Content" / "common.txt"
    )
    if candidate.is_file():
        return candidate
    candidate2 = (
        Path.home() / "tools" / "wordlists" / "SecLists-master"
        / "Discovery" / "Web-Content" / "common.txt"
    )
    if candidate2.is_file():
        return candidate2
    # when nothing can be probed it still returns a "most likely location" as a placeholder (the downstream error then shows the original path)
    return candidate2


FFUF_BIN: str = ""             # filled in by _resolve_bin() at the start of main()
URLFINDER_BIN: str = ""        # filled in by _resolve_bin() at the start of main()
GAU_BIN: str = ""              # filled in by _resolve_bin() at the start of main()
DEFAULT_WORDLIST: Path = _resolve_default_wordlist_path()  # a placeholder path, validated at runtime

# the per-invocation scan-urls limits
MAX_HOSTS = 50                 # the per manual scan host limit (so the dashboard cannot hang)
URLFINDER_MAX_URLS = 5000      # the per host urlfinder fetch limit
GAU_MAX_URLS = 5000            # the per host gau fetch limit
FFUF_RATE = 100                # the ffuf rate (requests per second), to keep it from scanning the outside world at random
FFUF_TIMEOUT = 30              # the ffuf per request timeout (seconds)
FFUF_THREADS = 20              # the ffuf concurrency
URLFINDER_TIMEOUT = 1200       # the URLFinder per host timeout (seconds) - the user decided on 2026-08-28 to raise it to 1200s
                                # the reason: a big site such as large-assets.example.com does not finish in 300s, gau passively returns 0 URLs and the ffuf dictionary is incomplete
                                # -> urlfinder needs enough time (20 minutes) to reliably collect the full URL set
                                # it is also shared by run_ffuf() (a naming leftover, it does not affect the actual ffuf semantics)
GAU_TIMEOUT = 120              # the gau per host timeout (seconds)
PER_HOST_OVERALL_TIMEOUT = 600 # the overall limit of the dashboard subprocess (the user decided)

# the hostname validation (aligned with import_scan_results.py:HOSTNAME_RE)
HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")

# the legitimate sources after the URL schema
VALID_SOURCES = ("ffuf", "urlfinder", "gau")

# the dangerous route keyword list (user decision Q7: it is flagged as risk_flag as soon as it appears in the URL path)
# it covers: the sensitive operations / the authentication / the debugging / the data export / the configuration / the file leak / the admin panel / the framework specific
RISK_KEYWORDS = (
    # the sensitive operations
    "delete", "remove", "destroy", "drop", "truncate", "reset", "purge",
    "kill", "shutdown", "terminate",
    # the authentication / the credentials
    "login", "signin", "signup", "register", "auth", "authenticate",
    "token", "session", "sso", "cas", "oauth", "jwt", "credentials",
    # the debugging / the internals
    "debug", "trace", "stacktrace", "exception", "errorlog",
    "actuator", "metrics", "healthcheck", "prometheus",
    # the API / the exposed documentation
    "swagger", "api-docs", "openapi", "graphql",
    # the data export / the backup / the credential leak
    "backup", "dump", "export", "import", "sql", "phpmyadmin",
    "phpinfo", ".env", ".git", "web.config", "htaccess",
    # the admin panels
    "admin", "administrator", "console", "manager", "dashboard",
    "wp-admin", "wp-login", "backend", "manage",
    # the commands / the system
    "shell", "cmd", "exec", "system", "runtime",
    # the framework specific
    "actuator", "jolokia", "env", "config",
)


def _detect_risk_flags(path: str) -> str:
    """Scan the path (lowercased) for the high risk keywords and return a comma
    separated string. Nothing found = an empty string.

    Only the path part is matched (no query string / fragment).
    The keywords match as a substring, they do not have to be a whole segment
    (for example /v1/admin/login hits both admin and login).
    """
    if not path:
        return ""
    p = path.lower()
    hits = []
    for kw in RISK_KEYWORDS:
        if kw in p:
            hits.append(kw)
    return ",".join(hits)


def _detect_is_static(path: str) -> int:
    """The user decided on 2026-08-26:
      - take the lowercase suffix after the last "." in the path
      - no "." -> 0 (not static)
      - the suffix is "js" or "css" -> 1 (static)
      - any other suffix (.png/.jpg/.svg/.woff and the like) -> 0 (not static, only js/css count)
      - an empty suffix (the path ends with ".", for example /v1/) -> 0 (not static)

    The trigger trg_whu_au uses is_static=0 as its gate - a static row never
    takes part in a
    change_type change, which satisfies "only a subdomain that is in scope and
    is not js/css can
    have its change_type modified".
    """
    if not path:
        return 0
    p = path.lower()
    idx = p.rfind(".")
    if idx < 0 or idx == len(p) - 1:
        return 0
    suffix = p[idx + 1:]
    return 1 if suffix in ("js", "css") else 0


# ============================================================
# the basic helpers
# ============================================================

def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _s(v: Any) -> Any:
    """The bind-safe coerce, aligned with import_scan_results._s.

    The httpx / ffuf fields occasionally carry title=None / dict / an odd type,
    and binding those directly blows up.
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _log(stage: str, msg: str) -> None:
    """stderr only; the dashboard subprocess reads stdout, so writing to stderr here does not pollute it."""
    sys.stderr.write(f"[scan_urls][{stage}] {msg}\n")
    sys.stderr.flush()


# ============================================================
# the entry point parsing
# ============================================================

def _read_hosts_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _resolve_wordlist(arg: str | None) -> Path:
    """The wordlist priority: the CLI --wordlist > the environment variable SCAN_WORDLIST > the default common.txt"""
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            raise RuntimeError(t("main.scan_urls.wordlist_missing", {"path": p}))
        return p
    env = os.environ.get("SCAN_WORDLIST")
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise RuntimeError(t("main.scan_urls.wordlist_env_missing", {"env": env}))
        return p
    if DEFAULT_WORDLIST.exists():
        return DEFAULT_WORDLIST
    raise RuntimeError(
        t("main.scan_urls.default_wordlist_missing", {"path": DEFAULT_WORDLIST})
    )


# ============================================================
# the three scanners
# ============================================================

def run_ffuf(seed_url: str, wordlist: Path) -> list[dict]:
    """The ffuf brute force for a single host. Returns
    [{url, status_code, content_type, content_length, word_count}].

    - only the hits "inside the main domain" are kept (`-fl 0` filters out the
      plain 404 and the other responses with no content)
    - status_code / content_type / length / words all come from the ffuf JSON
    """
    if not Path(FFUF_BIN).exists():
        raise RuntimeError(t("main.scan_urls.ffuf_missing", {"bin": FFUF_BIN}))
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        out_path = f.name
    try:
        cmd = [
            FFUF_BIN,
            "-u", f"{seed_url.rstrip('/')}/FUZZ",
            "-w", str(wordlist),
            "-mc", "200,201,202,204,301,302,307,308,401,403",  # the 5xx are not scanned
            "-ac",                                       # the automatic calibration (filters the noise)
            "-fl", "0",                                  # filters the responses with a length of 0
            "-rate", str(FFUF_RATE),
            "-timeout", str(FFUF_TIMEOUT),
            "-t", str(FFUF_THREADS),
            "-H", "User-Agent: Mozilla/5.0",
            "-of", "json",
            "-o", out_path,
            "-s",                                         # silent, it only writes the JSON file
        ]
        subprocess.run(
            cmd, capture_output=True, text=True, timeout=URLFINDER_TIMEOUT, check=False
        )
        # a non-zero ffuf exit code is mostly a rate limit, the JSON file may still hold some of the hits
        data = json.loads(Path(out_path).read_text(encoding="utf-8"))
        results = data.get("results", [])
    except subprocess.TimeoutExpired:
        _log("ffuf", f"timeout after {URLFINDER_TIMEOUT}s ({seed_url})")
        return []
    except json.JSONDecodeError as e:
        _log("ffuf", f"JSON parse failed ({seed_url}): {e}")
        return []
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    out = []
    for r in results:
        url = r.get("url")
        if not url:
            continue
        out.append({
            "url": url,
            "status_code": r.get("status"),
            "content_type": None,  # ffuf does not output the content-type; it stays None
            "content_length": r.get("length"),
            "word_count": r.get("words"),
        })
    return out[:URLFINDER_MAX_URLS]


def run_urlfinder(seed_url: str) -> list[dict]:
    """The URLFinder active crawler (the Chinese edition by pingc0y). Returns
    [{url, status_code, content_length, title}].

    - the command: `URLFinder -m 3 -s all -o <dir> -u <url>`
    - the output JSON structure (as measured):
        {
          "domain": [...],   # the subdomains
          "url": [...],      # the URLs it crawled actively (inside the main target domain)
          "urlOther": [...], # the off-site URLs (optional, they are recorded)
          "js": [...],       # the JS files
          "fuzz": [...],
          "info": {...}
        }
      the fields of an entry (the first letter is capitalised, the value is a string):
        {
          "Url":     "...",
          "Status":  "200",       # a string, it needs an int() conversion
          "Size":    "34253",     # the byte count, a string, it needs an int() conversion
          "Title":   "...",       # the HTML title
          "Redirect":"...",
          "Source":  "..."        # the page it was linked from
        }
      We take the url + urlOther buckets and extract Size/Title/Status from every
      entry
      (Size/Title used to be missed, so the size column was always empty and the
      user decided it "should be there").
    - there is a fallback too: when the output is not JSON it is parsed line by
      line as plain text.
    """
    if not Path(URLFINDER_BIN).exists():
        raise RuntimeError(t("main.scan_urls.urlfinder_missing", {"bin": URLFINDER_BIN}))
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "urlfinder.json"
        cmd = [
            URLFINDER_BIN,
            "-u", seed_url,
            "-m", "3",
            "-s", "all",
            "-t", "10",
            "-time", "10",
            "-max", str(URLFINDER_MAX_URLS),
            "-o", str(out_file),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=URLFINDER_TIMEOUT, check=False
            )
        except subprocess.TimeoutExpired:
            _log("urlfinder", f"timeout after {URLFINDER_TIMEOUT}s ({seed_url})")
            return []
        if not out_file.exists():
            _log("urlfinder", f"no output ({seed_url}); stderr={proc.stderr[:200]}")
            return []

        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            text = out_file.read_text(encoding="utf-8")
            urls = [
                line.strip() for line in text.splitlines()
                if line.strip().startswith(("http://", "https://"))
            ]
            return [{"url": u} for u in urls[:URLFINDER_MAX_URLS]]

        def _to_int(v: Any) -> int | None:
            """The Status/Size fields of URLFinder/ffuf are strings ("200" / "0").

            A special case: URLFinder returns Status="0" for a URL it flagged as
            "suspected dangerous route, verification skipped",
            which means it was not probed - such a value maps to None, so that it
            is not misread as HTTP 0 (which would be meaningless).
            """
            if v is None or v == "":
                return None
            s = str(v).strip()
            # the "skipped" status of URLFinder: 0 / 0.0
            if s in ("0", "0.0", "null", "None"):
                return None
            try:
                return int(float(s))
            except (ValueError, TypeError):
                return None

        # URLFinder puts the "dangerous route" information straight into the Title
        # field (for example "suspected dangerous route, verification skipped")
        # we extract it into a separate bool column is_dangerous and keep the
        # original Title (the title field is stored as usual)
        #   - is_dangerous = 1 means URLFinder flagged this URL as dangerous
        #   - danger_reason  = the description of the reason (usually "suspected
        #                      dangerous route, verification skipped")
        DANGER_TITLE_MARKERS = ("疑似危险", "危险路由", "已跳过验证", "danger", "risky")

        out: list[dict] = []
        if isinstance(data, dict):
            # URLFinder JSON:dict with buckets "url" / "urlOther"
            for bucket in ("url", "urlOther", "js"):
                entries = data.get(bucket, [])
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    url = entry.get("Url") or entry.get("url") or entry.get("URL")
                    if not url:
                        continue
                    status_raw = entry.get("Status") or entry.get("status")
                    title_raw = entry.get("Title") or entry.get("title") or None
                    # the danger flag: URLFinder writes the "dangerous" signal into the Title field
                    is_dangerous = 0
                    danger_reason = ""
                    if title_raw:
                        for marker in DANGER_TITLE_MARKERS:
                            if marker.lower() in title_raw.lower():
                                is_dangerous = 1
                                danger_reason = title_raw
                                break
                    out.append({
                        "url": url,
                        "status_code": _to_int(status_raw),
                        "content_length": _to_int(entry.get("Size")),
                        "title": title_raw,
                        "redirect": entry.get("Redirect") or None,
                        "link_source": entry.get("Source") or None,
                        "is_dangerous": is_dangerous,
                        "danger_reason": danger_reason,
                    })
        elif isinstance(data, list):
            # the fallback: the top level is a list of dicts
            for entry in data:
                if isinstance(entry, dict):
                    url = entry.get("url") or entry.get("Url") or entry.get("URL")
                    if url:
                        title_raw = entry.get("title") or entry.get("Title") or None
                        is_dangerous = 0
                        danger_reason = ""
                        if title_raw:
                            for marker in DANGER_TITLE_MARKERS:
                                if marker.lower() in title_raw.lower():
                                    is_dangerous = 1
                                    danger_reason = title_raw
                                    break
                        out.append({
                            "url": url,
                            "status_code": _to_int(
                                entry.get("status") or entry.get("Status")
                            ),
                            "content_length": _to_int(
                                entry.get("size") or entry.get("Size")
                            ),
                            "title": title_raw,
                            "redirect": entry.get("redirect") or entry.get("Redirect"),
                            "link_source": entry.get("source") or entry.get("Source"),
                            "is_dangerous": is_dangerous,
                            "danger_reason": danger_reason,
                        })

        return out[:URLFINDER_MAX_URLS]


def run_gau(domain: str) -> list[dict]:
    """The gau passive scan (wayback + otx + commoncrawl).

    - the input: the domain (the hostname, without the scheme)
    - the output: txt, one URL per line
    """
    if not Path(GAU_BIN).exists():
        raise RuntimeError(t("main.scan_urls.gau_missing", {"bin": GAU_BIN}))
    if not shutil.which(GAU_BIN) and not Path(GAU_BIN).exists():
        raise RuntimeError(t("main.scan_urls.gau_missing", {"bin": GAU_BIN}))
    try:
        # the --timeout of gau is a plain number of seconds (NOT "30s"); only the versions before 2.2 support "30s"
        proc = subprocess.run(
            [GAU_BIN, "--threads", "5", "--timeout", "30", domain],
            capture_output=True, text=True, timeout=GAU_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        _log("gau", f"timeout after {GAU_TIMEOUT}s ({domain})")
        return []
    if proc.returncode != 0 and not proc.stdout.strip():
        _log("gau", f"non-zero exit ({domain}): {proc.stderr[:200]}")
        return []
    urls = [
        line.strip() for line in proc.stdout.splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]
    return [{"url": u} for u in urls[:GAU_MAX_URLS]]


# ============================================================
# the database write
# ============================================================

def _parse_url(url: str) -> dict | None:
    """Split the url -> {scheme, host, port, path}. Returns None on failure (it is skipped)."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if not u.scheme or not u.hostname:
        return None
    port = u.port
    if port is None:
        port = 443 if u.scheme == "https" else 80
    return {
        "scheme": u.scheme,
        "host": u.hostname,
        "port": port,
        "path": u.path or "/",
    }


def persist(
    conn: sqlite3.Connection,
    business_id: int,
    hash_id: int,
    subdomain: str,
    source: str,
    rows: list[dict],
) -> int:
    """The batch INSERT into web_hash_urls.

    The ON CONFLICT DO UPDATE semantics (the simplified INSERT-or-REPLACE of
    SQLite):
      - a new row -> INSERT, rowcount=1
      - a conflicting row -> UPDATE (it updates last_seen / fetched_at /
        status_code and the like), rowcount=1

    It returns the **number of newly inserted rows** (the difference between the
    rows and the set that is already in the DB).
    The SQL rowcount is not reliable in SQLite (an update counts as 1 too), so it
    goes through the two phases of "query first, then insert".
    """
    ts = now()
    parsed_rows = []
    for r in rows:
        info = _parse_url(r["url"])
        if info is None:
            continue
        parsed_rows.append({
            "scheme": info["scheme"],
            "host": info["host"],
            "port": info["port"],
            "path": info["path"],
            "url": r["url"],
            "status_code": r.get("status_code"),
            "title": r.get("title"),                # only URLFinder has it
            "content_type": r.get("content_type"),
            "content_length": r.get("content_length"),  # ffuf + URLFinder
            "word_count": r.get("word_count"),         # only ffuf
            "redirect": r.get("redirect"),             # only URLFinder has it
            "link_source": r.get("link_source"),       # only URLFinder has it (the upstream page it came from)
            "risk_flag": _detect_risk_flags(info["path"]),  # derived from the path; it counts for every source
            "is_dangerous": r.get("is_dangerous", 0),  # URLFinder flagged it itself (extracted from the Title field)
            "danger_reason": r.get("danger_reason", ""),
            "is_static": _detect_is_static(info["path"]),  # the .js/.css suffix of the path decides it
        })

    if not parsed_rows:
        return 0

    # Step 1: take the set of URLs that this (hash_id, subdomain, source) already has (it is used to count the new ones)
    existing = {
        r[0] for r in conn.execute(
            "SELECT url FROM web_hash_urls "
            " WHERE hash_id = ? AND subdomain = ? AND source = ?",
            (hash_id, subdomain, source),
        ).fetchall()
    }

    # Step 2: INSERT OR IGNORE + UPDATE (two steps, so that the rowcount cannot be misreported)
    sql = """
        INSERT INTO web_hash_urls (
            hash_id, business_id, subdomain, source,
            scheme, host, port, path, url,
            status_code, title, content_type, content_length, response_hash,
            word_count,
            redirect, link_source, risk_flag, is_dangerous, danger_reason,
            is_static,
            first_seen, last_seen, fetched_at, is_active
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?, 1
        )
        ON CONFLICT(hash_id, subdomain, url, source) DO UPDATE SET
            last_seen = excluded.fetched_at,
            fetched_at = excluded.fetched_at,
            is_active = 1,
            status_code = excluded.status_code,
            title = excluded.title,
            content_type = excluded.content_type,
            content_length = excluded.content_length,
            word_count = excluded.word_count,
            redirect = excluded.redirect,
            link_source = excluded.link_source,
            risk_flag = excluded.risk_flag,
            is_dangerous = excluded.is_dangerous,
            danger_reason = excluded.danger_reason,
            is_static = excluded.is_static
    """
    new_count = 0
    for r in parsed_rows:
        is_new = r["url"] not in existing
        cur = conn.execute(sql, (
            hash_id, business_id, subdomain, source,
            r["scheme"], r["host"], r["port"], r["path"], r["url"],
            _s(r["status_code"]), _s(r["title"]), _s(r["content_type"]),
            _s(r["content_length"]), None,
            _s(r["word_count"]),
            _s(r["redirect"]), _s(r["link_source"]), _s(r["risk_flag"]),
            int(r["is_dangerous"] or 0),
            _s(r["danger_reason"]),
            int(r.get("is_static") or 0),
            ts, ts, ts,
        ))
        if is_new and cur.rowcount > 0:
            new_count += 1
    return new_count


def update_hash_url_count(conn: sqlite3.Connection, hash_id: int) -> None:
    """Update web_hashes.url_count = the current number of active URLs."""
    conn.execute("""
        UPDATE web_hashes
           SET url_count = (
               SELECT COUNT(*) FROM web_hash_urls
                WHERE hash_id = ? AND is_active = 1
           )
         WHERE id = ?
    """, (hash_id, hash_id))


# ============================================================
# the main entry point
# ============================================================

def _resolve_hash_id(conn: sqlite3.Connection, business_id: int, subdomain: str) -> int | None:
    """Given (business, subdomain) -> find the matching hash_id.

    The rule: take **all** the web_subdomains of that subdomain (including the
    historical rows with is_active=0),
    their hash_id should be the same under the current model of the user
    (1 subdomain ~= 1 hash); when they differ the one with
    the largest subdomain_count is taken as the main hash.

    The user decided on 2026-08-27: no longer restricted to `is_active=1`.
    - the reason: the semantics of a URL asset scan are "per subdomain", which
      is independent of the web_subdomains.is_active dimension;
      in practice a subdomain with is_active=0 often still has a lot of
      web_hash_urls history (large-assets.example.com is a typical case:
      web_subdomains is_active=0, web_hash_urls has 3017 rows).
    - the side effect (DEPRECATED 2026-10-07): the scan_urls write would trigger
      trg_ws_au and revive a subdomain with is_active=0 through the is_active
      0->1 path (writing change_type=4 / 6). That is the "toggle and watch the
      revival" semantics the user wants.
    - the is_active of web_hash_urls itself is not affected by this (it has always
      been decided by persist()).
    """
    rows = conn.execute("""
        SELECT ws.hash_id, COUNT(*) AS c
          FROM web_subdomains ws
         WHERE ws.business_id = ?
           AND ws.subdomain = ?
         GROUP BY ws.hash_id
         ORDER BY c DESC, ws.hash_id ASC
    """, (business_id, subdomain)).fetchall()
    if not rows:
        return None
    return rows[0][0]


def cmd_scan_urls(args: argparse.Namespace) -> int:
    """The entry point the dashboard calls. The return codes:
        0 everything succeeded
        2 the hosts file is wrong / the validation failed
        3 a sub-scanner failed
        1 the database write failed
    """
    db_path = Path(args.db).expanduser().resolve()
    business = args.business
    wordlist = _resolve_wordlist(args.wordlist)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in VALID_SOURCES:
            sys.stderr.write(t("main.scan_urls.unknown_source", {"source": s, "valid": VALID_SOURCES}) + "\n")
            return 2

    # 1) the hosts file
    hosts_file = Path(args.hosts_file).expanduser().resolve()
    if not hosts_file.exists():
        sys.stderr.write(t("main.scan_urls.hosts_file_missing", {"file": hosts_file}) + "\n")
        return 2
    hosts = _read_hosts_file(hosts_file)
    if not hosts:
        sys.stderr.write(t("main.scan_urls.hosts_file_empty") + "\n")
        return 2
    if len(hosts) > MAX_HOSTS:
        sys.stderr.write(t("main.scan_urls.too_many_hosts", {"count": len(hosts), "max": MAX_HOSTS}) + "\n")
        return 2
    invalid = [h for h in hosts if not HOSTNAME_RE.match(h)]
    if invalid:
        sys.stderr.write(t("main.scan_urls.invalid_hostname", {"hosts": invalid[:5]}) + "\n")
        return 2

    # 2) does the business exist?
    conn = sqlite3.connect(db_path)
    try:
        bid_row = conn.execute(
            "SELECT id FROM businesses WHERE TRIM(business_name) = ?",
            (business,),
        ).fetchone()
        if not bid_row:
            sys.stderr.write(t("main.scan_urls.business_not_found", {"business": business}) + "\n")
            return 2
        business_id = int(bid_row[0])

        # 3) run it host by host
        total_new = 0
        per_source_count = {s: 0 for s in sources}
        for host in hosts:
            hash_id = _resolve_hash_id(conn, business_id, host)
            if hash_id is None:
                _log("scan_urls", t("main.scan_urls.no_active_subdomains", {"host": host}))
                continue
            # ffuf needs scheme://host(:port)/FUZZ; https is used as the fallback here, while urlfinder takes the url form itself
            seed_url = f"https://{host}"

            for src in sources:
                _log("scan_urls", f"{host} / {src}: start")
                try:
                    if src == "ffuf":
                        rows = run_ffuf(seed_url, wordlist)
                    elif src == "urlfinder":
                        rows = run_urlfinder(seed_url)
                    elif src == "gau":
                        rows = run_gau(host)
                    else:
                        continue
                except RuntimeError as e:
                    _log("scan_urls", f"{host} / {src}: {e}")
                    return 3

                _log("scan_urls",
                     f"{host} / {src}: got {len(rows)} urls")
                per_source_count[src] += len(rows)

                try:
                    with conn:
                        new_count = persist(
                            conn, business_id, hash_id, host, src, rows
                        )
                        update_hash_url_count(conn, hash_id)
                except sqlite3.Error as e:
                    _log("scan_urls", f"{host} / {src}: db write failed {e}")
                    return 1
                total_new += new_count
                _log("scan_urls",
                     f"{host} / {src}: persisted (new={new_count})")

        # 4) the summary output (stdout, the dashboard picks it up)
        summary = {
            "ok": True,
            "business": business,
            "hosts": hosts,
            "sources": sources,
            "per_source_collected": per_source_count,
            "total_new_rows": total_new,
        }
        sys.stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return 0
    finally:
        conn.close()


# ============================================================
# the CLI framework
# ============================================================

def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scan_urls.py",
        description=t("main.scan_urls.description"),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan-urls", help=t("main.scan_urls.dashboard_entry"))
    p.add_argument("--db", required=True)
    p.add_argument("--business", required=True)
    p.add_argument("--hosts-file", required=True)
    p.add_argument("--sources", required=True,
                   help=t("main.scan_urls.arg_sources", {"valid": ','.join(VALID_SOURCES)}))
    p.add_argument("--wordlist", default=None)

    args = parser.parse_args(argv)

    # the tool binary resolution (env > PATH > raise).
    # a failure has to show the dashboard a non-zero exit and a clear stderr, instead of
    # blowing up as a Python traceback.
    global FFUF_BIN, URLFINDER_BIN, GAU_BIN
    try:
        if "ffuf" in args.sources.split(","):
            FFUF_BIN = _resolve_bin("FFUF_BIN", "ffuf")
        if "urlfinder" in args.sources.split(","):
            URLFINDER_BIN = _resolve_bin("URLFINDER_BIN", "URLFinder")
        if "gau" in args.sources.split(","):
            GAU_BIN = _resolve_bin("GAU_BIN", "gau")
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 3

    if args.cmd == "scan-urls":
        return cmd_scan_urls(args)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))