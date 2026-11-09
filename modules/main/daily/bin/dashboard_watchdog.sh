#!/bin/bash
# Dashboard watchdog — runs every 5 min via cron. If dashboard is not
# running, restart it. Cron runs as a separate daemon, so the harness
# can't kill this watchdog the way it kills direct Claude-spawned children.
# The script lives in <repo>/daily/bin/, two levels up is the repo root, then /daily is appended.
#
# The install path of the daily module differs between a source checkout
# (<repo>/modules/main/daily) and the image (/opt/srcradar/modules/main/daily),
# so the directory is resolved from the script location: two levels up is
# the daily module itself.
DAILY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
if ! pgrep -f 'python3 lib/dashboard.py' > /dev/null; then
    cd "$DAILY" || exit 1
    mkdir -p logs
    setsid nohup python3 lib/dashboard.py --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
        >> logs/dashboard_watchdog.log 2>&1 < /dev/null &
    disown
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] restarted dashboard (logs/dashboard_watchdog.log)" \
        >> logs/dashboard_watchdog.log
fi
