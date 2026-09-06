#!/bin/bash
# Dashboard watchdog — runs every 5 min via cron. If dashboard is not
# running, restart it. Cron runs as a separate daemon, so the harness
# can't kill this watchdog the way it kills direct Claude-spawned children.
# 脚本位于 <repo>/daily/bin/, 向上两级为 repo 根, 再追加 daily。
DAILY="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/daily"
if ! pgrep -f 'python3 lib/dashboard.py' > /dev/null; then
    cd "$DAILY" || exit 1
    setsid nohup python3 lib/dashboard.py >> logs/dashboard_watchdog.log 2>&1 < /dev/null &
    disown
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] restarted dashboard (logs/dashboard_watchdog.log)" \
        >> logs/dashboard_watchdog.log
fi
