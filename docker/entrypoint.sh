#!/usr/bin/env bash
# ============================================================================
# docker/entrypoint.sh — container startup
#
# Responsibilities (in order):
#   1. init-db at $SRC/db/recon.sqlite3 if missing; migrate legacy
#      /data/recon.sqlite3 from old srcradar-data volume if found
#   2. start cron service (so daily 03:00 CST schedule fires)
#   3. exec the user-provided CMD (default: srcradar --help)
#
# The DB path is fixed at /opt/srcradar/db/recon.sqlite3 (the user needs no env vars).
# /data only carries the enscan cookie (TODO Q3); it no longer holds the DB.
#
# TODO(Q3): ENScan cookie handling
#   - First preference: /data/enscan/ directory mounted by user (persistent)
#   - Fallback: ENV ENSCAN_COOKIE_FILE points to a single cookie file
#   - If neither present: db_align will fail on first -type enscan run; log
#     warning, do NOT fail entrypoint (so container stays usable for pdtm only)
#
# TODO(Q4): cron inside container
#   - service cron start is the conventional choice; survives container restart
#   - We do NOT switch to while-loop to preserve install_cron.sh semantics
# ============================================================================

set -euo pipefail

DATA=/data
SRC=/opt/srcradar
DB="$SRC/db/recon.sqlite3"
mkdir -p "$SRC/db"

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
if [ -f /data/recon.sqlite3 ] && [ ! -f "$DB" ]; then
    log "migrating legacy /data/recon.sqlite3 -> $DB"
    cp -a /data/recon.sqlite3 "$DB"
elif [ -f /data/recon.sqlite3 ] && [ -f "$DB" ]; then
    warn "both /data/recon.sqlite3 (legacy) and $DB (image) exist; using $DB. To import legacy data: cp /data/recon.sqlite3 $DB.bak && inspect manually"
fi

# ---- 1) init-db ----
if [ ! -f "$DB" ]; then
    log "init-db (no DB at $DB)"
    if ! (cd "$SRC/modules/main/db" && bash install.sh --path "$DB"); then
        err "init-db failed; abort"
        exit 1
    fi
    log "DB initialized: $DB"
else
    log "DB exists: $DB (skip init)"
fi

# ---- 2) cron service (TODO(Q4)) ----
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

# ---- 3) ENScan cookie hint (TODO(Q3)) ----
if [ -d "$DATA/enscan" ]; then
    log "ENScan cookie dir mounted: $DATA/enscan"
elif [ -n "${ENSCAN_COOKIE_FILE:-}" ] && [ -f "$ENSCAN_COOKIE_FILE" ]; then
    log "ENScan cookie file: $ENSCAN_COOKIE_FILE"
else
    warn "no ENScan cookie source; db_align -type enscan will fail until provided"
fi

# ---- 4) exec user CMD ----
log "exec: $*"
exec "$@"
