#!/usr/bin/env bash
# ============================================================================
# docker/entrypoint.sh — container startup
#
# Responsibilities (in order):
#   1. init-db at $SRC/modules/main/db/recon.sqlite3 if missing; migrate
#      legacy /data/recon.sqlite3 from the old srcradar-data volume if found
#   2. start the dashboard (0.0.0.0:8765) and the MCP daemon (0.0.0.0:8764)
#   3. register the dashboard watchdog in the crontab (every 5 minutes)
#   4. start cron service (so daily 03:00 CST schedule fires)
#   5. exec the user-provided CMD (default: srcradar --help)
#
# The DB path is fixed at /opt/srcradar/modules/main/db/recon.sqlite3
# (the user needs no env vars). It sits next to the db module because that
# is where the dashboard and the MCP daemon look for it.
# /data only carries the legacy DB (the srcradar-data volume).
#
# The language and the DNS resolver set are fixed at build time:
#   SRCDAR_LANG=en       (ENV, set in the Dockerfile)
#   resolver_set=global  (config/pdtm.conf, written in the Dockerfile)
#
# The dashboard and the MCP daemon bind 0.0.0.0 INSIDE the container: the
# docker proxy forwards to the container address, so a loopback bind would
# refuse the forwarded traffic. The host side is what stays private: the
# compose file maps both ports to 127.0.0.1, so only the host can reach them.
#
# Note on the MCP daemon: its own install.sh hardcodes a loopback bind and
# starts it without a --host, so it is not used here. The daemon is started
# directly with --host, which the daemon supports.
# ============================================================================

set -euo pipefail

DATA=/data
SRC=/opt/srcradar
# The DB lives next to the db module, not at the root of the tree:
#   - the dashboard resolves it as <daily>/../../db/recon.sqlite3
#     (modules/main/daily/lib/dashboard.py -> parents[2]/db)
#   - the MCP daemon resolves it as <its dir>/../../main/db/recon.sqlite3
#     (modules/public/srcradar-mcp-server/daemon.py)
# Both point at modules/main/db/recon.sqlite3, so that is where it goes.
DB_DIR="$SRC/modules/main/db"
DB="$DB_DIR/recon.sqlite3"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
MCP_PORT="${MCP_PORT:-8764}"
MCP_HOST="${MCP_HOST:-0.0.0.0}"
mkdir -p "$DB_DIR"

log()  { printf '[entrypoint] %s\n' "$*"; }
warn() { printf '[entrypoint][warn] %s\n' "$*" >&2; }
err()  { printf '[entrypoint][err]  %s\n' "$*" >&2; }

# ---- 0.5) symlink PD tools into $HOME/.pdtm/go/bin (legacy default) ----
# scanner.sh / scan.sh hardcode $HOME/.pdtm/go/bin/<tool> paths.
# Inside this image, the actual binaries live at /opt/srcradar/bin/.
# Symlink every binary so the hardcoded paths resolve without
# touching the scripts.
mkdir -p "$HOME/.pdtm/go/bin"
for b in /opt/srcradar/bin/*; do
    [ -x "$b" ] || continue
    ln -sf "$b" "$HOME/.pdtm/go/bin/$(basename "$b")"
done

# ---- 0.7) symlink ./bin/cdnmatch into pdtm/ for scanner.sh ----
# scanner.sh hardcodes './bin/cdnmatch' relative to its cwd (pdtm/).
# Image installs cdnmatch at /opt/srcradar/bin/. Symlink so the
# relative path resolves.
PDTM_DIR="$SRC/modules/main/pdtm"
if [ -x "$SRC/bin/cdnmatch" ] && [ ! -e "$PDTM_DIR/bin" ]; then
    ln -sf "$SRC/bin" "$PDTM_DIR/bin"
fi

# ---- 1.0) migrate legacy /data/recon.sqlite3 -> image DB ----
# Old srcradar-data volume kept DB at /data/recon.sqlite3 (top-level file);
# New image writes DB to /opt/srcradar/db/recon.sqlite3 (fixed).
# One-shot cp migration preserves legacy user data; /data/recon.sqlite3
# is not read after migration.
if [ -f "$DATA/recon.sqlite3" ] && [ ! -f "$DB" ]; then
    log "migrating legacy $DATA/recon.sqlite3 -> $DB"
    cp -a "$DATA/recon.sqlite3" "$DB"
elif [ -f "$DATA/recon.sqlite3" ] && [ -f "$DB" ]; then
    warn "both $DATA/recon.sqlite3 (legacy) and $DB (image) exist; using $DB. To import legacy data: cp $DATA/recon.sqlite3 $DB.bak && inspect manually"
fi

# ---- 1) init-db ----
if [ ! -f "$DB" ]; then
    log "init-db (no DB at $DB)"
    if ! (cd "$DB_DIR" && bash install.sh --path "$DB"); then
        err "init-db failed; abort"
        exit 1
    fi
    log "DB initialized: $DB"
else
    log "DB exists: $DB (skip init)"
fi

# ---- 2) the dashboard (0.0.0.0:8765) ----
# The dashboard reads the DB and renders the pages of the active language
# (SRCDAR_LANG=en here). It binds 0.0.0.0 so the docker proxy can reach
# it; the host side is mapped to 127.0.0.1 by the compose file.
DAILY_DIR="$SRC/modules/main/daily"
start_dashboard() {
    mkdir -p "$DAILY_DIR/logs"
    if pgrep -f 'python3 lib/dashboard.py' >/dev/null 2>&1; then
        log "dashboard already running"
        return 0
    fi
    log "starting the dashboard on ${DASHBOARD_HOST}:${DASHBOARD_PORT}"
    cd "$DAILY_DIR" || return 1
    setsid nohup python3 lib/dashboard.py \
        --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" \
        >> logs/dashboard.log 2>&1 < /dev/null &
    disown
    local i=0
    while [ "$i" -lt 30 ]; do
        if curl -sf "http://127.0.0.1:${DASHBOARD_PORT}/health" >/dev/null 2>&1; then
            log "dashboard is up on ${DASHBOARD_HOST}:${DASHBOARD_PORT}"
            return 0
        fi
        i=$((i+1))
        sleep 0.3
    done
    warn "the dashboard did not answer /health in 9s; see $DAILY_DIR/logs/dashboard.log"
    return 0
}
start_dashboard

# ---- 3) the MCP daemon (0.0.0.0:8764) ----
# The daemon is a stdlib-only python process. It forwards the MCP calls to
# the srcradar subcommands of this same tree.
MCP_DIR="$SRC/modules/public/srcradar-mcp-server"
start_mcp() {
    if [ ! -f "$MCP_DIR/daemon.py" ]; then
        warn "the MCP server is not in the image ($MCP_DIR/daemon.py); skipping"
        return 0
    fi
    mkdir -p "$MCP_DIR/logs"
    if pgrep -f 'daemon.py --http' >/dev/null 2>&1; then
        log "the MCP daemon is already running"
        return 0
    fi
    log "starting the MCP daemon on ${MCP_HOST}:${MCP_PORT}"
    cd "$MCP_DIR" || return 1
    setsid nohup python3 daemon.py --http --host "$MCP_HOST" --port "$MCP_PORT" \
        >> logs/server.log 2>&1 < /dev/null &
    disown
    local i=0
    while [ "$i" -lt 30 ]; do
        if curl -sf "http://127.0.0.1:${MCP_PORT}/health" >/dev/null 2>&1; then
            log "the MCP daemon is up on ${MCP_HOST}:${MCP_PORT}"
            return 0
        fi
        i=$((i+1))
        sleep 0.3
    done
    warn "the MCP daemon did not answer /health in 9s; see $MCP_DIR/logs/server.log"
    return 0
}
start_mcp

# ---- 4) the daily module: its cron job ----
# The daily module registers the 03:00 job through its own install_cron.sh,
# but that script builds a cron line for an interactive host:
#   /bin/zsh -c 'source ~/.zshrc; exec ...'
# The image has no zsh and no ~/.zshrc, so that line would fail every night.
# The image registers the line itself, with bash and an explicit PATH:
# the PD tools live in /opt/srcradar/bin (and in ~/.pdtm/go/bin), and the
# non-interactive cron shell has neither of them.
DAILY_DIR="$SRC/modules/main/daily"
CRON_MARKER="# daily_monitor.sh managed by the docker entrypoint"
CRON_LINE="0 3 * * * /usr/bin/flock -n $DAILY_DIR/.lock /bin/bash -c 'export PATH=/opt/srcradar/bin:$HOME/.pdtm/go/bin:$PATH; exec $DAILY_DIR/daily_monitor.sh -type pdtm,icp' >> $DAILY_DIR/logs/cron.log 2>&1"
if command -v crontab >/dev/null 2>&1; then
    mkdir -p "$DAILY_DIR/logs"
    # the cron shell has a minimal PATH: the PD tools live in /opt/srcradar/bin
    # and in ~/.pdtm/go/bin, so the PATH is written into the cron line itself
    CRON_TMP="$(mktemp)"
    {
        crontab -l 2>/dev/null | grep -v -F "$CRON_MARKER" | grep -v -F "$CRON_LINE" || true
        # the daily_monitor.sh line of install_cron.sh (the zsh one) is replaced
        crontab -l 2>/dev/null | grep -v -F "# daily_monitor.sh managed by $DAILY_DIR/install_cron.sh" \
            | grep -v -F "daily_monitor.sh -type pdtm,icp" || true
        echo "$CRON_MARKER"
        echo "$CRON_LINE"
    } > "$CRON_TMP"
    if crontab "$CRON_TMP"; then
        log "the daily job is registered in the crontab (03:00, TZ=$TZ)"
    else
        warn "the daily job could not be registered in the crontab"
    fi
    rm -f "$CRON_TMP"
else
    warn "there is no crontab, the daily job is not registered"
fi

# ---- 5) the dashboard watchdog in the crontab ----
# The watchdog restarts the dashboard if it dies. It runs every 5 minutes.
# The marker keeps the line idempotent: re-running the entrypoint replaces
# it instead of piling up copies.
WATCHDOG="$DAILY_DIR/bin/dashboard_watchdog.sh"
if [ -x "$WATCHDOG" ] && command -v crontab >/dev/null 2>&1; then
    WATCHDOG_MARKER="# srcradar dashboard watchdog managed by the entrypoint"
    WATCHDOG_LINE="*/5 * * * * $WATCHDOG >> $DAILY_DIR/logs/dashboard_watchdog.log 2>&1"
    {
        crontab -l 2>/dev/null | grep -v -F "$WATCHDOG_MARKER" | grep -v -F "$WATCHDOG_LINE" || true
        echo "$WATCHDOG_MARKER"
        echo "$WATCHDOG_LINE"
    } | crontab -
    log "the dashboard watchdog is registered in the crontab (every 5 minutes)"
else
    warn "the dashboard watchdog is not registered (script or crontab missing)"
fi

# ---- 6) cron service (TODO(Q4)) ----
# detection order: service command → direct /usr/sbin/cron → /etc/init.d/cron
# ubuntu:22.04 slim has no service by default, but the cron daemon itself is installed at /usr/sbin/cron
if command -v service >/dev/null 2>&1; then
    if service cron start >/dev/null 2>&1; then
        log "cron started (TZ=$TZ)"
    else
        warn "service cron start failed; daily 03:00 schedule will not fire"
    fi
elif [ -x /usr/sbin/cron ]; then
    # run the cron daemon directly (foreground or background)
    if /usr/sbin/cron && sleep 1 && pgrep -x cron >/dev/null 2>&1; then
        log "cron started directly (TZ=$TZ)"
    else
        warn "/usr/sbin/cron failed to start; daily 03:00 schedule will not fire"
    fi
elif [ -x /etc/init.d/cron ]; then
    if /etc/init.d/cron start >/dev/null 2>&1; then
        log "cron started via init.d (TZ=$TZ)"
    else
        warn "/etc/init.d/cron start failed; daily 03:00 schedule will not fire"
    fi
else
    warn "no cron implementation found; daily 03:00 schedule will not fire"
fi

# ---- 7) exec user CMD ----
log "exec: $*"
exec "$@"
