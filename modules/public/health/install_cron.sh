#!/usr/bin/env bash
#
# modules/public/health/install_cron.sh — install / uninstall the health cron entry.
#
# Usage:
#   ./install_cron.sh           # install (idempotent — replaces existing entry)
#   ./install_cron.sh uninstall  # remove our entry, keep others
#
# a cron entry fully independent of daily_monitor:
#   - daily_monitor:  runs at 03:00 (modules/main/daily/install_cron.sh)
#   - health:         runs at 03:15 (this script) — offset by 15 minutes, so it does not compete with daily for resources at the same time
#
# Trigger: daily at 03:15 (Beijing), calls `bash health_check.sh` (without --dryrun).
# `flock -n` prevents concurrency (health_check's own 5 checks finish within 1-2s,
# but it stops a new run from stacking on an unfinished previous one).
# `zsh -c 'source ~/.zshrc; exec ...'` picks up the user PATH (same idiom as daily_monitor).
#
# Re-install is safe: every run first strips the old managed lines (the marker + the 1 line right after it) and then appends the new line.
#

set -euo pipefail

HEALTH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$HEALTH/.lock"
SCRIPT="$HEALTH/health_check.sh"
LOG_DIR="$HEALTH/logs"

ACTION="${1:-install}"

# daily at 03:15 (Beijing); offset by 15 minutes from daily_monitor's 03:00
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