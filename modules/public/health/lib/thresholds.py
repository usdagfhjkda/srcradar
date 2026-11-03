"""
modules/public/health/lib/thresholds.py — 阈值常量 + merge

设计:
    - DEFAULT_THRESHOLDS 是兜底默认值(无论 conf 是否存在都生效)
    - merge_with_defaults(user_dict) 把 user_dict 覆写到 DEFAULT 之上,
      未指定的字段保持 DEFAULT 值
    - 字段类型:
        DISK_WARN_PCT            int
        DISK_ALERT_PCT           int
        CPU_LOAD5_PER_NPROC      float
        CPU_PCT_ALERT            int     (top -bn1 瞬时 CPU 占用百分数)
        MEM_ALERT_PCT            int
        CRON_MAX_AGE_HOURS       float
        WATCHED_LOGS             list[str]   (空格分隔的路径)
"""
from __future__ import annotations

DEFAULT_THRESHOLDS: dict[str, object] = {
    # 磁盘使用百分比告警线 (warn 80 / alert 90 是业界常用阈值)
    "DISK_WARN_PCT": 80,
    "DISK_ALERT_PCT": 90,
    # CPU: 5min load average / nproc 上限 (Linux 常用经验值 2.0)
    "CPU_LOAD5_PER_NPROC": 2.0,
    # CPU: top 瞬时占用百分数告警线
    "CPU_PCT_ALERT": 95,
    # 内存使用百分比 (MemTotal - MemAvailable) / MemTotal
    "MEM_ALERT_PCT": 90,
    # cron 任务日志 mtime 超过该小时数视为异常
    "CRON_MAX_AGE_HOURS": 25.0,
    # WATCHED_LOGS 不硬编码任何默认路径。operator 必须在 config/health.conf
    # 里自行填入要监控的 cron 任务日志路径(空格分隔,支持相对/绝对)。
    # 空列表 = 不监控任何 cron 日志(check_cron 不产生该类 finding)。
    # 这是有意设计:避免暴露本机部署路径到开源仓库。
    "WATCHED_LOGS": [],
}


def _coerce(value: str, default):
    """把字符串 conf 值转成 default 同类型;失败回退 default。"""
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
        # 空格分隔多值;空字符串 → 默认 list
        if not value.strip():
            return list(default)
        return value.split()
    return value


def merge_with_defaults(user_dict: dict[str, str] | None) -> dict[str, object]:
    """user_dict (raw str 来自 conf parse) 覆写到 DEFAULT,类型由 DEFAULT 决定。"""
    merged: dict[str, object] = dict(DEFAULT_THRESHOLDS)
    if not user_dict:
        return merged
    for k, raw in user_dict.items():
        if k in DEFAULT_THRESHOLDS:
            merged[k] = _coerce(raw, DEFAULT_THRESHOLDS[k])
    return merged


if __name__ == "__main__":
    # 自检:打印默认值
    import json
    print(json.dumps(merge_with_defaults(None), indent=2, default=str))