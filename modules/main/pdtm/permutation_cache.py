"""alterx permutation state cache.

Subcommands:
  filter  - read the candidate permutations on stdin, filter out the ones already resolved / still cached / matched by wildcard resolution, output the rest
  record  - write the dnsx resolution results back into permutation_state

The nxdomain cache window is 30 days; a wordlist hash change triggers a full retry.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from alterx_runs import mark_ran_with_conn
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t

CACHE_DAYS = 30  # nxdomain retry window


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
            kept.append(perm)  # wordlist changed, retry
            continue
        if status in ("resolved", "wildcard_hit"):
            skipped_resolved += 1
            continue
        if status == "nxdomain" and next_at and parse_iso(next_at) > now_ts:
            skipped_cached += 1
            continue
        kept.append(perm)

    print(
        t("main.permutation_cache.counts", {
            "seen": len(seen), "kept": len(kept),
            "resolved": skipped_resolved, "cached": skipped_cached,
        }),
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
        # same commit as alterx_runs, a half state cannot be rolled back. also written
        # when there are no candidates, so that things like FORCE_ALTERX=1 correctly reset the cadence clock.
        mark_ran_with_conn(
            conn, business_id, wordlist_hash,
            candidates=len(rows), resolved=len(resolved_set),
        )
        conn.commit()
    finally:
        conn.close()
    if rows:
        print(t("main.permutation_cache.written", {"count": len(rows)}), file=sys.stderr)
    else:
        print(t("main.permutation_cache.no_candidates"), file=sys.stderr)
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=t("main.permutation_cache.description"))
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("filter", help=t("main.permutation_cache.arg_filter"))
    pf.add_argument("--db", required=True)
    pf.add_argument("--business-id", type=int, required=True)
    pf.add_argument("--wordlist-hash", required=True)

    pr = sub.add_parser("record", help=t("main.permutation_cache.arg_writeback"))
    pr.add_argument("--db", required=True)
    pr.add_argument("--business-id", type=int, required=True)
    pr.add_argument("--wordlist-hash", required=True)
    pr.add_argument("--candidates", required=True, help=t("main.permutation_cache.arg_alterx_out"))
    pr.add_argument("--resolved", required=True, help=t("main.permutation_cache.arg_dnsx_out"))

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
