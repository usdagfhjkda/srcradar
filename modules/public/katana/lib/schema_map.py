"""schema_map.py — katana JSONL 行 → web_hash_urls INSERT 参数。

字段对齐 httpx(参考 modules/main/pdtm/import_scan_results.py:155-170):
  - host / port / scheme / path: 从 katana request.endpoint urlparse
  - status_code / content_length / body / headers: 直接取
  - content_type: headers 兜底
  - title: <title>(.*?)</title> 正则(URLFinder 同款,已知缺陷)
  - technologies: katana 是 technologies,入 web_hash_urls 改 tech(对齐 httpx key)
  - location: headers 兜底
  - response_hash: 必算 mmh3(body) — algo_diff 依赖(import_scan_results.py:159)
  - time: 顶层 timestamp ISO 字符串
  - input: katana request.endpoint 当 input

注:
  - 不在 web_hash_urls 表里的字段(technologies / form / xhr)不入库,只在
    日志里说一声(防 dashboard 看不到技术栈,但 schema 不支持)。
  - 写表侧用 INSERT OR IGNORE + UNIQUE(hash_id, subdomain, url, source) 防重,
    跟现有 scan_urls.py 一致。
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

# URLFinder 同款 title 正则(URLFinder crawler/state.go:194)
_TITLE_RE = re.compile(r"<[tT]itle>(.*?)</[tT]itle>")
# katana JSONL 顶层 timestamp 是 time.Time,标准库 json 会反序列化成 datetime;此处不强转,交给 caller

# web_hash_urls 列名(节选自 modules/main/db/schema.sql:233-262);下面 INSERT 用
WEB_HASH_URLS_COLUMNS = (
    "hash_id",          # 由 caller 算(同 hash_id 跨 URL 共用)
    "business_id",      # 由 caller 算
    "subdomain",
    "source",           # 固定 "katana"
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
    """从 katana request.endpoint URL 解析 host/port/scheme/path。

    跟 import_scan_results.py:97 host_from_value 同款思路。
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
    # urlparse().port 是 int 或 None;若 endpoint 不带 port → 走 scheme 默认
    if u.port:
        out["port"] = u.port
    elif out["scheme"] == "https":
        out["port"] = 443
    elif out["scheme"] == "http":
        out["port"] = 80
    out["path"] = u.path or "/"
    return out


def _extract_title(body: str) -> str:
    """URLFinder 同款 title 正则(<title>(.*?)</title>)。

    已知缺陷:
      - SPA / 无 title 标签 → 空字符串
      - 大小写敏感默认(<title> 不匹配 <TITLE>)— 但 URLFinder 正则用 [tT] 兼容,
        本函数沿用一致行为(见 _TITLE_RE)
    """
    if not body:
        return ""
    m = _TITLE_RE.search(body)
    return m.group(1).strip() if m else ""


def _mmh3_hash(body: str) -> str | None:
    """response_hash = mmh3.hash128(body) 的 hex 字符串。

    跟 import_scan_results.py:159-165 完全一致(取 body_mmh3 → hex)。
    mmh3 不可用时返回 None(避免硬依赖 — algo_diff 会降级,但 web_hash_urls
    写入不会因此失败,只是 is_static 之类下游逻辑会变)。
    """
    if not _HAS_MMH3 or not body:
        return None
    try:
        return format(mmh3.hash128(body), "x")  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001 — body 类型异常时静默降级
        return None


def _flatten_headers(headers: Any) -> str:
    """katana response.headers 是 dict,落库时按 httpx 风格压成 'k1: v1\nk2: v2' 串。

    web_hash_urls 表没 header 列(对照 schema.sql:233-262 确认),所以这份
    字符串不入库,仅用于本地日志/Debug。这里保留接口对称。
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
    """把 katana JSONL 一行(顶层 Result dict)映射成 web_hash_urls INSERT 参数字典。

    入参:
      row: katana -jsonl 一行,反序列化后的 dict
        {
          "timestamp": "2026-09-22T...",
          "request":  {"method": "GET", "endpoint": "https://...", "headers": {...}, ...},
          "response": {"status_code": 200, "headers": {...}, "body": "...", "content_length": 1234,
                       "technologies": ["Nginx", "jQuery"]},
          "error": ""  # 可空
        }
      business_id: businesses.id
      hash_id:     web_hashes.id(同 host:port 同一天共用一个 hash)
      fetched_at:  ISO timestamp 字符串
      is_dangerous: 已被 risks.py 拦下 → True(走 danger_reason 文案)

    返回:
      None — 该行无法入库(无 endpoint / 无 response)
      dict — 列名 → 值的 INSERT 参数字典
    """
    request = row.get("request") or {}
    response = row.get("response") or {}
    endpoint = request.get("endpoint") or ""
    if not endpoint:
        return None
    parsed = _parse_endpoint(endpoint)
    if not parsed["host"]:
        return None

    # body — 默认 KATANA_OMIT_BODY=0 (落库,供 url_diff 跑 8 步算法).
    # 操作员可设 KATANA_OMIT_BODY=1 完全跳过(节省存储 + 防 PII 入库),
    # 此分支由 katana_runner.py:_compress_body 落地前的 env 探测实现,
    # schema_map.py 仅负责把原始 body 透传给 caller.
    body = response.get("body") or ""
    if not body:
        # katana -ob 关 body 或 status_code 非 2xx/3xx — body 空,response_hash=None
        body_for_hash = ""
    else:
        body_for_hash = body

    headers = response.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    # content_type / location — 从 headers 兜底
    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    location = headers.get("location") or headers.get("Location") or ""

    # title — body 里正则抓
    title = _extract_title(body)

    # word_count — body 非空时粗算(len.split())
    word_count = len(body_for_hash.split()) if body_for_hash else 0

    # response_hash — mmh3(body);不可用时降级 status_code|content_length
    #   (跟 import_scan_results.py:356 fallback 同款)
    body_mmh3 = _mmh3_hash(body_for_hash)
    if body_mmh3 is not None:
        response_hash_value: str | None = body_mmh3
    else:
        sc = response.get("status_code") or 0
        cl = response.get("content_length") or 0
        response_hash_value = f"{sc}|{cl}"

    # technologies — katana 是 list[str],web_hash_urls 表 schema 无 tech 列
    #   (web_subdomains 才有 technologies 列,web_hash_urls 没);不入库
    techs = response.get("technologies") or []
    if techs and not isinstance(techs, list):
        techs = [str(techs)]

    # danger_reason 文案(跟 URLFinder state.go:127 完全一致)
    danger_reason = "疑似危险路由,已跳过验证" if is_dangerous else None

    # 时间字段 — fetched_at = last_seen = first_seen(本次发现的语义)
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
        "link_source": request.get("source") or None,  # katana 给出 URL 的来源页面
        "risk_flag": "",  # srcradar 自定义扩展位,本模块不写
        "is_dangerous": 1 if is_dangerous else 0,
        "danger_reason": danger_reason,
        "is_static": 0,  # katana 抓到的都是动态请求;静态资源(css/js/img)靠后置 gate
        "first_seen": first_seen,
        "last_seen": last_seen,
        "fetched_at": fetched_at,
        "is_active": 1,
        "change_type": 0,  # INSERT 触发器 trg_whu_ai 会把它升到 1
        # body 字段:默认 KATANA_OMIT_BODY=0 → 由 katana_runner.py:_compress_body
        # 压缩入 web_hash_urls.body (供 url_diff 跑 8 步算法).
        # KATANA_OMIT_BODY=1 时 schema_map.py 仍然回填 body 字符串,
        # 由 _compress_body 检测 env 后落 NULL — 与既有 OMIT_BODY 行为一致.
        "body": body,
    }


# ---- CLI 自检 ----
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

    # 危险 URL
    dangerous = map_row(
        {**sample, "request": {**sample["request"],
                               "endpoint": "https://example.com/user/delete/1"}},
        business_id=1, hash_id=101, fetched_at="2026-09-22T12:00:00+00:00",
        is_dangerous=True,
    )
    print("--- dangerous URL map ---")
    print(json.dumps(dangerous, ensure_ascii=False, indent=2))

    # 缺 endpoint
    empty = map_row({}, business_id=1, hash_id=102, fetched_at="x", is_dangerous=False)
    print(f"--- empty endpoint → {empty!r}")

    sys.exit(0)
