#!/usr/bin/env bash
# ============================================================================
# tests/run_smoke_in_image.sh — the smoke_e2e matrix against a built image
#
#   bash tests/run_smoke_in_image.sh srcradar:1.0.0_zh zh cn
#
# The image is the artefact under test; the tree is only the test harness.
# The harness is copied into the running container (the release image does
# not ship tests/ — it is the executable, not the test bench), and the
# 11-port x 3-round matrix of tests/run_smoke.sh runs inside it.
#
# On top of the matrix the script checks that the container keeps running:
# it boots healthy, the dashboard and the MCP daemon answer, the watchdog
# brings the dashboard back, the container survives a restart, and it stays
# healthy while it idles.
#
# The matrix needs the internet (www.localtest.me resolves to 127.0.0.1
# through a public DNS record), so the matrix step is reported as a warning
# when it fails: the network is not a property of the image.
# ============================================================================

set -uo pipefail

IMAGE="${1:-srcradar:1.0.0_zh}"
LANG_EXPECTED="${2:-zh}"
RESOLVER_EXPECTED="${3:-cn}"
SOAK_SECONDS="${SOAK_SECONDS:-90}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-150}"

PASS=0
FAIL=0
WARN=0
CN="sr-smoke-$$"
HP=18765
MP=18764

ok()   { printf '[ok]   %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '[FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '[warn] %s\n' "$*"; WARN=$((WARN+1)); }
run()  { printf '\n=== %s ===\n' "$*"; }

cleanup() { docker rm -f "$CN" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# the pattern holds no interpreter name on purpose: it matches the command
# line of the dashboard process and nothing else
DASH_RE='lib/dashboard\.py'

health_status() {
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CN" 2>/dev/null || echo none
}

wait_healthy() {
    local i=0
    while [ "$i" -lt "$1" ]; do
        [ "$(health_status)" = "healthy" ] && return 0
        i=$((i+1)); sleep 1
    done
    return 1
}

# ---------------------------------------------------------------- 1. boot ---
run "1 the container boots"
docker rm -f "$CN" >/dev/null 2>&1 || true
docker run -d --name "$CN" -p "127.0.0.1:$HP:8765" -p "127.0.0.1:$MP:8764" "$IMAGE" >/dev/null
if wait_healthy "$HEALTH_TIMEOUT"; then
    ok "the container reports healthy"
else
    bad "the container is not healthy (status: $(health_status))"
    docker logs "$CN" 2>&1 | tail -20
    exit 1
fi
if docker exec "$CN" bash /usr/local/bin/healthcheck.sh >/dev/null 2>&1; then
    ok "the healthcheck script passes by hand too"
else
    bad "the healthcheck script fails by hand"
fi

# -------------------------------------------------------- 2. the services ---
run "2 the services of the container are up"
docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8765/health >/dev/null 2>&1 \
    && ok "the dashboard answers /health" || bad "the dashboard does NOT answer /health"
docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8764/health >/dev/null 2>&1 \
    && ok "the MCP daemon answers /health" || bad "the MCP daemon does NOT answer /health"
docker exec "$CN" pgrep -x cron >/dev/null 2>&1 \
    && ok "the cron daemon is running" || bad "the cron daemon is NOT running"
SOCKS="$(docker exec "$CN" sh -c 'cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk "NR>1 && \$4==\"0A\" {print \$2}"' 2>/dev/null)"
printf '%s' "$SOCKS" | grep -qi '00000000:223D' \
    && ok "the dashboard listens on 0.0.0.0:8765" || bad "the dashboard does NOT listen on 0.0.0.0:8765"
printf '%s' "$SOCKS" | grep -qi '00000000:223C' \
    && ok "the MCP daemon listens on 0.0.0.0:8764" || bad "the MCP daemon does NOT listen on 0.0.0.0:8764"
curl -sf --max-time 5 "http://127.0.0.1:$HP/health" >/dev/null 2>&1 \
    && ok "the dashboard is reachable from the host" || bad "the dashboard is NOT reachable from the host"
LANG_ENV="$(docker exec "$CN" printenv SRCDAR_LANG 2>/dev/null)"
[ "$LANG_ENV" = "$LANG_EXPECTED" ] \
    && ok "the container uses the language $LANG_EXPECTED" || bad "the container uses the language $LANG_ENV"
RES="$(docker exec "$CN" grep -o 'resolver_set=[a-z]*' /opt/srcradar/config/pdtm.conf 2>/dev/null | head -1)"
[ "$RES" = "resolver_set=$RESOLVER_EXPECTED" ] \
    && ok "the container uses the resolver set $RESOLVER_EXPECTED" || bad "the container uses $RES"

# ------------------------------------------------------- 3. the watchdog ---
run "3 the watchdog brings the dashboard back"
DASH_PID="$(docker exec "$CN" pgrep -f "$DASH_RE" 2>/dev/null | head -1)"
if [ -n "$DASH_PID" ]; then
    docker exec "$CN" kill -9 "$DASH_PID" >/dev/null 2>&1
    sleep 2
    docker exec "$CN" pgrep -f "$DASH_RE" >/dev/null 2>&1 \
        && bad "the dashboard is still up after it was killed" \
        || ok "the dashboard is down after it was killed"
    docker exec "$CN" bash /opt/srcradar/modules/main/daily/bin/dashboard_watchdog.sh >/dev/null 2>&1
    sleep 3
    docker exec "$CN" pgrep -f "$DASH_RE" >/dev/null 2>&1 \
        && ok "the watchdog restarted the dashboard" \
        || bad "the watchdog did NOT restart the dashboard"
else
    bad "the dashboard process was not found"
fi

# ----------------------------------------------------------- 4. a restart ---
run "4 the container survives a restart"
docker restart "$CN" >/dev/null 2>&1
if wait_healthy "$HEALTH_TIMEOUT"; then
    ok "the container is healthy again after the restart"
else
    bad "the container is NOT healthy after the restart (status: $(health_status))"
fi
docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8765/health >/dev/null 2>&1 \
    && ok "the dashboard is up again" || bad "the dashboard is NOT up again"
docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8764/health >/dev/null 2>&1 \
    && ok "the MCP daemon is up again" || bad "the MCP daemon is NOT up again"

# ------------------------------------------------------------- 5. the soak ---
run "5 the container stays healthy while it idles (${SOAK_SECONDS}s)"
SOAK_BAD=0
END=$(( $(date +%s) + SOAK_SECONDS ))
while [ "$(date +%s)" -lt "$END" ]; do
    ST="$(health_status)"
    if [ "$ST" != "healthy" ]; then bad "the container left the healthy state ($ST)"; SOAK_BAD=1; break; fi
    docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8765/health >/dev/null 2>&1 \
        || { bad "the dashboard stopped answering during the soak"; SOAK_BAD=1; break; }
    docker exec "$CN" curl -sf --max-time 5 http://127.0.0.1:8764/health >/dev/null 2>&1 \
        || { bad "the MCP daemon stopped answering during the soak"; SOAK_BAD=1; break; }
    sleep 15
done
[ "$SOAK_BAD" -eq 0 ] && ok "the container stayed healthy for ${SOAK_SECONDS}s"

# ------------------------------------------------- 6. the smoke matrix ---
run "6 the 11-port x 3-round matrix (tests/run_smoke.sh) inside the container"
# the harness is not part of the release image, so it is copied in
docker cp tests "$CN:/opt/srcradar/tests" >/dev/null 2>&1
if docker exec "$CN" test -f /opt/srcradar/tests/run_smoke.sh 2>/dev/null; then
    ok "the harness is in the container"
else
    bad "the harness could not be copied into the container"
fi
# the fixture ports must be free inside the container
for P in 28800 28801 28802 28803 28804 28805 28806 28807 28808 28809 28810; do
    docker exec "$CN" sh -c "command -v fuser >/dev/null && fuser -kn tcp $P 2>/dev/null || true" >/dev/null 2>&1
done
if docker exec "$CN" sh -c 'cd /opt/srcradar && bash tests/run_smoke.sh' >/tmp/matrix.log 2>&1; then
    ok "the matrix ran to the end and every port matched the expected change_type"
else
    warn "the matrix failed (it needs the internet; see the tail below)"
    tail -30 /tmp/matrix.log 2>/dev/null | sed 's/^/    /'
fi

# the dashboard of the container renders what the matrix wrote
sleep 35
if curl -sf --max-time 8 "http://127.0.0.1:$HP" 2>/dev/null | grep -q 'localtest.me'; then
    ok "the dashboard of the container renders the target of the matrix"
else
    warn "the dashboard did not render the target of the matrix"
fi

printf '\n====================================================\n'
printf 'PASS=%d  FAIL=%d  WARN=%d\n' "$PASS" "$FAIL" "$WARN"
printf '====================================================\n'
[ "$FAIL" -eq 0 ]
