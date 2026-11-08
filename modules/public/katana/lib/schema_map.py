"""schema_map.py — katana JSONL row → web_hash_urls INSERT parameters.

Field alignment with httpx (see modules/main/pdtm/import_scan_results.py:155-170):
  - host / port / scheme / path: parsed from the katana request.endpoint urlparse
  - status_code / content_length / body / headers: taken as-is
  - content_type: headers fallback
  - title: <title>(.*?)</title> regex (same as URLFinder, known flaws)
  - technologies: katana calls it technologies, web_hash_urls stores it as tech (aligned with the httpx key)
  - location: headers fallback
  - response_hash: must compute mmh3(body) — algo_diff depends on it (import_scan_results.py:159)
  - time: the top-level timestamp ISO string
  - input: the katana request.endpoint is used as input

Notes:
  - Fields that are not in the web_hash_urls table (technologies / form / xhr) are not stored, they are
    only mentioned in the log (so the dashboard does not show the tech stack, but the schema does not support it).
  - The write side uses INSERT OR IGNORE + UNIQUE(hash_id, subdomain, url, source) to dedupe,
    consistent with the existing scan_urls.py.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

try:
    import mmh3  # type: ignore[import-not-found]

    _HAS_MMH3 = True
except ImportError:
    _HAS_MMH3 = False

# the same title regex as URLFinder (URLFinder crawler/state.go:194)
_TITLE_RE = re.compile(r"<[tT]itle>(.*?)</[tT]itle>")
# the top-level timestamp in the katana JSONL is a time.Time, stdlib json deserializes it into a datetime; do not force-cast it here, leave it to the caller

# web_hash_urls column names (excerpt from modules/main/db/schema.sql:233-262); used by the INSERT below
WEB_HASH_URLS_COLUMNS = (
    "hash_id",          # computed by the caller (shared across the URLs of the same hash_id)
    "business_id",      # computed by the caller
    "subdomain",
    "source",           # always "katana"
    "scheme",
    "url",
    "host",
    "port",
    "path",
    "status_code",
    "title",
    "content_type",
    "content_length",
    "response_hash",
    "word_count",
    "redirect",
    "link_source",
    "risk_flag",
    "is_dangerous",
    "danger_reason",
    "is_static",
    "first_seen",
    "last_seen",
    "fetched_at",
    "is_active",
    "change_type",
    "body",                   # url-diff: zlib(level=6) compressed body (KATANA_OMIT_BODY=0 default)
)


def _parse_endpoint(endpoint: str) -> dict[str, Any]:
    """Parse host/port/scheme/path from the katana request.endpoint URL.

    Same idea as import_scan_results.py:97 host_from_value.
    """
    out: dict[str, Any] = {
        "host": "",
        "port": None,
        "scheme": "",
        "path": "/",
    }
    if not endpoint:
        return out
    try:
        u = urlparse(endpoint)
    except (ValueError, TypeError):
        return out
    out["scheme"] = (u.scheme or "").lower()
    out["host"] = (u.hostname or "").lower()
    # urlparse().port is an int or None; if the endpoint has no port → use the scheme default
    if u.port:
        out["port"] = u.port
    elif out["scheme"] == "https":
        out["port"] = 443
    elif out["scheme"] == "http":
        out["port"] = 80
    out["path"] = u.path or "/"
    return out


def _extract_title(body: str) -> str:
    """The same title regex as URLFinder (<title>(.*?)</title>).

    Known flaws:
      - SPA / no title tag → empty string
      - case sensitive by default (<title> does not match <TITLE>) — but the URLFinder
        regex uses [tT] for compatibility, this function keeps the same behaviour
        (see _TITLE_RE)
    """
    if not body:
        return ""
    m = _TITLE_RE.search(body)
    return m.group(1).strip() if m else ""


def _mmh3_hash(body: str) -> str | None:
    """response_hash = the hex string of mmh3.hash128(body).

    Exactly the same as import_scan_results.py:159-165 (take body_mmh3 → hex).
    Returns None when mmh3 is unavailable (avoids a hard dependency — algo_diff
    degrades, but the web_hash_urls write does not fail because of it, only the
    downstream logic such as is_static changes).
    """
    if not _HAS_MMH3 or not body:
        return None
    try:
        return format(mmh3.hash128(body), "x")  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001 — degrade silently when the body has an unexpected type
        return None


def _flatten_headers(headers: Any) -> str:
    """katana response.headers is a dict; when storing it, it is flattened into a
    'k1: v1\nk2: v2' string in the httpx style.

    The web_hash_urls table has no header column (confirmed against
    schema.sql:233-262), so this string is not stored, it is only used for the
    local log/debug. The interface is kept symmetric here.
    """
    if not headers or not isinstance(headers, dict):
        return ""
    return "\n".join(f"{k}: {v}" for k, v in headers.items())


def map_row(
    row: dict[str, Any],
    *,
    business_id: int,
    hash_id: int,
    fetched_at: str,
    is_dangerous: bool,
) -> dict[str, Any] | None:
    """Map one katana JSONL line (the top-level Result dict) into a web_hash_urls
    INSERT parameter dict.

    Arguments:
      row: one deserialized line of katana -jsonl
        {
          "timestamp": "2026-09-22T...",
          "request":  {"method": "GET", "endpoint": "https://...", "headers": {...}, ...},
          "response": {"status_code": 200, "headers": {...}, "body": "...", "content_length": 1234,
                       "technologies": ["Nginx", "jQuery"]},
          "error": ""  # may be empty
        }
      business_id: businesses.id
      hash_id:     web_hashes.id (the same host:port on the same day shares one hash)
      fetched_at:  ISO timestamp string
      is_dangerous: already blocked by risks.py → True (uses the danger_reason text)

    Returns:
      None — the row cannot be stored (no endpoint / no response)
      dict — the INSERT parameter dict of column name → value
    """
    request = row.get("request") or {}
    response = row.get("response") or {}
    endpoint = request.get("endpoint") or ""
    if not endpoint:
        return None
    parsed = _parse_endpoint(endpoint)
    if not parsed["host"]:
        return None

    # body — the default is KATANA_OMIT_BODY=0 (stored, so url_diff can run the 8-step algorithm).
    # The operator can set KATANA_OMIT_BODY=1 to skip it entirely (saves storage + keeps PII
    # out of the db), this branch is implemented by the env probe that runs before
    # katana_runner.py:_compress_body lands,
    # schema_map.py only passes the raw body through to the caller.
    body = response.get("body") or ""
    if not body:
        # katana -ob turns the body off or status_code is not 2xx/3xx — the body is empty, response_hash=None
        body_for_hash = ""
    else:
        body_for_hash = body

    headers = response.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    # content_type / location — fallback to the headers
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    location = headers.get("location") or headers.get("Location") or ""

    # title — grabbed from the body with a regex
    title = _extract_title(body)

    # word_count — a rough count when the body is not empty (len.split())
    word_count = len(body_for_hash.split()) if body_for_hash else 0

    # response_hash — mmh3(body); degrades to status_code|content_length when unavailable
    #   (the same fallback as import_scan_results.py:356)
    body_mmh3 = _mmh3_hash(body_for_hash)
    if body_mmh3 is not None:
        response_hash_value: str | None = body_mmh3
    else:
        sc = response.get("status_code") or 0
        cl = response.get("content_length") or 0
        response_hash_value = f"{sc}|{cl}"

    # technologies — katana gives a list[str], the web_hash_urls schema has no tech column
    #   (only web_subdomains has a technologies column, web_hash_urls does not); not stored
    techs = response.get("technologies") or []
    if techs and not isinstance(techs, list):
        techs = [str(techs)]

    # the danger_reason text (exactly the same as URLFinder state.go:127)
    danger_reason = "疑似危险路由,已跳过验证" if is_dangerous else None

    # time fields — fetched_at = last_seen = first_seen (the semantics of "first seen this time")
    first_seen = fetched_at
    last_seen = fetched_at

    return {
        "hash_id": hash_id,
        "business_id": business_id,
        "subdomain": parsed["host"],
        "source": "katana",
        "scheme": parsed["scheme"],
        "url": endpoint,
        "host": parsed["host"],
        "port": parsed["port"],
        "path": parsed["path"],
        "status_code": response.get("status_code"),
        "title": title or None,
        "content_type": content_type or None,
        "content_length": response.get("content_length"),
        "response_hash": response_hash_value,
        "word_count": word_count,
        "redirect": location or None,
        "link_source": request.get("source") or None,  # the page katana took the URL from
        "risk_flag": "",  # a srcradar custom extension column, this module does not write it
        "is_dangerous": 1 if is_dangerous else 0,
        "danger_reason": danger_reason,
        "is_static": 0,  # everything katana fetches is a dynamic request; static assets (css/js/img) are handled by a later gate
        "first_seen": first_seen,
        "last_seen": last_seen,
        "fetched_at": fetched_at,
        "is_active": 1,
        "change_type": 0,  # the INSERT trigger trg_whu_ai bumps it to 1
        # body field: the default is KATANA_OMIT_BODY=0 → compressed into web_hash_urls.body
        # by katana_runner.py:_compress_body (so url_diff can run the 8-step algorithm).
        # With KATANA_OMIT_BODY=1 schema_map.py still fills in the body string,
        # _compress_body detects the env and stores NULL — consistent with the existing OMIT_BODY behaviour.
        "body": body,
    }


# ---- CLI self-check ----
if __name__ == "__main__":
    import json
    import sys

    sample = {
        "timestamp": "2026-09-22T12:00:00Z",
        "request": {
            "method": "GET",
            "endpoint": "https://api.example.com:8443/v1/users?x=1",
            "source": "https://example.com/main.js",
        },
        "response": {
            "status_code": 200,
            "headers": {
                "content-type": "application/json; charset=utf-8",
                "location": "",
                "server": "nginx",
            },
            "body": '{"users": []}<html><title>Users</title></html>',
            "content_length": 42,
            "technologies": ["Nginx", "jQuery"],
        },
        "error": "",
    }
    out = map_row(
        sample,
        business_id=1,
        hash_id=100,
        fetched_at="2026-09-22T12:00:00+00:00",
        is_dangerous=False,
    )
    print("--- safe URL map ---")
    print(json.dumps(out, ensure_ascii=False, indent=2))

    # dangerous URL
    dangerous = map_row(
        {**sample, "request": {**sample["request"],
                               "endpoint": "https://example.com/user/delete/1"}},
        business_id=1, hash_id=101, fetched_at="2026-09-22T12:00:00+00:00",
        is_dangerous=True,
    )
    print("--- dangerous URL map ---")
    print(json.dumps(dangerous, ensure_ascii=False, indent=2))

    # missing endpoint
    empty = map_row({}, business_id=1, hash_id=102, fetched_at="x", is_dangerous=False)
    print(f"--- empty endpoint → {empty!r}")

    sys.exit(0)
