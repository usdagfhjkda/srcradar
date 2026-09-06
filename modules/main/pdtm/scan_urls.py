#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_urls.py — URL 级资产扫描(ffuf / URLFinder / gau) → web_hash_urls 表。

与 import_scan_results.py 的关系:
  - 独立模块,不依赖 import_scan_results.py 的内部函数
  - 入库风格对齐(_s() coerce / now() / PRAGMA foreign_keys / with conn 事务)
  - 调用方:dashboard subprocess 调 `python3 scan_urls.py scan-urls --...`

跨项目依赖(用户约束):
  - daily(dashboard.py) → pdtm(scan_urls.py):单向 subprocess
  - daily 升级不影响 pdtm;失败吞掉(best-effort,和 import_scan_results → score.py 一致)

不接 diff.py / cron:
  - 不写 change_type 触发器(Q3:仅 dashboard 手动)
  - 不刷 web_hashes.url_count(由 dashboard 在 reload 前一次性 UPDATE)

字段语义参考 web_subdomains(用户要求"和子域名资产有类似的字段"),
URL 特有字段(host/scheme/path/url/source/word_count)做补充。

工具二进制路径(运行时探测):
  - ffuf / URLFinder / gau 均不在标准 PATH,优先读环境变量 FFUF_BIN /
    URLFINDER_BIN / GAU_BIN;未设则用 shutil.which 探测;都失败则启动时报错。
  - URLFinder 是中文社区版(pingc0y),GitHub 上无同名官方包,需自行准备。

默认 wordlist: SecLists Discovery/Web-Content/common.txt
  - 优先读 SCAN_URLS_WORDLIST 环境变量;未设则按以下顺序探测:
      1. $RECON_ROOT/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt
      2. ~/tools/wordlists/SecLists-master/Discovery/Web-Content/common.txt
    都失败则报错(让运维明确装位置,而不是静默跑错字典)。
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
from typing import Any, Iterable
from urllib.parse import urlparse

DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "recon.sqlite3"

# 工具二进制:env > PATH > 报错(不再硬编码 /home/ubuntu/...)


def _resolve_bin(env_var: str, fallback_name: str) -> str:
    """env > shutil.which > 报错。返回绝对路径字符串。"""
    p = os.environ.get(env_var)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    which = shutil.which(fallback_name)
    if which:
        return which
    raise FileNotFoundError(
        f"[scan_urls] 找不到 {fallback_name}; "
        f"请设置 ${env_var} 指向绝对路径,或把 {fallback_name} 放进 PATH"
    )


def _resolve_default_wordlist_path() -> Path:
    """默认 wordlist 路径(env > $RECON_ROOT/tools/wordlists > ~/tools/wordlists)。

    不在此处强制要求文件存在 —— 下游 _resolve_wordlist(arg) 在最终都失败时
    才会报错,给运维留足配置空间。
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
    # 都探测不到,仍返回一个"最可能的位置"作占位(下游报错时给出原始路径)
    return candidate2


FFUF_BIN: str = ""             # 由 main() 启动时 _resolve_bin() 填充
URLFINDER_BIN: str = ""        # 由 main() 启动时 _resolve_bin() 填充
GAU_BIN: str = ""              # 由 main() 启动时 _resolve_bin() 填充
DEFAULT_WORDLIST: Path = _resolve_default_wordlist_path()  # 占位路径,运行时校验

# 单次 scan-urls 上限
MAX_HOSTS = 50                 # 单次手动扫描上限(host 数,防 dashboard 卡死)
URLFINDER_MAX_URLS = 5000      # 单 host urlfinder 抓取上限
GAU_MAX_URLS = 5000            # 单 host gau 抓取上限
FFUF_RATE = 100                # ffuf 速率(每秒请求数),防止对外网乱扫
FFUF_TIMEOUT = 30              # ffuf 单请求超时(秒)
FFUF_THREADS = 20              # ffuf 并发
URLFINDER_TIMEOUT = 1200       # URLFinder 单 host 超时(秒) — 用户 2026-08-28 拍板加到 1200s
                                # 原因:large-assets.example.com 等大站 300s 抓不完,gau 被动 0 URL,ffuf 字典不全
                                # → 必须给 urlfinder 足够时间(20 分钟)才能稳定抓到完整 URL 集合
                                # 也被 run_ffuf() 共用(命名遗留,不影响 ffuf 实际语义)
GAU_TIMEOUT = 120              # gau 单 host 超时(秒)
PER_HOST_OVERALL_TIMEOUT = 600 # dashboard subprocess 整体上限(用户拍板)

# hostname 校验(对齐 import_scan_results.py:HOSTNAME_RE)
HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")

# URL schema 后允许的合法 source
VALID_SOURCES = ("ffuf", "urlfinder", "gau")

# 危险路由关键词清单(用户决策 Q7:URL 路径里出现即标记 risk_flag)
# 涵盖:敏感操作 / 鉴权 / 调试 / 数据导出 / 配置 / 文件泄露 / 管理面板 / 框架特定
RISK_KEYWORDS = (
    # 敏感操作
    "delete", "remove", "destroy", "drop", "truncate", "reset", "purge",
    "kill", "shutdown", "terminate",
    # 鉴权 / 凭据
    "login", "signin", "signup", "register", "auth", "authenticate",
    "token", "session", "sso", "cas", "oauth", "jwt", "credentials",
    # 调试 / 内部
    "debug", "trace", "stacktrace", "exception", "errorlog",
    "actuator", "metrics", "healthcheck", "prometheus",
    # API / 文档暴露
    "swagger", "api-docs", "openapi", "graphql",
    # 数据导出 / 备份 / 凭据泄露
    "backup", "dump", "export", "import", "sql", "phpmyadmin",
    "phpinfo", ".env", ".git", "web.config", "htaccess",
    # 管理面板
    "admin", "administrator", "console", "manager", "dashboard",
    "wp-admin", "wp-login", "backend", "manage",
    # 命令 / 系统
    "shell", "cmd", "exec", "system", "runtime",
    # 框架特有
    "actuator", "jolokia", "env", "config",
)


def _detect_risk_flags(path: str) -> str:
    """扫描 path(小写)里出现的高危关键词,返回逗号分隔字符串。无 = 空串。

    仅匹配 path 部分(不含 query string / fragment)。
    关键词是子串匹配,不要求是完整段(例如 /v1/admin/login 会同时命中 admin 和 login)。
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
    """用户 2026-08-26 拍板:
      - 取 path 中最后一个 '.' 之后的小写后缀
      - 没有 '.' → 0 (非静态)
      - 后缀 == 'js' 或 'css' → 1 (静态)
      - 其它后缀(.png/.jpg/.svg/.woff 等)→ 0 (非静态,只看 js/css)
      - 后缀为空(路径以 '.' 结尾,如 /v1/)→ 0 (非静态)

    触发器 trg_whu_au 用 is_static=0 作 gate — 静态行永远不参与
    change_type 修改,严格满足"只有满足当前子域 && 非 js/css 才可能
    修改 change_type"。
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
# 基础工具
# ============================================================

def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _s(v: Any) -> Any:
    """Bind-safe coerce,与 import_scan_results._s 对齐。

    httpx / ffuf 字段偶发 title=None / dict / 异常类型,直接 bind 会炸。
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _log(stage: str, msg: str) -> None:
    """stderr 单写;dashboard subprocess 收 stdout,这里只 stderr 不污染。"""
    sys.stderr.write(f"[scan_urls][{stage}] {msg}\n")
    sys.stderr.flush()


# ============================================================
# 入口解析
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
    """wordlist 优先级:CLI --wordlist > 环境变量 SCAN_WORDLIST > 默认 common.txt"""
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            raise RuntimeError(f"wordlist 不存在: {p}")
        return p
    env = os.environ.get("SCAN_WORDLIST")
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise RuntimeError(f"SCAN_WORDLIST={env} 不存在")
        return p
    if DEFAULT_WORDLIST.exists():
        return DEFAULT_WORDLIST
    raise RuntimeError(
        f"默认 wordlist 不存在: {DEFAULT_WORDLIST};用 --wordlist 指定或 export SCAN_WORDLIST"
    )


# ============================================================
# 三个扫描器
# ============================================================

def run_ffuf(seed_url: str, wordlist: Path) -> list[dict]:
    """ffuf 单 host 爆破。返回 [{url, status_code, content_type, content_length, word_count}]。

    - 只保留"主域名内"的命中(`-fl 0` 过滤掉纯 404 等无响应)
    - status_code / content_type / length / words 全部来自 ffuf JSON
    """
    if not Path(FFUF_BIN).exists():
        raise RuntimeError(f"ffuf 不存在: {FFUF_BIN}")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        out_path = f.name
    try:
        cmd = [
            FFUF_BIN,
            "-u", f"{seed_url.rstrip('/')}/FUZZ",
            "-w", str(wordlist),
            "-mc", "200,201,202,204,301,302,307,308,401,403",  # 不扫 5xx
            "-ac",                                       # 自动 calibration(过滤噪音)
            "-fl", "0",                                  # 过滤 0 长响应
            "-rate", str(FFUF_RATE),
            "-timeout", str(FFUF_TIMEOUT),
            "-t", str(FFUF_THREADS),
            "-H", "User-Agent: Mozilla/5.0",
            "-of", "json",
            "-o", out_path,
            "-s",                                         # 静默,只输出 JSON 文件
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=URLFINDER_TIMEOUT
        )
        # ffuf 退出码非零大多是 rate-limit 触发,JSON 文件可能仍含部分命中
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
            "content_type": None,  # ffuf 不输出 content-type;留 None
            "content_length": r.get("length"),
            "word_count": r.get("words"),
        })
    return out[:URLFINDER_MAX_URLS]


def run_urlfinder(seed_url: str) -> list[dict]:
    """URLFinder(中文版 by pingc0y)主动爬虫。返回 [{url, status_code, content_length, title}]。

    - 命令:`URLFinder -m 3 -s all -o <dir> -u <url>`
    - 输出 JSON 结构(实测):
        {
          "domain": [...],   # 子域名
          "url": [...],      # 主动爬到的 URL(主目标域内)
          "urlOther": [...], # 站外 URL(可选录入)
          "js": [...],       # JS 文件
          "fuzz": [...],
          "info": {...}
        }
      每条 entry 字段(首字母大写,值是字符串):
        {
          "Url":     "...",
          "Status":  "200",       # 字符串,需 int() 转换
          "Size":    "34253",     # 字节数,字符串,需 int() 转换
          "Title":   "...",       # HTML title
          "Redirect":"...",
          "Source":  "..."        # 哪个页面里链过来的
        }
      我们抽 url + urlOther 两个 bucket,每个 entry 都提取 Size/Title/Status
      (之前漏 Size/Title,size 列一直是空,用户拍板"应该有")。
    - 也支持退路:不是 JSON 时按纯文本逐行解析。
    """
    if not Path(URLFINDER_BIN).exists():
        raise RuntimeError(f"URLFinder 不存在: {URLFINDER_BIN}")
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
                cmd, capture_output=True, text=True, timeout=URLFINDER_TIMEOUT
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
            """URLFinder/ffuf 的 Status/Size 字段是字符串("200" / "0")。

            特例:URLFinder 对"疑似危险路由,已跳过验证"的 URL 返回 Status="0"
            表示未探测 — 这种值映射成 None,避免被误判为 HTTP 0(无意义)。
            """
            if v is None or v == "":
                return None
            s = str(v).strip()
            # URLFinder 的"已跳过"状态:0 / 0.0
            if s in ("0", "0.0", "null", "None"):
                return None
            try:
                return int(float(s))
            except (ValueError, TypeError):
                return None

        # URLFinder 把"危险路由"信息直接放在 Title 字段里(如"疑似危险路由,已跳过验证")
        # 我们抽取成独立 bool 列 is_dangerous,并保留 Title 原文(title 字段照常入库)
        #   - is_dangerous = 1 表示该 URL 被 URLFinder 标记为危险
        #   - danger_reason  = 危险原因描述(通常="疑似危险路由,已跳过验证")
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
                    # 危险标记:URLFinder 把"危险"信号写在 Title 字段
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
            # 退路:顶层就是 list of dicts
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
    """gau 被动扫描(wayback + otx + commoncrawl)。

    - 输入:域名(hostname,不带 scheme)
    - 输出:txt,每行一个 URL
    """
    if not Path(GAU_BIN).exists():
        raise RuntimeError(f"gau 不存在: {GAU_BIN}")
    if not shutil.which(GAU_BIN) and not Path(GAU_BIN).exists():
        raise RuntimeError(f"gau 不存在: {GAU_BIN}")
    try:
        # gau 的 --timeout 是纯数字秒数(NOT "30s");version<2.2 老版本才支持 "30s"
        proc = subprocess.run(
            [GAU_BIN, "--threads", "5", "--timeout", "30", domain],
            capture_output=True, text=True, timeout=GAU_TIMEOUT,
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
# 入库
# ============================================================

def _parse_url(url: str) -> dict | None:
    """拆 url → {scheme, host, port, path}。失败返回 None(跳过)。"""
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
    """batch INSERT web_hash_urls。

    ON CONFLICT DO UPDATE 语义(SQLite 的 INSERT-or-REPLACE 简化版):
      - 新行 → INSERT, rowcount=1
      - 冲突行 → UPDATE(更新 last_seen / fetched_at / status_code 等), rowcount=1

    返回**新插入行数**(对比 rows 与 DB 已有集合的差集)。
    SQL rowcount 在 SQLite 里不可靠(更新也算 1),所以走"先查再插"两阶段。
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
            "title": r.get("title"),                # URLFinder 才有
            "content_type": r.get("content_type"),
            "content_length": r.get("content_length"),  # ffuf + URLFinder
            "word_count": r.get("word_count"),         # 仅 ffuf
            "redirect": r.get("redirect"),             # URLFinder 才有
            "link_source": r.get("link_source"),       # URLFinder 才有(来自哪个上游页面)
            "risk_flag": _detect_risk_flags(info["path"]),  # path 派生;所有 source 都算
            "is_dangerous": r.get("is_dangerous", 0),  # URLFinder 主动标记(从 Title 字段抽)
            "danger_reason": r.get("danger_reason", ""),
            "is_static": _detect_is_static(info["path"]),  # 路径后缀 .js/.css 判定
        })

    if not parsed_rows:
        return 0

    # Step 1: 取本 (hash_id, subdomain, source) 已有的 URL 集合(用于算 new)
    existing = {
        r[0] for r in conn.execute(
            "SELECT url FROM web_hash_urls "
            " WHERE hash_id = ? AND subdomain = ? AND source = ?",
            (hash_id, subdomain, source),
        ).fetchall()
    }

    # Step 2: INSERT OR IGNORE + UPDATE(分两步避免 rowcount 误报)
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
    """更新 web_hashes.url_count = 当前 active URL 数。"""
    conn.execute("""
        UPDATE web_hashes
           SET url_count = (
               SELECT COUNT(*) FROM web_hash_urls
                WHERE hash_id = ? AND is_active = 1
           )
         WHERE id = ?
    """, (hash_id, hash_id))


# ============================================================
# 主入口
# ============================================================

def _resolve_hash_id(conn: sqlite3.Connection, business_id: int, subdomain: str) -> int | None:
    """给定 (business, subdomain) → 找对应 hash_id。

    规则:取该 subdomain 下**所有**(含 is_active=0 历史行)的 web_subdomains,
    它们的 hash_id 在用户当前模型下应当一致(1 subdomain ≈ 1 hash);不一致时取
    subdomain_count 最大的那个作为主 hash。

    用户 2026-08-27 决策:不再限定 `is_active=1`。
    - 原因:URL 资产扫描的语义是"对子域",与 web_subdomains.is_active 维度独立;
      实际场景里 is_active=0 的子域往往仍有大量 web_hash_urls 历史数据(本次
      large-assets.example.com 即典型:web_subdomains is_active=0,web_hash_urls 3017 条)。
    - 副作用:scan_urls 写入会触发 trg_ws_au 把 is_active=0 的子域通过 is_active
      0→1 复活(写 change_type=4 / 6)。这是用户想要的"toggle 看复活"语义。
    - web_hash_urls 自身 is_active 不受此影响(一直由 persist() 决定)。
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
    """dashboard 调入口。返回码:
        0 全部成功
        2 hosts 文件错 / 校验失败
        3 子扫描器失败
        1 入库失败
    """
    db_path = Path(args.db).expanduser().resolve()
    business = args.business
    wordlist = _resolve_wordlist(args.wordlist)
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for s in sources:
        if s not in VALID_SOURCES:
            sys.stderr.write(f"错误:未知 source '{s}'(合法: {VALID_SOURCES})\n")
            return 2

    # 1) hosts 文件
    hosts_file = Path(args.hosts_file).expanduser().resolve()
    if not hosts_file.exists():
        sys.stderr.write(f"错误:hosts 文件不存在 {hosts_file}\n")
        return 2
    hosts = _read_hosts_file(hosts_file)
    if not hosts:
        sys.stderr.write("错误:hosts 文件为空\n")
        return 2
    if len(hosts) > MAX_HOSTS:
        sys.stderr.write(f"错误:host 数 {len(hosts)} 超过上限 {MAX_HOSTS}\n")
        return 2
    invalid = [h for h in hosts if not HOSTNAME_RE.match(h)]
    if invalid:
        sys.stderr.write(f"错误:hostname 不合法 {invalid[:5]}\n")
        return 2

    # 2) 业务存在?
    conn = sqlite3.connect(db_path)
    try:
        bid_row = conn.execute(
            "SELECT id FROM businesses WHERE TRIM(business_name) = ?",
            (business,),
        ).fetchone()
        if not bid_row:
            sys.stderr.write(f"错误:业务 '{business}' 不在 businesses 表\n")
            return 2
        business_id = int(bid_row[0])

        # 3) 逐 host 跑
        total_new = 0
        per_source_count = {s: 0 for s in sources}
        for host in hosts:
            hash_id = _resolve_hash_id(conn, business_id, host)
            if hash_id is None:
                _log("scan_urls", f"{host}: 该子域下无 active web_subdomains,跳过")
                continue
            # ffuf 需要 scheme://host(:port)/FUZZ;这里用 https 兜底,urlfinder 自带 url 形式
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

        # 4) 总结输出(stdout 给 dashboard 抓)
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
# CLI 框架
# ============================================================

def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="scan_urls.py",
        description="URL 资产扫描(ffuf / URLFinder / gau)→ web_hash_urls 表",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan-urls", help="dashboard 调入口:扫描指定 hosts 的 URL 资产")
    p.add_argument("--db", required=True)
    p.add_argument("--business", required=True)
    p.add_argument("--hosts-file", required=True)
    p.add_argument("--sources", required=True,
                   help=f"逗号分隔子集,合法: {','.join(VALID_SOURCES)}")
    p.add_argument("--wordlist", default=None)

    args = parser.parse_args(argv)

    # 工具二进制解析(env > PATH > 报错)。
    # 失败要让 dashboard 看到非零退出 + 清晰 stderr,而不是炸成 Python traceback。
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