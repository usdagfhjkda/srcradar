"""risks.py — the dangerous route blacklist of the katana module.

Uses the URLFinder default 5 words (URLFinder/config/config.go:19):
    remove / delete / insert / update / logout

Belt and braces:
  1. blocked at the source — katana_enrich.sh blocks them with the -crawl-out-scope regex
  2. gate before the insert — is_dangerous_url() sets is_dangerous=1, danger_reason=placeholder text
     (exactly aligned with URLFinder behaviour, so the srcradar web_hash_urls.is_dangerous column gets a value)

Notes:
  - substring matching has inherent false positives (a URL holding a read-only query path
    such as delete-check is blocked too);
    this is a native URLFinder flaw, this module inherits it and does not introduce a more
    complex check (the cost/complexity is not worth it)
  - the 5-word blacklist can be extended via the KATANA_EXTRA_RISKS environment variable (comma separated),
    e.g. KATANA_EXTRA_RISKS="signout,drop,truncate" python3 ...
"""
from __future__ import annotations

import os
import re

# the same 5 words as URLFinder (URLFinder/config/config.go:19)
DEFAULT_RISKS: tuple[str, ...] = (
    "remove",
    "delete",
    "insert",
    "update",
    "logout",
)


def get_risks() -> tuple[str, ...]:
    """Return the effective blacklist tuple.

    Priority: the env KATANA_EXTRA_RISKS additions > DEFAULT_RISKS.
    Duplicate words are removed automatically (the tuple order is kept).
    """
    extra = os.environ.get("KATANA_EXTRA_RISKS", "").strip()
    if not extra:
        return DEFAULT_RISKS
    seen: dict[str, None] = {}
    for w in DEFAULT_RISKS:
        seen.setdefault(w, None)
    for w in (s.strip().lower() for s in extra.split(",") if s.strip()):
        seen.setdefault(w, None)
    return tuple(seen.keys())


def _build_pattern(risks: tuple[str, ...]) -> re.Pattern[str]:
    """Build the regex used by katana -crawl-out-scope.

    Example: the 5-word default → r"/(remove|delete|insert|update|logout)/i"
    Note: katana -cos accepts Go RE2 syntax; this construction is RE2 compatible (it only uses | / i).
    """
    if not risks:
        return re.compile(r"$^")
    alts = "|".join(re.escape(w) for w in risks)
    return re.compile(f"/({alts})/i")


_cached_risks: tuple[str, ...] | None = None
_cached_pattern: re.Pattern[str] | None = None


def crawl_out_scope_pattern() -> str:
    """Return the regex string used by katana -crawl-out-scope.

    Caller:
        katana ... -crawl-out-scope "<the return value of this function>"
    """
    global _cached_risks, _cached_pattern
    risks = get_risks()
    if _cached_risks != risks or _cached_pattern is None:
        _cached_risks = risks
        _cached_pattern = _build_pattern(risks)
    return _cached_pattern.pattern


def is_dangerous_url(url: str) -> bool:
    """Gate before the insert: the URL contains any risk word → True.

    Exactly the same behaviour as URLFinder crawler/state.go:127:
        if strings.Contains(u, v) { ... mark danger }
    """
    if not url:
        return False
    lower = url.lower()
    return any(w in lower for w in get_risks())


if __name__ == "__main__":
    import sys

    test_cases = [
        ("https://api.example.com/users", False),
        ("https://api.example.com/user/delete/1", True),
        ("https://example.com/logout", True),
        ("https://example.com/about", False),
        ("https://example.com/INSERT_USER", True),
        ("https://example.com/soft-delete-check", True),
        ("", False),
        ("https://example.com/wp-login.php", False),
    ]
    fail = 0
    for url, expected in test_cases:
        got = is_dangerous_url(url)
        ok = got == expected
        fail += int(not ok)
        marker = "OK" if ok else "FAIL"
        print(f"[{marker}] is_dangerous_url({url!r}) = {got} (expected {expected})")
    print(f"crawl_out_scope_pattern = {crawl_out_scope_pattern()!r}")
    print(f"self-test: {len(test_cases) - fail}/{len(test_cases)} passed")
    sys.exit(0 if fail == 0 else 1)
