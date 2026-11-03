#!/usr/bin/env bash
#
# modules/public/health/install_cron.sh — install / uninstall the health cron entry.
#
# Usage:
#   ./install_cron.sh           # install (idempotent — replaces existing entry)
#   ./install_cron.sh uninstall  # remove our entry, keep others
#
# 与 daily_monitor 完全独立的 cron entry:
#   - daily_monitor:  03:00 跑 (modules/main/daily/install_cron.sh)
#   - health:         03:15 跑 (本脚本) — 错开 15 分钟,避免同一时刻和 daily 同时争资源
#
# 触发:每日 03:15 (Beijing),调 `bash health_check.sh`(不带 --dryrun)。
# `flock -n` 防并发(health_check 自身 5 项 check 1-2s 内能完成,
# 但防止上次未跑完又叠加)。
# `zsh -c 'source ~/.zshrc; exec ...'` 拿用户 PATH(同 daily_monitor idiom)。
#
# 重新安装安全:每次执行会先剥离老 managed 行(marker + 紧跟 1 行)再追加新行。
#

set -euo pipefail

HEALTH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HEALTH/.lock"
SCRIPT="$HEALTH/health_check.sh"
LOG_DIR="$HEALTH/logs"

ACTION="${1:-install}"

# 每日 03:15 (Beijing);错开 daily_monitor 的 03:00 15 分钟
CRON_SCHEDULE_MIN="15"
CRON_SCHEDULE_HOUR="3"

# Wrap the command in `zsh -c 'source ~/.zshrc; exec ...'` so cron picks up the
# user's interactive PATH. zsh non-interactive does NOT read .zshrc by default,
# so source it explicitly. .bashrc isn't read by non-interactive bash either,
# and .zshrc contains oh-my-zsh which is zsh-only — so use zsh here.
CRON_CMD="/bin/zsh -c 'source ~/.zshrc; exec $SCRIPT'"
CRON_LINE="$CRON_SCHEDULE_MIN $CRON_SCHEDULE_HOUR * * * /usr/bin/flock -n $LOCK $CRON_CMD >> $LOG_DIR/health_cron.log 2>&1"
# A unique marker so install/uninstall can find our line.
MARKER="# health_check.sh managed by $HEALTH/install_cron.sh"

show_entry() {
    cat <<EOF
$MARKER
$CRON_LINE
EOF
}

install() {
    mkdir -p "$LOG_DIR"
    touch "$LOG_DIR/health_cron.log"
    tmp="$(mktemp)"
    if ! crontab -l > "$tmp" 2>/dev/null; then
        : > "$tmp"   # empty crontab
    fi
    # Drop prior entries (marker line + the cron line that follows it),
    # then append the new one. Same awk as uninstall, so re-installs
    # stay idempotent and never leave orphan cron lines.
    {
        awk -v marker="$MARKER" '
            { lines[NR] = $0 }
            END {
                skip = 0
                for (i = 1; i <= NR; i++) {
                    if (skip) { skip = 0; continue }
                    if (lines[i] == marker) { skip = 1; continue }
                    print lines[i]
                }
            }
        ' "$tmp"
        show_entry
    } | crontab -
    rm -f "$tmp"
    echo "[+] crontab installed:"
    show_entry
}

uninstall() {
    tmp="$(mktemp)"
    if ! crontab -l > "$tmp" 2>/dev/null; then
        echo "[!] no crontab for $(whoami); nothing to do"
        rm -f "$tmp"
        return 0
    fi
    if grep -q -F "$MARKER" "$tmp"; then
        # Drop the marker + the line that follows it.
        awk -v marker="$MARKER" '
            { lines[NR] = $0 }
            END {
                skip = 0
                for (i = 1; i <= NR; i++) {
                    if (skip) { skip = 0; continue }
                    if (lines[i] == marker) { skip = 1; continue }
                    print lines[i]
                }
            }
        ' "$tmp" > "$tmp.new"
        crontab "$tmp.new"
        echo "[+] crontab entry removed"
    else
        echo "[!] no health_check.sh entry found in crontab; nothing to do"
    fi
    rm -f "$tmp" "$tmp.new"
}

case "$ACTION" in
    install)    install ;;
    uninstall)  uninstall ;;
    *)          echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac