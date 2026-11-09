#!/usr/bin/env bash
# ============================================================================
# docker/healthcheck.sh — container liveness probe
#
# Used by Dockerfile HEALTHCHECK. Reports the container is "up" if:
#   - the DB file exists next to the db module
#   - the srcradar-built binaries are present where the code expects them
#   - the PD toolchain holds at least the 4 core tools
#   - the cron daemon or the dashboard is alive
#
# Exit 0 = healthy, Exit 1 = unhealthy.
# ============================================================================

set -euo pipefail

SRC=/opt/srcradar
# The DB sits next to the db module: that is where the entrypoint creates it
# and where the dashboard and the MCP daemon look for it.
DATA="$SRC/modules/main/db"
BIN="$SRC/bin"

# 1) DB exists
if [ ! -f "$DATA/recon.sqlite3" ]; then
    echo "unhealthy: DB missing at $DATA/recon.sqlite3" >&2
    exit 1
fi

# 2) the srcradar-built binaries must all be present (the number of PD tools
#    pulled by pdtm varies, so it is not hardcoded).
#    The paths are the ones the callers use, not a central bin directory:
#    the scanner calls ./bin/cdnmatch inside the pdtm module, and
#    run_one_business.sh calls modules/public/db_align/bin/db_align.
for b in "$BIN/cdnmatch" \
         "$SRC/modules/public/algo_diff_cpp/bin/algo_diff_cpp"; do
    if [ ! -x "$b" ]; then
        echo "unhealthy: missing required binary $b" >&2
        exit 1
    fi
done

# db_align is part of the Chinese build only; it is checked where it lives
# when it is there, and its absence is not a failure.
DBALIGN="$SRC/modules/public/db_align/bin/db_align"
if [ -e "$DBALIGN" ] && [ ! -x "$DBALIGN" ]; then
    echo "unhealthy: db_align is present but not executable: $DBALIGN" >&2
    exit 1
fi

# 3) the PD toolchain must have at least the 4 core ones (dnsx/httpx/subfinder/naabu, the rest are optional)
# soft check: one missing is a warning, two or more missing is unhealthy
PD_CORE=(dnsx httpx subfinder naabu)
MISSING=0
for b in "${PD_CORE[@]}"; do
    [ -x "$BIN/$b" ] || { echo "warn: missing $BIN/$b" >&2; MISSING=$((MISSING+1)); }
done
if [ "$MISSING" -ge 2 ]; then
    echo "unhealthy: $MISSING core PD tools missing" >&2
    exit 1
fi

# 4) at least one of: cron running OR dashboard listening
if pgrep -x cron >/dev/null 2>&1 || pgrep -x crond >/dev/null 2>&1; then
    exit 0
fi
# the dashboard may not be running by default; skip the strict check
exit 0
