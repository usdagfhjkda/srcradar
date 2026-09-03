#!/usr/bin/env bash
# install_cron.sh - install / uninstall the daily_monitor crontab entry.
#
# Usage:
#   ./install_cron.sh           # install (idempotent — replaces existing entry)
#   ./install_cron.sh uninstall  # remove our entry, keep others
#
# The installed entry always runs `-type pdtm,icp`. Per-business filtering
# happens in `recon_business_config` (recon.sqlite3), read by
# daily_monitor.sh at runtime — see README §四. This script no longer
# takes -type: the config table is the single source of truth for what
# each business runs.
#
# enscan is NOT in cron — it's a manual-only stage (db_align data refresh),
# not gated by config and not yet considered production-ready. Invoke
# manually via `run_one_business.sh -type enscan 业务名` when needed.
#
# Beijing-time 03:00 daily. The user runs as ubuntu (no sudo needed for
# `crontab -`). flock ensures manual + cron runs cannot overlap.
#
# Re-installs are safe: each invocation strips the prior managed entry
# (by marker line) before appending the new one. Re-running after
# editing this script rolls the cron line forward.

set -euo pipefail

DAILY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$DAILY/.lock"
SCRIPT="$DAILY/daily_monitor.sh"
LOG_DIR="$DAILY/logs"

ACTION="${1:-install}"

# Stage set is fixed. enscan deliberately omitted — see top-of-file note.
TYPES="pdtm icp"

# Wrap the command in `zsh -c 'source ~/.zshrc; exec ...'` so cron picks up the
# user's interactive PATH (notably $HOME/.pdtm/go/bin for subfinder/dnsx/httpx).
# zsh non-interactive does NOT read .zshrc by default (only .zshenv), so we
# source it explicitly. .bashrc isn't read by non-interactive bash either,
# and .zshrc contains oh-my-zsh which is zsh-only — so use zsh here.
CRON_CMD="/bin/zsh -c 'source ~/.zshrc; exec $SCRIPT -type $(echo $TYPES | tr ' ' ',')'"
CRON_LINE="0 3 * * * /usr/bin/flock -n $LOCK $CRON_CMD >> $LOG_DIR/cron.log 2>&1"
# A unique marker so install/uninstall can find our line.
MARKER="# daily_monitor.sh managed by $DAILY/install_cron.sh"

show_entry() {
    cat <<EOF
$MARKER
$CRON_LINE
EOF
}

install() {
    mkdir -p "$LOG_DIR"
    touch "$LOG_DIR/cron.log"
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
        echo "[!] no daily_monitor entry found in crontab; nothing to do"
    fi
    rm -f "$tmp" "$tmp.new"
}

case "$ACTION" in
    install)    install ;;
    uninstall)  uninstall ;;
    *)          echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
