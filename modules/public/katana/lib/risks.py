"""risks.py — katana 模块的危险路由黑名单。

沿用 URLFinder 默认 5 词(URLFinder/config/config.go:19):
    remove / delete / insert / update / logout

双重保险:
  1. 源头拦截 — katana_enrich.sh 用 -crawl-out-scope regex 拦住
  2. 入库前 gate — is_dangerous_url() 标 is_dangerous=1, danger_reason=占位文案
     (跟 URLFinder 行为完全对齐,srcradar web_hash_urls.is_dangerous 列因此有值)

注意:
  - 子串匹配有固有误伤(URL 含 delete-check 这种只读查询路径会被拦);
    这是 URLFinder 原生缺陷,本模块继承,不引入更复杂判断(成本/复杂度不划算)
  - 5 词黑名单通过环境变量 KATANA_EXTRA_RISKS 可追加(逗号分隔),
    例:KATANA_EXTRA_RISKS="signout,drop,truncate" python3 ...
"""
from __future__ import annotations

import os
import re

# URLFinder 同款 5 词(URLFinder/config/config.go:19)
DEFAULT_RISKS: tuple[str, ...] = (
    "remove",
    "delete",
    "insert",
    "update",
    "logout",
)


def get_risks() -> tuple[str, ...]:
    """返回生效的黑名单 tuple。

    优先级:env KATANA_EXTRA_RISKS 追加 > DEFAULT_RISKS。
    重复词自动去重(保持 tuple 顺序)。
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
    """构造 katana -crawl-out-scope 用的 regex。

    例:5 词默认 → r"/(remove|delete|insert|update|logout)/i"
    注:katana -cos 接受 Go RE2 语法;本构造兼容 RE2(只用 | / i)。
    """
    if not risks:
        return re.compile(r"$^")
    alts = "|".join(re.escape(w) for w in risks)
    return re.compile(f"/({alts})/i")


_cached_risks: tuple[str, ...] | None = None
_cached_pattern: re.Pattern[str] | None = None


def crawl_out_scope_pattern() -> str:
    """返回 katana -crawl-out-scope 用的 regex 字符串。

    调用方:
        katana ... -crawl-out-scope "<本函数返回值>"
    """
    global _cached_risks, _cached_pattern
    risks = get_risks()
    if _cached_risks != risks or _cached_pattern is None:
        _cached_risks = risks
        _cached_pattern = _build_pattern(risks)
    return _cached_pattern.pattern


def is_dangerous_url(url: str) -> bool:
    """入库前 gate:URL 含任一风险词 → True。

    与 URLFinder crawler/state.go:127 行为完全一致:
        if strings.Contains(u, v) { ... 标 danger }
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
