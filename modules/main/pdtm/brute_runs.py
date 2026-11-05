"""shuffledns cadence control: decides whether this round runs the brute derivation.

Subcommands:
  should-run  - look up brute_runs.last_ran_at and compare it against the cadence in days.
               first run (no row) => treated as expired, exit 0; within the cadence (fresh) => exit 1.
  mark        - write the current time into brute_runs.last_ran_at (UPSERT).

Environment variables / flags:
  BRUTE_CADENCE_DAYS / --cadence-days   cadence in days, default 30; 0 means "always expired" (run every time)
  FORCE_BRUTE / --force                 1 means ignore the cadence and force one run; applies to both should-run and mark
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
# The i18n lookup lives in modules/main/lib, which is not on sys.path at this
# depth. Walk up to the repo root (the directory that holds "modules") and add
# the lib directory to the path.
for _p in Path(__file__).resolve().parents:
    if (_p / "modules").is_dir():
        sys.path.insert(0, str(_p / "modules" / "main" / "lib"))
        break
from i18n import t

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
       True  -> caller should run brute; return code 0
       False -> caller should skip brute; return code 1
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT last_ran_at FROM brute_runs WHERE business_id=?",
            (business_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return True, t("main.brute_runs.no_history")

    last_ts = parse_iso(row[0])
    if last_ts == 0.0:
        return True, t("main.brute_runs.timestamp_unparsable")

    age = datetime.now(timezone.utc).timestamp() - last_ts
    age_days = age / 86400.0
    if age_days >= cadence_days:
        return True, t("main.brute_runs.overdue", {"age_days": age_days, "cadence_days": cadence_days})
    return False, t("main.brute_runs.not_due", {"age_days": age_days, "cadence_days": cadence_days})


def mark_ran(db_path: str, business_id: int, wordlist_hash: str,
             candidates: int, resolved: int) -> None:
    """Standalone upsert. Opens its own connection and commits."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO brute_runs
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
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=t("main.brute_runs.cadence_control"))
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("should-run", help=t("main.brute_runs.should_run_help"))
    ps.add_argument("--db", required=True)
    ps.add_argument("--business-id", type=int, required=True)
    ps.add_argument("--cadence-days", type=int,
                    default=int(os.environ.get("BRUTE_CADENCE_DAYS", DEFAULT_CADENCE_DAYS)))
    ps.add_argument("--force", action="store_true",
                    help=t("main.brute_runs.force_help"))

    pm = sub.add_parser("mark", help=t("main.brute_runs.mark_help"))
    pm.add_argument("--db", required=True)
    pm.add_argument("--business-id", type=int, required=True)
    pm.add_argument("--wordlist-hash", required=True)
    pm.add_argument("--candidates", type=int, default=0)
    pm.add_argument("--resolved", type=int, default=0)

    args = p.parse_args()

    if args.cmd == "should-run":
        forced = args.force or os.environ.get("FORCE_BRUTE") == "1"
        if forced:
            print(t("main.brute_runs.forced"), file=sys.stderr)
            return 0
        run, reason = should_run(args.db, args.business_id, args.cadence_days)
        print(f"[brute_runs] {reason}", file=sys.stderr)
        return 0 if run else 1

    if args.cmd == "mark":
        mark_ran(args.db, args.business_id, args.wordlist_hash,
                 args.candidates, args.resolved)
        print(f"[brute_runs] mark business_id={args.business_id}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
