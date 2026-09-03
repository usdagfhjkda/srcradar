#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alterx 排列状态缓存。

子命令:
  filter  - 读 stdin 上的候选排列,过滤掉已解析/缓存未到期/泛解析命中的项,输出剩余
  record  - 把 dnsx 解析结果写回 permutation_state

nxdomain 缓存窗口 30 天,词表 hash 变更触发全量重试。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from alterx_runs import mark_ran_with_conn

CACHE_DAYS = 30  # nxdomain 重试窗口


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


def base_of(permutation: str) -> str:
    parts = permutation.lower().rstrip(".").split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return permutation.lower().rstrip(".")


def filter_permutations(db_path: str, business_id: int, wordlist_hash: str,
                        candidates: Iterable[str]) -> list[str]:
    conn = sqlite3.connect(db_path)
    cached: dict[str, tuple[str, str | None, str]] = {}
    try:
        for row in conn.execute(
            "SELECT permutation, status, next_attempt_at, wordlist_hash "
            "FROM permutation_state WHERE business_id = ?",
            (business_id,),
        ):
            cached[row[0]] = (row[1], row[2], row[3])
    finally:
        conn.close()

    now_ts = time.time()
    kept: list[str] = []
    skipped_resolved = 0
    skipped_cached = 0
    seen: set[str] = set()
    for line in candidates:
        perm = line.strip().lower().rstrip(".")
        if not perm or perm in seen:
            continue
        seen.add(perm)
        row = cached.get(perm)
        if row is None:
            kept.append(perm)
            continue
        status, next_at, wlh = row
        if wlh != wordlist_hash:
            kept.append(perm)  # 词表变更,重新尝试
            continue
        if status in ("resolved", "wildcard_hit"):
            skipped_resolved += 1
            continue
        if status == "nxdomain" and next_at and parse_iso(next_at) > now_ts:
            skipped_cached += 1
            continue
        kept.append(perm)

    print(
        f"[permutation_cache] 输入去重 {len(seen)}, 保留 {len(kept)}, "
        f"跳过已解析 {skipped_resolved}, 跳过缓存未到期 {skipped_cached}",
        file=sys.stderr,
    )
    return kept


def record_permutations(db_path: str, business_id: int, wordlist_hash: str,
                          candidates: Iterable[str], resolved: Iterable[str]) -> int:
    resolved_set: set[str] = set()
    for line in resolved:
        first = line.strip().split()[0] if line.strip() else ""
        if first:
            resolved_set.add(first.lower().rstrip("."))

    now = now_iso()
    next_retry = (datetime.now(timezone.utc) + timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")

    rows: list[tuple] = []
    for line in candidates:
        perm = line.strip().lower().rstrip(".")
        if not perm:
            continue
        status = "resolved" if perm in resolved_set else "nxdomain"
        next_at = None if status == "resolved" else next_retry
        rows.append((business_id, base_of(perm), perm, status, wordlist_hash, now, next_at))

    conn = sqlite3.connect(db_path)
    try:
        if rows:
            conn.executemany(
                """INSERT INTO permutation_state
                   (business_id, base_domain, permutation, status, wordlist_hash,
                    last_attempt_at, next_attempt_at, attempts)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(business_id, base_domain, permutation) DO UPDATE SET
                     status = excluded.status,
                     wordlist_hash = excluded.wordlist_hash,
                     last_attempt_at = excluded.last_attempt_at,
                     next_attempt_at = excluded.next_attempt_at,
                     attempts = permutation_state.attempts + 1
                """,
                rows,
            )
        # 与 alterx_runs 在同一 commit,半状态无法回滚。无候选时也写
        # alterx_runs,这样 FORCE_ALTERX=1 之类也能正确重置节奏时钟。
        mark_ran_with_conn(
            conn, business_id, wordlist_hash,
            candidates=len(rows), resolved=len(resolved_set),
        )
        conn.commit()
    finally:
        conn.close()
    if rows:
        print(f"[permutation_cache] 写回 {len(rows)} 条 (alterx_runs 同事务标记)", file=sys.stderr)
    else:
        print("[permutation_cache] 无候选,但仍标记 alterx_runs 以重置节奏时钟", file=sys.stderr)
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="alterx 排列状态缓存")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("filter", help="过滤已解析/缓存未到期的 permutation")
    pf.add_argument("--db", required=True)
    pf.add_argument("--business-id", type=int, required=True)
    pf.add_argument("--wordlist-hash", required=True)

    pr = sub.add_parser("record", help="写回 dnsx 解析结果")
    pr.add_argument("--db", required=True)
    pr.add_argument("--business-id", type=int, required=True)
    pr.add_argument("--wordlist-hash", required=True)
    pr.add_argument("--candidates", required=True, help="alterx 输出文件")
    pr.add_argument("--resolved", required=True, help="dnsx 解析后输出文件")

    args = p.parse_args()

    if args.cmd == "filter":
        kept = filter_permutations(args.db, args.business_id, args.wordlist_hash, sys.stdin)
        for perm in kept:
            print(perm)
        return 0

    if args.cmd == "record":
        with open(args.candidates, encoding="utf-8", errors="replace") as f:
            candidates = f.readlines()
        with open(args.resolved, encoding="utf-8", errors="replace") as f:
            resolved = f.readlines()
        record_permutations(args.db, args.business_id, args.wordlist_hash, candidates, resolved)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
