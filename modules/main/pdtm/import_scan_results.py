#!/usr/bin/env python3
"""将 scanner.sh 的 Web/TCP 结果写入 recon.sqlite3。"""

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


DEFAULT_DB = Path(__file__).resolve().parent.parent / "db" / "recon.sqlite3"
DEFAULT_SCAN_DIR = Path(__file__).resolve().parent / "scan_results"

# 单点扫描模式 (scan-onesite 子命令) 用的常量。
# 2026-10-03: HTTPX_BIN 路径解析优先级 — env HTTPX_BIN 覆盖 > config/pdtm.conf httpx_bin > 默认 bin/httpx。
# 与 pdtm/lib/resolve_tools.sh 的 bash helper 行为对齐(单源 config)。

def _resolve_httpx_bin() -> str:
    """解析 httpx 二进制路径,优先级:
    1. 环境变量 HTTPX_BIN (caller 覆盖)
    2. config/pdtm.conf 的 httpx_bin (由 install.sh / patches/install.sh 写入)
       — 路径由 SRCRADAR_CONF 环境变量覆盖,默认 <repo>/config/pdtm.conf
    3. 默认 <script_dir>/bin/httpx (与本脚本同目录)

    返回第一个 Path.exists() 的路径;都不存在时返回默认路径(由 caller 检查).
    """
    # 1. 环境变量
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

    # 3. 默认
    return str(Path(__file__).resolve().parent / "bin" / "httpx")


def _find_pdtm_conf() -> "Path | None":
    """定位 config/pdtm.conf 路径。
    优先级: 环境变量 SRCRADAR_CONF > 相对脚本位置推断 <repo>/config/pdtm.conf.
    返回 None 当无法推断。
    """
    env_val = os.environ.get("SRCRADAR_CONF")
    if env_val:
        return Path(env_val)
    # import_scan_results.py 在 modules/main/pdtm/,上 3 层 = <repo>/
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[2]  # pdtm -> main -> <repo>
    return repo_root / "config" / "pdtm.conf"


HTTPX_BIN = _resolve_httpx_bin()
HOSTNAME_RE = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9]*\.)+[a-zA-Z]{2,}$")
MAX_HOSTS = 200            # 单次 scan-onesite 上限
HTTPX_TIMEOUT = 120        # httpx subprocess 超时（秒）


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _s(v: Any) -> Any:
    """Bind-safe coerce: None→None; str/int/float 原样; dict/list/其它容器→str()。

    httpx JSON 字段偶尔会出现 title=None / technologies=dict / status_code=int
    之类,直接传给 sqlite3 binding 会抛 "Error binding parameter N - probably
    unsupported type"。强转成 str 后 SQLite binding 不会炸,空容器→'' 也是合法 TEXT。
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float)):
        return v
    return str(v)


def _normalize_tech(v: Any) -> Any:
    """把 technologies 归一化成稳定的字符串形态。

    httpx 的 tech 字段恒为 list[str]。原先它走通用的 _s() 强转, 而 _s() 对
    list 一律 str(v), 于是 Python 的 list 字面量表示被当成字符串存进库:
        str(['Alibaba Cloud CDN'])  ->  "['Alibaba Cloud CDN']"

    同一份技术栈, 取决于当次扫描走的是 httpx JSONL 主路径还是 raw text 兜底
    路径, 落库形态就在 "['X']" / "X" / NULL 之间跳 —— STEP 1 的 technologies
    快字段比较于是命中, 判成变更, 而 body 其实逐字节相同。

    格式沿用仓库内已有约定:
        list -> ",".join(str(t) for t in v)      # ["A","B"] -> "A,B"
        str  -> 原样
        None -> None

    刻意不复用 _s(): _s 是通用的 bind-safe 强转, 被 title / content_type 等
    共用, 在那里改行为会波及其他字段。
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="scanner.sh 结果入库工具")
    parser.add_argument("--business", required=True, help="业务名称")
    parser.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--scan-dir", default=str(DEFAULT_SCAN_DIR), help="scanner 输出目录")
    parser.add_argument("--target-file", default="target.txt", help="可测资产文件")
    parser.add_argument("--exclude-file", default="exclude.txt", help="非可测资产文件")
    parser.add_argument("--wildcard-file", default="wildcard.txt", help="泛解析域文件,这些域在 scopes 表里 is_wildcard=1")
    parser.add_argument("--dry-run", action="store_true", help="只解析和统计，不写数据库、不删除文件")
    return parser.parse_args()


def clean_line(line: str) -> str | None:
    line = line.strip().lstrip("﻿")
    if not line or line.startswith("#"):
        return None
    return line.split()[0].strip().lower().rstrip(".")


def read_scope_file(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"范围文件不存在: {path}")
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
    """glob 匹配 (与 scan.sh ERE 路径同语义,见 target_glob.matches_glob)。"""
    from target_glob import matches_glob
    return matches_glob(host, scope)


def split_excludes(excludes: list[str]) -> tuple[list[str], list[str]]:
    """兼容旧调用:返回 (空列表, 清洗后的 exclude 列表)。

    旧版分域名/关键词两组,新版全部按 glob 编译,无 keyword 分支 (B 决定)。
    保留这个函数是为了不破坏其他引用;实际过滤走 matches_exclude()。
    """
    cleaned = []
    for item in excludes:
        value = item.strip().lower().rstrip(".")
        if not value or value.startswith("#"):
            continue
        cleaned.append(value)
    return ([], cleaned)


def matches_exclude(host: str, excludes: list[str]) -> bool:
    """glob 编译 + 锚定 re.search (走 target_glob,无 keyword 分支)。"""
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
    """从 httpx jsonl 的 `input` 字段取真实 subdomain。

    阶段 8 的 input 格式是 "<sub>,<ip>:<port>" (scanner.sh 阶段 8 构造),
    httpx 连的是 IP, 所以 jsonl 里的 `url` 只有 IP 字面量、不含 subdomain。
    `input` 保留了构造时的原始字符串, 是唯一可靠的资产身份来源。

    返回规范化后的 subdomain (小写、去尾部点), 无法解析时返回 None,
    由调用方回退到 IP 反查路径。刻意不接受裸 IP 作为 real_sub —— 那种情况
    说明 input 不是 stage 8 格式, 交给下游兜底更诚实。
    """
    if not isinstance(raw_input, str):
        return None
    value = raw_input.strip()
    if not value or "," not in value:
        return None
    candidate = value.split(",", 1)[0].strip().lower().rstrip(".")
    if not candidate:
        return None
    # 裸 IP 不是 subdomain(没有 Host 头的语义), 交给下游 IP 反查
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
            raise ValueError(f"{path}:{number} 不是有效 JSONL: {exc}") from exc
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
                # 2026-10-21: 真实 subdomain。阶段 8 的 input 格式是
                # "<sub>,<ip>:<port>", httpx 连的是 IP, 所以上面的 `url` 只有
                # IP、没有 sub; 而 `input` 保留了原始 "<sub>,<ip>:<port>"。
                # 下游 load_web_records() 靠这个字段确定资产身份, 不再靠 IP 反查
                # 猜域名 (见该函数 fanout 注释)。real_sub=None 时下游走兜底。
                "real_sub": _real_sub_from_input(item.get("input")),
                "status_code": first_value(item, "status-code", "status_code", "statusCode"),
                "content_length": first_value(item, "content-length", "content_length", "contentLength"),
                "title": first_value(item, "title"),
                # 归一化成稳定形态 (见 _normalize_tech): httpx 的 tech 是
                # list[str], 直接强转会落成 Python 的 list 字面量表示,
                # 形态逐日跳变 -> 快字段比较误判成变更。
                "technologies": _normalize_tech(first_value(item, "tech", "technologies")),
                "response_hash": response_hash(item),
                "body_simhash": body_simhash(item),
                "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                # 2026-10-03: 提取 host_ip + a[] → ips 列(下游 _upsert_web_records 写入 DB)。
                # httpx JSONL 顶层有 host_ip(string) 和 a(list[str]) 字段,
                # 合并去重保留顺序。raw_json parse 是兜底路径(老数据 / httpx 字段改名)。
                "ips": _extract_ips_from_item(item),
                "content_type": first_value(item, "content_type", "contentType") or "",
                # PATCH-FINAL-URL-2026-10-28: httpx 跟随 -follow-redirects 后的
                # 最终落点。2xx 响应里该字段不存在 -> 空串 (比较时自动跳过);
                # 3xx 响应里是稳定的域名形态, 不随扫描阶段连 IP 还是连域名而变。
                # 不能用 url 列代替: url 的形态逐日会翻转 (域名 <-> IP),
                # 补进快字段会把"形态抖动"误判成变更。
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
    """从 httpx JSONL item 顶层提取 host_ip + a[] 合并去重,JSON 数组字符串。

    返回值固定为 '["ip1","ip2",...]' 或 '[]'。下游 _upsert_web_records 优先用
    record['ips'];若为空则 fallback parse raw_json。"""
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

    # 2026-10-21: 删掉 IP -> 多域名 fanout, key 去掉 response_hash。
    #
    # 为什么删 fanout:
    #   阶段 8 每条 input 已经是一个域名一条探测 (scanner.sh 阶段 8 对该 IP 下
    #   每个 sub 各生成一行 "<sub>,<ip>:<port>"), 每条结果的 Host 头不同、响应
    #   可以完全不同。之前 _resolve_host() 把 host(IP) 反查成该 IP 下的全部域名,
    #   再把同一条 record (含 body/status_code/content_length/raw_json) 无差别
    #   复制给每个域名 —— 共享 CDN 入口的 IP 上有多个 vhost 时, 别的 Host 头的
    #   响应就被挂到了本域名名下。实测: 一个 IP 上10 个域名, 其中一个的 3 个 port
    #   拿到的都是另一个站点(跨 Host 跳转终点)的页面, 每天 content_length 抖动
    #   ~千字节, diff 每天误报 changed。前提已验证: 每域名形态单一(混合 0/N),
    #   说明每域名本来就有独立探测结果, 删掉复制不丢数据。
    #
    # 为什么 key 去掉 response_hash:
    #   web_subdomains 的唯一键是 (business_id, subdomain, port), 不含 hash。
    #   hash 在 key 里时, 同一资产在同一轮出现两个 hash 会产生两条 record,
    #   两条都进 _upsert_web_records 打同一行, 谁最后写谁赢 —— 顺序即真相。
    #   去掉后一资产一条 record, 裁决权交回数据库唯一键。
    #
    # hash 的角色不变: 仍然作为 payload 传给 _upsert_web_records, 由
    # web_hashes 的 UNIQUE(business_id, response_hash) 内容寻址决定 hash_id 是
    # 新增还是复用历史簇(hash 内容回退时复用旧簇, 现有代码的 ON CONFLICT +
    # 二次 SELECT 已覆盖, 不需要在这里判断)。
    records: dict[tuple[str, int], dict[str, Any]] = {}
    fallback_ip_count = 0
    for record in raw:
        hash_value = record.get("response_hash") or f"{record['status_code']}|{record['content_length']}"
        record["response_hash"] = hash_value

        # 资产身份优先用 input 里的真实 subdomain; 拿不到才回退 IP 反查。
        domain = record.get("real_sub")
        if not domain:
            domains = _resolve_host(record["host"], ip_domains)
            domain = domains[0] if domains else record["host"]
            fallback_ip_count += 1
        record["domain"] = domain

        # 不做跨域名复制。同一 (domain, port) 后到的覆盖先到的 —— 与
        # web_subdomains 的 ON CONFLICT(business_id, subdomain, port) 语义一致。
        records[(domain, record["port"])] = record

    if fallback_ip_count:
        # 兜底路径的 record 响应字段来自 IP 反查, 可信度低于 input 路径。
        # 只记计数, 不静默: 上游格式变了这里会显式暴露。
        print(
            "[import] WARN: %d/%d 条 web record 的 input 无真实 subdomain, "
            "已回退 IP 反查确定资产身份"
            % (fallback_ip_count, len(raw)),
            file=sys.stderr,
        )
    return list(records.values())


def load_tcp_records(scan_dir: Path, ip_domains: dict[str, list[str]]) -> list[dict[str, Any]]:
    # non_cdn_tcp_ports.txt 现在是 scanner 在 IP:端口 层面剔除 web 后的非web端口,
    # 每行即 IP:端口。资产身份 = (ip, port),hosts 记录解析到该 IP 的域名。
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
    # tcp_assets 旧 schema(host 主键)迁移:资产身份改为 (business_id, ip, port)。
    # 库内数据不重要,检测到旧列结构直接重建(无历史数据迁移)。
    tcp_cols = {row[1] for row in conn.execute("PRAGMA table_info(tcp_assets)").fetchall()}
    if tcp_cols and "ip" not in tcp_cols:
        conn.execute("DROP TABLE tcp_assets")
    # web_subdomains 旧 schema(UNIQUE (hash_id, subdomain, port),无 business_id)迁移:
    # 资产身份改为 (business_id, subdomain, port)。SQLite 不能直接 DROP UNIQUE,
    # 所以整表重建,迁移时按 (business_id, subdomain, port) 去重,
    # 保留每组中 is_active=1 / last_seen 最新 / id 最大的那行。
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
    # web_subdomains 新增 ips 列(2026-10-04):import_scan_results.py:798-813
    # 写入 httpx JSONL 提取的 host_ip + a[] 数组,schema.sql 已加,
    # 老库需要 ALTER 升级。
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
    # 旧库迁移:scopes 表加 is_wildcard 列 + permutation_state 表
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

    # brute_runs (2026-09-15 新增):与 alterx_runs 同结构,补齐已有库
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
    """插入 / 刷新 web_hashes + web_subdomains 行；不触碰同业务其它 is_active=1 行。

    Returns:
      (hash_ids, web_count, new_hash_ids)
        hash_ids       — 这次扫到的所有 hash (response_hash -> hash_id)
        web_count      — 写入 / 更新的 web_subdomains 行数
        new_hash_ids   — 这次扫描**新增**的 web_hashes 行 id（首次出现）

    由两类调用方使用：
      1. persist() 内部（cron 全量路径）。persist() 自己负责先 UPDATE ... is_active=0 全清场。
      2. cmd_scan_onesite（dashboard "加一行看看" 轻量路径）。调用方不做 deactivate。

    每条 record 需要的键：response_hash、host、port、domain（可缺，回退到 host）；
    可选：url / status_code / content_length / title / technologies / raw_json。
    response_hash 缺失的 record 会被静默丢弃（与 persist() 原行为一致）。

    Caller 负责 commit。本函数不开启 / 提交事务。

    跨项目依赖：
      `new_hash_ids` 由 persist() 用作参数调
        ../daily/lib/score.py score-new --db <db> --ids <ids>
      来给新 hash 设初始 score（cron-only）。cmd_scan_onesite() 不调（手动加的
      不自动评分；运营人员可以在 dashboard 里手动设）。见 README §"Web Hash 评分"。
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
        # 存在性预检：这是 INSERT vs UPDATE 唯一可靠的判别手段
        # （SQLite 的 ON CONFLICT DO UPDATE 不暴露这个信号）
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
        # 2026-10-03: 写 ips 列(从 httpx JSONL 提取的 host_ip + a[] 数组)。
        # 优先 record['ips'](parse_json_web 已经从 item 顶层提取),
        # 若空则 fallback parse raw_json(老数据 / httpx 字段改名时的兜底)。
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
        # 2026-10-07 refactor: trg_ws_au 已 no-op 化,不再 set change_type=2。
#     复活位的初值由 Python 显式写 (commit 2)。
        # Look up the simhash on both sides; if both are present and the
        # hamming distance is within threshold, the body is "near-duplicate"
        # (e.g. ad slot, timestamp jitter, CDN whitespace drift) so we
        # downgrade change_type to 0 to suppress false-positive changes.
        #
        # DEPRECATED 2026-09-16: this whole simhash branch has been superseded
        # by the algo_diff block immediately below. The branch is kept for
        # now (a) so the existing simhash infrastructure (hash_simhash_map
        # table + hamming helper) stays documented in source, and (b) as a
        # fallback if algo_diff is ever disabled. The branch is currently a
        # no-op — algo_diff owns the final change_type decision. See
        # modules/main/daily/lib/algo_diff/README.md for the rationale.
        # 2026-10-07 refactor: DEPRECATED simhash 近邻降级写 change_type=0 (commit 1 后续).
        # E 已由 if False 早退,本注释仅为与 B 风格统一,便于 grep 发现。
        # 原因: import 阶段不应写 change_type=0,按分工 "diff 写 0/2"。
        # 原 simhash 降级逻辑 (回滚时把 if False 改成 if True: 即可):
        #   - 取 prior_response_hash 与 new response_hash 的 body_simhash
        #   - 若 hamming_simhash(...) <= SIMHASH_HAMMING_THRESHOLD 则写 change_type = 0
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
    # 2026-10-08: reset ct for this business。临时关闭 trigger + diff.py + algo_diff.write
    # 路径之后, web_subdomains.change_type 唯一的 reset 入口。后续 diff 阶段
    # (未开启时) 才从 baseline=0 开始累计 1/2/4/8。
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

    # --- cron-only: 给新增的 hash 设初始 score ---
    # 见 README §"Web Hash 评分"。失败时吞掉（best-effort）；import 本身已成功。
    # Subprocess 调 ../daily/lib/score.py 是跨项目解耦：daily 包可以独立升级，
    # 这里只关心"有没有给新 hash 打分"。
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

    print(f"业务: {args.business}")
    print(f"可测范围: {len(targets)}，非可测范围: {len(excludes)}，泛解析域: {len(wildcards)}")
    print(f"Web 记录: {len(web)}，TCP 记录: {len(tcp)}，缺少 hash 的 Web: {missing_hash}")
    if not web and not tcp:
        print("错误: 没有找到可入库的 Web 或 TCP 结果", file=sys.stderr)
        return 1
    if missing_hash:
        print("错误: 存在缺少 httpx response hash 的 Web 记录，拒绝提交", file=sys.stderr)
        return 1
    if args.dry_run:
        print("dry-run：未写入数据库，未删除扫描文件")
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
        print(f"入库失败，扫描文件已保留: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"入库成功: Web {web_count} 条，TCP {tcp_count} 条")
    return 0


# ---------------------------------------------------------------------------
# scan-onesite 子命令（轻量模式）
# ---------------------------------------------------------------------------
# 不走 dnsx / cdnmatch / naabu 等前置阶段。直接 subprocess 跑 httpx，结果用
# _upsert_web_records() 写两行表。**不** 触发 persist() 的 is_active=0 清场，
# 调用方只用一次 BEGIN/COMMIT 把 web_records 落库，commit 退出即结束。
# ---------------------------------------------------------------------------

def _parse_scan_onesite_args(argv: list[str]) -> argparse.Namespace:
    """scan-onesite 子命令的 argparse。与 main() 的 parse_args() 不共享。"""
    parser = argparse.ArgumentParser(
        prog="import_scan_results.py scan-onesite",
        description="仅扫描增量域名并入库（同业务其它 is_active=1 行不动）",
    )
    parser.add_argument("--business", required=True, help="业务名")
    parser.add_argument("--hosts-file", required=True, type=Path,
                        help="一行一个域名的输入文件，可写 # 注释")
    parser.add_argument("--db", default=os.environ.get("DB") or os.environ.get("RECON_DB") or str(DEFAULT_DB), help="SQLite 数据库路径")
    return parser.parse_args(argv)


def _read_hosts_file(path: Path) -> list[str]:
    """读 hosts 文件：跳过空行 + # 注释；去重保序；strip 小写；去尾点。"""
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
    """同步跑 httpx → 解析 JSONL → 返回 web_records。

    失败模式：FileNotFoundError / TimeoutExpired / 非零退出且空 stdout
    均抛 RuntimeError，由调用方映射成 exit code。
    """
    if not Path(HTTPX_BIN).exists():
        raise RuntimeError(f"找不到 httpx：{HTTPX_BIN}")

    stdin_blob = ("\n".join(hosts) + "\n").encode("utf-8")
    # 不带 -l:httpx 默认从 stdin 读 host 列表。scanner.sh 用 -l <file> + < /dev/null;
    # 这里走"无临时文件"路线,完全靠 input= 喂 stdin。
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
        raise RuntimeError(f"无法启动 httpx：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"httpx 超时（{HTTPX_TIMEOUT}s）") from exc

    if proc.returncode != 0 and not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace").strip() or f"exit={proc.returncode}"
        raise RuntimeError(f"httpx 失败：{err[:300]}")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".jsonl") as f:
            f.write(proc.stdout)
            tmp_path = Path(f.name)
        try:
            return parse_json_web(tmp_path)
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"解析 httpx 输出失败：{exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def cmd_scan_onesite(args: argparse.Namespace) -> int:
    """仅 httpx 扫描指定 hosts → web_hashes + web_subdomains；不动 is_active 其它行。"""
    db_path = Path(args.db).expanduser().resolve()
    business = args.business

    # 1) 读 + 校验 hosts
    hosts_file = args.hosts_file.expanduser().resolve()
    if not hosts_file.exists():
        sys.stderr.write(f"错误：找不到 hosts 文件：{hosts_file}\n")
        return 2
    hosts = _read_hosts_file(hosts_file)
    if not hosts:
        sys.stderr.write("错误：hosts 文件为空（全部是空行 / 注释）\n")
        return 2
    if len(hosts) > MAX_HOSTS:
        sys.stderr.write(f"错误：域名数量 {len(hosts)} 超过上限 {MAX_HOSTS}\n")
        return 2
    invalid = [h for h in hosts if not HOSTNAME_RE.match(h)]
    if invalid:
        sys.stderr.write(
            f"错误：hostname 不合法：{', '.join(invalid[:5])}\n"
        )
        return 2

    # 2) 跑 httpx
    try:
        web_records = _run_httpx(hosts)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3

    # 3) 入库（PRAGMA + with conn: 包事务；不解锁/不刷 tcp_assets）
    fetched_at = now()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            create_tables(conn)
            # businesses 表平时由 ymicp 拥有 / 共享方维护;scan-onesite 是任意时刻入口,
            # 兜底建一下(缺则建,否则什么都不做),避免改其它项目的 DDL。
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
            # scan-onesite: 不调 score.py（手动加的 hash 留给运营在 dashboard 手动设分）
            _upsert_web_records(conn, business_id, fetched_at, web_records)
    except sqlite3.Error as exc:
        sys.stderr.write(f"错误：入库失败：{exc}\n")
        return 1
    finally:
        conn.close()

    sys.stdout.write(
        f"[+] 扫描完成：{len(web_records)} 条写入 {business} (业务 id={business_id})\n"
    )
    return 0


def _dispatch() -> int:
    """argv 首位为 'scan-onesite' 走轻量；其它（无参 / --business / --scan-dir）保留原 main() 行为。"""
    if len(sys.argv) > 1 and sys.argv[1] == "scan-onesite":
        sub_args = _parse_scan_onesite_args(sys.argv[2:])
        return cmd_scan_onesite(sub_args)
    return main()


if __name__ == "__main__":
    raise SystemExit(_dispatch())
