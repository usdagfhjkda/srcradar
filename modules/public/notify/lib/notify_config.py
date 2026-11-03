#!/usr/bin/env python3
"""
modules/public/notify/lib/notify_config.py — 解析 config/notify.conf

格式 (同 modules/main/lib/load_config.py):
    key=value        value 末尾空白自动 trim
    # 整行注释     行内 # 后内容忽略

用法:
    python3 notify_config.py <config_path>           # 输出 shell-eval 行
    python3 notify_config.py <config_path> <key>     # 输出单 key 值
    python3 notify_config.py --help                  # 输出本 help

设计:
    - 与 modules/main/lib/load_config.py 走同款 parse_config 模式,
      但语义简化:仅解析键值对,不区分 type / section。
    - 真凭据绝不写入 stdout 之外的任何 sink(本脚本只 print,不写文件)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DOC = """\
notify_config.py — 解析 config/notify.conf (key=value)

用法:
    python3 notify_config.py <config_path>           输出 shell-eval 形式
    python3 notify_config.py <config_path> <key>     输出单 key 值
    python3 notify_config.py --help                  本帮助

退出码: 0=ok 1=参数 2=文件不存在 3=语法
"""


def parse_config(path: Path) -> dict[str, str] | None:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[notify_config] cannot read {path}: {exc}", file=sys.stderr)
        return None
    for raw in text.splitlines():
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        if not raw.strip():
            continue
        if "=" not in raw:
            print(f"[notify_config] syntax error (no '='): {raw!r}", file=sys.stderr)
            return None
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.rstrip()
        if not KEY_RE.match(key):
            print(f"[notify_config] invalid key: {key!r}", file=sys.stderr)
            return None
        out[key] = value
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(DOC, file=sys.stderr)
        return 1
    if sys.argv[1] in ("-h", "--help"):
        print(DOC)
        return 0

    cfg_path = Path(sys.argv[1])
    if not cfg_path.is_file():
        print(f"[notify_config] config not found: {cfg_path}", file=sys.stderr)
        return 2

    kv = parse_config(cfg_path)
    if kv is None:
        return 3

    if len(sys.argv) >= 3:
        print(kv.get(sys.argv[2], ""))
        return 0

    for k, v in kv.items():
        safe = v.replace("'", "'\"'\"'")
        print(f"{k}='{safe}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
