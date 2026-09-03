#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icp_mapp_query.py

从 stdin 读取公司名（每行一个），调用本地 ymicp 的 /query/mapp 接口，
抓取全部分页并把小程序备案信息写入 SQLite。

用法：
  # 单个查询并设置归属业务，默认写入 ../db/recon.sqlite3
  echo 'ExampleCo|ExampleCo子公司有限公司' | python3 icp_mapp_query.py

  # 批量查询（每行可写“业务名|公司名”或单独公司名）
  python3 icp_mapp_query.py < companies.txt

  # 自定义参数
  echo 'TestBiz' | python3 icp_mapp_query.py --base http://127.0.0.1:16181 \
                                     --user admin --pass '你的密码' \
                                     --db ../db/recon.sqlite3
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "db/recon.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    business_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    business_id INTEGER REFERENCES businesses(id),
    unit_name TEXT NOT NULL UNIQUE,
    nature_name TEXT,
    main_licence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mapp_records (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    source_data_id INTEGER,
    service_name TEXT NOT NULL,
    service_licence TEXT NOT NULL UNIQUE,
    service_type INTEGER,
    content_type_name TEXT,
    domain TEXT,
    record_updated_at TEXT,
    fetched_at TEXT NOT NULL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_mapp_company_id
    ON mapp_records(company_id);
CREATE INDEX IF NOT EXISTS idx_mapp_service_name
    ON mapp_records(service_name);
CREATE INDEX IF NOT EXISTS idx_mapp_record_updated_at
    ON mapp_records(record_updated_at);
"""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="stdin 公司名 → ymicp /query/mapp → SQLite"
    )
    p.add_argument("--base", default="http://127.0.0.1:16181",
                   help="ymicp base URL（默认 http://127.0.0.1:16181）")
    p.add_argument("--user", default=os.environ.get("YMICP_USER", "admin"),
                   help="Basic Auth 用户名（也可环境变量 YMICP_USER）")
    p.add_argument("--pass", dest="password",
                   default=os.environ.get("YMICP_PASS", ""),
                   help="Basic Auth 密码（也可环境变量 YMICP_PASS）")
    p.add_argument("-b", "--business",
                   help="业务模式：从 DB 加载该业务下所有子公司并抓取；"
                        "严格匹配 businesses.business_name；此模式下 stdin 被忽略")
    p.add_argument("--db", default=str(DEFAULT_DB_PATH),
                   help=f"SQLite 路径（默认 {DEFAULT_DB_PATH}）")
    p.add_argument("--page-size", type=int, default=10,
                   help="每页条数（默认 10）")
    p.add_argument("--pages", type=int, default=0,
                   help="最多抓取页数（默认 0=全部）")
    p.add_argument("--sleep", type=float, default=30.0,
                   help="每个 ymicp 请求之间的间隔秒数（默认 30.0）")
    p.add_argument("--json", action="store_true",
                   help="额外打印最后一页原始 JSON（中文）到 stderr")
    return p.parse_args()


def fetch_mapp(base: str, name: str, page_num: int, page_size: int,
               auth: tuple, timeout: int = 30) -> Dict[str, Any]:
    """调 ymicp /query/mapp，返回原始 JSON。"""
    url = f"{base.rstrip('/')}/query/mapp"
    params = {"search": name, "pageNum": page_num, "pageSize": page_size}
    r = requests.get(url, params=params, auth=auth, timeout=timeout)
    r.raise_for_status()
    return r.json()


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    """兼容多种返回结构：{params: {list: [...]}} / {data: [...]} / [...] / 单 dict。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            records = params.get("list")
            if isinstance(records, list):
                return [x for x in records if isinstance(x, dict)]
        for k in ("data", "records", "list", "items", "results"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # 看起来就是单条记录本身
        if any(f in payload for f in ("serviceName", "unitName", "mainLicence")):
            return [payload]
    return []


def extract_last_page(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None

    containers = []
    params = payload.get("params")
    if isinstance(params, dict):
        containers.append(params)
    containers.append(payload)

    for container in containers:
        for key in ("lastPage", "pages", "totalPages"):
            if key not in container:
                continue
            try:
                return int(container[key])
            except (TypeError, ValueError):
                pass
    return None


def open_database(path: str) -> Tuple[sqlite3.Connection, Path]:
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        company_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(companies)")
        }
        if "business_id" not in company_columns:
            conn.execute(
                "ALTER TABLE companies ADD COLUMN business_id INTEGER "
                "REFERENCES businesses(id)"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_companies_business_id "
            "ON companies(business_id)"
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn, db_path


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def lookup_business_subsidiaries(
    conn: sqlite3.Connection, business_name: str
) -> List[str]:
    """按业务名查 DB，返回该业务下所有子公司 unit_name 列表（按字母排序）。

    业务名严格匹配；命中 0 业务时抛 ValueError。
    """
    row = conn.execute(
        "SELECT id FROM businesses WHERE business_name = ?",
        (business_name,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"业务 '{business_name}' 在数据库 businesses 中不存在"
        )
    business_id = int(row[0])
    rows = conn.execute(
        "SELECT unit_name FROM companies WHERE business_id = ? "
        "ORDER BY unit_name",
        (business_id,),
    ).fetchall()
    return [r[0] for r in rows]


def prepare_records(
    records: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    unique: Dict[str, Dict[str, Any]] = {}
    skipped = 0
    for record in records:
        unit_name = clean_text(record.get("unitName"))
        service_name = clean_text(record.get("serviceName"))
        service_licence = clean_text(record.get("serviceLicence"))
        if not unit_name or not service_name or not service_licence:
            skipped += 1
            continue
        unique[service_licence] = record
    return list(unique.values()), skipped


def persist_records(
    conn: sqlite3.Connection,
    records: List[Dict[str, Any]],
    business_name: Optional[str] = None,
) -> Tuple[int, int, int, int]:
    valid_records, skipped = prepare_records(records)
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    company_ids: Dict[str, int] = {}
    inserted = 0
    updated = 0
    unchanged = 0

    with conn:
        business_id = None
        if business_name is not None:
            conn.execute(
                "INSERT INTO businesses (business_name) VALUES (?) "
                "ON CONFLICT(business_name) DO NOTHING",
                (business_name,),
            )
            business_row = conn.execute(
                "SELECT id FROM businesses WHERE business_name = ?",
                (business_name,),
            ).fetchone()
            if business_row is None:
                raise sqlite3.IntegrityError(f"无法读取业务 ID：{business_name}")
            business_id = int(business_row[0])

        company_records: Dict[str, Dict[str, Any]] = {}
        for record in valid_records:
            unit_name = clean_text(record.get("unitName"))
            if unit_name is not None:
                company_records[unit_name] = record

        for unit_name, record in company_records.items():
            conn.execute(
                """
                INSERT INTO companies (
                    business_id, unit_name, nature_name, main_licence,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(unit_name) DO UPDATE SET
                    business_id = COALESCE(
                        excluded.business_id, companies.business_id
                    ),
                    nature_name = COALESCE(excluded.nature_name, companies.nature_name),
                    main_licence = COALESCE(excluded.main_licence, companies.main_licence),
                    updated_at = CASE
                        WHEN COALESCE(excluded.business_id, companies.business_id)
                                 IS NOT companies.business_id
                          OR COALESCE(excluded.nature_name, companies.nature_name)
                                 IS NOT companies.nature_name
                          OR COALESCE(excluded.main_licence, companies.main_licence)
                                 IS NOT companies.main_licence
                        THEN excluded.updated_at
                        ELSE companies.updated_at
                    END
                """,
                (
                    business_id,
                    unit_name,
                    clean_text(record.get("natureName")),
                    clean_text(record.get("mainLicence")),
                    fetched_at,
                    fetched_at,
                ),
            )
            row = conn.execute(
                "SELECT id FROM companies WHERE unit_name = ?", (unit_name,)
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError(f"无法读取公司 ID：{unit_name}")
            company_ids[unit_name] = int(row[0])

        for record in valid_records:
            unit_name = clean_text(record.get("unitName"))
            service_licence = clean_text(record.get("serviceLicence"))
            if unit_name is None or service_licence is None:
                continue

            raw_json = json.dumps(
                record, ensure_ascii=False, separators=(",", ":")
            )
            existing = conn.execute(
                "SELECT raw_json FROM mapp_records WHERE service_licence = ?",
                (service_licence,),
            ).fetchone()
            if existing is None:
                inserted += 1
            else:
                try:
                    changed = json.loads(existing[0]) != record
                except (json.JSONDecodeError, TypeError):
                    changed = existing[0] != raw_json
                if not changed:
                    unchanged += 1
                    continue
                updated += 1

            conn.execute(
                """
                INSERT INTO mapp_records (
                    company_id, source_data_id, service_name, service_licence,
                    service_type, content_type_name, domain, record_updated_at,
                    fetched_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_licence) DO UPDATE SET
                    company_id = excluded.company_id,
                    source_data_id = excluded.source_data_id,
                    service_name = excluded.service_name,
                    service_type = excluded.service_type,
                    content_type_name = excluded.content_type_name,
                    domain = excluded.domain,
                    record_updated_at = excluded.record_updated_at,
                    fetched_at = excluded.fetched_at,
                    raw_json = excluded.raw_json
                """,
                (
                    company_ids[unit_name],
                    record.get("dataId"),
                    clean_text(record.get("serviceName")),
                    service_licence,
                    record.get("serviceType"),
                    clean_text(record.get("contentTypeName")),
                    clean_text(record.get("domain")),
                    clean_text(record.get("updateRecordTime")),
                    fetched_at,
                    raw_json,
                ),
            )

    return inserted, updated, unchanged, skipped


def main() -> int:
    args = parse_args()
    auth = (args.user, args.password) if args.password else None

    business_override = clean_text(args.business)
    if args.business is not None and business_override is None:
        print("--business 不能为空", file=sys.stderr)
        return 1

    try:
        raw = sys.stdin.read()
    except KeyboardInterrupt:
        return 130

    queries: List[Tuple[Optional[str], str]] = []
    conn: Optional[sqlite3.Connection] = None
    db_path: Optional[Path] = None
    had_error = False

    try:
        if business_override is not None:
            # 业务模式：从 DB 加载该业务下所有子公司
            try:
                conn, db_path = open_database(args.db)
            except (OSError, sqlite3.Error) as error:
                print(f"数据库初始化失败：{error}", file=sys.stderr)
                return 1
            if raw.strip():
                print("注意：-b 模式下忽略 stdin 输入", file=sys.stderr)
            try:
                subsidiaries = lookup_business_subsidiaries(
                    conn, business_override
                )
            except ValueError as error:
                print(f"错误：{error}", file=sys.stderr)
                return 1
            if not subsidiaries:
                print(
                    f"错误：业务 '{business_override}' 在数据库中暂无子公司，"
                    f"请先入库公司",
                    file=sys.stderr,
                )
                return 1
            print(
                f"[业务模式] 业务 '{business_override}'："
                f"已加载 {len(subsidiaries)} 家子公司"
            )
            for name in subsidiaries:
                print(f"  - {name}")
            queries = [(business_override, name) for name in subsidiaries]
        else:
            # 旧模式：stdin 输入
            if not raw.strip():
                print(
                    "用法：echo '公司全称' | python3 icp_mapp_query.py",
                    file=sys.stderr,
                )
                return 1
            for line_number, line in enumerate(raw.splitlines(), 1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                line_business = None
                company_name = text
                if "|" in text:
                    business_part, company_part = text.split("|", 1)
                    line_business = clean_text(business_part)
                    company_name = company_part.strip()
                    if line_business is None or not company_name:
                        print(
                            f"第 {line_number} 行格式错误，应为：业务名|公司名",
                            file=sys.stderr,
                        )
                        return 1
                queries.append((line_business, company_name))
            if not queries:
                print("stdin 没有有效公司名", file=sys.stderr)
                return 1
            try:
                conn, db_path = open_database(args.db)
            except (OSError, sqlite3.Error) as error:
                print(f"数据库初始化失败：{error}", file=sys.stderr)
                return 1

        for index, (business_name, name) in enumerate(queries, 1):
            print(f"\n[{index}/{len(queries)}] 查询词：{name}")
            print(f"归属业务：{business_name or '未指定'}")

            all_records: List[Dict[str, Any]] = []
            page_num = 1
            pages_used = 0
            last_payload = None
            query_complete = True

            while True:
                try:
                    payload = fetch_mapp(
                        args.base, name, page_num, args.page_size, auth
                    )
                except requests.HTTPError as error:
                    print(f"HTTP 错误：{error}", file=sys.stderr)
                    query_complete = False
                    had_error = True
                    break
                except requests.RequestException as error:
                    print(f"网络错误：{error}", file=sys.stderr)
                    query_complete = False
                    had_error = True
                    break

                last_payload = payload
                records = extract_records(payload)
                all_records.extend(records)
                pages_used += 1
                last_page = extract_last_page(payload)

                if not records:
                    if last_page is not None and page_num < last_page:
                        print(
                            f"第 {page_num} 页为空，但接口声明共有 {last_page} 页",
                            file=sys.stderr,
                        )
                        query_complete = False
                        had_error = True
                    break
                if last_page is not None and page_num >= last_page:
                    break
                if args.pages and page_num >= args.pages:
                    break
                if last_page is None and len(records) < args.page_size:
                    break

                page_num += 1
                time.sleep(args.sleep + random.uniform(0, 0.5))

            if args.json and last_payload is not None:
                print(
                    "\n[debug JSON] "
                    + json.dumps(last_payload, ensure_ascii=False, indent=2),
                    file=sys.stderr,
                )

            if not query_complete:
                print(f"抓取页数：{pages_used}")
                print(f"API 记录：{len(all_records)}")
                print("写入状态：查询不完整，本次未写入")
                print(f"数据库：{db_path}")
                continue

            try:
                inserted, updated, unchanged, skipped = persist_records(
                    conn, all_records, business_name
                )
            except sqlite3.Error as error:
                print(f"数据库写入失败：{error}", file=sys.stderr)
                had_error = True
                continue

            print(f"抓取页数：{pages_used}")
            print(f"API 记录：{len(all_records)}")
            print(f"有效唯一记录：{inserted + updated + unchanged}")
            print(
                f"新增：{inserted}，更新：{updated}，"
                f"未变化：{unchanged}，跳过：{skipped}"
            )
            print(f"数据库：{db_path}")

            if index < len(queries):
                time.sleep(args.sleep + random.uniform(0, 0.5))

    finally:
        if conn is not None:
            conn.close()

    return 1 if had_error else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\033[33m[中断]\033[0m", file=sys.stderr)
        sys.exit(130)
