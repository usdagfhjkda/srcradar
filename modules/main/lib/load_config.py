#!/usr/bin/env python3
"""
load_config.py — parse srcradar/config/*.conf

Format:
    # a whole line starting with # (leading whitespace allowed) is a comment, skip it
    everything after the first # on a line is ignored (inline comment)
    trailing whitespace of value is trimmed automatically
    blank lines are skipped
    format: key=value    (key must not contain = or #, value may contain any character)

Usage:
    python3 load_config.py <config_path>
        print content that shell can source to stdout
    python3 load_config.py <config_path> <key>
        print the value of a single key (empty string means not set)

Exit codes:
    0 = success
    1 = bad arguments
    2 = file does not exist or cannot be read
    3 = syntax error (invalid key, etc)
"""
import re
import sys
from pathlib import Path

# key naming: letters, digits and underscore, at least 1 character, no = or # and no whitespace
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
        # wrap in single quotes, escape an inner ' as '"'"'
        safe = v.replace("'", "'\"'\"'")
        print(f"{k}='{safe}'")


if __name__ == "__main__":
    main()