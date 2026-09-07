#!/usr/bin/env python3
"""
load_config.py — 解析 srcradar/config/*.conf

格式:
    # 整行以 # 开头(允许前导空白)是注释,跳过
    行内第一个 # 后所有内容忽略(行内注释)
    value 末尾空白自动 trim
    空行跳过
    格式: key=value    (key 不允许含 = 或 #,value 内允许任意字符)

用法:
    python3 load_config.py <config_path>
        输出 shell 可 source 的内容到 stdout
    python3 load_config.py <config_path> <key>
        输出单个 key 的值(空字符串表示未设)

退出码:
    0 = 成功
    1 = 参数错
    2 = 文件不存在或读不了
    3 = 语法错(key 不合法等)
"""
import re
import sys
from pathlib import Path

# key 命名:字母数字下划线,至少1字符,不允许 = 或 # 或空白
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_config(path: Path):
    """yield (key, value) for each valid line; skip comments/blanks."""
    out = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        # strip line-internal comment (first '#' onward)
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        if not raw.strip():
            continue
        # split on first '='
        if "=" not in raw:
            print(f"[load_config] syntax error (no '='): {raw!r}", file=sys.stderr)
            return None
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.rstrip()  # trim trailing whitespace only
        if not KEY_RE.match(key):
            print(f"[load_config] invalid key: {key!r}", file=sys.stderr)
            return None
        out[key] = value
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(sys.argv[1])
    if not cfg_path.is_file():
        print(f"[load_config] config not found: {cfg_path}", file=sys.stderr)
        sys.exit(2)

    kv = parse_config(cfg_path)
    if kv is None:
        sys.exit(3)

    if len(sys.argv) >= 3:
        # query single key
        key = sys.argv[2]
        print(kv.get(key, ""))
        return

    # full dump in shell-eval form (already plain key=value)
    for k, v in kv.items():
        # 单引号包裹,内部 ' 用 '"'"' 转义
        safe = v.replace("'", "'\"'\"'")
        print(f"{k}='{safe}'")


if __name__ == "__main__":
    main()