"""
modules/public/health/lib/health_config.py — parse config/health.conf

Format (same as modules/public/notify/lib/notify_config.py):
    key=value        trailing whitespace in the value is trimmed
    # whole-line comment     everything after an inline # is ignored

Fields:
    DISK_WARN_PCT            disk usage >= this percentage is recorded as warn (default 80)
    DISK_ALERT_PCT           disk usage >= this percentage is recorded as alert (default 90)
    CPU_LOAD5_PER_NPROC      5min load average / nproc ceiling (default 2.0)
    CPU_PCT_ALERT            top -bn1 instantaneous CPU usage alert line (default 95)
    MEM_ALERT_PCT            memory usage >= this percentage is recorded as alert (default 90)
    CRON_MAX_AGE_HOURS       alert when the cron job log mtime is older than this many hours (default 25)
    WATCHED_LOGS             space-separated list of log paths (preloaded with dashboard_watchdog.log)

Usage:
    python3 health_config.py <config_path>           output the shell-eval lines
    python3 health_config.py <config_path> <key>     output the value of a single key
    python3 health_config.py --help                  this help

Design:
    - the same parse_config pattern as notify_config.py, pure stdlib
    - a space-separated multi-value like WATCHED_LOGS stays a raw string after parsing,
      the caller split()s it
    - the real config (filled in by the operator) lives in config/health.conf (.gitignore keeps it out),
      this file only reads it, it is not committed
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DOC = """\
health_config.py — parse config/health.conf (key=value)

Usage:
    python3 health_config.py <config_path>           output in shell-eval form
    python3 health_config.py <config_path> <key>     output the value of a single key
    python3 health_config.py --help                  this help

Exit codes: 0=ok 1=arguments 2=file not found 3=syntax
"""


def parse_config(path: Path) -> dict[str, str] | None:
    """Read a key=value config file; blank lines / whole-line # / everything after an inline # is ignored.

    Returns None means the file does not exist or the parse failed; returns an empty dict means the file is entirely empty.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[health_config] cannot read {path}: {exc}", file=sys.stderr)
        return None
    for raw in text.splitlines():
        if "#" in raw:
            raw = raw.split("#", 1)[0]
        if not raw.strip():
            continue
        if "=" not in raw:
            print(f"[health_config] syntax error (no '='): {raw!r}", file=sys.stderr)
            return None
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.rstrip()
        if not KEY_RE.match(key):
            print(f"[health_config] invalid key: {key!r}", file=sys.stderr)
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
        print(f"[health_config] config not found: {cfg_path}", file=sys.stderr)
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