"""katana_runner.py — katana subprocess 包装 + JSONL 解析 + 入 web_hash_urls。

与 modules/main/pdtm/scan_urls.py 的关系:
  - 不依赖 scan_urls.py 内部函数(单向 subprocess 边界)
  - 入库风格对齐:_s() coerce / now() / PRAGMA foreign_keys / with conn 事务 /
    INSERT OR IGNORE + ON CONFLICT UPDATE(见 modules/main/pdtm/scan_urls.py:566-610)
  - 失败吞掉 best-effort(跟 import_scan_results → score.py 同款)

CLI:
  python3 katana_runner.py enrich \
    --db db/recon.sqlite3 \
    --business ExampleCo \
    --hosts-file /tmp/hosts.txt

输出(stdout JSON):
  {"ok": true, "business": "ExampleCo", "n_hosts": N, "n_rows_scanned": N,
   "n_new": N, "n_dangerous": N, "n_errors": N, "elapsed_s": N.N}

环境变量:
  KATANA_BIN              二进制路径(env > shutil.which > 报错)
  KATANA_TIMEOUT          单 host 超时秒数(默认 600;大字典场景用户拍板)
  KATANA_EXTRA_FLAGS      追加 flags(空格分隔),例如 --no-sandbox
  KATANA_OMIT_BODY=1      关闭 body 落库(节省存储,response_hash 退化为 status|length)

  # 速率/线程控制(对齐 httpx srcradar 配置 scanner.sh:328: -rate-limit 50)
  KATANA_RATE_LIMIT       全局 rate-limit req/s(默认 50,与 httpx 同款;0=走 katana 默认 150)
  KATANA_RATE_LIMIT_MIN   全局 rate-limit req/min(默认 0=不加)
  KATANA_CONCURRENCY      并发 fetchers 数(默认 10;0=走 katana 默认 10)
  KATANA_PARALLELISM      并发 input 数(默认 10;0=走 katana 默认 10)
  KATANA_HOST_RATE_LIMIT  单 host rate-limit req/s(默认 0=不限)
  KATANA_DELAY            请求间隔秒数(默认 0=不加,配 SRC 站点防 WAF 可设 1)
  KATANA_REQUEST_TIMEOUT  单请求超时秒数(默认 10;与 httpx -timeout 5 不同,katana
                          较重,SRC 站点给 10s 更稳)
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

SOURCE = "katana"

DEFAULT_TIMEOUT = 600  # 与 scan_urls.py 的 SCAN_URLS_TIMEOUT=600 同款(用户拍板)


def _resolve_bin(env_var: str, fallback_name: str) -> str:
    """env > shutil.which > 报错。

    跟 modules/main/pdtm/scan_urls.py:_resolve_bin 完全一致的策略,
    不硬编码 /home/ubuntu/...(踩过的坑)。
    """
    p = os.environ.get(env_var)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    which = shutil.which(fallback_name)
    if which:
        return which
    raise FileNotFoundError(
        f"找不到 {fallback_name};设 {env_var} 环境变量或装到 PATH"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _coerce(v):
    """None / '' / 0 都返回 None(避免入库后被触发器误判)。"""
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
    """跟 scan_urls.py:701 bid_row 同款:TRIM(business_name)=? 取 id。"""
    row = conn.execute(
        "SELECT id FROM businesses WHERE TRIM(business_name)=?",
        (biz_name,),
    ).fetchone()
    return row[0] if row else None


def _resolve_hash_id(conn: sqlite3.Connection, business_id: int, host: str) -> int | None:
    """同 (business, subdomain) → 最近 hash_id(取最大 web_subdomains.id 那一行)。

    跟 scan_urls.py:637 _resolve_hash_id 完全一致逻辑;
    不可用时 caller 自己 INSERT web_hashes(本期不实现,fallback 给 None 跳过)。
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
    """构造 katana 命令。

    - -jsonl           JSONL 输出(每行一条 Result)
    - -js-crawl        JS 文件解析(endpoint 提取)
    - -form-extraction 表单字段提取
    - -xhr-extraction  XHR 调用 URL 提取
    - -eof             排除指定字段(精简输出大小)
    - -crawl-out-scope 危险路由 regex 拦截(risks.py 同款)
    - depth 3 / timeout 10 / crawl-duration 300 给上限,避免长跑失控
    """
    # ---- 速率 / 线程控制(对齐 httpx srcradar 配置)----
    # 默认值在 lib/katana_runner.py 顶部 docstring 列出;env 覆盖 → 0/空 = 不加 flag
    # 用 _int_env 读,失败返回 0(= katana 默认,不加 flag)
    def _int_env(name: str, default: int = 0) -> int:
        """读 env int。env 未设或非数字 → 返回 default。

        默认值映射(对齐 httpx srcradar scanner.sh:328 温和档):
          KATANA_RATE_LIMIT       default 50  (httpx -rate-limit 50)
          KATANA_CONCURRENCY      default 10  (katana 默认也是 10,显式声明)
          KATANA_PARALLELISM      default 10
          KATANA_REQUEST_TIMEOUT  default 10  (httpx -timeout 5 对 katana 偏紧)
        其它 KATANA_RATE_LIMIT_MIN / KATANA_HOST_RATE_LIMIT / KATANA_DELAY 默认 0(不启用)
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
        # 精简输出:raw / stored_response_path / knowledgebase / forms / xhr_requests
        # 都是大块数据,web_hash_urls 表用不到,只留 body(已精简到我们需要的)
        "-eof", "raw,stored_response_path,knowledgebase,forms,xhr_requests",
        "-silent",
    ]

    # 请求级超时(单请求,跟 KATANA_TIMEOUT 单 host 超时区分)
    req_timeout = _int_env("KATANA_REQUEST_TIMEOUT", 10)
    cmd += ["-timeout", str(req_timeout)]

    # 速率 / 并发(对齐 httpx rate-limit=50,threads=10)
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
    """跑一次 katana,返回 parse 后的 list[dict]。

    失败处理:
      - timeout / non-zero exit / 解析失败 → 返回 [],stderr 一行说明
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
            # 跳过单行坏 JSON,不整批 fail(katana 偶尔尾部会有空行/警告)
            continue
    return rows


def _upsert_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    business_id: int,
    fetched_at: str,
) -> tuple[int, int, int]:
    """入库 web_hash_urls;返回 (n_new, n_dangerous, n_errors)。

    SQL 完全照搬 scan_urls.py:566-610 的范式:
      INSERT OR IGNORE + ON CONFLICT(hash_id, subdomain, url, source) DO UPDATE
    """
    # 一次性把 (hash_id, subdomain, source) 已有 URL 集合拉出来,后面判断 new
    # 同 (business, host) 共享 hash_id,见 _resolve_hash_id
    by_subdomain: dict[str, int] = {}  # host → hash_id
    for host in {r["subdomain"] for r in rows if r.get("subdomain")}:
        hid = _resolve_hash_id(conn, business_id, host)
        if hid is not None:
            by_subdomain[host] = hid
    # 没 hash_id 的 host 跳过 — 本期不做 web_hashes 自创建(后续可补)
    skipped_no_hash = 0
    for r in rows:
        if r.get("subdomain") and r["subdomain"] not in by_subdomain:
            skipped_no_hash += 1
    rows = [r for r in rows if r.get("subdomain") in by_subdomain]

    if not rows:
        return 0, 0, skipped_no_hash

    # 已有 URL 集合(用于算 new_count)
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
                # body 字段:默认 KATANA_OMIT_BODY=0 → 落库(zlib level=6);
                # 设 KATANA_OMIT_BODY=1 时 schema_map 已把 body 字符串回填
                # "",_compress_body 探测 env 后落 NULL,跟既有 column 的 None 收敛一致.
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
    """主入口:对一个业务名下所有 host 跑 katana + 入库。"""
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
            # 双重保险:入库前再过 risks.py gate
            mapped: list[dict] = []
            for r in rows:
                endpoint = (r.get("request") or {}).get("endpoint") or ""
                dang = is_dangerous_url(endpoint)
                m = map_row(
                    r,
                    business_id=business_id,
                    hash_id=0,  # 临时占位,_upsert_rows 内部重算
                    fetched_at=fetched_at,
                    is_dangerous=dang,
                )
                if m is not None:
                    mapped.append(m)
            total_scanned += len(mapped)

            if not mapped:
                continue

            try:
                with conn:  # 事务
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
        description="katana URL 资产扫描 → web_hash_urls",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enrich", help="对一组 host 跑 katana 并入库")
    p.add_argument("--db", required=True, help="recon.sqlite3 路径")
    p.add_argument("--business", required=True, help="业务名(businesses.business_name)")
    p.add_argument("--hosts-file", required=True, help="子域列表文件,一行一个")
    p.add_argument("--dry-run", action="store_true", help="只跑 katana,不写库")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.cmd == "enrich":
        return cmd_enrich(args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
