#!/usr/bin/env python3
r"""target_glob.py — 把 target.txt / exclude.txt 编译为 glob 语义 ERE/正则。

设计:
- `*` 语义: glob 通配,匹配任意字符(包括 `.`)。等价 ERE `.*`。
- 锚定: 匹配主机名边界 `(^|\.)pattern$`,避免 `*.example.com` 误命中 `notexample.com`。
- Base 提取: 从右往左累积不含 `*` 的连续 label,首个含 `*` 的 label 处停止。
  例:
    cc-*.example.com   -> example.com
    *.example.com      -> example.com
    aaa.*.bbb.com      -> bbb.com
    ccc.bbb.com        -> ccc.bbb.com

CLI 用法(scan.sh 调用):
    python3 target_glob.py targets-ere  --input target.txt
    python3 target_glob.py excludes-ere --input exclude.txt
    python3 target_glob.py all-bases    --input target.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path


def extract_base(pattern: str) -> str:
    """Extract the base domain from a glob pattern (the rightmost run of labels without a *)."""
    parts = pattern.strip().lower().rstrip(".").split(".")
    base_parts: list[str] = []
    for label in reversed(parts):
        if not label or "*" in label:
            break
        base_parts.insert(0, label)
    return ".".join(base_parts)


def glob_to_anchored_ere(pattern: str) -> str:
    """glob -> anchored ERE.

    re.escape() escapes every regex metacharacter (including `*` -> `\\*`),
    then the escaped `\\*` is turned back into `.*`.
    Anchored to the hostname boundary: `(^|\\.)pattern$`.

    Note: the non-capturing group `(?:^|\\.)` cannot be used, because
    scan.sh:84/145-148 calls `grep -E` (POSIX ERE), where a non-capturing
    group is invalid and GNU grep silently reports 0 matches.
    The capturing group `(^|\\.)` has the same semantics and is valid ERE.
    """
    p = pattern.strip().lower().rstrip(".")
    if not p:
        return ""
    escaped = re.escape(p).replace(r"\*", ".*")
    return f"(^|\\.){escaped}$"


def compile_targets_ere(targets: Iterable[str]) -> str:
    """Return one ERE per line, ready to feed to grep -E; an empty input returns a regex that never matches."""
    patterns = [glob_to_anchored_ere(t) for t in targets]
    patterns = [p for p in patterns if p]
    if not patterns:
        return "^$)$"
    return "\n".join(patterns)


def matches_glob(host: str, pattern: str) -> bool:
    """Glob matching (for the host decision on the Python side).

    Uses the same anchoring semantics as the ERE compilation path
    (`glob_to_anchored_ere`), so that the Python side and the grep side
    cannot drift apart: the pattern is anchored to the hostname boundary
    and a subdomain prefix is allowed.
    """
    h = host.strip().lower().rstrip(".")
    ere = glob_to_anchored_ere(pattern)
    if not h or not ere:
        return False
    try:
        return re.search(ere, h) is not None
    except re.error:
        return False


def matches_any_glob(host: str, patterns: Iterable[str]) -> bool:
    return any(matches_glob(host, p) for p in patterns if p and p.strip())


def compile_exclude_patterns(excludes: Iterable[str]) -> list[re.Pattern]:
    """Every exclude is compiled as a glob into an anchored ERE (no keyword branch, decision B)."""
    patterns: list[re.Pattern] = []
    for item in excludes:
        value = item.strip().lower().rstrip(".")
        if not value or value.startswith("#"):
            continue
        ere = glob_to_anchored_ere(value)
        if not ere:
            continue
        try:
            patterns.append(re.compile(ere))
        except re.error as e:
            print(f"[target_glob] skip bad exclude {value!r}: {e}", file=sys.stderr)
    return patterns


def matches_exclude_glob(host: str, excludes: Iterable[str]) -> bool:
    h = host.strip().lower().rstrip(".")
    return any(p.search(h) for p in compile_exclude_patterns(excludes))


def _read_scope_lines(path: str) -> list[str]:
    """Read a file, skipping blank lines and # comments, lowercasing and dropping the trailing dot, deduplicating while keeping the order."""
    seen: set[str] = set()
    out: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            v = line.strip().lstrip("﻿").lower().rstrip(".")
            if not v or v.startswith("#"):
                continue
            token = v.split()[0] if v.split() else ""
            if not token or token.startswith("#"):
                continue
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def partition_alive_hosts(alive_path: str, bases: list[str], output_dir: str) -> dict:
    """Bucket alive.txt by base: every host goes to the longest matching
    base, and a host with no match goes to unmatched.

    The longer match wins: the more label segments a base has, the more
    specific it is (a subdomain base is longer than the root domain base).
    Example: bases=[example.com, api.example.com], host=dev.api.example.com
        -> matches api.example.com (longer, more specific).
    Two bases of the same length never match each other by accident (two
    bases of the same length that contain one another would have a
    different number of labels, so their lengths differ).

    Files written:
      <output_dir>/alive.base.<base>   one host per line
      <output_dir>/alive.unmatched     hosts that could not be classified
                                       (in theory this cannot happen,
                                       because alive.txt already went
                                       through targets.regex in stage 1.5)
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    sorted_bases = sorted({b for b in bases if b}, key=lambda b: -len(b))

    handles: dict[str, object] = {}
    counts: dict[str, int] = {b: 0 for b in sorted_bases}
    unmatched_path = out / "alive.unmatched"
    unmatched_count = 0
    total = 0

    for b in sorted_bases:
        handles[b] = (out / f"alive.base.{b}").open("w", encoding="utf-8")
    unmatched = unmatched_path.open("w", encoding="utf-8")

    try:
        with open(alive_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                h = line.strip().lower().rstrip(".")
                if not h:
                    continue
                total += 1
                assigned = False
                for b in sorted_bases:
                    if h == b or h.endswith("." + b):
                        handles[b].write(h + "\n")
                        counts[b] += 1
                        assigned = True
                        break
                if not assigned:
                    unmatched.write(h + "\n")
                    unmatched_count += 1
    finally:
        for h in handles.values():
            h.close()
        unmatched.close()

    return {"total": total, "unmatched": unmatched_count, "counts": counts}


def main() -> int:
    ap = argparse.ArgumentParser(description="target.txt / exclude.txt glob 编译器")
    ap.add_argument(
        "mode",
        choices=["targets-ere", "excludes-ere", "base", "all-bases", "all-glob-check", "partition-alive"],
    )
    ap.add_argument("--input", required=True, help="scope 文件 / alive.txt 路径")
    ap.add_argument("--bases", default="", help="partition-alive 用,逗号分隔 base 列表")
    ap.add_argument("--output-dir", default="", help="partition-alive 用,分桶输出目录")
    args = ap.parse_args()

    if args.mode == "partition-alive":
        if not args.bases:
            sys.exit("[partition-alive] --bases 不能为空")
        if not args.output_dir:
            sys.exit("[partition-alive] --output-dir 不能为空")
        bases = [b.strip().lower().rstrip(".") for b in args.bases.split(",") if b.strip()]
        if not bases:
            sys.exit("[partition-alive] --bases 解析后为空")
        stats = partition_alive_hosts(args.input, bases, args.output_dir)
        n_assigned = sum(stats["counts"].values())
        print(
            f"[partition-alive] total={stats['total']} assigned={n_assigned} "
            f"unmatched={stats['unmatched']} bases={len(bases)}",
            file=sys.stderr,
        )
        for b, c in stats["counts"].items():
            if c > 0:
                print(f"  {b}: {c}", file=sys.stderr)
        if stats["unmatched"] > 0:
            print(f"  unmatched: {stats['unmatched']} (详见 {args.output_dir}/alive.unmatched)", file=sys.stderr)
        return 0

    lines = _read_scope_lines(args.input)

    if args.mode == "targets-ere":
        print(compile_targets_ere(lines))
    elif args.mode == "excludes-ere":
        # uses the same compilation logic as targets (no keyword branch)
        print(compile_targets_ere(lines))
    elif args.mode == "base":
        for ln in lines:
            print(extract_base(ln))
    elif args.mode == "all-bases":
        for b in dict.fromkeys(extract_base(ln) for ln in lines):
            if b:
                print(b)
    elif args.mode == "all-glob-check":
        # self-check: list the base of every pattern (for debug / verification)
        for ln in lines:
            print(f"{ln}\t{extract_base(ln)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
