#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alterx 节奏控制:决定本轮是否跑 alterx 派生。

子命令:
  should-run  - 查 alterx_runs.last_ran_at,与 cadence 天数比较。
               首跑(无行)=> 视为过期,退 0;节奏内(新鲜)=> 退 1。
  mark        - 把当前时间写入 alterx_runs.last_ran_at (UPSERT)。
               接受 --conn 标志以便并入外层事务(与 permutation_state.record 共享 commit)。

环境变量 / 标志:
  ALTERX_CADENCE_DAYS / --cadence-days   节奏天数,默认 30,设 0 即"永远过期"(每跑都跑)
  FORCE_ALTERX / --force                 1 表示忽略节奏强制跑一次,should-run 与 mark 都生效
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DEFAULT_CADENCE_DAYS = 30


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0.0


def should_run(db_path: str, business_id: int, cadence_days: int) -> tuple[bool, str]:
    """Returns (should_run, reason). Exit code semantics:
       True  -> caller should run alterx; return code 0
       False -> caller should skip alterx; return code 1
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT last_ran_at FROM alterx_runs WHERE business_id=?",
            (business_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return True, "无历史记录(首跑)"

    last_ts = parse_iso(row[0])
    if last_ts == 0.0:
        return True, "历史时间戳无法解析,视为过期"

    age = datetime.now(timezone.utc).timestamp() - last_ts
    age_days = age / 86400.0
    if age_days >= cadence_days:
        return True, f"已过 {age_days:.1f} 天 ≥ 节奏 {cadence_days} 天"
    return False, f"上次跑在 {age_days:.1f} 天前 < 节奏 {cadence_days} 天"


def mark_ran(db_path: str, business_id: int, wordlist_hash: str,
             candidates: int, resolved: int) -> None:
    """Standalone upsert. Opens its own connection and commits."""
    conn = sqlite3.connect(db_path)
    try:
        _upsert_on_conn(conn, business_id, wordlist_hash, candidates, resolved)
        conn.commit()
    finally:
        conn.close()


def mark_ran_with_conn(conn: sqlite3.Connection, business_id: int,
                       wordlist_hash: str, candidates: int, resolved: int) -> None:
    """Public hook: caller owns the transaction so this UPSERT commits
    atomically with the caller's other writes (used by
    permutation_cache.record_permutations to keep alterx_runs + permutation_state
    writes in one commit — half-state is unrecoverable)."""
    _upsert_on_conn(conn, business_id, wordlist_hash, candidates, resolved)


def _upsert_on_conn(conn: sqlite3.Connection, business_id: int,
                    wordlist_hash: str, candidates: int, resolved: int) -> None:
    conn.execute(
        """INSERT INTO alterx_runs
           (business_id, last_ran_at, wordlist_hash, candidates, resolved)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(business_id) DO UPDATE SET
             last_ran_at = excluded.last_ran_at,
             wordlist_hash = excluded.wordlist_hash,
             candidates = excluded.candidates,
             resolved = excluded.resolved
        """,
        (business_id, now_iso(), wordlist_hash, candidates, resolved),
    )


def main() -> int:
    p = argparse.ArgumentParser(description="alterx 节奏控制")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("should-run", help="判断本轮是否跑 alterx")
    ps.add_argument("--db", required=True)
    ps.add_argument("--business-id", type=int, required=True)
    ps.add_argument("--cadence-days", type=int,
                    default=int(os.environ.get("ALTERX_CADENCE_DAYS", DEFAULT_CADENCE_DAYS)))
    ps.add_argument("--force", action="store_true",
                    help="强制返回 run,忽略节奏(env FORCE_ALTERX=1 也开)")

    pm = sub.add_parser("mark", help="写回 last_ran_at")
    pm.add_argument("--db", required=True)
    pm.add_argument("--business-id", type=int, required=True)
    pm.add_argument("--wordlist-hash", required=True)
    pm.add_argument("--candidates", type=int, default=0)
    pm.add_argument("--resolved", type=int, default=0)

    args = p.parse_args()

    if args.cmd == "should-run":
        forced = args.force or os.environ.get("FORCE_ALTERX") == "1"
        if forced:
            print(f"[alterx_runs] FORCE_ALTERX 强制本轮跑", file=sys.stderr)
            return 0
        run, reason = should_run(args.db, args.business_id, args.cadence_days)
        print(f"[alterx_runs] {reason}", file=sys.stderr)
        return 0 if run else 1

    if args.cmd == "mark":
        mark_ran(args.db, args.business_id, args.wordlist_hash,
                 args.candidates, args.resolved)
        print(f"[alterx_runs] mark business_id={args.business_id}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
