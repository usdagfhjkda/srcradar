"""
modules/public/health/lib/thresholds.py — threshold constants + merge

Design:
    - DEFAULT_THRESHOLDS are the fallback defaults (they apply whether or not the conf exists)
    - merge_with_defaults(user_dict) overlays user_dict on top of DEFAULT,
      the fields that are not specified keep their DEFAULT value
    - field types:
        DISK_WARN_PCT            int
        DISK_ALERT_PCT           int
        CPU_LOAD5_PER_NPROC      float
        CPU_PCT_ALERT            int     (top -bn1 instantaneous CPU usage percentage)
        MEM_ALERT_PCT            int
        CRON_MAX_AGE_HOURS       float
        WATCHED_LOGS             list[str]   (space-separated paths)
"""
from __future__ import annotations

DEFAULT_THRESHOLDS: dict[str, object] = {
    # disk usage percentage alert lines (warn 80 / alert 90 are the common industry thresholds)
    "DISK_WARN_PCT": 80,
    "DISK_ALERT_PCT": 90,
    # CPU: 5min load average / nproc ceiling (2.0 is the common Linux rule of thumb)
    "CPU_LOAD5_PER_NPROC": 2.0,
    # CPU: alert line for the instantaneous top usage percentage
    "CPU_PCT_ALERT": 95,
    # memory usage percentage (MemTotal - MemAvailable) / MemTotal
    "MEM_ALERT_PCT": 90,
    # a cron job log whose mtime is older than this many hours counts as abnormal
    "CRON_MAX_AGE_HOURS": 25.0,
    # WATCHED_LOGS hardcodes no default path. The operator must fill in the cron job
    # log paths to watch in config/health.conf themselves (space-separated, relative or absolute).
    # an empty list = no cron log is watched (check_cron produces no such finding).
    # this is intentional: it avoids exposing local deployment paths in the open source repo.
    "WATCHED_LOGS": [],
}


def _coerce(value: str, default):
    """Convert a string conf value to the type of the default; on failure fall back to the default."""
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        # space-separated multi-value; an empty string -> the default list
        if not value.strip():
            return list(default)
        return value.split()
    return value


def merge_with_defaults(user_dict: dict[str, str] | None) -> dict[str, object]:
    """user_dict (raw str coming from the conf parse) is overlaid onto DEFAULT, the type is decided by DEFAULT."""
    merged: dict[str, object] = dict(DEFAULT_THRESHOLDS)
    if not user_dict:
        return merged
    for k, raw in user_dict.items():
        if k in DEFAULT_THRESHOLDS:
            merged[k] = _coerce(raw, DEFAULT_THRESHOLDS[k])
    return merged


if __name__ == "__main__":
    # self-check: print the default values
    import json
    print(json.dumps(merge_with_defaults(None), indent=2, default=str))