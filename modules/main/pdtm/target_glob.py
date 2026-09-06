#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""target_glob.py — 把 target.txt / exclude.txt 编译为 glob 语义 ERE/正则。

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
from pathlib import Path
from typing import Iterable


def extract_base(pattern: str) -> str:
    """从 glob pattern 提取 base 域(最右连续不含 * 的 label 段)。"""
    parts = pattern.strip().lower().rstrip(".").split(".")
    base_parts: list[str] = []
    for label in reversed(parts):
        if not label or "*" in label:
            break
        base_parts.insert(0, label)
    return ".".join(base_parts)


def glob_to_anchored_ere(pattern: str) -> str:
    """glob -> 锚定 ERE。

    re.escape() 转义所有正则元字符(包括 `*` → `\\*`),然后把转义后的 `\\*` 还原为 `.*`。
    锚定到主机名边界:`(^|\\.)pattern$`。

    注:不能用 `(?:^|\\.)` 非捕获组,因为 scan.sh:84/145-148 调用的是
    `grep -E`(POSIX ERE),非捕获组在 ERE 不合法,GNU grep 静默返回 0 命中。
    用 `(^|\\.)` 捕获组等价语义、合法 ERE。
    """
    p = pattern.strip().lower().rstrip(".")
    if not p:
        return ""
    escaped = re.escape(p).replace(r"\*", ".*")
    return f"(^|\\.){escaped}$"


def compile_targets_ere(targets: Iterable[str]) -> str:
    """返回可喂给 grep -E 的一行一条 ERE,空输入返回永不匹配的正则。"""
    patterns = [glob_to_anchored_ere(t) for t in targets]
    patterns = [p for p in patterns if p]
    if not patterns:
        return "^$)$"
    return "\n".join(patterns)


def matches_glob(host: str, pattern: str) -> bool:
    """glob 匹配(用于 Python 端的 host 判定)。

    与 ERE 编译路径 (`glob_to_anchored_ere`) 用同一套锚定语义,避免 Python 端
    与 grep 端行为分裂:pattern 锚定到主机名边界,允许子域名前缀。
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
    """exclude 全部按 glob 编译为锚定 ERE(无 keyword 分支,B 决定)。"""
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
    """读文件,跳过空行和 # 注释,统一小写去尾点,去重保序。"""
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
    """把 alive.txt 按 base 分桶:每个 host 归到最长匹配的 base,无匹配→unmatched。

    长匹配优先:base label 段越长 = 越具体(子域名 base 比根域名 base 长)。
    例: bases=[example.com, api.example.com], host=dev.api.example.com
        → 匹配 api.example.com(更长、更具体)。
    bases 之间长度相同但不相同的话不会互相误匹配(同长度 base 若有包含关系则
    base label 数不同 → 长度必不同)。

    写文件:
      <output_dir>/alive.base.<base>   一行一个 host
      <output_dir>/alive.unmatched     无法归类的 host(理论上不应出现,因 alive.txt
                                       已在 stage 1.5 过 targets.regex)
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
        # 与 targets 用同一套编译逻辑(无 keyword 分支)
        print(compile_targets_ere(lines))
    elif args.mode == "base":
        for ln in lines:
            print(extract_base(ln))
    elif args.mode == "all-bases":
        for b in dict.fromkeys(extract_base(ln) for ln in lines):
            if b:
                print(b)
    elif args.mode == "all-glob-check":
        # 自检: 列出每个 pattern 的 base(给 debug / 验证用)
        for ln in lines:
            print(f"{ln}\t{extract_base(ln)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
