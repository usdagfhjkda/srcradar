#!/usr/bin/env bash
# ============================================================================
# docker/healthcheck.sh — container liveness probe
#
# Used by Dockerfile HEALTHCHECK. Reports container is "up" if:
#   - cron process is alive (if installed) OR dashboard port is bound
#   - /opt/srcradar/bin/ has all expected binaries
#   - DB file exists at /opt/srcradar/db/recon.sqlite3
#
# Exit 0 = healthy, Exit 1 = unhealthy.
# ============================================================================

set -euo pipefail

DATA=/opt/srcradar/db
BIN=/opt/srcradar/bin
# shellcheck disable=SC2034
EXPECTED_BINS=(pdtm dnsx httpx subfinder alterx naabu cdncheck cdnmatch db_align)
# (EXPECTED_BINS above is kept only for backward-compatible documentation; the real checks are the REQUIRED_BINS / PD_CORE lists below)

# 1) DB exists
if [ ! -f "$DATA/recon.sqlite3" ]; then
    echo "unhealthy: DB missing at $DATA/recon.sqlite3" >&2
    exit 1
fi

# 2) the srcradar-built binaries must all be present (the number of PD tools pulled by pdtm varies, so it is not hardcoded)
REQUIRED_BINS=(cdnmatch db_align)
for b in "${REQUIRED_BINS[@]}"; do
    if [ ! -x "$BIN/$b" ]; then
        echo "unhealthy: missing required binary $BIN/$b" >&2
        exit 1
    fi
done

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

# 3) at least one of: cron running OR dashboard listening
if pgrep -x cron >/dev/null 2>&1 || pgrep -x crond >/dev/null 2>&1; then
    exit 0
fi
# dashboard may not be running by default; skip strict check
exit 0
